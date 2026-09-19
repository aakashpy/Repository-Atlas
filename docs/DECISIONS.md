# Project Decisions Log 
## Stack - Language: 
Python 3.14 - Environment: WSL2 + Docker - Vector DB: Qdrant (self-hosted, free) - Embeddings: sentence-transformers, all-MiniLM-L6-v2, GPU-accelerated (384 dims) 
## Why - Local embeddings chosen over paid APIs: 
project will re-embed frequently as it grows; avoids ongoing cost and rate limits. - Qdrant chosen over Chroma: strong delete/update-by-filter support, needed for handling stale data later. - GPU confirmed available (GTX 1650 Ti) and enabled for faster re-indexing at scale.

## Phase 2 — Retrieval Quality Findings

Tested 6 queries against PoC. Found 3 issues:
1. No relevance cutoff — fixed with MIN_SCORE threshold in search.py
2. Fixed-line chunking split code arbitrarily — fixed with AST-based
   chunking for .py files (function/class boundaries)
3. Casual/keyword-heavy queries can outrank the technically correct
   answer — requires hybrid search (semantic + BM25). DEFERRED to a
   later phase; revisit after core pipeline (change detection,
   stale-data handling) is built.

Chunking implemented as a dispatcher (chunk_file) so language-specific
chunkers (e.g. tree-sitter for non-Python languages) can be added later
without restructuring.

## Phase 4 — Stale Data Handling

Found and fixed a real bug: git rename status ("R100") wasn't handled
by get_changed_files/run_update, causing renamed files to be silently
skipped — old chunks stayed stale, new path was never indexed.

Fix: parse rename lines to extract both old and new paths; delete
old path's chunks, then index new path's (current) content.

Tested and verified:
- Modify, Delete, Rename, and Rename+content-change-in-same-commit
  all handled correctly with no stale data or missing content.

## Phase 5 — Generation Layer

Added local LLM (Ollama, qwen2.5:3b) as the generation step, completing
the actual RAG loop (retrieval + generation).

Found and partially addressed a real quality issue: retrieval correctly
surfaces multiple relevant chunks, but the small local model sometimes
picks the wrong one to build its answer around (e.g. prioritizing a
historical changelog mention over the directly relevant class docs).

Attempted fix: more explicit prompt instructing multi-source synthesis
and priority rules. Result: WORSE, not better — model produced longer,
more confident-sounding but reasoning-flawed answers, including one
hallucinated file path not present in the retrieved context.

Conclusion: this is a genuine capability ceiling of a 3B local model for
multi-source reasoning, not a prompt-engineering problem. Known
trade-off of the free/local/private choice made earlier.

DECISION: Accept as a known PoC limitation. Revisit later — options if
needed: larger local model (7B+), paid API for generation only, or
restructure to reduce reasoning burden (e.g. rank-and-present single
best source instead of asking for multi-source synthesis).

## Phase 7 — Unified Update Trigger

Single post-commit hook now runs both update_index.py (current-state
collection) and history_update.py (history collection) on every commit.
No manual/separate triggering needed going forward.

Two collections kept separate (requests_poc, requests_history) rather
than merged, since they store fundamentally different content:
full current snapshots vs. incremental diffs. Query-time: search both,
let the existing relevance gate naturally surface whichever is
actually relevant to the question — no upfront question-classification
step, given known reasoning limits of the local 3B model.

## Phase 5/7 Revisit — LLM Backend Decision

Built a swappable LLM backend (llm_backend.py) supporting both local
(Ollama, qwen2.5:3b) and cloud (Gemini, free tier) generation.

Evidence-based comparison on identical retrieval + prompts:
- Local (Qwen2.5-3B): failed at multi-source synthesis (picked wrong/
  less relevant chunk), and hallucinated specific facts not present in
  retrieved context when the topic overlapped with its training data
  (public repo). Confirmed via direct verification against retrieved
  chunks.
- Cloud (Gemini, gemini-flash-latest): correctly synthesized multiple
  sources, prioritized the genuinely relevant answer, and honestly
  flagged when context was insufficient. No hallucination found.

DECISION: Gemini set as default backend for now. Ollama kept as a
fallback option (useful for fully private/offline scenarios or if
free-tier limits become a constraint). Real target use case (private
codebase) has much lower hallucination risk than our public test repo,
since the model won't have pretrained knowledge of private code —
Gemini's synthesis-quality advantage still applies regardless.

## Phase 6 (Full) — Complete Historical Backfill

Processed full git history: 6,504 commits → 26,476 chunks.

Real-world data issues found and fixed during backfill:
- UnicodeDecodeError on some diffs (non-UTF-8 bytes from old commits) —
  fixed with errors="replace" on all subprocess calls.
- IndexError on malformed rename lines from git (rare edge case) —
  fixed with defensive parsing, degrading to plain "modified" status.

End-to-end verified: query "how has authentication been implemented
over the years" produced an accurate, multi-year historical narrative
(2011-2014), independently spot-checked against raw retrieved chunks —
all cited dates/facts confirmed genuine, no hallucination.

This validates the original project vision: answering both
"how does X work" (current state) and "how has X evolved" (history)
for technical and non-technical users, grounded in real project data.

## Market Research — Existing Tools (Sept 2026)

Surveyed the landscape before continuing: DeepWiki (repo-to-wiki, free
for public repos only), Aider (open-source, tree-sitter repo-mapping —
relevant reference for our deferred multi-language chunking gap),
Sourcegraph Cody (enterprise-only, ~$16k/year), Greptile and Unblocked
(closest to our "history + why" vision, both paid $29-30/seat/month,
closed source).

Finding: no free/self-hosted tool currently combines current-state +
full history RAG with local-first privacy, which was our original
motivation. This validates continuing our own build rather than
adopting an existing tool.

DECISION: Continue building our own approach. Treat Aider's tree-sitter
repo-map as a future reference when we tackle multi-language chunking
(see LIMITATIONS_AND_ROADMAP.md), not something to adopt now.

## Configuration Externalized

