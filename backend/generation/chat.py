import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for sibling packages

from retrieval.search import search_raw
from retrieval.search_history import search_history_raw
from generation.llm_backend import generate_text
from generation.generate import build_prompt, BACKEND

MAX_HISTORY_TURNS = 6  # keep last N (question, answer) pairs in context


def format_history(history: list) -> str:
    return "\n".join(f"{turn['role'].upper()}: {turn['text']}" for turn in history)


def rewrite_query(history: list, question: str) -> str:
    """Condense a follow-up question + conversation history into a standalone query."""
    if not history:
        return question

    prompt = f"""Given the conversation history below, rewrite the follow-up question as a
standalone question that can be understood without the conversation history.
Preserve any specific names (classes, functions, files) mentioned earlier if the
follow-up question refers to them. Output ONLY the rewritten question, nothing else.

CONVERSATION HISTORY:
{format_history(history)}

FOLLOW-UP QUESTION: {question}

STANDALONE QUESTION:"""

    return generate_text(prompt, backend=BACKEND).strip()


def chat_turn(history: list, question: str, top_k: int = 6) -> dict:
    standalone_query = rewrite_query(history, question)

    current_chunks = search_raw(standalone_query, top_k=top_k)
    history_chunks = search_history_raw(standalone_query, top_k=top_k)

    if not current_chunks and not history_chunks:
        answer = "No relevant information found for this query."
    else:
        prompt = build_prompt(standalone_query, current_chunks, history_chunks)
        answer = generate_text(prompt, backend=BACKEND)

    return {
        "answer": answer,
        "standalone_query": standalone_query,
        "current_chunks": current_chunks,
        "history_chunks": history_chunks,
    }


def main():
    print("Conversational RAG chat. Type 'exit' or 'quit' to stop.\n")
    history = []

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break

        result = chat_turn(history, question)
        answer = result["answer"]
        standalone_query = result["standalone_query"]

        if standalone_query != question:
            print(f"[rewritten query: {standalone_query}]")
        print(f"\nAssistant: {answer}\n")

        history.append({"role": "user", "text": question})
        history.append({"role": "assistant", "text": answer})
        history = history[-(MAX_HISTORY_TURNS * 2):]


if __name__ == "__main__":
    main()
