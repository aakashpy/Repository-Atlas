import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector, Filter, FieldCondition, MatchText, MatchValue
from core.pipeline import get_sparse_model, get_model, get_client, get_query_prompt_name, COLLECTION_NAME, rerank, get_reranking_candidate_pool
from config import get_config

MIN_SCORE = get_config()["retrieval"]["min_score_current"]
MAX_FILE_CHUNKS = get_config()["retrieval"]["max_file_chunks"]
MAX_REFERENCED_SYMBOLS = get_config()["retrieval"]["max_referenced_symbols"]
QUERY_PROMPT_NAME = get_query_prompt_name()
CANDIDATE_POOL = get_reranking_candidate_pool()

# Shared singleton with pipeline.py/search_history.py -- loading a second
# copy of a larger embedding model (e.g. Qwen3-Embedding, ~1.2GB) alongside
# the one search_history.py already loads via the same singleton would
# needlessly double VRAM usage.
model = get_model()
client = get_client()

_ABOUT_PROJECT_RE = re.compile(
    r"\b(this|the)\b(?:\s+\w+){0,2}\s+(project|library|repo|repository|codebase)\b"
    r"|\bwhat (is|does) (this|it)\b",
    re.IGNORECASE,
)


def _is_about_project_query(query: str) -> bool:
    return bool(_ABOUT_PROJECT_RE.search(query))


def _get_project_overview_chunk(limit: int = 1):
    """
    Direct metadata lookup for the repo-root README's overview chunk.
    Boosted for broad "what does this project do" style questions: these
    score poorly on pure embedding similarity (generic phrasing vs. a
    specific project description -- verified directly, cosine ~0.05) even
    though the actual answer is right there. Same rationale as the
    identifier-boosting in search_history.py: don't rely on similarity
    search for a lookup we can answer exactly via metadata.
    """
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(
            must=[FieldCondition(key="filepath", match=MatchText(text="README.md"))]
        ),
        limit=20,
    )
    root_readmes = [
        p for p in results
        if p.payload["filepath"].endswith("README.md") and "/tests/" not in p.payload["filepath"]
    ]
    candidates = root_readmes or results

    # scroll() order isn't chunk-sequential -- prefer the chunk that starts
    # with the file's top-level (single "#") heading, i.e. its intro section,
    # over an arbitrary later section like "## Cloning the repository".
    h1_chunks = [p for p in candidates if re.match(r"^#\s+\S", p.payload["text"].strip())]
    chosen = h1_chunks or candidates

    return [
        {
            "filepath": p.payload["filepath"],
            "text": p.payload["text"],
            "score": 1.0,
            "symbol_name": p.payload.get("symbol_name"),
            "referenced_symbols": p.payload.get("referenced_symbols", []),
        }
        for p in chosen[:limit]
    ]


_FILENAME_RE = re.compile(r"\b([\w][\w\-]*\.[A-Za-z]{1,5})\b")


def _extract_filename_candidates(query: str) -> list:
    """
    Find query tokens shaped like a filename (word.ext, e.g. "test_utils.py",
    "README.md", "config.yaml"). Deliberately not tied to any fixed
    extension list -- this has to work for whatever file types exist in
    whatever codebase is indexed, not just this project's .py/.md/.rst/.txt
    set. False positives (e.g. "e.g.") are harmless: the lookup below only
    boosts if the candidate matches a real filename in the collection.
    """
    seen = []
    for m in _FILENAME_RE.findall(query):
        if m.lower() not in (c.lower() for c in seen):
            seen.append(m)
    return seen


def _get_file_chunks_by_name(candidates: list, limit: int = MAX_FILE_CHUNKS):
    """
    Direct metadata lookup for every chunk belonging to a file the query
    names explicitly (e.g. "explain test_utils.py"). Whole-file questions
    can't be answered by top-k similarity ranking: a file with many
    functions has its content split across many chunks that each compete
    individually against the entire corpus, and mostly lose to unrelated
    chunks that just share generic wording (verified directly -- a file's
    own function chunks routinely rank below other files' docs). Same
    rationale as the identifier/date/about-project boosting above: don't
    rely on similarity search for a lookup we can answer exactly via
    metadata.
    """
    if not candidates:
        return []

    seen_chunk_ids = set()  # dedup by CHUNK, not by file -- a file has many chunks
    boosted = []
    for candidate in candidates:
        results, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[FieldCondition(key="filepath", match=MatchText(text=candidate))]
            ),
            limit=200,  # generous upper bound before exact-basename filtering below
        )
        for p in results:
            filepath = p.payload["filepath"]
            # MatchText matches on tokens, so "utils.py" would also hit
            # "test_utils.py" -- require the basename to match exactly.
            if Path(filepath).name.lower() != candidate.lower():
                continue
            if p.id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(p.id)
            boosted.append({
                "filepath": filepath,
                "text": p.payload["text"],
                "score": 1.0,
                "symbol_name": p.payload.get("symbol_name"),
                "referenced_symbols": p.payload.get("referenced_symbols", []),
            })
            if len(boosted) >= limit:
                return boosted

    return boosted


