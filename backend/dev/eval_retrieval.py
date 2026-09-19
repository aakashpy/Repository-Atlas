"""
Retrieval evaluation: computes real Recall@K and MRR (Mean Reciprocal Rank)
against a small, hand-labeled set of (question, expected file) pairs, instead
of eyeballing whether results "look" relevant.

Deliberately scoped to file-level correctness, not exact-chunk-level: a hit
means the expected file appears somewhere in the top-K results, which is
both a lot more robust to write (chunk boundaries can shift as chunking
logic evolves) and matches how this project's own filename-boosting already
treats "the right file" as the meaningful unit of correctness.

History is intentionally NOT included here: requests_history currently holds
62/6,493 commits (see docs/DECISIONS.md, "History Made Fully Optional").
Building a history eval set against that small, arbitrary slice would
produce numbers that look precise but aren't representative of anything --
worth doing once the backfill is further along, not now.

Usage: python3 dev/eval_retrieval.py (from backend/)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from retrieval.search import search_raw

# Each entry: a natural-language question a real user might ask, phrased
# WITHOUT naming the target file directly (naming it would just trigger
# exact filename-boosting and always "pass", which tests nothing about the
# actual ranking/reranking quality this eval is meant to measure), plus the
# basename of the file that should genuinely answer it. Mappings verified
# directly against the real source (grep'd class/function names) before
# being used as ground truth -- see docs/DECISIONS.md.
EVAL_SET = [
    ("how do I send a GET request", "api.py"),
    ("how do I send a POST request", "api.py"),
    ("how does a session reuse connections and state across multiple requests", "sessions.py"),
    ("how do cookies get merged between a session and an individual request", "cookies.py"),
    ("what exception is raised when a request times out", "exceptions.py"),
    ("what exception is raised on a connection error", "exceptions.py"),
    ("how are HTTP redirects followed and resolved", "sessions.py"),
    ("how is HTTP Basic Authentication implemented", "auth.py"),
    ("how is HTTP Digest Authentication implemented", "auth.py"),
    ("how do I write my own custom authentication class", "authentication.rst"),
    ("what HTTP status codes does the library define", "status_codes.py"),
    ("how does the library decide which transport adapter handles a request", "sessions.py"),
    ("how is a case-insensitive dictionary implemented for HTTP headers", "structures.py"),
    ("how do I install this library", "install.rst"),
    ("what's a minimal first example of making a request", "quickstart.rst"),
    ("how do response hooks work", "hooks.py"),
    ("how is the default User-Agent string constructed", "utils.py"),
    ("how is SSL certificate verification configured for a request", "adapters.py"),
    ("what internal helper functions does the library use that aren't public API", "_internal_utils.py"),
    ("how can I stream a large response body in chunks", "models.py"),
    ("what does the PreparedRequest class represent", "models.py"),
    ("who are the maintainers and contributors of this project", "AUTHORS.rst"),
]


def score_eval_set(top_k: int = 6, search_params=None, candidate_pool=None):
    """
    Same scoring as evaluate() below, minus the per-question printing --
    used by eval_hnsw.py to run this same eval set multiple times with
    different Qdrant search_params (exact=True, different hnsw_ef values),
    and by eval_reranker.py to sweep reranking.candidate_pool sizes, then
    compare the aggregate numbers directly.
    """
    hits = 0
    reciprocal_ranks = []
    for query, expected_file in EVAL_SET:
        results = search_raw(query, top_k=top_k, search_params=search_params, candidate_pool=candidate_pool)
        filenames = [Path(r["filepath"]).name for r in results[:top_k]]
        rank = next((i for i, name in enumerate(filenames, 1) if name == expected_file), None)
        reciprocal_ranks.append(1 / rank if rank else 0)
        if rank:
            hits += 1
    return hits / len(EVAL_SET), sum(reciprocal_ranks) / len(reciprocal_ranks)


def evaluate(top_k: int = 6):
    hits = 0
    reciprocal_ranks = []
    print(f"Running retrieval evaluation: {len(EVAL_SET)} questions, top_k={top_k}\n")

    for query, expected_file in EVAL_SET:
        results = search_raw(query, top_k=top_k)
        # search_raw() can return MORE than top_k items -- boosted (exact
        # metadata match) results are never truncated, and cross-file
        # symbol-reference definitions get appended on top as bonus
        # context. Neither is part of the ranked top-K this metric is
        # measuring, so the list is sliced back down to a true top_k here.
        filenames = [Path(r["filepath"]).name for r in results[:top_k]]

        rank = None
        for i, name in enumerate(filenames, 1):
            if name == expected_file:
                rank = i
                break

        if rank:
            hits += 1
            reciprocal_ranks.append(1 / rank)
            print(f"HIT  (rank {rank}) | {query}")
        else:
            reciprocal_ranks.append(0)
            print(f"MISS         | {query}  -- expected {expected_file}, got {filenames}")

    recall_at_k = hits / len(EVAL_SET)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)

    print(f"\nRecall@{top_k}: {recall_at_k:.2%}  ({hits}/{len(EVAL_SET)})")
    print(f"MRR:       {mrr:.3f}")
    return recall_at_k, mrr


if __name__ == "__main__":
    evaluate()
