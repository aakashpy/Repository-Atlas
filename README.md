# Repository Atlas

A RAG (Retrieval-Augmented Generation) system that turns a codebase — its
current source and its entire git history — into a searchable knowledge
base you can ask questions of in plain English. It answers both "how does
this work" (current state) and "how has this changed" (project history),
for technical and non-technical users alike, with every answer grounded
in and citing the actual retrieved source — not the model's memory.

## Status

Validated end-to-end against two real, differently-shaped repositories:
[psf/requests](https://github.com/psf/requests) (Python, 6,500+ commits,
fully backfilled) and [gorilla/mux](https://github.com/gorilla/mux) (Go).
Retrieval quality, answer faithfulness, and search-index accuracy are
measured with real numbers, not assumed — see
[docs/LIMITATIONS_AND_ROADMAP.md](docs/LIMITATIONS_AND_ROADMAP.md) for
exactly what's proven and what's still open.

## What it does

- **Ingests any codebase's current state** — code and docs, chunked along
  real syntactic boundaries (tree-sitter: Python, JS/TS, Go, Java, C/C++,
  with a safe fallback for anything else), never mid-function. Oversized
  chunks (a class with many methods) are split further automatically so
  nothing gets silently truncated by the models that read it later.
- **Resolves cross-file references** — when a retrieved piece of code
  calls a function or class defined elsewhere, the system pulls in that
  real definition too (via Universal Ctags), so the LLM isn't guessing
  what an unfamiliar name means.
- **Ingests full git history** — every commit's message and diff, plus
  (for Python) exactly which functions changed, as a separate, permanent
  record you can ask "how has X evolved" questions against.
- **Auto-updates both** on every commit, via a git hook — no manual
  re-indexing ever needed.
- **Answers questions** with hybrid search (semantic + keyword) across
  current code and history, a cross-encoder reranker for more precise
  ranking, several rule-based shortcuts for cases embedding similarity
  handles poorly (exact filenames, dates, identifiers, "what is this
  project"), and an LLM that only states what's actually in the
  retrieved sources.
- **Shows its work** — the web UI's side panel displays exactly which
  chunks were retrieved and sent to the LLM for every answer, so nothing
  has to be taken on faith.

## Quick start

```bash
# 1. Generate config.yaml interactively (repo path, models, thresholds) --
#    validates what it can as you go (Qdrant reachable, embedding model
#    actually loads, etc.) instead of failing later.
python3 backend/setup_config.py

# 2. Confirm everything is wired up correctly before a real ingest
python3 backend/check_config.py

# 3. Index the current-state codebase -- fast, minutes even for a large repo
python3 backend/ingestion/ingest.py

# 4. Optional: backfill full commit history (can take hours on a large repo;
#    safe to interrupt and resume)
python3 backend/history/history_ingest.py
```

Then ask questions:

```bash
python3 backend/generation/generate.py how does authentication work
python3 backend/generation/generate.py how has authentication changed over time
```

Full details, including manual (non-interactive) config setup and every
other script, in [docs/SETUP.md](docs/SETUP.md) and
[docs/USAGE.md](docs/USAGE.md).

## Web UI

A single-page chat interface with a live side panel showing exactly which
chunks were retrieved and sent to the LLM for each turn:

```bash
cd backend
uvicorn main:app --reload
# open http://127.0.0.1:8000
```

## Project layout

```
backend/              config.py/.yaml, main.py, setup_config.py, check_config.py
backend/core/         pipeline, chunking, symbol resolution, file exclusions
backend/ingestion/    full + incremental current-state indexing
backend/history/      full + incremental commit-history indexing
backend/retrieval/    hybrid search (current-state and history)
backend/generation/   prompt building, chat, LLM backend
backend/dev/          evaluation/debugging tools -- not part of the running product
frontend/             static single-page chat UI served by the FastAPI app
docs/                 architecture, setup, usage, decisions log
test_data/            target repo(s) this instance is indexing (not tracked in git)
```