Moved all hardcoded settings (repo path, collection names, relevance
thresholds, chunk sizes, LLM backend/model choice) into config.yaml,
loaded once via config.py (singleton pattern, consistent with
pipeline.py's existing model/client caching).

Found and fixed a naming bug during this refactor: initial config had
project.name set to the current-state collection name itself
("requests_poc"), causing the history collection name to incorrectly
resolve to "requests_poc_history" instead of "requests_history".
Fixed by making project.name the base name ("requests"), with "_poc"
and "_history" both derived as suffixes.

Switching this system to a different project now requires editing only
config.yaml — no code changes needed across the 8+ files that
previously had hardcoded values.

## Function-Level History Tracking

Upgraded history from file-level to function-level (Option B from
earlier discussion): for every commit touching a .py file, we now
also store one chunk per changed function/class, using AST-based
before/after comparison to detect added/modified/removed functions
precisely (not just "this file changed").

Found and fixed real issues along the way:
- Test-commit contamination (our own Phase 4 testing) was polluting
  history with a giant artifact diff dump, skewing rankings. Cleaned
  up by removing all commits authored by our test identity.
- Even after cleanup, discovered a genuine ranking gap: function_change
  chunks (long, code-heavy) were being outranked by short message/diff
  chunks in normal hybrid search, for "how has X evolved" style
  queries — the exact query type function-level tracking was built for.
- FIXED via explicit boosting: when a query contains an identifier-like
  token (PascalCase or snake_case), we directly look up matching
  function_change chunks via metadata filter (not similarity search)
  and prioritize them ahead of normal ranking results.

Full backfill re-run with function-level tracking: 6,504 commits ->
31,903 chunks (up from 26,476 file-level-only).

Verified end-to-end: "how has PreparedRequest evolved" now produces a
detailed, accurate, multi-year technical narrative (2013-2015),
correctly grounded in boosted function-level chunks.

## Regression Testing — Boosting Bugs Found & Fixed

Systematic regression testing after function-level history work
uncovered two real bugs, not caught during initial development:

1. `function_name` was missing from returned boosted/hybrid result
   dicts (present in Qdrant payload, but not surfaced to callers) —
   fixed by including it explicitly in both result-building paths.

2. Multi-identifier queries (e.g. "PreparedRequest and Session") only
   returned results for the FIRST identifier found — each candidate's
   lookup used the full top_k as its limit, so the first one crowded
   out all others before truncation. Fixed by splitting the limit
   across candidates (limit // number_of_candidates).

Lesson: single-identifier testing (our original validation) did not
exercise this code path — multi-identifier testing was necessary to
surface it.

## Systematic Regression Test — Post Function-Level History

Ran a deliberate regression pass across 5 areas after completing
config externalization and function-level history work, rather than
assuming prior feature-level testing was sufficient. Result: 4/5 areas
passed cleanly; multi-identifier boosting had 2 real bugs (see above),
now fixed and re-verified.

## "Stale README" Bug — Investigated, Not Reproducible

CLAUDE.md previously recorded an open issue: after re-cloning
test_data/requests fresh, a Qdrant-stored README.md chunk appeared to
contain old/wrong content (a `git config --global fetch.fsck...` line)
not present in the real file.

Traced the full pipeline end to end: disk content (`cat`, `Path.read_text()`,
`git log`) confirmed that line is genuine, current upstream psf/requests
content, not stale data. `chunk_file()` run directly on the real file
reproduces it correctly. Qdrant's stored payload matched the chunker's
output byte-for-byte. Only one Qdrant container was running, no stale
second instance. Full RAG queries (search.py and generate.py) both
surfaced this content correctly and accurately.

DECISION: no bug existed at the time of investigation; the original
problem description was inaccurate (likely a stale observation from
before a container restart, or a misread of legitimate file content).
Closed without a code change.

## Current-State Ingestion Was Missing All Documentation Guides

`ingest.py`'s `ALLOWED_EXTENSIONS` was `{".py", ".md", ".txt"}` — `.rst`
was never included. This silently excluded the entire `docs/user/`
guide (quickstart.rst, authentication.rst, advanced.rst, install.rst),
which is exactly the onboarding content a new user asks about
("how do I install this", "how do I authenticate").

Fixed by adding `.rst` to `ALLOWED_EXTENSIONS`. Re-ingest went from 52
to 68 files (337 to 430 Qdrant points). Verified with direct before/
after re-tests: install instructions, quickstart GET example, and
authentication code examples all went from "no information found" to
correct, source-grounded answers pulled straight from the new .rst
content.

## Conversational Chat Interface (chat.py)

Added multi-turn conversation support via a new `chat.py`, reusing
`search_raw`/`search_history_raw`/`build_prompt`/`BACKEND` from the
existing modules unchanged — retrieval-layer code needed zero
modification for this.

Chose the "condense-question rewrite" architecture (one extra LLM call
per follow-up turn folds conversation history into a standalone query
before retrieval) over two alternatives considered: a no-rewrite
heuristic (cheaper, but fails on pronoun-heavy follow-ups) and full
agentic tool-calling (most flexible, but breaks the backend-agnostic
`generate_text(prompt, backend)` contract this project deliberately
built for Gemini/Ollama swapping — Gemini's automatic function calling
has no Ollama equivalent).

Verified with a 3-turn test: "How does Session handle cookies?" →
"How has it changed over time?" (correctly rewritten to reintroduce
"Session" and "cookie persistence", which also correctly triggered
search_history.py's identifier-boosting) → "What about PreparedRequest
— similar history?" (correctly carried the topic forward while
swapping in the new entity).

Manual testing surfaced two follow-up bugs, both fixed in
generate.py's prompt:
- Vague/broad questions ("explain how requests works") returned thin,
  one-note answers. Fixed by raising `top_k` from 3 to 6 for the final
  generation call, giving the LLM more material to synthesize from.
- Ambiguous or personified questions ("when is your birthday") were
  silently reinterpreted (e.g. answering with a version number as if
  that were obviously the right reading) with no indication to the
  user. Fixed with an explicit INTERPRETATION RULE instructing the
  model to state its reinterpretation before answering. Follow-up
  testing showed this rule over-triggers on ordinary phrasing
  containing "you" (e.g. "can you explain...") — left as a known,
  low-severity side effect rather than fixed, since it's harmless
  boilerplate rather than a wrong answer.

## Retrieval Quality Overhaul

User goal: retrieval should be good enough for both technical and
non-technical users asking "how do I use X" / "how has X evolved"
questions against a real repo. Built a retrieval-only test harness
(search_raw/search_history_raw called directly, no LLM) across 16
diverse questions (how-to, broad/non-technical, code, history, date,
off-topic) to get an evidence-based baseline before changing anything.

Found and fixed four real, distinct problems:

1. **Doc chunking was a blind 40-line window.** `.md`/`.rst` files got
   the same line-window chunker as everything else, which routinely
   cut a how-to section in half (e.g. the CA-bundle example and the
   timeout tuple syntax in advanced.rst were split from their
   explanations). Fixed with a new `chunk_prose()` in pipeline.py:
   splits on Markdown ATX headers / RST title-underlines so each chunk
   is one coherent section; oversized sections fall back to
   paragraph-grouped sub-chunks with the heading carried forward.
   `.py` chunking (AST-based) was untouched; diff-chunking in
   history_pipeline.py was confirmed to use a separate code path, so
   requests_history did not need re-ingesting.

2. **Badge/image markup was diluting embeddings.** README.md's intro
   chunk mixed 5 PyPI/CI badge markdown links with the actual
   descriptive prose. Measured directly: cosine similarity between
   "What does this project do?" and the real README description was
   0.051 — lower than an unrelated Contributor's Guide chunk (0.422).
   Fixed by stripping pure badge/image lines (Markdown badge links,
   RST image directives) before chunking.

3. **Trivial chunks were polluting results.** Bare RST anchor lines
   (e.g. `.. _authentication:`) were becoming their own near-empty
   chunks that spuriously ranked in top-3. Fixed by merging any
   trivial pre-heading fragment into the section that follows.

4. **"What does this project do?"-style questions can't be fixed by
   embedding quality alone.** Even after the badge fix, generic
   "about this project" phrasing doesn't reliably out-score unrelated
   docs on pure similarity search — a genuine small-model (MiniLM)
   limitation, not a chunking bug. Fixed the same way identifier
   boosting already handles function/class names: `search.py` now
   detects "this project/library/repo/codebase" phrasing and does a
   direct metadata lookup for the README's real intro chunk (picking
   the H1-heading chunk specifically, since Qdrant's scroll() order
   isn't chunk-sequential), boosted ahead of similarity ranking.

Also added **exact-date lookup** to search_history.py, same pattern:
`_extract_date_candidate()` recognizes "October 22, 2011" / "22 Oct
2011" / "2011-10-22" style phrasing (ISO format matches how
`git log --date=short` stores it) and does a direct metadata filter
instead of similarity search. This fixed a real gap found in manual
testing: a commit from an exact date was retrievable in one
conversation turn but produced "no information found" when asked
about that same date directly, since embedding similarity has no real
way to treat a specific calendar date as an exact filter.

Verified end-to-end after each fix, retrieval-only first (before/after
comparison across all 16 baseline questions) and then through the full
Gemini pipeline (8 targeted re-tests, one per fix). All 8 produced
correct, source-grounded answers, including the two hardest cases:
"What files were committed on October 22, 2011?" (exact file list,
previously impossible) and the CA-bundle question (correct
`verify='/path/to/certfile'` example, despite that chunk ranking #4
rather than top-3 at the raw retrieval layer — covered in practice by
generate.py's top_k=6).

Known remaining gap: the CA-bundle chunk's #4 ranking (vs. a
"Certifi CA Bundle" chunk that literally contains the words "CA
Bundle" in its heading) wasn't root-caused further, since top_k=6
already covers it at generation time. Left as a documented, low-
severity limitation rather than over-engineering a fix for a case
that doesn't currently produce a wrong answer.
## Frontend/Backend Restructure + Web Chat UI

**Goal**: move from CLI-only usage to a real product shape — a single-page
web chat that both technical and non-technical users can point at a repo,
plus visibility into what retrieval actually sent the LLM (needed to keep
trusting the retrieval-quality work from the previous phase as the project
grows).

**Structure**: all Python (`pipeline.py`, `search.py`, `search_history.py`,
`generate.py`, `chat.py`, `llm_backend.py`, ingestion/update scripts,
`config.py`/`config.yaml`, `.env`) moved into `backend/`, unchanged in
behavior. New `frontend/` holds a static `index.html` + `app.js` +
`style.css` — plain JS, no build step, matching the project's existing
"minimal dependencies" style. New `backend/main.py` is a FastAPI app that
both serves `/api/chat` and mounts `frontend/` as static files from the
same process, so there's no separate frontend dev server and no CORS
configuration needed.

**Chat state**: kept client-side. The browser holds the running
`(role, text)` message list and resends it with every request; the backend
stays stateless (no session store, no DB). This matches the project's
current single-user, single-process usage — a session store would be
premature for a PoC with no auth or multi-tenancy yet.

**Chunk exposure**: `chat.py`'s `chat_turn()` used to return
`(answer, standalone_query)`; it now returns a dict that also includes
`current_chunks` and `history_chunks` — the exact same lists
`search_raw`/`search_history_raw` produced and that `build_prompt()` sent to
the LLM, not a separate/approximate view. The frontend's side panel renders
these per turn (file path, score, date/chunk-type for history results),
so what the user sees retrieved is provably what the LLM actually got.

**Path robustness fix (found during this restructure, not scoped to it)**:
`ingest.py`, `update_index.py`, `history_ingest.py`, and `history_update.py`
all resolved `project.repo_path` (or a hardcoded `"test_data/requests"`
duplicate in the latter two) relative to the *process's current working
directory*. That was fine when everything lived at the repo root and was
always invoked from there, but moving scripts into `backend/` would silently
break them if ever run with `backend/` as the cwd (e.g. `cd backend &&
python3 ingest.py`) instead of the repo root. Fixed generically: added
`config.get_repo_path()`, which resolves `repo_path` relative to
`Path(__file__).parent.parent` (the project root, one level above
`backend/`) regardless of the caller's cwd, and switched all four scripts
to use it instead of ad hoc relative strings. This is a generic
correctness fix, not specific to the `psf/requests` test repo — applies
identically to any `repo_path` in `config.yaml`.

**Also updated**: `test_data/requests/.git/hooks/post-commit` (local,
not version-controlled) now calls `backend/update_index.py` and
`backend/history_update.py`; `README.md`/`docs/USAGE.md` command examples
now use `backend/<script>.py`; `CLAUDE.md`'s key-files list updated to the
new paths, and its now-resolved "CURRENT ISSUE BEING DEBUGGED" section
(the stale-README investigation, already logged above as not reproducible)
removed since it no longer reflects real state and its file paths were
stale.

**Verified**: started `uvicorn main:app` from `backend/`, confirmed the
static frontend serves at `/`, then round-tripped a real question through
`/api/chat` end-to-end through Gemini (got a correctly source-grounded
answer with 6 current-code + 6 history chunks attached), and confirmed the
condense-question rewrite still works through the new endpoint (a
follow-up "when was it added?" correctly rewrote to "When was the
PreparedRequest class added?" using client-sent history). Also hit and
fixed an unrelated infra issue found during this verification: the Qdrant
Docker container was up but reporting zero collections despite the data
being present on disk under the correct bind mount — a container restart
reloaded them correctly; not a code bug, but worth knowing if this
happens again after a host/WSL restart.

## Whole-File Question Boosting (search.py)

**Problem** (user-reported via the new web UI's retrieval side panel):
"explain me file test_utils.py" returned mostly irrelevant chunks
(`CONTRIBUTING.md`, `HISTORY.md`, `ISSUE_TEMPLATE.md`) plus exactly ONE
real chunk of the target file (its import block) out of 37 chunks that
file actually has in the collection. The LLM's answer was accordingly
thin and generic — it had genuinely never seen any of the file's real
test functions.

**Root cause**: `.py` files are AST-chunked per function/class, so a file
with 37 functions becomes 37 separate chunks that each compete
individually, via top-k hybrid ranking, against the ENTIRE corpus. A
generic query like "explain file X" doesn't embed distinctively enough to
make that file's own chunks outrank unrelated docs that share superficial
wording (e.g. "test suite" in CONTRIBUTING.md) — so most of the target
file's own content loses the ranking competition and never reaches the
LLM. This is the same class of gap as the earlier "about project"
retrieval fix, just for named files instead of the whole repo.

**Rejected fix**: just raising `top_k`. Discussed directly with the user
first. Doesn't guarantee completeness (more slots doesn't mean the right
file's chunks fill them), adds noise to every query type globally (not
just whole-file questions), and doesn't scale (a bigger file loses more
regardless of `top_k`).

**Fix**: a fourth boosting path in `search.py`, same pattern as the
existing about-project/identifier/date boosting — `_extract_filename_
candidates()` detects filename-shaped tokens in the query via a generic
`word.ext` regex (not tied to any fixed extension list, so it works for
any codebase's file types, not just this project's .py/.md/.rst/.txt),
and `_get_file_chunks_by_name()` does a direct Qdrant metadata lookup
(`filepath` MatchText filter + exact-basename check, since MatchText is
token-based and would also match e.g. "utils.py" against "test_utils.py")
for every chunk belonging to that file, bypassing similarity ranking
entirely. Capped at `retrieval.max_file_chunks` in config.yaml (default
20) to bound context size for very large files.

**Bug found and fixed during verification**: the first implementation
deduplicated boosted results by `filepath`, which is correct for
identifier/date boosting (one chunk per match there) but wrong here — all
37 of a file's chunks share the same filepath, so after the first one was
added, every subsequent chunk from that SAME file was incorrectly treated
as a duplicate and dropped, silently reproducing the original bug (still
only 1 chunk). Fixed by deduplicating on the Qdrant point ID instead,
which only excludes genuine duplicate chunks (e.g. if two filename
candidates in one query both matched the same chunk), not distinct chunks
from the same file.

**Also fixed a truncation bug this exposed**: `search_raw()`'s merge step
used to do `merged[:top_k]` unconditionally, which would have silently cut
a 20-chunk boosted result back down to `top_k` (default 3-6) — completely
defeating the fix. Changed so boosted results are never truncated; only
the additional hybrid "filler" results are capped, by whatever budget
remains after the boosted set (`max(0, top_k - len(boosted))`).

**Verified**: retrieval-only, `search_raw("explain me file test_utils.py")`
now returns 20/37 real chunks of the target file (capped by
`max_file_chunks`), zero unrelated docs. Regression-checked that normal
queries and the about-project boost still behave as before (unchanged
chunk counts/files). End-to-end through Gemini: `generate.py`'s answer
went from a vague, imports-only summary to a detailed, accurate,
function-by-function breakdown of the file's actual test coverage
(`set_environ`, `get_auth_from_url`, `requote_uri`, `should_bypass_
proxies`, `super_len`, etc.) — all real functions that exist in the file.

**Scope note**: intentionally limited to `search.py` (current-state
collection). `search_history_raw()` already surfaces a file's diffs
reasonably via literal BM25 text matching on diff headers (they contain
`diff --git a/path b/path`), and "give me this file's full history" is a
much larger completeness question (years of commits) that wasn't the
reported problem — left as a possible separate follow-up, not bundled in.

## Phase 2: Generic Multi-Language Chunking + Cross-File Symbol Resolution

**Decision to move here**: after the whole-file boosting fix, the user decided the
single-repo/single-language PoC was done and asked to invest directly in what makes
the system actually generic per its stated long-term goal (CLAUDE.md: pointing this
at a real private company codebase). Discussed and rejected full LSP integration
(would require standing up a language server + build environment per language per
repo -- doesn't scale to "arbitrary codebase") in favor of the same stack real tools
like Sourcegraph use: Tree-sitter for parsing, Universal Ctags for cross-file symbol
resolution. Planned in Plan Mode with the user before any code was written; the full
plan (with all trade-offs) is preserved in that session's plan file.

**Tree-sitter multi-language chunking** (`backend/code_chunking.py`, new):
Replaces `pipeline.py`'s old `chunk_python_file()`, which was hard-wired to
Python's own `ast` module -- a hard blocker for any non-Python codebase. Supports
Python, JS/TS, Go, Java, C/C++ via `tree-sitter-language-pack`, with the same
granularity as before (only TOP-LEVEL definitions become their own chunk -- a
class's methods stay inside its one chunk, matching the old chunker exactly).
Falls back to the existing line-window chunker (`chunk_text`) when a language
isn't mapped or parsing fails, same as the old `except SyntaxError` fallback,
generalized. Every chunk now also carries a `symbol_name` (the function/class/
type name, when extractable) -- needed for the symbol resolution below, and
useful in its own right (shown in the web UI's retrieval panel).

Per-language node-type mappings (which tree-sitter node kinds count as a
"chunkable definition", and where each one's name field actually lives) were
**verified empirically against real parses**, not assumed from memory -- grammar
internals vary in real ways: Python wraps a decorated function in
`decorated_definition` with no direct name field (must unwrap to the inner
`function_definition`); JS/TS wrap exports in `export_statement` the same way;
Go's `type_declaration` nests the actual name under a `type_spec` child; C/C++
function names live inside a `declarator` subtree (via `function_declarator` ->
`identifier`), not a simple `name` field the way Python/Go/Java do. Getting this
wrong would have silently produced `symbol_name=None` for huge swaths of
non-Python code, so each case was tested against a real snippet before being
encoded into `_extract_name()`.

**Cross-file symbol resolution** (`backend/symbol_index.py`, new): Universal
Ctags builds one map of every definable symbol name -> {file, line} across the
whole repo (`ctags -R --output-format=json`, always rebuilt from scratch per
ingest/update run rather than patched incrementally -- fast enough that
re-running is simpler and more correct than tracking cross-file invalidation).
At ingest time, each chunk is tagged with the *names* (not definitions) of any
other repo-defined symbols its text mentions (`find_referenced_symbols` --
plain regex + membership check against the symbol index, language-agnostic by
construction).

**Explicitly rejected**: baking referenced definitions directly into chunk text
at ingest time. Discussed two options with the user, who chose live resolution
specifically because of the staleness risk -- this project's very first task
this session was debugging a stale-chunk bug, and Option A would have
reintroduced exactly that failure mode (a chunk's baked-in copy of a referenced
definition going stale the moment that definition changes elsewhere, with
nothing to detect it, since incremental updates only re-index the file that
actually changed). Instead, `search.py::_expand_referenced_symbols()` resolves
referenced symbols LIVE at generation time -- same direct-metadata-lookup
pattern as the existing identifier/date/filename boosting, one level of
expansion only (a referenced symbol's own references aren't chased further),
capped by `retrieval.max_referenced_symbols` (default 5). `generate.py`'s
prompt labels these distinctly (`[Referenced definition: X | File: ...]`) with
an explicit rule that a referenced definition is supporting context, not
necessarily the direct answer.

**Bug found and fixed during implementation, unrelated to this phase's core
work**: today's earlier `config.get_repo_path()` genericity fix (making
`repo_path` resolve relative to the project root regardless of caller cwd) had
an unintended side effect once `ingest.py` was re-run under it for the first
time since that fix landed -- `walk_project()` was yielding the full absolute
filesystem path as each chunk's stored `filepath` (e.g.
`/home/aakash/projects/second-brain-rag/test_data/requests/tests/test_utils.py`
instead of `test_data/requests/tests/test_utils.py`), and `update_index.py`'s
incremental path would have stored the *same* absolute-path format too, but
computed slightly differently -- a latent bug that would have made
`delete_file_chunks()`'s exact-filepath matching silently stop finding a file's
existing chunks on its next incremental update, leaking orphaned/duplicate
chunks over time. Fixed by having both scripts store `filepath` relative to the
project root (`Path.relative_to(PROJECT_ROOT)`), while still using the
absolute, cwd-independent path for actual filesystem/git operations --
verified by re-ingesting and confirming chunks are keyed by the original clean
relative paths again.

**Verified**:
- Chunking regression check: compared the old ast-based chunker against the
  new tree-sitter chunker on 5 real files (`test_utils.py`, `utils.py`,
  `models.py`, `sessions.py`, `adapters.py`) -- identical chunk COUNTS in every
  case (37, 45, 6, 6, 4). The only content differences were decorated functions
  (`@pytest.mark.parametrize`, `@overload`): the new chunker correctly keeps a
  decorator attached to its function as one chunk, where the old one left the
  decorator floating in the leftover/imports chunk -- a genuine improvement,
  not a regression.
- Graceful degradation confirmed: with `universal-ctags` not yet installed,
  `build_symbol_index()` prints a warning and returns an empty index rather
  than failing ingestion; `_expand_referenced_symbols()` is a no-op in that
  case (empty `referenced_symbols` on every chunk).
- Full re-ingest (`python3 ingest.py`, 68 files) and end-to-end retrieval
  regression check across the previously-fixed cases (whole-file boosting,
  about-project boosting, normal ranked search) -- all still correct after the
  payload-schema change (new `symbol_name`/`referenced_symbols` fields added
  without breaking existing boosting logic).
- End-to-end through Gemini via the web UI (`/api/chat`): confirmed the new
  chunk/payload shape flows through correctly, `RetrievedChunk`'s new optional
  fields (`symbol_name`, `referenced_symbols`, `referenced_definition_of`)
  serialize correctly, and the retrieval side panel can display them.
- **Cross-file symbol resolution, verified end-to-end after the user installed
  `universal-ctags`**: re-ran `ingest.py`, which built a 1,276-symbol index
  across `test_data/requests`. Confirmed 522/577 chunks got at least one
  `referenced_symbols` entry. Picked a real case -- `api.py`'s `options()`
  chunk referencing `Response`/`Request`/`request` -- and confirmed
  `_expand_referenced_symbols()` resolved `Response` and `Request` to their
  actual class definitions in `models.py`. End-to-end: `search_raw("explain
  me file api.py")` returned all 9 of `api.py`'s own chunks (via the existing
  filename boosting) PLUS the 2 referenced `models.py` definitions, correctly
  labeled `[REF DEF of ...]`. Through Gemini, the resulting answer correctly
  described every function in `api.py` (`get`/`post`/`put`/`patch`/`delete`/
  `options`/`head`/`request`) and separately, correctly, mentioned that
  `Response` is imported from `.models` -- using the referenced-definition
  context as supporting information rather than treating it as the main
  subject, exactly as the CITATION RULE intends. Some noise in
  `referenced_symbols` was observed as expected (e.g. common names like
  `str`, `all`, `copy` occasionally matching an unrelated symbol elsewhere in
  the repo) -- acceptable per the design tradeoff agreed with the user, since
  resolution only runs for chunks actually retrieved for a real query.

**Explicitly deferred, per the plan agreed with the user**:
- `history_pipeline.py`'s `get_function_map()` (function-level diff chunking)
  remains Python-`ast`-only. History/diff tracking is not yet as generic as
  current-state chunking now is -- a known, temporary inconsistency, not
  addressed in this phase.
- Embedding model swap (e.g. to a code-tuned model like
  `jina-embeddings-v2-base-code`, confirmed to fit the 4GB GPU at ~161M
  params/~320MB fp16) intentionally left for a separate follow-up once chunk
  shapes are fully settled, so any retrieval-quality change can be
  attributed to one variable at a time, and `MIN_SCORE`/`MIN_SCORE_HISTORY`
  can be recalibrated freshly against whatever model is chosen.
- Multi-language support was validated structurally (empirical node-type
  testing on Python, JS, TS, Go, Java, C, C++ snippets) but only Python has
  been proven against a real repo -- `test_data/requests` is 100% Python, so
  there is no live evidence yet of retrieval quality on another language's
  actual codebase. Flagged explicitly, per the user's own choice not to add a
  second test repo for this phase.

## Production Migration to Qwen3-Embedding-0.6B, and History-Ingest Resume Support

**Decision**: after a real A/B comparison (20 stratified questions across all QA-benchmark
categories, plain hybrid search, apples-to-apples against the production MiniLM collection),
the user approved making `Qwen/Qwen3-Embedding-0.6B` the permanent production embedding
model. The comparison showed 3 direct fixes of previously-flagged, unresolved retrieval gaps
(install command ranking, `InvalidSchema` exception, current-maintainers section) plus
generally higher-confidence, better-separated top-1 scores, with no meaningful regressions
found in the sample.

**Environment gaps hit and fixed getting Qwen3 running at all** (none are model bugs --
this GPU/WSL2 environment was simply missing pieces newer models need):
- `universal-ctags` (already installed for symbol resolution) wasn't the issue here; Qwen3's
  rotary position embeddings use a Triton-JIT-compiled kernel that needs a C compiler at
  runtime. `gcc` was missing entirely (`sudo apt-get install build-essential`), and even with
  `gcc` present, the build failed a second time on a missing `Python.h` (`sudo apt-get
  install python3.14-dev`). Both are one-time, low-risk system installs, same category as
  the `ctags` install earlier.

**Migration changes**:
- `config.yaml`: `embedding.model_name` -> `Qwen/Qwen3-Embedding-0.6B`, `dimension` -> 1024,
  plus three new keys: `query_prompt_name` ("query" -- Qwen3 is an asymmetric model, queries
  need an instruction prefix, documents don't), `max_seq_length` (1024), `encode_batch_size`
  (8).
- `pipeline.py::get_model()` now applies `max_seq_length` from config after loading. New
  `get_query_prompt_name()` / `get_encode_batch_size()` helpers, generic (not Qwen3-specific)
  so a future model swap only needs a config change.
- `pipeline.py::embed_and_upsert_file()` and `history_pipeline.py::process_commit()` both
  pass `batch_size=get_encode_batch_size()` to `.encode()`.
- `search.py` and `search_history.py`'s query encoding now pass
  `prompt_name=get_query_prompt_name()` (`None` for models with no named prompts -- verified
  equivalent to omitting the argument, so this is safe for any model).
- **Found and fixed along the way**: `search.py` was instantiating its own separate
  `SentenceTransformer`, while `search_history.py` already reused `pipeline.get_model()`'s
  shared singleton. Harmless with MiniLM (~90MB) but would have silently loaded a second
  ~1.2GB copy of Qwen3 in the same process (`generate.py`/`chat.py` import both modules
  together) on a 4GB GPU. Fixed `search.py` to reuse the shared singleton too --
  verified `search.model is search_history.model` is `True` after the fix.

**Sequence-length incident during the parallel A/B ingest** (caught before it hit
production): the first attempt at building a parallel Qwen3 test collection stalled at
99% GPU / near-max VRAM with almost no progress. Root cause: one real chunk (a whole test
class, `TestRequests`) is ~81KB / ~20k+ tokens. Qwen3's 32k context window means it actually
attends over the full thing at quadratic cost with no flash-attention support on this GPU,
unlike MiniLM which silently truncates at 256 tokens. Measuring the real corpus distribution
(median 382 chars, p99 ~12k chars, only 17/577 chunks over 4000 chars) showed the fix needed
both a sequence cap AND a batch-size cap -- a 2048-token cap alone still stalled once a batch
happened to contain several long outliers together. `max_seq_length=1024` +
`encode_batch_size=8` resolved it cleanly.

**Relevance-gate recalibration**: `MIN_SCORE` was tuned for MiniLM's score distribution.
Measured Qwen3's dense-only cosine scores directly against the real corpus: clearly
off-topic queries ("how do I bake a chocolate cake", "explain quantum entanglement") scored
0.14-0.33; genuine on-topic queries (including two deliberately vague/generic ones) scored
0.39-0.82. Set `min_score_current`/`min_score_history` to 0.37 -- a small margin above the
worst off-topic case, comfortably below the weakest genuine on-topic case. Only the
current-state side has real evidence behind this number; the history-side threshold is the
same starting value, not yet independently validated against re-ingested history data.

**Rollout**: current-state (`requests_poc`, 577 chunks) re-ingested and verified in ~5
minutes. History (`requests_history`, 31,905 chunks -- ~55x larger) re-ingested via a direct
wipe-and-rebuild (user's explicit choice over a safer parallel-build-then-swap, given this
project has no uptime requirement), taking on the order of many hours on this GPU.

**History-ingest resume support** (`history_ingest.py`, added mid-run after the user asked
what happens if they stop it): the original full-backfill script had no checkpointing --
`create_history_collection()` wiped the collection exactly once at the very start, and a
re-run after any interruption would wipe and restart from commit #1, discarding all
progress. Added a checkpoint file (`.history_ingest_progress` in the repo directory,
separate from `update_index.py`'s unrelated `.last_indexed_commit`) that records the last
fully-processed commit hash, written only *after* that commit's chunks are confirmed
upserted. On start, if a checkpoint exists (and the collection still exists), the script
resumes from the next commit instead of wiping and restarting; if the checkpoint commit
can't be found in the current history (e.g. a rewritten repo), it falls back to a fresh
run. Point IDs are already deterministic (`commit_hash::filepath::chunk_type::index`), so
even re-processing the same commit twice on an unlucky interruption is a harmless overwrite,
not a duplicate -- resume correctness doesn't depend on the checkpoint write being
perfectly atomic with the upsert. Verified the checkpoint save/load/resume-index logic in
isolation (fresh state, round-trip, correct resume index, missing-checkpoint fallback,
clear-on-completion) without touching the live in-progress production run. Note: this only
protects *future* runs -- editing the script on disk does not retroactively add resume
capability to the process already running in memory.

## History Ingest: Newest-First Ordering

User correction, applied as a pure code edit while the live history re-ingest kept running
undisturbed (verified: syntax check + confirmed the running process's progress was
unaffected before and after). `history_ingest.py`'s full backfill previously used
`git log --reverse` (oldest-first) -- changed to git log's default order (newest-first).
Rationale: most users care about recent history far more than deep legacy commits, and
processing newest-first means an interrupted run -- now safe to do at all, thanks to the
resume support added just before this -- leaves the most relevant/recent history recorded
first, not the oldest. Confirmed the resume checkpoint logic (`all_hashes.index(checkpoint)
+ 1`) is order-agnostic, so this required no other changes. Only affects future invocations
of the script, same caveat as resume support itself -- does not reorder or affect the
currently in-progress run.

## History Made Fully Optional, With Live Graceful Degradation

**Motivated directly by a real incident**: the history re-ingest (started under the
pre-resume-support code) got killed by the OS due to system memory pressure at 11,903/31,905
chunks. Restarting triggered `create_history_collection()`'s unconditional wipe (correct
given no checkpoint existed for that run), and the user interrupted the restart moments
later -- leaving `requests_history` at only 62/6,493 commits. Separately, generation had
already been observed to hard-crash entirely (`Vector dimension error: expected dim: 384,
got 1024`) any time `requests_history` was mid-migration or otherwise not in a queryable
state, since `generate.py` unconditionally called `search_history_raw()` with no fallback.

**Decision**: history should never be an all-or-nothing prerequisite. A user should be able
to (a) explicitly choose not to bother with history at all, and (b) use the tool normally on
current-state search at every point during a history backfill, including before it starts,
while it's partially done, and if it's broken or mid-migration -- never blocked by history's
state.

**Implementation**:
- New `history.enabled` key in `config.yaml` (default `true`, so existing behavior for
  anyone already relying on it is unchanged).
- `search_history.py::_history_available()` -- a live (not cached) check: disabled in
  config, or the collection doesn't exist, both degrade to unavailable. Checked at the top
  of `search_history_raw()`. Live rather than cached specifically so that starting a backfill
  in the background while the web UI is already running makes history search start working
  automatically, no restart required.
- `search_history_raw()`'s entire body wrapped in try/except: any failure querying history
  (dimension mismatch, connection issue, anything unanticipated) degrades to `[]` with a
  logged warning, rather than propagating and crashing generation. `generate.py`/`chat.py`
  already treated an empty history result as "no historical evidence for this question," not
  an error, so this required no changes upstream -- the crash was purely from the exception
  propagating out of `search_history.py`, never from ambiguity about what to do with zero
  history results.
- `history_ingest.py`'s `__main__` and `history_update.py::process_latest_commit()` both
  check the same flag and exit early with a plain explanation if history is disabled, instead
  of doing (or attempting) unnecessary work.

**Verified**: with the real, current 62-point/18-commit partial history collection (the
actual state left by the incident above), `generate.py` runs end-to-end with no crash,
correctly sources a detailed answer from current-state chunks alone, and
`search_history_raw()` returns cleanly (empty, not an error) for a query with no relevant
history yet.

## Cross-Encoder Reranking Added

**Motivation**: flagged as the one real conceptual gap in the RAG Concepts Audit -- the
existing boosting mechanisms are rule-based metadata lookups, and RRF fusion combines two
independent ranking signals (dense + sparse), but neither is a learned reranker that scores
a query and a candidate jointly. A cross-encoder is slower per-pair but far more precise
than comparing independently-computed embedding vectors, which is why it's only ever run
against a modest post-hybrid-search candidate pool, never the whole collection.

**Implementation**:
- New shared `pipeline.py::get_reranker()` (lazy singleton, mirrors `get_model()`) loading
  `cross-encoder/ms-marco-MiniLM-L-6-v2` -- small enough to share the GPU with the Qwen3
  embedding model, ~80MB. Returns `None` if `reranking.enabled` is `false` in `config.yaml`,
  so downstream code has one cheap check rather than needing its own flag logic.
- New `pipeline.py::rerank(query, results)`: scores every `(query, chunk_text)` pair
  jointly, re-sorts by that score, and -- wrapped in try/except -- falls back to the
  original RRF order on any failure rather than raising. Same resilience pattern as history
  search's graceful degradation.
- `search.py`/`search_history.py`: the hybrid fusion query now fetches a wider pool
  (`reranking.candidate_pool`, default 20, up from the old `limit=top_k`) so there's
  something meaningful for the reranker to reorder, then calls `rerank()` on that pool
  before the existing boosted-results merge step. Boosted (exact filename/date/identifier/
  about-project) results still bypass ranking entirely, unchanged -- reranking only reorders
  the hybrid "filler" pool, never the exact-match lookups.

**Verified directly**: ran `search.py "how does authentication work"` before/after --
post-rerank, the two most directly on-topic docs (`docs/user/authentication.rst`,
`docs/user/advanced.rst`) sorted to the top with clearly separated cross-encoder scores
(2.40, 2.19), while an off-topic changelog mention of "Authentication improvements" scored
negative (-1.36) and dropped down. Ran `search_history.py "dependency version bump"` --
correctly surfaced dependabot config/version-bump commits. Confirmed `reranking.enabled:
false` falls back cleanly to plain hybrid order with no error. Ran `generate.py "how does
authentication work"` end-to-end -- correctly grounded, correctly cited answer through the
full reranked pipeline.

## Retrieval Evaluation: Real Recall@K / MRR

**Motivation**: the RAG Concepts Audit flagged retrieval quality as tested only by eyeballing
results, never by a computed metric. The plan to bootstrap a gold set from the earlier
100-question QA benchmark turned out not to be viable -- that benchmark was never saved to
disk (only its existence and outcome are recorded in this file), so a new, smaller, real
gold set was built from scratch instead: `backend/eval_retrieval.py`, 22 hand-written
questions, each paired with the one source file that should genuinely answer it. Every
mapping was verified directly against the real source (`grep`-confirmed class/function
names) before being used as ground truth, not assumed from memory.

**Scope decision**: history was deliberately excluded. `requests_history` currently holds
62/6,493 commits -- building a labeled eval set against that small, arbitrary slice would
produce numbers that look precise but aren't representative of anything real; worth doing
once the backfill is further along.

**Metric decision**: correctness is measured at the file level (does the expected file
appear in the top-K results), not exact-chunk level -- more robust to chunking logic
changing over time, and matches how this project's own filename-boosting already treats
"the right file" as the meaningful unit. Questions were deliberately phrased without naming
the target file directly, since naming it would just trigger exact filename-boosting and
always "pass" -- testing nothing about actual ranking/reranking quality.

**A real bug caught while building this**: the first version of the eval script counted a
hit anywhere in `search_raw()`'s returned list, but that list can be longer than `top_k` --
boosted (exact metadata match) results are never truncated, and cross-file symbol-reference
definitions get appended as bonus context on top. Two early "hits" turned out to be at
positions 8 and 10, inside that unbounded bonus tail, not the real ranked top-6. Fixed by
slicing to `results[:top_k]` before scoring.

**Result** (top_k=6, current-state collection only): **Recall@6 = 90.91% (20/22), MRR =
0.469**. Two genuine misses, both real gaps rather than eval artifacts: "how does the
library decide which transport adapter handles a request" (expected `sessions.py`, whose
`get_adapter()` does the actual routing decision; retrieved `adapters.py`-adjacent results
instead) and "how can I stream a large response body in chunks" (expected `models.py`,
where `iter_content()` lives; retrieved doc/test files discussing streaming instead of the
implementation itself).

## HNSW/ANN: Measured, Not Tuned

**Motivation**: the audit flagged that Qdrant's HNSW index (used automatically for every
collection) has never been configured, tuned, or measured -- everything has been running on
whatever defaults Qdrant ships with, unverified.

**What was actually measurable without a collection rebuild**: `exact` and `hnsw_ef` are
Qdrant *search-time* parameters, not index-build-time ones -- they can be varied per-query
against the existing collection, no re-ingest required. `m` and `ef_construct` (the graph's
actual structural parameters) are build-time only and would require a full rebuild to test.

**Implementation**: `search.py::search_raw()` gained an optional `search_params` argument
(default `None` -- no behavior change for any real caller), threaded into both Qdrant
queries. One real subtlety caught while wiring this in: Qdrant's fusion queries (used for
hybrid dense+sparse search) do NOT apply a top-level `search_params` to their `Prefetch`
stages -- each `Prefetch` needs its own `params` field, or `exact`/`hnsw_ef` silently has no
effect on the actual dense-vector search happening inside it. `eval_hnsw.py` reruns the same
22-question eval set from `eval_retrieval.py` under different search params, so every row is
a like-for-like comparison.

**Result**: Recall@6 and MRR were **identical -- 90.91% / 0.469 -- across the default
(unconfigured) settings, `exact=True` (brute-force, guaranteed-correct ground truth), and
`hnsw_ef` at 64, 128, and 256.** At this collection's current size (~577 vectors in
`requests_poc`), the approximate index is already returning the same results exact search
would -- there is no accuracy being traded away right now. This tracks with how HNSW
approximation error is generally known to behave: it shows up at much larger scale (tens of
thousands of vectors+), not at a few hundred.

