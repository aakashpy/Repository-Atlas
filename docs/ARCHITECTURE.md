# Architecture

## Overview

The system maintains two parallel, independently-searchable knowledge bases
about a codebase:

1. **Current state** — what the code looks like right now.
2. **History** — how it got there, commit by commit.

Both are kept in sync automatically via a single git hook, and both are
searched together at query time, letting an LLM decide which is relevant
to a given question.

## Data flow
                ┌─────────────────┐
                │   git commit     │
                └────────┬─────────┘
                         │ triggers
                ┌────────▼─────────┐
                │  post-commit hook │
                └────────┬─────────┘
             ┌───────────┴───────────┐
             ▼                       ▼
    ┌────────────────┐     ┌──────────────────┐
    │ update_index.py │     │ history_update.py │
    │ (current state) │     │    (history)      │
    └────────┬────────┘     └─────────┬─────────┘
             ▼                         ▼
    ┌────────────────┐     ┌──────────────────┐
    │  requests_poc   │     │ requests_history  │
    │  (Qdrant coll.) │     │  (Qdrant coll.)   │
    └─────────────────┘     └──────────────────┘
             │                         │
             └───────────┬─────────────┘
                          ▼
                 ┌─────────────────┐
                 │   generate.py    │
                 │ (search both →   │
                 │  LLM synthesis)  │
                 └────────┬─────────┘
                          ▼
                      Answer to user

## Components

### 0. Configuration (`config.yaml`, `config.py`)

All project-specific settings live in `config.yaml` — repo path, Qdrant
connection details, embedding model, chunk sizes, relevance thresholds,
and LLM backend/model choice. `config.py` loads this once (singleton
pattern, same approach as the cached model/client in `pipeline.py`) and
every other file reads from it via `get_config()`.

Collection names are derived, not set directly: `project.name` in the
config (e.g. `"requests"`) becomes `requests_poc` (current-state) and
`requests_history` (history) via `get_current_collection_name()` /
`get_history_collection_name()` in `config.py` — this keeps the two
collections consistently paired to one project name.

Secrets (API keys) are kept separate, in `.env` (git-ignored) — never
in `config.yaml`, since config files are expected to be shareable/
committed, unlike secrets.

Pointing this system at a different project requires editing only
`config.yaml` — no code changes needed elsewhere.

### 1. Current-state pipeline (`pipeline.py`, `ingest.py`, `update_index.py`)

- **Chunking:** `chunk_file()` dispatches by file type — `.py` files use
  `chunk_python_file()` (AST-based: splits by function/class boundaries,
  keeping each definition whole). Other files use `chunk_text()`
  (line-based, 40 lines with 5-line overlap).
- **Embedding:** dense (semantic meaning, 384-dim, `all-MiniLM-L6-v2`,
  GPU-accelerated) + sparse (keyword/BM25, via `fastembed`).
- **Storage:** `requests_poc` Qdrant collection, hybrid schema (named
  `dense`/`sparse` vectors per point).
- **IDs:** deterministic, hashed from `filepath::chunk_index` — re-indexing
  the same content always maps to the same ID, so updates overwrite rather
  than duplicate.
- **Updates:** `update_index.py` diffs the current commit against the last
  indexed commit, handling Added/Modified/Deleted/Renamed files, including
  the case of a rename with a content change in the same commit.

### 2. History pipeline (`history_pipeline.py`, `history_ingest.py`, `history_update.py`)

- For every commit, for every file it touched: stores the **commit message**
  and the **diff** as separate, linked chunks (same `commit_hash` in
  payload), avoiding embedding-length truncation on large diffs.
- Same hybrid (dense+sparse) storage pattern as current-state.
- Diffs longer than 2000 characters get further split via `chunk_text()`.
- `requests_history` never deletes records (append-only) — a file being
  deleted or renamed doesn't remove its past history entries.

### 3. Retrieval (`search.py`, `search_history.py`)

Both use the same two-stage pattern:
1. **Relevance gate** — a plain dense-only cosine similarity check. If the
   best match is below `MIN_SCORE` (0.35), return nothing — this is what
   lets the system say "I don't know" instead of forcing an answer.
2. **Hybrid fused search** — if the gate passes, a proper ranked search
   combining dense (semantic) + sparse (BM25) results via Qdrant's
   RRF (Reciprocal Rank Fusion), which handles cases plain semantic search
   misses (e.g. exact function/class names, or when literal wording matters
   as much as meaning).

### 4. Generation (`generate.py`, `llm_backend.py`)

- Retrieves from **both** `requests_poc` and `requests_history` for every
  query — no upfront "is this a history question" classification step
  (deliberately avoided; see DECISIONS.md for why).
- Builds one prompt labeling sources as "Current Code" vs "History",
  instructing the model to use whichever is relevant, combine both if
  needed, and explicitly avoid outside/pretrained knowledge.
- `llm_backend.py` abstracts the actual LLM call behind one function,
  `generate_text(prompt, backend)` — swappable between:
  - `"ollama"` — local, free, fully private (Qwen2.5-3B)
  - `"gemini"` — cloud, free tier, better reasoning quality (current default)

## Why two collections instead of one

Current-state and history store fundamentally different content: a full
current snapshot of a function vs. an incremental diff of one change to it.
Merging them into one collection wouldn't remove this distinction — it
would just move it into a field instead of a collection boundary. Keeping
them separate makes each collection's schema and purpose unambiguous.

## Automatic updates

A single `post-commit` git hook (in the tracked repo's `.git/hooks/`)
runs both `update_index.py` and `history_update.py` after every commit.
No manual re-indexing step exists in normal operation.