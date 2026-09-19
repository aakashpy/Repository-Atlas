"""
HNSW/ANN evaluation: measures whether Qdrant's default (unconfigured)
approximate search is actually losing any retrieval quality compared to
exact brute-force search, and whether raising the search-time `ef`
parameter (how many candidates HNSW considers per query) changes anything.

Reuses the same 22-question labeled eval set and Recall@K/MRR scoring as
eval_retrieval.py, run multiple times with different Qdrant search_params,
so every row below is directly comparable -- same questions, same metric,
only the search parameter changes.

No collection rebuild involved: `exact` and `hnsw_ef` are search-time
parameters, not index-build-time ones (`m`/`ef_construct` are build-time
and are NOT covered by this script -- see docs/DECISIONS.md for why they
weren't tested here).

Usage: python3 dev/eval_hnsw.py (from backend/)
"""

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from qdrant_client.models import SearchParams
from eval_retrieval import score_eval_set

RUNS = [
    ("default (unconfigured HNSW)", None),
    ("exact=True (brute-force ceiling)", SearchParams(exact=True)),
    ("hnsw_ef=64", SearchParams(hnsw_ef=64)),
    ("hnsw_ef=128", SearchParams(hnsw_ef=128)),
    ("hnsw_ef=256", SearchParams(hnsw_ef=256)),
]


def run():
    print(f"HNSW/ANN evaluation -- same 22-question eval set, varying search params\n")
    print(f"{'Configuration':<35} {'Recall@6':>10} {'MRR':>8} {'Time (22 q)':>14}")
    print("-" * 70)

    for label, params in RUNS:
        start = time.time()
        recall, mrr = score_eval_set(search_params=params)
        elapsed = time.time() - start
        print(f"{label:<35} {recall:>9.2%} {mrr:>8.3f} {elapsed:>12.1f}s")


if __name__ == "__main__":
    run()