**Decision**: don't add `hnsw_config` exposure to `config.yaml` or rebuild the collection
with different `m`/`ef_construct` -- there's no measured problem for those settings to fix,
and adding an unused tuning knob with zero evidence behind it isn't worth the surface area.
This is a measured "not needed yet," not an unexamined default -- worth re-running this same
`eval_hnsw.py` script once the corpus is significantly larger (e.g. after the full history
backfill), where an approximation gap becomes plausible.

## Genericity Validated End-to-End on a Second Real Repo (gorilla/mux)

**Motivation**: Phase 2 (tree-sitter chunking + ctags symbol resolution) was validated
earlier only with standalone scratchpad scripts proving the chunker and symbol index work
on real Go source -- never actually ingested into Qdrant or exercised through search,
boosting, reranking, or generation. The user caught this gap directly ("but i thought we had
some other repo being tested and not the request repo?") and, once retrieval evaluation and
HNSW/ANN measurement were done, asked for this closed out properly before anything else
(history backfill, RAG evaluation, hallucination detection, and reranker-assumption
validation were explicitly deferred to be tackled after this).

**Scope decision** (user-confirmed via question): a simple, temporary `config.yaml` swap to
`gorilla/mux`, not persistent multi-project support -- matches the project's actual
long-term goal (one deployment per private codebase, not juggling several at once), and
avoids building a feature nothing currently needs. `history.enabled` was set to `false` for
this test, since history/eval work was explicitly deferred to later.

**What was run**: `ingest.py` against `test_data/mux` with zero code changes -- only
`project.name`/`repo_path` edited. Result: 336 unique symbols indexed, 18 files, 307 chunks
in a fresh `mux_poc` collection.

**Verified end-to-end, not just structurally**:
- `search.py` on a natural-language Go question ("how does the router match an incoming
  request to a route") correctly ranked README.md, doc.go, and mux.go at the top, reranker
  scores clearly separated (7.44, 5.98, 4.87).
- An identifier-style query ("Vars") correctly top-ranked the real `Vars()` function
  definition in `mux.go` over test files that merely reference it.
- About-project boosting correctly surfaced `README.md` for "what is this project" -- same
  generic regex-based mechanism as `requests`, no repo-specific logic anywhere.
- Filename boosting correctly returned all 25 chunks of `route.go` for "explain route.go".
- Cross-file symbol resolution correctly pulled in `Router` and `Handler` as referenced
  definitions for chunks that call them -- confirmed via `referenced_definition_of` in the
  actual results, not assumed.
- `generate.py` produced a fully correct, grounded answer about `Vars()` (what it does, how
  to call it, a matching usage example), sourced entirely from the real Go files, through the
  exact same prompting/citation rules used for `requests`.

**After verification**: `config.yaml` switched back to `requests` (user-confirmed default,
since it has the most data and is what's been tested all session). Confirmed lossless:
`requests_poc` still reports 577 points, untouched -- switching `project.name` only changes
which collection is *active*, per already-documented behavior (`docs/USAGE.md`, "Note on
config changes"). `mux_poc` was left in place, not deleted, as further evidence/for future
use.

**Conclusion**: this is the first time the *entire* pipeline -- not just chunking and symbol
indexing in isolation -- has been proven to work on a repo it wasn't built or tuned against,
in a different language, with zero code changes. `docs/LIMITATIONS_AND_ROADMAP.md` updated
accordingly (it had drifted significantly out of date -- still claimed "CLI-only" and
"hardcoded repo path," both long since resolved).

## Backend Reorganized: `backend/dev/` Split Out

**Motivation**: user request, directly following the genericity work above -- keep files
built for testing/evaluating this project itself separate from the files that make up the
actual running system, so `backend/`'s top level reads as "the product," not a mix of
product code and one-off tooling.

**Moved into new `backend/dev/`**: `eval_retrieval.py`, `eval_rag.py`, `eval_hnsw.py`
(this session's evaluation scripts), `count_points.py`, `check_file.py` (debugging
utilities), `test_embedding.py` (an early GPU/library sanity check, predates the Qwen3
migration -- still hardcoded to `all-MiniLM-L6-v2` on purpose, since its job is checking the
GPU/sentence-transformers stack works at all, not the currently-configured model).

**Left untouched, top-level**: every file that's part of an actual code path used by
`main.py`/`chat.py`/`generate.py`/ingestion -- `config.py`, `pipeline.py`,
`code_chunking.py`, `symbol_index.py`, `ingest.py`, `update_index.py`,
`history_pipeline.py`, `history_ingest.py`, `history_update.py`, `search.py`,
`search_history.py`, `generate.py`, `chat.py`, `llm_backend.py`, `main.py`.

**Implementation**: each moved script does `from pipeline import ...` / `from search import
...` etc, which broke once it moved into a subfolder (Python doesn't add a script's parent's
parent to `sys.path` automatically). Fixed with a two-line shim at the top of each moved
file (`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`) rather than
restructuring `backend/` into a proper installable package -- that would have required
changing how every other script in the project is invoked (documented throughout
USAGE.md/SETUP.md as plain `python3 backend/x.py`), a much bigger blast radius than this
task called for.

**Verified**: `dev/count_points.py` and `dev/check_file.py` re-run from their new location
with identical output; `dev/eval_retrieval.py` re-run in full, identical result (Recall@6 =
90.91%, MRR = 0.469) confirming the move didn't silently change behavior; `dev/eval_hnsw.py`
and `dev/eval_rag.py` confirmed to import cleanly. `docs/USAGE.md`, `docs/SETUP.md`, and
`CLAUDE.md` updated to the new paths -- `CLAUDE.md`'s "Key files" section was also missing
`code_chunking.py`/`symbol_index.py` entirely and still described the embedding model as
`all-MiniLM-L6-v2`/384-dim from before the Qwen3 migration, both corrected while already
touching that section.

## Full History Backfill Completed — Real Bug Found and Fixed Along the Way

`requests_history` went from 62/6,493 commits (under 1%) to a full backfill of all 6,494
commits (the repo gained one commit between the earlier count and this run) -- 34,659 points
total. This closed the single biggest known gap in the project (roadmap item #1).

**A real crash, not a memory issue this time**: the backfill died partway through (at commit
1855/6494) with `qdrant_client.http.exceptions.UnexpectedResponse: 400 Bad Request` --
`"JSON payload (44692624 bytes) is larger than allowed (limit: 33554432 bytes)"`. Root cause,
confirmed by inspecting the exact commit: `724ae1274e87b9636550fbab737c07b2ba359d5`, "new logo
:D" (2016), which replaced `ext/requests-logo.ai` -- an Adobe Illustrator binary file that git
diffed as text. That diff was 2.35MB with individual lines up to 27KB long.

`pipeline.py::chunk_text()` (the shared line-window chunker used for oversized diffs, and the
fallback for any file type without a dedicated chunker) only bounded chunks by **line count**
(`chunk_size` lines per chunk), never by character length. A handful of pathologically long
lines can make a "40-line chunk" many megabytes -- oversized both for embedding and for a
single Qdrant upsert request. This isn't specific to `requests` or to `.ai` files: any real
codebase can have a vendored binary diffed as text, a minified bundle, or a generated data
dump, all of which produce the same failure mode.

**Fix, in two parts**:
1. `chunk_text()` now also caps every chunk at `chunking.max_chunk_chars` (default 4000),
   slicing a chunk into raw character-length pieces if the line-window chunk comes out
   oversized. Verified directly against the exact offending diff: previously one unbounded
   chunk of ~2.3MB, now 1,774 chunks each capped at exactly 4,000 characters.
2. `history_pipeline.py::process_commit()` now batches its upsert in groups of 200 points
   instead of one `client.upsert()` call for the whole commit -- defense in depth against a
   commit that touches many large files at once (a mass reformat, a dependency vendor bump),
   which the character-length cap alone wouldn't fully protect against.

**Verified the fix against the exact commit that crashed** before trusting it with the full
run: called `process_commit()` directly on commit `724ae127...` in isolation -- it completed
and upserted all 2,245 points with no error (this one commit alone took ~25-45 minutes to
embed on the 4GB GPU, since 2,245 chunks for one commit is a massive outlier compared to a
typical commit's handful of chunks). Only after that succeeded was the full backfill resumed.

**Environment note, not a code bug**: separately, a standalone verification script got killed
by a transient low-memory condition during model loading, before any embedding started --
this happened once and did not recur when the same step was retried moments later with
healthy memory. Consistent with this project's earlier-documented pattern of occasional
WSL2/host memory pressure spikes unrelated to the code being run.

**Result**: `requests_history` now holds all 6,494 commits' message, diff, and (for Python
files) function-level change chunks -- 34,659 points, collection status `green`. Point IDs
are deterministic (`commit_hash::filepath::chunk_type::index`), so the earlier partial data
(62 commits) and the one commit re-processed twice across the crash/resume were harmless
overwrites, not duplicates.

## eval_rag.py Completed a Full Run — and Found a Bug in Itself

`eval_rag.py` (automated LLM-judge faithfulness/relevancy scoring, 11 questions from
`eval_retrieval.py`'s gold set) finally completed end-to-end, after two earlier attempts
failed on a rate limit and a low-memory kill respectively (see earlier entries). With memory
healthy, the run itself was fast and uneventful.

**First full result**: Faithfulness 10/11 (91%), Relevancy 11/11 (100%). The one failure --
"how do I send a GET request" -- was flagged as citing file paths ("the answer references
file paths... that do not appear anywhere in the provided source material").

**That "hallucination" turned out to be a bug in the eval script, not the RAG system.**
`generate.py::build_prompt()` labels every chunk shown to the LLM with `[File: <path>]` (and
`[date | filepath]` for history chunks) -- the LLM legitimately cites these labels, and
`CITATION RULE` in the prompt explicitly tells it to. But `eval_rag.py`'s judge-context
reconstruction only joined the raw `c["text"]` fields, silently dropping those labels. So the
judge was shown LESS context than the LLM actually had, and flagged a legitimately-grounded
citation as an invented fact -- a false positive caused entirely by the eval script having its
own, divergent, second implementation of context formatting.

**Fix**: extracted the labeling logic out of `build_prompt()` into a new
`generate.py::build_context_text()`, called by both `build_prompt()` (for the real generation
prompt) and `eval_rag.py` (for the judge's "source material" input) -- one implementation
instead of two that can silently drift apart.

**Corrected result after the fix**: Faithfulness 11/11 (100%), Relevancy 10/11 (91%). The
previously-failing question now passes. The one remaining failure is a genuine, different
finding: "what internal helper functions does the library use that aren't public API" got an
answer describing modules/type aliases generically without naming actual helper functions --
an incomplete answer to a specific request, not a hallucination (faithful=True on that
question; only relevant=False).

**Takeaway**: this is exactly the kind of thing `eval_rag.py` exists to catch, but it also
demonstrates that an eval script's own logic needs the same "verify with direct evidence"
scrutiny as production code -- a plausible-looking automated hallucination flag was actually
the harness disagreeing with itself about what "the context" was.

## Reranker's Own Assumptions Validated — Candidate Pool Fine, Token Limit Is a Real Problem

Two settings picked as reasonable-sounding defaults when reranking was added
(`reranking.candidate_pool: 20`, and the cross-encoder's max token length, never actually
checked) were measured against real data via a new `dev/eval_reranker.py`.

**Candidate pool size**: swept 5/10/15/20/30/50 against the same 22-question Recall@6/MRR
eval set `eval_retrieval.py` and `eval_hnsw.py` already use.

| pool | Recall@6 | MRR |
|---|---|---|
| 5  | 86.36% | 0.508 |
| 10 | 90.91% | 0.486 |
| 15 | 90.91% | 0.471 |
| 20 (config default) | 90.91% | 0.469 |
| 30 | 86.36% | 0.460 |
| 50 | 86.36% | 0.458 |

Recall@6 peaks on a plateau (10-20) and **drops back down** at 30/50 -- a bigger pool is not
strictly better: it hands the reranker a noisier candidate set, giving its own imperfect
scoring more chances to rank a distractor above the true answer. The current default (20)
sits inside the good plateau; pool=10 gets identical recall with a better MRR and less GPU
work per query. Left the default as-is rather than changing it opportunistically -- see
Verified section below for why.

**Cross-encoder token limit**: measured directly (`CrossEncoder.max_length`) at 512 tokens for
`cross-encoder/ms-marco-MiniLM-L-6-v2`, confirming what was previously just assumed. Then
measured real (query, chunk) pairs from the actual production candidate pool (329 pairs, 22
questions at candidate_pool=20): mean length **1,028 tokens -- already double the limit**.
**104/329 pairs (31.6%) exceed 512 tokens and are silently truncated** by the cross-encoder
(BERT-style pair tokenization: `[CLS] query [SEP] chunk [SEP]`, hard-capped at 512 total). The
worst cases are chunks from `test_requests.py` at **~24,600 tokens**, truncated down to 512 --
over 97% of the chunk's content discarded before the reranker ever scores it.

**Root cause, not a surprise in isolation**: this traces directly to the Phase 2 chunking
design decision ("only top-level siblings of the root become their own chunk... a whole class
including its methods stays one chunk") -- a large test class with many test methods
legitimately produces one very large chunk under that design. That was a deliberate tradeoff
at the time (matching AST-chunking granularity, not a bug), but this measurement is the first
real evidence of its cost: such chunks are effectively invisible to the reranker beyond
whatever fits in the first ~500 combined tokens, meaning its relevance score for them is based
on a small, arbitrary fragment, not the chunk's actual content.

**Left both as-is for now, findings reported rather than acted on unilaterally**: reducing
`candidate_pool` to 10 is a low-risk, evidence-backed win, but splitting oversized code chunks
(the real fix for the truncation problem) is a bigger structural change to `code_chunking.py`
that trades off against the existing "whole class in one chunk" design intentionally chosen
earlier -- a decision worth making deliberately, not as a side effect of a validation pass.

## Deployment Portability: Real Gaps Found and Fixed

Prompted by a direct question: if this is pushed to a real GitHub repo and someone else clones
it and edits `config.yaml`, does it actually work? Checked rather than assumed, and found three
real problems:

1. **`.gitignore` was broken.** Its third line read `__pycache__/ *.pyc.env` -- a single
   malformed pattern (gitignore has no space-separated multi-pattern syntax), so `__pycache__/`
   was never actually ignored, and `.env` (holding `GEMINI_API_KEY`) was not ignored at ALL.
   Verified via `git log --all -- '**/.env'` that nothing had leaked yet (this repo has zero
   commits so far) -- but the very first `git add -A` would have committed a real secret. Fixed
   the file to one pattern per line, and added `test_data/` (a fresh clone shouldn't ship
   whatever target repo the previous operator happened to be testing against -- each deployer
   clones their own).
2. **`llm.ollama_model`/`llm.gemini_model` in `config.yaml` were dead config.** They looked
   like the way to choose a model, but `llm_backend.py` had its own separate hardcoded
   `OLLAMA_MODEL`/`GEMINI_MODEL` constants that ignored them entirely. Fixed by having
   `generate_text()` read both from `get_config()["llm"]` at call time, with the old hardcoded
   values kept only as defaults if the keys are missing.
3. **`backend/requirements.txt` had no pinned versions** -- a fresh `pip install` could pull
   different (newer) package versions than what was actually tested, a real portability risk
   for a multi-day-old project already using several fast-moving libraries (`sentence-transformers`,
   `torch`, `qdrant-client`). Pinned every line to the exact version actually installed and
   verified working in this environment.

**Real, still-standing gap, not fixed (a bigger feature, not a bug)**: swapping the embedding
model isn't just a config edit -- it requires a full re-ingest (different vector dimension) and
manual threshold recalibration, which had no tooling at all until the next entry below.

## Config Preflight Checker (`backend/check_config.py`)

**Motivation**: directly follows from the portability question above. A new deployer (or the
same deployer trying a different model) currently only finds out their config is broken by
watching `ingest.py` fail partway through, or worse, running with silently-wrong relevance
thresholds. Built a two-stage checker instead of leaving this as tribal knowledge in docs.

**Stage 1 (`python3 backend/check_config.py`, no ingested data needed)**: actually loads the
configured embedding model (and checks its real output dimension matches `config.yaml`'s
`embedding.dimension` -- a mismatch here would silently corrupt a fresh collection), the sparse
model, and the reranker; confirms Qdrant is reachable; confirms the configured LLM backend
responds to a real minimal call (not just "is an API key present" -- an invalid key or
unreachable Ollama service is caught too); confirms `project.repo_path` exists and is a git
repo; checks (as a warning, not a failure, matching the existing graceful-degradation design)
whether Ctags is installed and whether a GPU is available.

**Stage 2 (`--calibrate`, run after `ingest.py`)**: automates the exact method used to
hand-calibrate Qwen3's thresholds earlier (see "Production Migration to Qwen3-Embedding-0.6B"
above) -- score a fixed set of deliberately off-topic probe questions, and a set of on-topic
probe questions generated by the configured LLM from real chunks sampled out of the freshly
ingested collection (so this works for any codebase, not just ones with hand-written test
questions), then suggest a `min_score_current` value sitting between the worst on-topic score
and the best off-topic score. If the two ranges overlap, it says so explicitly and refuses to
suggest a number, rather than printing a false-confidence threshold.

**Verified end-to-end against the real, live `requests` config and its already-ingested
935-point collection**: Stage 1 passed all 9 checks cleanly (Qwen3 loading and producing
correct 1024-dim vectors, reranker scoring, Gemini responding, ctags found, GPU detected).
Stage 2 generated 6 real on-topic probes via Gemini from sampled chunks (scores 0.666-0.857)
against the same 6 fixed off-topic probes used for the original Qwen3 calibration (scores
0.174-0.303) -- a clean gap, suggested `min_score_current: 0.41`, close to (slightly more
conservative than) the hand-calibrated production value of `0.37`.

**Honest limitation, stated directly in the tool's own output, not just here**: the on-topic
probes are LLM-generated from a small sample (default 6 chunks), not a large hand-labeled set --
this is a fast, automatic starting point for recalibration, not a replacement for the kind of
larger, deliberate evaluation `eval_retrieval.py`/`eval_rag.py` do.

## Interactive Config Generator (`backend/setup_config.py`)

**Motivation**: `check_config.py` validates a config that already exists, but a brand-new
deployer still had to hand-write a full `config.yaml` from scratch, including a value
(`embedding.dimension`) that `check_config.py` itself exists to catch mismatches on. Closed the
gap on the other side: a script that asks for every setting and writes the file.

**Design**: common settings (repo path, project name, history on/off, LLM backend + model + API
key, embedding model) are always asked, with sensible defaults. Less commonly changed settings
(chunk sizes, thresholds, reranking pool size, ctags binary) are shown with their default value
and can be batch-accepted or customized one at a time -- so every value that ends up in the file
is visible either way, not just the ones the user was directly prompted for.

**Real validation while asking, not just after**: repo path existence + whether it's a real git
repo (checked live); Qdrant reachability at the given host/port (checked live); Ollama
reachability if chosen (checked live); embedding model dimension -- if it's not the known
default (Qwen3), the model is actually loaded and encoded once to detect its real output
dimension automatically, plus a best-effort check of whether it exposes a named "query" prompt
(asymmetric models) via `model.prompts`. This directly prevents the exact class of error
`check_config.py` was built to catch after the fact.

**Safety**: backs up any existing `config.yaml` to `config.yaml.bak` before overwriting (added
`*.bak` to `.gitignore`), and requires explicit confirmation before overwriting at all.

**Verified end-to-end**: ran it with piped answers accepting every default against the real
project (repo path `test_data/requests`, Qwen3 default so no reload needed, existing
`GEMINI_API_KEY` correctly detected and kept rather than re-prompted). The written
`config.yaml` parses as valid YAML with all 9 expected top-level sections and is functionally
identical to the hand-maintained original. Backup file correctly created and cleaned up after
the test.

## Ingestion Now Respects the Target Repo's .gitignore, Plus Custom Exclude Patterns

**Motivation**: user request, directly following the deployment-portability work above --
`ingest.py`'s only exclusion mechanism was a small hardcoded `IGNORE_DIRS` set
(`.git`, `venv`, `__pycache__`, `qdrant_storage`, `node_modules`). Real target repos have their
own `.gitignore` with their own conventions (`.env`, `build/`, `dist/`, `vendor/`, `.venv/`,
language-specific ignore patterns), none of which were respected -- a real risk for the
project's actual long-term goal (pointing this at a private company codebase) where a
gitignored file is very plausibly gitignored *because* it holds a secret or generated content
nobody wants embedded (and possibly later cited back by the LLM).

**New module `backend/file_exclusions.py`**: two exclusion sources, both applied:
1. Whatever the target repo's own `.gitignore` (plus nested `.gitignore` files,
   `.git/info/exclude`, global excludes) already excludes -- evaluated via `git check-ignore
   --stdin -z`, not a hand-rolled gitignore-pattern reimplementation (nested files and negation
   patterns are genuinely easy to get subtly wrong by hand). Batched into one subprocess call
   per run, not one per file. Degrades to "nothing extra excluded" if `repo_path` isn't a real
   git repo or git fails, rather than crashing ingestion -- same graceful-degradation pattern
   already used for Ctags.
2. New `config.yaml` key `ingestion.exclude_patterns` (default `[]`) -- simple glob patterns
   (`fnmatch`) the user lists explicitly, checked against both the full relative path and the
   bare filename. Applies regardless of the target repo's own git configuration, including
   files that ARE tracked/committed but shouldn't be searched (a vendored SDK, generated code
   checked into the repo).

**Wired into both `ingest.py` (full walk) and `update_index.py` (incremental)**, not just one --
`update_index.py` processes files that were part of a real commit, so real `.gitignore` rarely
matters there (git won't normally let an ignored file get committed), but `exclude_patterns`
still needs to apply, since it's independent of git's own ignore state. Handled per-status
carefully for renames specifically: deleting a path's old chunks is always safe regardless of
exclusion (a harmless no-op if never indexed), but only the steps that would actually *add* an
excluded file's content are skipped -- a file renamed INTO an excluded path still gets its old
chunks cleaned up; a file renamed OUT of one still gets indexed under its new name.

**`setup_config.py` and `check_config.py` both updated**: the interactive generator now asks
for extra exclude patterns (comma-separated) and writes them into the file; the checker reports
how many of a sample of real files in `repo_path` are currently gitignored, and how many custom
patterns are configured, so a user sees this working before committing to a full ingest.

**Verified directly against the real `requests` repo**, not just unit-tested in isolation:
created `toy.py` (matches `requests`' own real `.gitignore` entry) and `env/dir1/leaked.py`
(inside a gitignored directory, testing nested-directory exclusion specifically) -- both
correctly excluded, "Skipping 2 gitignored/excluded file(s)" printed. Separately verified the
custom-pattern path in isolation from the extension allowlist (which would have excluded a
`.pem` file anyway regardless of this feature): created `local_secrets.py` (a normally-allowed
`.py` extension) with `exclude_patterns: ["local_secrets.py"]` configured -- correctly excluded,
while a real legitimate file (`sessions.py`) remained included, confirming no over-exclusion.
All test files and the temporary config change were cleaned up after verification.

## Backend Reorganized Into Subpackages by Role

**Motivation**: user request to arrange `backend/`'s (by now 20+) flat files into folders. The
project had explicitly deferred this once before (see the earlier `backend/dev/` split entry:
"restructuring `backend/` into a proper installable package... would have required changing how
every other script in the project is invoked... a much bigger blast radius than that task called
for") -- this time it's the actual ask, not a side effect of something smaller, so it was worth
doing properly rather than deferring again.

**Structure agreed with the user (of two options presented)**: group by role --
`backend/core/` (pipeline, code_chunking, symbol_index, file_exclusions -- the shared toolbox),
`backend/ingestion/` (ingest, update_index), `backend/history/` (history_pipeline,
history_ingest, history_update), `backend/retrieval/` (search, search_history),
`backend/generation/` (generate, chat, llm_backend). `main.py`, `config.py`/`config.yaml`,
`setup_config.py`, `check_config.py` stay at `backend/`'s top level as entry points/shared
config. `backend/dev/` unchanged.

**Preserved the existing invocation style deliberately**: every moved file gets the same
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` shim `backend/dev/` scripts
already used, so `backend/` itself is always reachable regardless of which subpackage a script
lives in. This means every script still runs as plain `python3 backend/<subpath>/<file>.py` --
no `python -m package.module` invocation change, matching the same reasoning that motivated
deferring this earlier (avoid changing the documented invocation pattern). Internal imports
became package-qualified (`from core.pipeline import ...`, `from retrieval.search import ...`)
rather than stacking more flat `sys.path` tricks, for clarity now that cross-package imports are
common (e.g. `retrieval/search_history.py` importing `history/history_pipeline.py`).

**Real bug class caught and fixed proactively, not after the fact**: two files
(`ingestion/ingest.py`, `ingestion/update_index.py`) computed `PROJECT_ROOT` via
`Path(__file__).parent.parent` for storing clean relative filepaths -- correct when they lived
directly in `backend/` (two levels above the project root), but wrong once moved one level
deeper into `ingestion/` (would have silently pointed at `backend/` instead of the project root,
reintroducing the exact "chunks keyed by the wrong path format" bug class the Phase 2 work
already found and fixed once before for a similar reason). Caught by exhaustively grepping every
`Path(__file__)` occurrence across `backend/` before doing any file moves, not discovered after
the fact -- both fixed to `.parent.parent.parent` (now resolved, for robustness) before ever
running the moved code.

**Also updated**: the already-installed local git hook
(`test_data/requests/.git/hooks/post-commit`) now calls `backend/ingestion/update_index.py` and
`backend/history/history_update.py`; `README.md`, `docs/USAGE.md`, `docs/SETUP.md`, and
`CLAUDE.md`'s "Key files" section (rewritten with subpackage headers) all updated to the new
paths. `docs/DECISIONS.md`'s own earlier entries were deliberately left describing the old flat
paths as they were at the time -- this is a log of what was actually true when each decision was
made, not a document that gets retroactively rewritten.

**Verified end-to-end, not just that imports resolve**:
- Every one of the 16 moved/entry-point modules (`config`, all four `core/` modules, both
  `ingestion/` modules, all three `history/` modules, both `retrieval/` modules, all three
  `generation/` modules, `main`) imported cleanly in one pass with zero failures.
- `ingest.py`'s `walk_project()` re-run directly: 68 files found (matches the pre-move
  baseline exactly), `PROJECT_ROOT` correctly resolved to the real project root, sample stored
  filepath still the same clean relative format (`test_data/requests/setup.py`).
- `backend/dev/count_points.py` re-run: 935 points, unchanged.
- `check_config.py` re-run in full: all 10 stage-1 checks pass (embedding/sparse/reranker
  models load, Qdrant and Gemini reachable, exclusions check, ctags, GPU) -- every one of these
  now flows through at least one moved module.
- A real end-to-end chat turn (`generation.chat.chat_turn`, the exact function `main.py` calls)
  produced a correct, grounded answer with 11 current-code + 6 history chunks -- proving the
  full retrieval -> generation chain works through the new package structure, not just that
  individual modules import.
- The already-running live web server (`uvicorn main:app --reload`) survived the entire
  reorganization automatically -- `--reload`'s file watcher picked up the moves and deletions
  as they happened and kept reloading successfully; confirmed with a live `/api/chat` request
  after all moves completed, correct grounded answer returned.

## Project Renamed: Second Brain RAG -> Repository Atlas

**Motivation**: user question about why the project was named "Second Brain RAG" surfaced that
the name described the general *category* (an external-memory tool, a common knowledge-
management metaphor, plus the RAG technique) rather than what actually differentiates this
project -- current-state *and* full-history retrieval, both grounded and citable. Discussed
alternatives (Codelore, Chronicode, RepoMemory, CodeAtlas, Groundwork); user chose **Repository
Atlas**.

**Updated everywhere the old name appeared as branding**: `README.md` title, `CLAUDE.md` title,
`backend/main.py`'s FastAPI `title=`, `backend/setup_config.py`'s startup banner, and
`frontend/index.html`'s `<title>`/`<h1>`. The project's directory path
(`/home/aakash/projects/second-brain-rag`) was deliberately left unchanged -- renaming it would
mean moving a live, running project (active venv, running uvicorn process, Qdrant storage paths,
the installed git hook's absolute `cd` path) for a purely cosmetic change, a much larger and
riskier operation than the rename actually called for. A project's folder name and its display
name differing is normal and not confusing on its own.

**Also renamed the companion file-guide artifact** ("Second Brain Atlas" -> "Repository Atlas
Guide") to avoid two different things both being called some variant of "Atlas" under the old
project name.

**Verified live**: the running FastAPI server's `/openapi.json` title updated automatically via
`--reload` to "Repository Atlas" without a manual restart, confirmed directly rather than
assumed.

## Oversized Code Chunks Split — Truncation Problem Fixed, Measured Before/After

Implemented the fix flagged above: `code_chunking.py::chunk_code_file()` now checks every
top-level chunk (a whole class/function) against `chunking.max_chunk_chars` (the same 4000-char
config key already used for oversized diffs). Chunks under the limit are completely unchanged.
An oversized chunk (e.g. a class with many methods) is split:

1. **One level of recursive splitting first**: find nested definitions inside it (methods
   inside a class) via `_find_nested_definitions()`, one piece per method plus one piece for
   whatever comes before the first method (signature/docstring/fields). Each method-piece gets
   labeled `[Inside <EnclosingName>]` for context and carries **its own** extracted
   `symbol_name` (not the enclosing class's) -- a real improvement over blanket-tagging every
   fragment with the class name, since it keeps cross-file symbol-reference lookups working
   correctly for individual methods, not just the class as a whole.
2. **Hard character-slicing backstop** for anything still oversized after that (a single huge
   method, or a language/construct with no further nested structure to split on) -- reuses the
   exact same slicing trick `chunk_text()` already uses for oversized diffs, not a new
   mechanism.

**Per-language node types verified empirically before use**, not assumed (consistent with how
the original Phase 2 chunking work was done): parsed real synthetic snippets and confirmed
Python/C++ reuse the *same* node type for a nested method as for a top-level function
(`function_definition`), while JavaScript/TypeScript (`method_definition`) and Java
(`method_declaration`) use a distinct nested-only type not previously in `CHUNK_NODE_TYPES` --
added both, since the existing top-level detection logic is reused unchanged for nested search
(these types simply never occur as direct children of the file root, so adding them cannot
affect top-level chunking).

**Verified in three stages**:
1. **Synthetic test** (a 200-method Python class, deliberately huge): 202 chunks produced, max
   chunk size 105 characters (limit 4000), each method correctly getting its own `symbol_name`.
2. **The exact real file that caused the original crash/measurement**
   (`test_data/requests/tests/test_requests.py`, previously one ~24,600-token chunk for its
   `TestRequests` class): now 234 chunks, max chunk size 2,563 characters, zero chunks over the
   limit, each test method correctly named (`test_entry_points`, `test_invalid_url`, etc.)
   instead of one undifferentiated blob tagged just `TestRequests`.
3. **Full re-ingest + re-measurement against real retrieval/reranking**: `requests_poc` went
   from 577 to 935 points (finer granularity from splitting). Re-ran `eval_reranker.py`'s
   token-length measurement: mean pair length dropped from **1,028 to 287.6 tokens**, max from
   **24,604 to 1,370 tokens**, and the truncated-pair rate dropped from **31.6% to 14.2%**. The
   catastrophic cases are gone entirely; the residual truncated cases are files whose single
   oversized chunk had no further nested structure to split on (e.g. `status_codes.py`, a large
   generated dict) and fell through to the character-slicing backstop -- 4000 characters of
   dense code can still land a little over 512 tokens for some content, a real but much smaller
   remaining gap than before, not chased further in this pass.

**A small, honest, measured trade-off found**: `eval_retrieval.py`'s Recall@6/MRR moved from
90.91%/0.469 to 86.36%/0.456 (22-question set) -- not a clean regression: one previous miss
("how can I stream a large response body in chunks", `models.py`) became a hit, while two new
misses appeared ("how do cookies get merged...", expected `cookies.py`; "what exception is
raised on a connection error", expected `exceptions.py`). Investigated directly rather than
assumed: `exceptions.py`'s content is completely untouched by this fix (its classes were never
oversized), yet its miss appeared anyway -- ruling out a bug in the splitting logic itself.
Root cause is a corpus-wide side effect: going from 577 to 935 chunks shifts BM25 term-frequency
statistics and reranker candidate-pool competition for borderline queries across the whole
collection, not just the files that were actually split. A real, modest cost of fixing a much
more severe problem (97%+ content loss on the worst chunks) -- reported honestly rather than
glossed over, not chased further in this pass.
