"""
Validates two of the reranker's own configured assumptions -- both picked
as reasonable-sounding defaults when reranking was added, never measured
against real data since:

1. reranking.candidate_pool (config.yaml, default 20) -- how many hybrid
   results get reranked before boosting/truncation to top_k. Too small and
   the reranker never even sees the truly best chunk; too large and it
   wastes GPU time reranking chunks that were never going to make the cut.
   Swept against the same 22-question Recall@6/MRR eval set used elsewhere
   (see eval_retrieval.py) to see whether a different pool size actually
   changes the outcome, the same method eval_hnsw.py used for HNSW params.

2. The cross-encoder's max_seq_length (measured directly: 512 tokens for
   cross-encoder/ms-marco-MiniLM-L-6-v2). Each (query, chunk) pair is
   tokenized and truncated to fit this JOINTLY -- a long chunk can silently
   lose its back half. Measures, on real chunks retrieved for the eval
   set's real questions, how many pairs actually exceed 512 tokens, rather
   than assuming it from the chunking config alone.

Usage: python3 dev/eval_reranker.py (from backend/)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from eval_retrieval import score_eval_set, EVAL_SET
from retrieval.search import search_raw
from core.pipeline import get_reranker, get_reranking_candidate_pool


def sweep_candidate_pool():
    print("=== candidate_pool sweep (Recall@6 / MRR, 22-question eval set) ===\n")
    configured = get_reranking_candidate_pool()
    for pool in [5, 10, 15, 20, 30, 50]:
        recall, mrr = score_eval_set(top_k=6, candidate_pool=pool)
        marker = "  <- current config.yaml default" if pool == configured else ""
        print(f"candidate_pool={pool:>3}  Recall@6={recall:.2%}  MRR={mrr:.3f}{marker}")


def measure_token_lengths():
    print("\n=== (query, chunk) token length vs. cross-encoder's max_seq_length ===\n")
    reranker = get_reranker()
    max_len = reranker.max_length
    tokenizer = reranker.tokenizer
    configured_pool = get_reranking_candidate_pool()
    print(f"Cross-encoder max_seq_length: {max_len} tokens")
    print(f"Measuring against the real candidate pool as used in production (candidate_pool={configured_pool})\n")

    lengths = []
    truncated_examples = []
    for query, _ in EVAL_SET:
        # top_k == candidate_pool so nearly the whole real hybrid pool
        # (as reranked in production) comes back, not just a top_k slice.
        results = search_raw(query, top_k=configured_pool, candidate_pool=configured_pool)
        for r in results:
            # Same pairing CrossEncoder.predict() tokenizes internally.
            enc = tokenizer(query, r["text"], truncation=False)
            length = len(enc["input_ids"])
            lengths.append(length)
            if length > max_len:
                truncated_examples.append((length, r["filepath"], query))

    lengths.sort()
    n = len(lengths)
    over = len(truncated_examples)
    print(f"Pairs measured: {n}")
    print(f"Min: {lengths[0]}  Median: {lengths[n // 2]}  Max: {lengths[-1]}  Mean: {sum(lengths) / n:.1f}")
    print(f"Pairs exceeding {max_len} tokens (silently truncated by the cross-encoder): {over}/{n} ({over / n:.1%})")
    if truncated_examples:
        print("\nExamples of truncated pairs (token_count | file | query):")
        for length, filepath, query in sorted(truncated_examples, reverse=True)[:5]:
            print(f"  {length:>4} | {filepath} | {query}")


if __name__ == "__main__":
    sweep_candidate_pool()
    measure_token_lengths()
