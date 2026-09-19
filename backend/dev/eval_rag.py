"""
RAG evaluation: automates the "does this answer actually look right" check
that was previously done by manually reading through a 100-question
benchmark by hand. Uses a second, separate LLM call as a judge -- the same
idea behind RAGAS's faithfulness/answer-relevancy metrics, scoped down to a
standalone script instead of pulling in the full RAGAS framework.

For each question: runs the real end-to-end pipeline (retrieval + prompt +
generation -- the same code paths generate.py uses, not a re-implementation),
then asks a second LLM call to judge two things against the ACTUAL context
the answer was given, not from its own memory of the codebase:
  - Faithfulness: is every claim in the answer actually supported by the
    retrieved context?
  - Relevancy: does the answer actually address the question asked?

Reuses every other question from eval_retrieval.py's 22-question set (~11
questions) rather than all 22 or a fresh set -- each question here costs two
real LLM calls (one to generate the answer, one to judge it), so this keeps
the run fast and the API usage modest while still covering a real spread of
topics.

Usage: python3 dev/eval_rag.py (from backend/)
"""

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from google.genai.errors import ClientError
from retrieval.search import search_raw
from retrieval.search_history import search_history_raw
from generation.generate import build_prompt, build_context_text
from generation.llm_backend import generate_text
from eval_retrieval import EVAL_SET

BACKEND = "gemini"

QUESTIONS = [q for q, _ in EVAL_SET[::2]]

# The Gemini free tier caps at 15 requests/minute -- this eval makes two
# calls per question (generate + judge), so a fixed pace between calls
# keeps the whole run under that cap instead of bursting and getting
# rate-limited partway through (as the first run of this script did).
REQUEST_INTERVAL_SECONDS = 4.5


def _generate_paced(prompt: str) -> str:
    time.sleep(REQUEST_INTERVAL_SECONDS)
    for attempt in range(3):
        try:
            return generate_text(prompt, backend=BACKEND)
        except ClientError as e:
            if e.code == 429 and attempt < 2:
                print("  (rate limited -- waiting 60s...)")
                time.sleep(60)
                continue
            raise

JUDGE_PROMPT = """You are evaluating whether an AI-generated answer about a codebase is properly grounded in the source material it was given, and whether it actually addresses the question asked.

QUESTION:
{query}

SOURCE MATERIAL THE ANSWER WAS GIVEN:
{context}

GENERATED ANSWER:
{answer}

Evaluate two things:
1. FAITHFULNESS: Is every factual claim in the answer actually supported by the source material above? A correct "I don't know / not covered" answer counts as faithful. Answer PASS only if every claim is grounded in the sources; answer FAIL if the answer states anything not present in the sources above.
2. RELEVANCY: Does the answer actually address what was asked, using the sources? Answer PASS or FAIL.

Respond in EXACTLY this format, nothing else:
FAITHFULNESS: PASS or FAIL
RELEVANCY: PASS or FAIL
REASON: <one short sentence, only if either is FAIL, else "n/a">
"""


def _judge(query, context, answer):
    prompt = JUDGE_PROMPT.format(query=query, context=context, answer=answer)
    verdict = _generate_paced(prompt)

    faithful = "FAITHFULNESS: PASS" in verdict
    relevant = "RELEVANCY: PASS" in verdict
    reason_line = next((l for l in verdict.splitlines() if l.startswith("REASON:")), "REASON: n/a")

    return faithful, relevant, reason_line.replace("REASON:", "").strip()


def evaluate():
    faithful_count = 0
    relevant_count = 0
    total = 0

    print(f"Running RAG evaluation: {len(QUESTIONS)} questions\n")

    for query in QUESTIONS:
        current_chunks = search_raw(query, top_k=6)
        history_chunks = search_history_raw(query, top_k=6)

        if not current_chunks and not history_chunks:
            print(f"SKIP (no retrieval) | {query}")
            continue

        prompt = build_prompt(query, current_chunks, history_chunks)
        answer = _generate_paced(prompt)

        # Judge must see exactly what the LLM saw: build_context_text() is
        # the same labeled source material build_prompt() embeds in the
        # generation prompt (file-path/date labels included). An earlier
        # version of this script re-joined only the raw c["text"] fields,
        # dropping those labels -- the judge then saw LESS than the LLM did
        # and flagged a legitimately-grounded filepath citation as an
        # ungrounded claim, a false FAIL caused by the eval script itself,
        # not a real hallucination.
        context_text = build_context_text(current_chunks, history_chunks)

        faithful, relevant, reason = _judge(query, context_text, answer)
        total += 1
        if faithful:
            faithful_count += 1
        if relevant:
            relevant_count += 1

        status = "PASS" if (faithful and relevant) else "FAIL"
        print(f"{status}  faithful={faithful} relevant={relevant} | {query}")
        if not (faithful and relevant):
            print(f"      reason: {reason}")

    print(f"\nFaithfulness: {faithful_count}/{total} ({faithful_count/max(total,1):.0%})")
    print(f"Relevancy:    {relevant_count}/{total} ({relevant_count/max(total,1):.0%})")


if __name__ == "__main__":
    evaluate()