def _expand_referenced_symbols(chunks: list, limit: int = MAX_REFERENCED_SYMBOLS):
    """
    Resolve cross-file symbol references LIVE, at generation time, rather
    than baking referenced definitions into chunk text at ingest time --
    avoids staleness (a referenced definition changing elsewhere wouldn't
    silently go stale in every chunk that merely mentions it) and keeps
    each chunk's own embedding precise/undiluted. One level of expansion
    only: a referenced symbol's own references aren't chased further.
    """
    own_names = {c.get("symbol_name") for c in chunks if c.get("symbol_name")}
    referenced, seen = [], set(own_names)
    for c in chunks:
        for name in c.get("referenced_symbols") or []:
            if name not in seen:
                referenced.append(name)
                seen.add(name)

    extra = []
    for name in referenced:
        if len(extra) >= limit:
            break
        results, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="symbol_name", match=MatchValue(value=name))]),
            limit=1,
        )
        if results:
            p = results[0]
            extra.append({
                "filepath": p.payload["filepath"],
                "text": p.payload["text"],
                "score": 1.0,
                "symbol_name": p.payload.get("symbol_name"),
                "referenced_symbols": [],  # not expanded further -- one level only
                "referenced_definition_of": name,
            })

    return extra


def search_raw(query: str, top_k: int = 3, search_params=None, candidate_pool=None):
    """
    Hybrid search with a relevance gate:
    1. Raw dense-only cosine check decides if the query is relevant at all.
    2. If it passes, hybrid (dense+sparse fused) search provides the ranking.
    PLUS explicit boosting: "what does this project do"-style questions get
    the README overview chunk directly, and queries naming a specific file
    (e.g. "explain test_utils.py") get ALL of that file's chunks directly,
    both ahead of normal ranking.

    search_params (qdrant_client.models.SearchParams), when given, is passed
    straight through to both Qdrant queries below -- e.g. exact=True for
    brute-force ground-truth comparison, or hnsw_ef=N to test search-time
    recall/speed tradeoffs. None (the default) means Qdrant's own defaults,
    unchanged from normal operation -- this parameter exists purely for
    HNSW/ANN evaluation (see dev/eval_hnsw.py), not used by any real caller.

    candidate_pool, when given, overrides the module-level CANDIDATE_POOL
    (from config.yaml's reranking.candidate_pool) for this call only --
    exists purely for dev/eval_reranker.py to sweep pool sizes without
    restarting the process between each one, not used by any real caller.
    """
    dense_vector = model.encode(query, prompt_name=QUERY_PROMPT_NAME).tolist()

    filename_candidates = _extract_filename_candidates(query)
    boosted = _get_file_chunks_by_name(filename_candidates)
    if not boosted and _is_about_project_query(query):
        boosted = _get_project_overview_chunk()

    # Gate check: plain cosine similarity, magnitude-based (not rank-based)
    gate_check = client.query_points(
        collection_name=COLLECTION_NAME,
        query=dense_vector,
        using="dense",
        limit=1,
        search_params=search_params,
    ).points

    if not gate_check or gate_check[0].score < MIN_SCORE:
        # even if the hybrid gate fails, boosted exact matches still count
        return boosted + _expand_referenced_symbols(boosted)

    # Passed the gate — now get the properly ranked hybrid results
    sparse_vector = list(get_sparse_model().embed([query]))[0]

    # Fetch a wider pool than top_k -- reranking below works best with more
    # candidates to choose from than the final answer needs.
    pool_limit = max(top_k, candidate_pool if candidate_pool is not None else CANDIDATE_POOL)

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            # search_params (exact/hnsw_ef) only affects the HNSW index, so
            # it only makes sense on the dense prefetch stage -- a fusion
            # query's own top-level search_params does NOT reach into
            # prefetch stages, this has to be set here directly.
            Prefetch(query=dense_vector, using="dense", limit=pool_limit, params=search_params),
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
            "score": r.score,
            "symbol_name": r.payload.get("symbol_name"),
            "referenced_symbols": r.payload.get("referenced_symbols", []),
        }
        for r in results
    ]

    # Cross-encoder reranking: scores query+chunk jointly over this pool,
    # more precise than the RRF fusion order above. Only reorders this
    # hybrid pool -- boosted (exact metadata match) results below still
    # bypass ranking entirely, same as before reranking existed.
    hybrid_results = rerank(query, hybrid_results)

    # Boosted results are never truncated -- e.g. all of a named file's
    # chunks must survive even if there are more of them than top_k, or
    # whole-file questions would just be silently cut back down again.
    # Only the extra hybrid filler is capped, by whatever budget is left.
    seen = {b["filepath"] for b in boosted}
    merged = boosted[:]
    remaining_budget = max(0, top_k - len(boosted))
    for r in hybrid_results:
        if len(merged) - len(boosted) >= remaining_budget:
            break
        if r["filepath"] not in seen:
            merged.append(r)
            seen.add(r["filepath"])

    return merged + _expand_referenced_symbols(merged)

def search(query: str, top_k: int = 3):
    results = search_raw(query, top_k=top_k)

    if not results:
        print("No relevant information found for this query.")
        return

    for i, result in enumerate(results, 1):
        print(f"\n--- Result {i} (score: {result['score']:.4f}) ---")
        print(f"File: {result['filepath']}")
        print(result["text"][:300])


if __name__ == "__main__":
    query = " ".join(sys.argv[1:])
    if not query:
        print("Usage: python3 search.py <your question>")
    else:
        search(query)