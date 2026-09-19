import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for sibling packages

from retrieval.search import search_raw
from retrieval.search_history import search_history_raw
from generation.llm_backend import generate_text

BACKEND = "gemini"  # switch to "ollama" to go back to local

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5:3b"


def build_context_text(current_chunks: list, history_chunks: list) -> str:
    """
    The labeled source material the LLM is actually shown -- factored out
    of build_prompt() so eval_rag.py's judge can be given the EXACT same
    context the generation call saw (not a re-derived approximation that
    can silently drop the file-path/date labels the LLM legitimately cites,
    which caused a false-positive "hallucination" in an earlier eval run).
    """
    sections = []

    if current_chunks:
        current_text = "\n\n---\n\n".join(
            (
                f"[Referenced definition: {c['referenced_definition_of']} | File: {c['filepath']}]\n{c['text']}"
                if c.get("referenced_definition_of") else
                f"[File: {c['filepath']}]\n{c['text']}"
            )
            for c in current_chunks
        )
        sections.append(f"CURRENT CODE (what the code looks like today):\n{current_text}")

    if history_chunks:
        history_text = "\n\n---\n\n".join(
            f"[{c['date']} | {c['filepath']}]\n{c['text']}"
            for c in history_chunks
        )
        sections.append(f"HISTORY (past changes, commit messages and diffs):\n{history_text}")

    return "\n\n===\n\n".join(sections)


def build_prompt(query: str, current_chunks: list, history_chunks: list) -> str:
    all_context = build_context_text(current_chunks, history_chunks)

    return f"""You are a technical assistant answering questions about a codebase,
for an audience that may include non-technical project members.

STRICT RULE: Only state facts, dates, version numbers, or details that are
EXPLICITLY written in the sources below. Do NOT use any outside knowledge you
may have about this project, even if you recognize it. If a specific detail
(a date, version number, or fact) is not explicitly present in the sources,
do not mention it or guess it.

You have two kinds of information below: CURRENT CODE (how things work today)
and HISTORY (how things changed over time). Use whichever is relevant to the
question. If neither source contains enough explicit information to answer,
say so clearly instead of guessing or filling gaps from memory.

INTERPRETATION RULE: You are an assistant *about* this codebase, not an
entity within it. If the question is ambiguous, informal, or personifies you
(e.g. asks about "your" birthday, age, or creator), explicitly say how you're
interpreting it as a question about the project before answering — do not
silently answer as if the reinterpretation were obvious.

CITATION RULE: Each source below is labeled with its real file path (and
date, for history). If you reference where something comes from, use that
actual file name or date — never invent labels like "Source 1" or
"Current Source 2". Only cite a source when it adds real clarity; don't
cite every sentence. A source labeled "[Referenced definition: X | File: ...]"
is the definition of a name (function/class/type) that the actually-relevant
code calls or uses — treat it as supporting context for understanding that
code, not as the direct answer to the question unless the question is
specifically about X.

{all_context}

Question: {query}

Answer:"""


def generate_answer(query: str, top_k: int = 6):
    current_chunks = search_raw(query, top_k=top_k)
    history_chunks = search_history_raw(query, top_k=top_k)

    if not current_chunks and not history_chunks:
        print("No relevant information found for this query.")
        return

    prompt = build_prompt(query, current_chunks, history_chunks)
    answer = generate_text(prompt, backend=BACKEND)
    print(answer)


if __name__ == "__main__":
    query = " ".join(sys.argv[1:])
    if not query:
        print("Usage: python3 generate.py <your question>")
    else:
        generate_answer(query)