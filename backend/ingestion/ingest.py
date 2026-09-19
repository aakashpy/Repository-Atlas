import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from config import get_repo_path
from qdrant_client.models import VectorParams, Distance
from core.pipeline import get_client, embed_and_upsert_file, COLLECTION_NAME, EMBEDDING_DIM
from core.code_chunking import LANGUAGE_BY_EXTENSION
from core.symbol_index import build_symbol_index
from core.file_exclusions import filter_excluded

IGNORE_DIRS = {".git", "venv", "__pycache__", "qdrant_storage", "node_modules"}
DOC_EXTENSIONS = {".md", ".txt", ".rst"}
# Every language code_chunking.py can tree-sitter-parse, plus plain docs --
# derived from LANGUAGE_BY_EXTENSION rather than duplicated here, so adding
# a language there automatically makes it ingestable.
ALLOWED_EXTENSIONS = set(LANGUAGE_BY_EXTENSION.keys()) | DOC_EXTENSIONS
REPO_PATH = get_repo_path()
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # backend/ingestion/ingest.py -> ingestion/ -> backend/ -> project root

def walk_project(root: str):
    root_path = Path(root)
    candidates = []  # (path, relative-to-repo-root path string)
    for path in root_path.rglob("*"):
        if path.is_dir():
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.suffix not in ALLOWED_EXTENSIONS:
            continue
        candidates.append((path, str(path.relative_to(root_path))))

    # One batched call, not one per file: everything the target repo's own
    # .gitignore excludes (node_modules, .env, build output, whatever that
    # repo already ignores), plus any custom ingestion.exclude_patterns
    # from config.yaml. See file_exclusions.py.
    excluded = filter_excluded(root, [rel for _, rel in candidates])
    if excluded:
        print(f"Skipping {len(excluded)} gitignored/excluded file(s).")

    for path, rel in candidates:
        if rel in excluded:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        # Store paths relative to the project root (e.g. "test_data/requests/x.py"),
        # not REPO_PATH's absolute filesystem path -- keeps stored/displayed
        # filepaths short and portable, and consistent with what
        # update_index.py's incremental updates store for the same file.
        try:
            display_path = str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            display_path = str(path)
        yield display_path, content


if __name__ == "__main__":
    from core.pipeline import create_hybrid_collection
    create_hybrid_collection()

    symbol_index = build_symbol_index(REPO_PATH)
    print(f"Symbol index built: {len(symbol_index)} unique symbols.")

    file_count = 0
    for filepath, content in walk_project(REPO_PATH):
        embed_and_upsert_file(filepath, content, symbol_index=symbol_index)
        file_count += 1

    print(f"Indexed {file_count} files.")