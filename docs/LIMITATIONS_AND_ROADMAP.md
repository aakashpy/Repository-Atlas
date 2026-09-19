# Known Limitations & Roadmap

Honest accounting of what this PoC does well, what it doesn't handle yet,
and what's genuinely worth doing next — not just a wishlist.

## Known limitations (present today)

### Configuration
- Target repo, thresholds, embedding/LLM/reranking model choice, and every
  other tunable setting now live in `backend/config.yaml` — switching to a
  different project is a config edit + re-ingest, not a code change
  (validated directly: see "What's already been validated" below).
- `retrieval.min_score_current`/`min_score_history` (0.37) were calibrated
  empirically against Qwen3's score distribution on one test repo
  (`requests`) — not independently re-validated on `mux` or any other
  codebase's content and terminology. A different corpus could shift this.
  `backend/check_config.py --calibrate` now automates re-measuring this
  after a model or corpus change, but it's a fast heuristic (a handful of
  LLM-generated probes), not a substitute for a real evaluation pass.
- **Ingestion now respects the target repo's own `.gitignore`** (via
  `git check-ignore`, not a hardcoded directory list) plus a
  user-configurable `ingestion.exclude_patterns` list — closes a real
  risk for the eventual private-codebase goal, where a gitignored file
  is plausibly gitignored because it holds a secret. See DECISIONS.md,
  "Ingestion Now Respects the Target Repo's .gitignore."
- **Portability for a new deployer was checked directly, not assumed** —
  found and fixed three real gaps: a broken `.gitignore` that didn't
  actually exclude `.env` (a secret-leak risk on first commit) or
  `__pycache__/`; `llm.ollama_model`/`gemini_model` config keys that were
  silently ignored by `llm_backend.py` (fixed); and unpinned
  `requirements.txt` versions (now pinned to what's actually verified
  working). See DECISIONS.md, "Deployment Portability."
- Still a real gap, not yet built: changing the embedding model or LLM
  backend requires several manual system-level steps documented in
  `SETUP.md` (Docker/Qdrant, Ctags, GPU drivers, and model-specific needs
  like the compiler toolchain Qwen3's Triton kernel required) — nothing
  automates system setup itself, only config/model validation
  (`check_config.py`) and threshold recalibration.

### Language support
- Multi-language chunking (tree-sitter: Python, JS/TS, Go, Java, C/C++) and
  cross-file symbol resolution (Universal Ctags) are implemented and
  validated end-to-end on a second, real, differently-shaped repo
  (`gorilla/mux`, Go) — not just Python. See DECISIONS.md for the full
  validation. Languages outside that set still fall back to generic
  line-based chunking.
- Not yet validated: whether the relevance-gate threshold and reranker
  behave equally well on a codebase whose natural-language content (docs,
  comments) differs a lot more from `requests`'/`mux`'s — both are
  English-documented open-source HTTP libraries, a fairly narrow slice of
  what "any codebase" could mean.
- **Oversized code chunks are now split** — a large class (many methods)
  no longer becomes one unbounded chunk; it's split into one piece per
  method (each correctly named) plus a character-slicing backstop for
  anything still too big. Measured before/after (see DECISIONS.md,
  "Oversized Code Chunks Split"): mean reranker pair length dropped from
  1,028 to 287.6 tokens, worst case from 24,604 to 1,370 tokens, truncated
  pairs from 31.6% to 14.2%. A small residual gap remains for single
  oversized chunks with no further nested structure to split on (e.g. a
  large generated dict) — the character backstop caps at 4,000 characters,
  which for dense code can still land a little over the reranker's
  512-token limit. Also found a small, honest trade-off: Recall@6 moved
  from 90.91% to 86.36% on the 22-question set, traced to a corpus-wide
  BM25/ranking shift from more chunks overall, not a bug in the splitting
  logic (confirmed by checking a file untouched by the fix still shifted).

### History granularity and completeness
- Function-level history tracking (`function_change` chunks) exists for
  Python via `history_pipeline.py`'s AST-based diffing, but stays
  Python-only — a non-Python repo (e.g. `mux`) gets file-level history
  only, same limitation the multi-language chunking work doesn't cover
  since it's a different, still-`ast`-based code path (deliberately
  deferred, see DECISIONS.md Phase 2 entry).
- History is **append-only** with no pruning strategy. On a very long-lived
  project, the history collection will grow indefinitely — no plan yet for
  archiving or summarizing very old, low-relevance history.
- `requests_history` now holds a **full backfill of all 6,494 commits**
  (34,659 points) — see DECISIONS.md, "Full History Backfill Completed."
  Along the way this surfaced and fixed a real bug: `chunk_text()` only
  bounded chunks by line count, so a commit with a pathologically long line
  (a binary file diffed as text) could produce a multi-megabyte chunk and
  crash the ingest with a Qdrant request-size error. Now fixed generically
  (character-length cap + batched upserts), not specific to the one commit
  that surfaced it.

### LLM / generation quality
- Tested and confirmed: a small local model (Qwen2.5-3B via Ollama)
  struggles with multi-source synthesis and can hallucinate facts it
  recognizes from pretrained knowledge (a bigger risk on well-known public
  repos than on a private codebase — see DECISIONS.md for the evidence).
