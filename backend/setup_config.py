"""
Interactive setup: asks for every setting that goes into config.yaml,
validates what can cheaply be validated right now (does the repo path
exist, is Qdrant reachable, does the embedding model actually load and
what dimension does it really produce, is Ollama reachable), and writes
config.yaml -- so a new deployer doesn't have to hand-edit a YAML file and
guess at values like `embedding.dimension` that check_config.py would
otherwise catch as a mismatch only after the fact.

Common settings are always asked. Less commonly changed ones are shown
with their default and can be batch-accepted, or customized one at a time
-- so you always see every value that ends up in the file, without being
forced through 20 prompts for the common case of "point this at my repo
and pick a model."

Usage:
  python3 backend/setup_config.py
"""
import sys
import getpass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
ENV_PATH = Path(__file__).resolve().parent / ".env"


def ask(prompt_text, default=None, required=False):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{prompt_text}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if not val and required:
            print("  This is required.")
            continue
        return val


def ask_yes_no(prompt_text, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    val = input(f"{prompt_text} {suffix}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def ask_float(prompt_text, default):
    val = ask(prompt_text, str(default))
    try:
        return float(val)
    except ValueError:
        print(f"  Not a number, using default {default}.")
        return default


def ask_int(prompt_text, default):
    val = ask(prompt_text, str(default))
    try:
        return int(val)
    except ValueError:
        print(f"  Not a number, using default {default}.")
        return default


def section(title):
    print(f"\n--- {title} ---")


def detect_embedding_dimension(model_name):
    """
    Actually loads the model and encodes a sample string, so the config
    gets the real dimension instead of asking the user to know or guess
    it -- check_config.py exists specifically because a wrong dimension
    here silently corrupts a fresh collection.
    """
    print(f"  Loading '{model_name}' to detect its real output dimension (may download it first)...")
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
        vec = model.encode("dimension probe")
        dim = len(vec)
        prompts = getattr(model, "prompts", None) or {}
        query_prompt_name = "query" if "query" in prompts else None
        print(f"  Loaded OK -- {dim}-dim vectors. {'Detected an asymmetric model (query prompt: query).' if query_prompt_name else 'No named query prompt detected.'}")
        return dim, query_prompt_name
    except Exception as e:
        print(f"  Couldn't load it here ({type(e).__name__}: {e}).")
        return None, None


def check_qdrant_reachable(host, port):
    try:
        from qdrant_client import QdrantClient
        QdrantClient(host=host, port=port, timeout=3).get_collections()
        return True
    except Exception:
        return False


def check_ollama_reachable():
    try:
        import requests
        requests.get("http://localhost:11434/api/tags", timeout=3)
        return True
    except Exception:
        return False


def main():
    print("Repository Atlas -- interactive config setup")
    print("Press Enter to accept a shown default.\n")

    if CONFIG_PATH.exists():
        if not ask_yes_no(f"{CONFIG_PATH} already exists. Overwrite it (a backup will be kept)?", default=False):
            print("Aborted -- leaving the existing config.yaml untouched.")
            return

    # ---- project ----
    section("Target project")
    repo_path_input = ask("Path to the repo you want to index (absolute, or relative to the project root)", required=True)
    repo_path_abs = (PROJECT_ROOT / repo_path_input).resolve() if not Path(repo_path_input).is_absolute() else Path(repo_path_input)
    if not repo_path_abs.is_dir():
        print(f"  Warning: {repo_path_abs} doesn't exist yet -- you can still save this and fix it before running ingest.py.")
    elif not (repo_path_abs / ".git").exists():
        print(f"  Warning: {repo_path_abs} exists but has no .git -- history features won't work until it's a real git repo.")
    else:
        print(f"  Found a git repo at {repo_path_abs}.")

    default_name = Path(repo_path_input).name or "myproject"
    project_name = ask("Project name (used as a prefix for Qdrant collection names)", default=default_name)

    history_enabled = ask_yes_no("Enable git history indexing (search_history, history_ingest.py)?", default=True)

    print("  The repo's own .gitignore (node_modules, .env, build output, etc.) is always")
    print("  respected automatically -- detected live via `git check-ignore`, nothing to configure.")
    extra_excludes_input = ask("  Extra file/path patterns to ALSO exclude, comma-separated (e.g. *.pem, secrets/*) -- blank for none", default="")
    exclude_patterns = [p.strip() for p in extra_excludes_input.split(",") if p.strip()]

    # ---- qdrant ----
    section("Qdrant (vector database)")
    qdrant_host = ask("Qdrant host", default="localhost")
    qdrant_port = ask_int("Qdrant port", default=6333)
    if check_qdrant_reachable(qdrant_host, qdrant_port):
        print("  Reachable.")
    else:
        print(f"  Warning: couldn't reach Qdrant at {qdrant_host}:{qdrant_port} right now -- make sure it's running before you ingest (see docs/SETUP.md).")

    # ---- embedding ----
    section("Embedding model")
    print("  Default is Qwen/Qwen3-Embedding-0.6B (1024-dim, GPU-accelerated, what this project ships tuned for).")
    embedding_model = ask("Embedding model name (any sentence-transformers model)", default="Qwen/Qwen3-Embedding-0.6B")

    if embedding_model == "Qwen/Qwen3-Embedding-0.6B":
        embedding_dim = 1024
        query_prompt_name = "query"
        print("  Using the known values for the default model (1024-dim, query prompt: query) -- skipping a reload.")
    else:
        detected_dim, detected_prompt = detect_embedding_dimension(embedding_model)
        if detected_dim is not None:
            embedding_dim = detected_dim
            query_prompt_name = detected_prompt
        else:
            embedding_dim = ask_int("  Enter its output dimension manually", default=384)
            query_prompt_name = ask("  Query prompt name (blank for none)", default="") or None

    max_seq_length = ask_int("Max sequence length (tokens) -- caps embedding compute per chunk", default=1024)
    encode_batch_size = ask_int("Embedding batch size", default=8)

    # ---- LLM backend ----
    section("LLM backend (generates the actual answers)")
    llm_backend = ask("Backend -- 'gemini' (cloud, free tier) or 'ollama' (local, private)", default="gemini")
    while llm_backend not in ("gemini", "ollama"):
        print("  Must be 'gemini' or 'ollama'.")
        llm_backend = ask("Backend", default="gemini")

    gemini_model = "gemini-flash-lite-latest"
    ollama_model = "qwen2.5:3b"

    if llm_backend == "gemini":
        gemini_model = ask("Gemini model name", default=gemini_model)
        existing_key = None
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    existing_key = line.split("=", 1)[1]
        if existing_key:
            print(f"  Found an existing GEMINI_API_KEY in {ENV_PATH.name} -- keeping it.")
        else:
            key = getpass.getpass("  Enter your GEMINI_API_KEY (input hidden, blank to skip and set it later): ").strip()
            if key:
                with open(ENV_PATH, "a") as f:
                    f.write(f"GEMINI_API_KEY={key}\n")
                print(f"  Wrote it to {ENV_PATH.name}.")
            else:
                print(f"  Skipped -- add GEMINI_API_KEY to {ENV_PATH.name} before running the LLM.")
    else:
        ollama_model = ask("Ollama model name", default=ollama_model)
        if check_ollama_reachable():
            print("  Ollama is reachable.")
        else:
            print("  Warning: couldn't reach Ollama at localhost:11434 -- make sure it's running (see docs/SETUP.md).")

    # ---- advanced settings ----
    section("Advanced settings")
    defaults = {
        "min_score_current": 0.37,
        "min_score_history": 0.37,
        "max_file_chunks": 20,
        "max_referenced_symbols": 5,
        "text_chunk_size": 40,
        "text_chunk_overlap": 5,
        "diff_chunk_threshold": 2000,
        "prose_max_chunk_size": 70,
        "max_chunk_chars": 4000,
        "reranking_enabled": True,
        "reranking_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "candidate_pool": 20,
        "ctags_binary": "ctags",
    }
    print("  Everything below defaults to the values this project was tuned/verified with.")
    if embedding_model != "Qwen/Qwen3-Embedding-0.6B":
        print("  NOTE: you're using a different embedding model -- min_score_current/min_score_history")
        print("  below will very likely need recalibrating. Run 'python3 backend/check_config.py")
        print("  --calibrate' after your first ingest to get a real suggested value instead of guessing.")

    if ask_yes_no("Customize advanced settings one at a time instead of accepting all defaults?", default=False):
        defaults["min_score_current"] = ask_float("Relevance gate threshold, current-state search", defaults["min_score_current"])
        defaults["min_score_history"] = ask_float("Relevance gate threshold, history search", defaults["min_score_history"])
        defaults["max_file_chunks"] = ask_int("Max chunks returned for a whole-file question", defaults["max_file_chunks"])
        defaults["max_referenced_symbols"] = ask_int("Max cross-file symbol definitions pulled in per query", defaults["max_referenced_symbols"])
        defaults["text_chunk_size"] = ask_int("Line-window chunk size (fallback chunker)", defaults["text_chunk_size"])
        defaults["text_chunk_overlap"] = ask_int("Line-window chunk overlap", defaults["text_chunk_overlap"])
        defaults["diff_chunk_threshold"] = ask_int("Diff size (chars) above which a diff gets sub-chunked", defaults["diff_chunk_threshold"])
        defaults["prose_max_chunk_size"] = ask_int("Max lines per doc (.md/.rst) section chunk", defaults["prose_max_chunk_size"])
        defaults["max_chunk_chars"] = ask_int("Hard character cap per chunk (any chunker)", defaults["max_chunk_chars"])
        defaults["reranking_enabled"] = ask_yes_no("Enable cross-encoder reranking?", defaults["reranking_enabled"])
        if defaults["reranking_enabled"]:
            defaults["reranking_model"] = ask("Reranker model name", defaults["reranking_model"])
            defaults["candidate_pool"] = ask_int("Reranking candidate pool size", defaults["candidate_pool"])
        defaults["ctags_binary"] = ask("Universal Ctags binary name/path", defaults["ctags_binary"])
    else:
        for k, v in defaults.items():
            print(f"    {k}: {v}")

    # ---- write config.yaml ----
    if CONFIG_PATH.exists():
        backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
        backup_path.write_text(CONFIG_PATH.read_text())
        print(f"\nBacked up existing config to {backup_path.name}.")

    query_prompt_line = f'"{query_prompt_name}"' if query_prompt_name else "null"
    exclude_patterns_line = "[" + ", ".join(f'"{p}"' for p in exclude_patterns) + "]"

    content = f"""project:
  name: "{project_name}"          # used as a prefix for Qdrant collection names
  repo_path: "{repo_path_input}"

ingestion:
  exclude_patterns: {exclude_patterns_line}   # extra glob patterns never read into the index, on top of
                          # whatever the target repo's own .gitignore already excludes

history:
  enabled: {str(history_enabled).lower()}   # set to false to skip history entirely: don't run history_ingest.py/history_update.py,
                   # and search/chat silently proceed on current-state results only. Also self-degrades
                   # automatically (with a warning, not a crash) if this is true but the history collection
                   # doesn't exist yet or a query against it fails for any reason -- e.g. mid-migration, or
                   # a backfill that hasn't finished. "Use whatever data is available" is the default behavior,
                   # not just an opt-in.

qdrant:
  host: "{qdrant_host}"
  port: {qdrant_port}

embedding:
  model_name: "{embedding_model}"
  dimension: {embedding_dim}
  query_prompt_name: {query_prompt_line}   # asymmetric model: queries get an instruct prefix, documents don't. null for models with no named prompts
  max_seq_length: {max_seq_length}
  encode_batch_size: {encode_batch_size}

chunking:
  text_chunk_size: {defaults["text_chunk_size"]}
  text_chunk_overlap: {defaults["text_chunk_overlap"]}
  diff_chunk_threshold: {defaults["diff_chunk_threshold"]}
  prose_max_chunk_size: {defaults["prose_max_chunk_size"]}   # section-aware chunking for .md/.rst; oversized sections split by paragraph
  max_chunk_chars: {defaults["max_chunk_chars"]}      # hard backstop on chunk byte size regardless of line count -- see docs/DECISIONS.md

retrieval:
  min_score_current: {defaults["min_score_current"]}   # run `python3 backend/check_config.py --calibrate` after ingest.py to re-measure this
  min_score_history: {defaults["min_score_history"]}
  max_file_chunks: {defaults["max_file_chunks"]}         # cap on how many chunks a single filename-boost lookup returns (whole-file questions)
  max_referenced_symbols: {defaults["max_referenced_symbols"]}   # cap on how many cross-file symbol definitions get pulled in per query

reranking:
  enabled: {str(defaults["reranking_enabled"]).lower()}                                    # set false to skip reranking entirely -- falls back to plain RRF hybrid order
  model_name: "{defaults["reranking_model"]}"  # small enough to share the GPU with the embedding model
  candidate_pool: {defaults["candidate_pool"]}                                # how many hybrid results to fetch and rerank before boosting/truncation to top_k

ctags:
  binary: "{defaults["ctags_binary"]}"   # universal-ctags binary name/path; used for cross-file symbol resolution

llm:
  backend: "{llm_backend}"              # "gemini" or "ollama"
  ollama_model: "{ollama_model}"
  gemini_model: "{gemini_model}"
"""
    CONFIG_PATH.write_text(content)
    print(f"\nWrote {CONFIG_PATH}.")

    print("\nNext steps:")
    print("  1. python3 backend/check_config.py        # confirm everything actually loads/connects")
    print("  2. python3 backend/ingest.py               # index the current-state codebase")
    if history_enabled:
        print("  3. python3 backend/history_ingest.py        # (optional) backfill full commit history")
    print("  4. python3 backend/check_config.py --calibrate   # re-measure relevance thresholds if you changed the embedding model")

    if ask_yes_no("\nRun the preflight checker (check_config.py) now?", default=True):
        import subprocess
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "check_config.py")])


if __name__ == "__main__":
    main()
