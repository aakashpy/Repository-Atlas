import sys
import subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from core.pipeline import embed_and_upsert_file, delete_file_chunks
from core.symbol_index import build_symbol_index
from core.file_exclusions import filter_excluded

from config import get_repo_path

REPO_PATH = get_repo_path()
STATE_FILE = f"{REPO_PATH}/.last_indexed_commit"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # backend/ingestion/update_index.py -> ingestion/ -> backend/ -> project root


def _display_path(absolute_path: str) -> str:
    """
    Path relative to the project root (e.g. "test_data/requests/x.py"),
    matching what ingest.py's full rebuild stores -- REPO_PATH is now an
    absolute filesystem path (needed for git subprocess calls to work
    regardless of caller cwd), but chunks must be keyed/displayed
    consistently across full ingests and incremental updates, or
    delete_file_chunks() would stop matching a file's existing chunks.
    """
    try:
        return str(Path(absolute_path).relative_to(PROJECT_ROOT))
    except ValueError:
        return absolute_path


def get_current_commit(repo_path: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def get_changed_files(repo_path: str, old_commit: str, new_commit: str):
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", "--name-status", old_commit, new_commit],
        capture_output=True, text=True, check=True,
    )
    changes = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]

        if status[0] == "R":
            old_path, new_path = parts[1], parts[2]
            changes.append(("R", old_path, new_path))
        else:
            filepath = parts[1]
            changes.append((status[0], filepath, None))

    return changes


def load_last_indexed_commit():
    path = Path(STATE_FILE)
    if path.exists():
        return path.read_text().strip()
    return None


def save_last_indexed_commit(commit: str):
    Path(STATE_FILE).write_text(commit)


def run_update():
    repo = REPO_PATH
    current_commit = get_current_commit(repo)
    last_commit = load_last_indexed_commit()

    if last_commit is None:
        print("No previous index found. Run ingest.py first for a full index.")
        return

    if last_commit == current_commit:
        print("No new commits since last index. Nothing to do.")
        return

    changes = get_changed_files(repo, last_commit, current_commit)
    print(f"Found {len(changes)} changed file(s).")

    # Custom ingestion.exclude_patterns (config.yaml) apply here too, not
    # just in ingest.py's full walk -- a committed file can still be one
    # the user doesn't want indexed. Real .gitignore rules rarely matter
    # here (git won't normally let an ignored file get committed at all),
    # but this also catches a force-added ignored file. See
    # file_exclusions.py.
    relpaths = [filepath for _, filepath, _ in changes] + [extra for _, _, extra in changes if extra]
    excluded = filter_excluded(repo, relpaths)
    if excluded:
        print(f"Skipping {len(excluded)} excluded file(s): {sorted(excluded)}")

    # Rebuilt fresh every run (not patched incrementally) -- a changed file
    # can affect symbols referenced from anywhere else in the repo, and
    # ctags is cheap enough that re-running it is simpler than trying to
    # patch the cross-file map correctly. See symbol_index.py.
    symbol_index = build_symbol_index(repo)

    for change in changes:
        status, filepath, extra = change
        full_path = f"{repo}/{filepath}"

        # Deleting a path's chunks is always safe to do regardless of
        # exclusion (a harmless no-op if it was never indexed) -- only
        # skip the steps that would actually ADD an excluded file's
        # content. This matters most for renames: a file renamed INTO an
        # excluded path must still have its OLD chunks cleaned up, and a
        # file renamed OUT of an excluded path must still get indexed
        # under its new name.
        if status == "D":
            print(f"Deleting chunks for removed file: {filepath}")
            delete_file_chunks(_display_path(full_path))
        elif status == "R":
            new_path = f"{repo}/{extra}"
            print(f"Renaming: {filepath} -> {extra}")
            delete_file_chunks(_display_path(full_path))  # remove old path's chunks -- always safe
            if extra in excluded:
                continue
            try:
                content = Path(new_path).read_text(encoding="utf-8")
                embed_and_upsert_file(_display_path(new_path), content, symbol_index=symbol_index)  # index under new path
            except (UnicodeDecodeError, FileNotFoundError):
                continue

        elif status in ("A", "M"):
            delete_file_chunks(_display_path(full_path))  # always safe -- no-op if never indexed
            if filepath in excluded:
                continue
            print(f"Re-indexing changed file: {filepath}")
            try:
                content = Path(full_path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            embed_and_upsert_file(_display_path(full_path), content, symbol_index=symbol_index)

    save_last_indexed_commit(current_commit)
    print(f"Update complete. Now indexed up to commit {current_commit}")


if __name__ == "__main__":
    run_update()