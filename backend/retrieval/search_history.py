import sys
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from core.pipeline import get_model, get_client, get_sparse_model, get_query_prompt_name, rerank, get_reranking_candidate_pool
from history.history_pipeline import HISTORY_COLLECTION
from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector
from qdrant_client.models import Filter, FieldCondition, MatchValue
from config import get_config

model = get_model()
client = get_client()

MIN_SCORE_HISTORY = get_config()["retrieval"]["min_score_history"]
QUERY_PROMPT_NAME = get_query_prompt_name()
HISTORY_ENABLED = get_config().get("history", {}).get("enabled", True)
CANDIDATE_POOL = get_reranking_candidate_pool()


def _history_available() -> bool:
    """
    Live check, not cached -- so history search starts working automatically
    the moment a backfill/re-ingest actually finishes, without needing to
    restart whatever's using this module (chat.py, the web server, ...).
    """
    if not HISTORY_ENABLED:
        return False
    try:
        return client.collection_exists(HISTORY_COLLECTION)
    except Exception:
        return False


def _extract_identifier_candidates(query: str):
    """Find query words that look like function/class names (PascalCase or snake_case)."""
    words = re.findall(r'\b\w+\b', query)
    candidates = [
        w for w in words
        if (w[0].isupper() and any(c.islower() for c in w))  # PascalCase, e.g. PreparedRequest
        or ('_' in w and w.islower())  # snake_case, e.g. get_file_diff
    ]
    return candidates


_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_ISO_DATE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')
_MONTH_DAY_YEAR = re.compile(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')
_DAY_MONTH_YEAR = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b')


def _extract_date_candidate(query: str):
    """
    Detect a specific calendar date in the query (e.g. "October 22, 2011",
    "22 Oct 2011", "2011-10-22") and normalize it to YYYY-MM-DD, matching
    how commit dates are stored (history_ingest.py uses `git log --date=short`).
    Returns None if no unambiguous date is found -- deliberately skips
    bare numeric formats like "10/22/2011" since day/month order is ambiguous.
    """
    m = _ISO_DATE.search(query)
    if m:
        return m.group(0)

    m = _MONTH_DAY_YEAR.search(query)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"

    m = _DAY_MONTH_YEAR.search(query)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"

    return None


def _get_boosted_date_chunks(date_str: str, limit: int = 5):
    """
    Direct metadata lookup for every history chunk (commit message, diff,
    function_change) from an exact calendar date -- semantic search has no
    real way to treat "October 22, 2011" as an exact filter, so this bypasses
    similarity search the same way identifier-boosting does for names.
    """
    if not date_str:
        return []
    results, _ = client.scroll(
        collection_name=HISTORY_COLLECTION,
        scroll_filter=Filter(
            must=[FieldCondition(key="date", match=MatchValue(value=date_str))]
        ),
        limit=limit,
    )
    return [
        {
            "filepath": p.payload["filepath"],
            "text": p.payload["text"],
            "chunk_type": p.payload["chunk_type"],
            "function_name": p.payload.get("function_name"),
            "date": p.payload["date"],
            "commit_hash": p.payload["commit_hash"],
            "score": 1.0,
        }
        for p in results
    ]


def _get_boosted_function_chunks(candidates: list, limit: int = 3):
    """Direct metadata lookup for function_change chunks matching identifier names."""
    if not candidates:
        return []

    client = get_client()
    boosted = []
    per_candidate_limit = max(1, limit // len(candidates))

    for name in candidates:
        results, _ = client.scroll(
            collection_name=HISTORY_COLLECTION,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="chunk_type", match=MatchValue(value="function_change")),
                    FieldCondition(key="function_name", match=MatchValue(value=name)),
                ]
            ),
            limit=per_candidate_limit,
        )
        for point in results:
            boosted.append({
                "filepath": point.payload["filepath"],
                "text": point.payload["text"],
                "chunk_type": point.payload["chunk_type"],
                "function_name": point.payload.get("function_name"),
                "date": point.payload["date"],
                "commit_hash": point.payload["commit_hash"],
                "score": 1.0,
            })

    return boosted