- Current default (Gemini free tier) fixes this in testing, but:
  - Free-tier cloud APIs can return transient errors (e.g. `503` "high
    demand") — no automatic retry logic exists yet.
  - Using a cloud API means code/questions leave the local machine —
    acceptable for public test data, a real decision point for a private
    codebase (see DECISIONS.md for the reasoning).

### Operational
- The git hook must be **manually installed per clone/machine** — not
  something git syncs automatically. No setup script exists yet to
  automate this for a team.
- No automated test suite — all verification so far has been manual,
  targeted testing (a real strength for catching specific bugs, but not
  a substitute for regression protection as the system grows).
- No access control — anyone with access to the running system can query
  everything indexed. Not evaluated for multi-user/permission scenarios.

### Interface
- A web chat UI exists (`backend/main.py` + `frontend/`) with a retrieval
  side panel showing exactly which chunks were used for each answer — no
  longer CLI-only. Still single-user, no auth, one process serving one
  configured project at a time (see "Configuration" above).

### Evaluation
- Retrieval quality, ANN/HNSW behavior, and RAG answer quality are all now
  measured with real numbers — see DECISIONS.md:
  - Recall@6 = 90.91%, MRR = 0.469 (retrieval).
  - Qdrant's default HNSW index matches brute-force exact search at this
    collection's current size.
  - `eval_rag.py` (LLM-judge faithfulness/relevancy scoring): Faithfulness
    11/11 (100%), Relevancy 10/11 (91%) on an 11-question set. Along the
    way this found and fixed a real bug in the eval script itself (its
    judge was shown different context than the LLM actually saw, causing
    a false-positive hallucination flag) — see DECISIONS.md.
- What's still missing: `eval_rag.py` is a **manual eval script**, run on
  demand against a small (11-question) set — it is not a live safeguard
  wired into `generate.py`/`chat.py` that checks every real answer as it's
  produced, and it hasn't been run against a larger or more adversarial
  question set. "Hallucination detection" in the sense of an always-on,
  in-the-loop check does not exist yet; what exists is an offline
  benchmark.

## What's already been validated (don't need to re-litigate)

- Core RAG loop (retrieval + generation) works end-to-end.
- Hybrid search (dense + sparse) meaningfully improves retrieval quality
  over semantic-only search — proven with before/after evidence.
- Incremental updates are correct and idempotent (verified: modify,
  delete, rename, and rename+content-change-in-one-commit all handled
  correctly, with no duplication or data loss).
- The relevance-gate approach correctly distinguishes answerable from
  unanswerable queries.
- Cross-encoder reranking measurably improves ranking quality over plain
  RRF fusion (verified with real before/after scores).
- Retrieval quality is measured, not assumed: Recall@6 = 90.91%, MRR =
  0.469 on a 22-question hand-labeled gold set.
- ANN/HNSW: measured (not assumed) that Qdrant's default approximate index
  is already matching brute-force exact search at this collection's
  current size — no accuracy being traded away yet.
- Full history backfill: `requests_history` holds all 6,494 commits
  (34,659 points), not a partial sample.
- RAG answer quality is measured, not assumed: `eval_rag.py` reports
  Faithfulness 11/11 (100%), Relevancy 10/11 (91%) via LLM-judge scoring
  against the actual retrieved context.
- Config preflight checking: `check_config.py` verified end-to-end against
  the real live config (all 9 stage-1 checks pass; stage-2 calibration
  produced a sensible threshold suggestion with a clean on-topic/off-topic
  score gap) — see DECISIONS.md, "Config Preflight Checker."
- Reranker assumptions measured, not assumed: `candidate_pool=20` sits in
  a reasonable range across repeated measurements. The cross-encoder's
  512-token truncation problem, found and quantified (31.6% of pairs,
  worst case 97%+ content loss), was then fixed by splitting oversized
  code chunks -- truncation rate down to 14.2%, worst case down to 1,370
  tokens. See DECISIONS.md, "Reranker's Own Assumptions Validated" and
  "Oversized Code Chunks Split."
- **Genericity across languages/repos, validated end-to-end**: switching
  `config.yaml` to a second, real, differently-shaped repo (`gorilla/mux`,
  Go, zero code changes) and re-ingesting correctly produced tree-sitter
  chunking, ctags symbol resolution, hybrid search, cross-encoder
  reranking, all four boosting mechanisms, and grounded, correctly-cited
  Gemini answers about real Go code — not just an isolated chunker/symbol-
  index smoke test (an earlier, narrower version of this check), a real
  full-pipeline run. See DECISIONS.md.

## Roadmap — recommended next steps, in priority order

1. **Widen RAG evaluation, and consider a live check** — `eval_rag.py` now
   has real numbers, but only on an 11-question offline set; a larger/more
   adversarial question set and/or wiring a lightweight faithfulness check
   into the live chat flow (not just an on-demand script) would go further.
2. **Automated tests** — codify the manual verification done throughout
   this project (rename handling, idempotent updates, relevance gating,
   multi-repo genericity) into a real test suite, so future changes can't
   silently reintroduce these bugs.
3. **Point this at an actual private codebase** — the real test of value,
   and removes the public-repo hallucination risk entirely; the multi-repo
   validation above is a proxy for this, not a replacement.