def search_history_raw(query: str, top_k: int = 5):
    """
    Hybrid history search with a relevance gate, PLUS explicit boosting:
    if the query mentions a specific function/class name, its function_change
    chunks are prioritized via direct lookup, ahead of normal ranking. Same
    for an exact calendar date in the query (e.g. "what changed on Oct 22,
    2011") -- looked up directly via metadata filter rather than similarity.

    Degrades to [] rather than raising if history is disabled, not yet
    ingested, or a query against it fails for any reason (e.g. mid-migration
    dimension mismatch) -- generate.py/chat.py already treat an empty history
    result as "no history evidence for this question," not an error, so
    current-state search keeps working on its own regardless of history's
    state. This is what lets ingestion treat history as fully optional
    instead of an all-or-nothing prerequisite.
    """
    if not _history_available():
        return []

    try:
        candidates = _extract_identifier_candidates(query)
        boosted = _get_boosted_function_chunks(candidates, limit=top_k)

        date_candidate = _extract_date_candidate(query)
        if date_candidate:
            seen = {(b["commit_hash"], b["filepath"], b["chunk_type"]) for b in boosted}
            for b in _get_boosted_date_chunks(date_candidate, limit=top_k):
                key = (b["commit_hash"], b["filepath"], b["chunk_type"])
                if key not in seen:
                    boosted.append(b)
                    seen.add(key)

        dense_vector = model.encode(query, prompt_name=QUERY_PROMPT_NAME).tolist()

        gate_check = client.query_points(
            collection_name=HISTORY_COLLECTION,
            query=dense_vector,
            using="dense",
            limit=1,
        ).points

        if not gate_check or gate_check[0].score < MIN_SCORE_HISTORY:
            return boosted  # even if hybrid search gate fails, boosted exact matches still count

        sparse_vector = list(get_sparse_model().embed([query]))[0]

        # Fetch a wider pool than top_k -- reranking below works best with
        # more candidates to choose from than the final answer needs.
        pool_limit = max(top_k, CANDIDATE_POOL)

        results = client.query_points(
            collection_name=HISTORY_COLLECTION,
            prefetch=[
                Prefetch(query=dense_vector, using="dense", limit=pool_limit),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_vector.indices.tolist(),
                        values=sparse_vector.values.tolist(),
                    ),
                    using="sparse",
                    limit=pool_limit,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=pool_limit,
        ).points

        hybrid_results = [
            {
                "filepath": r.payload["filepath"],
                "text": r.payload["text"],
                "chunk_type": r.payload["chunk_type"],
                "function_name": r.payload.get("function_name"),
                "date": r.payload["date"],
                "commit_hash": r.payload["commit_hash"],
                "score": r.score,
            }
            for r in results
        ]

        # Cross-encoder reranking: scores query+chunk jointly over this
        # pool, more precise than the RRF fusion order above. Only reorders
        # this hybrid pool -- boosted (exact metadata match) results above
        # still bypass ranking entirely, same as before reranking existed.
        hybrid_results = rerank(query, hybrid_results)

        # Merge: boosted first, then hybrid results, deduplicated, capped at top_k
        seen = {(b["commit_hash"], b["filepath"], b["chunk_type"]) for b in boosted}
        merged = boosted[:]
        for r in hybrid_results:
            key = (r["commit_hash"], r["filepath"], r["chunk_type"])
            if key not in seen:
                merged.append(r)
                seen.add(key)

        return merged[:top_k]
    except Exception as e:
        print(f"Warning: history search failed ({e}); continuing with current-state results only.")
        return []


def search_history(query: str, top_k: int = 5):
    results = search_history_raw(query, top_k=top_k)
    if not results:
        print("No relevant information found for this query.")
        return
    for i, r in enumerate(results, 1):
        print(f"\n--- Result {i} (score: {r['score']:.4f}) ---")
        print(f"Date: {r['date']} | Commit: {r['commit_hash'][:8]} | Type: {r['chunk_type']}")
        print(f"File: {r['filepath']}")
        print(r["text"][:300])


if __name__ == "__main__":
    query = " ".join(sys.argv[1:])
    search_history(query)