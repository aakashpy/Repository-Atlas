import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from history.history_ingest import get_function_level_changes
from history.history_ingest import get_changed_files_in_commit, get_file_diff, get_commit_metadata_full
from ingestion.update_index import get_current_commit
from history.history_pipeline import process_commit
from config import get_repo_path, get_config

REPO = get_repo_path()


def process_latest_commit():
    """Add the current HEAD commit's changes to the history collection."""
    if not get_config().get("history", {}).get("enabled", True):
        print("history.enabled is false in config.yaml -- skipping (current-state update still ran).")
        return

    commit_hash = get_current_commit(REPO)
    commit = get_commit_metadata_full(REPO, commit_hash)

    files = get_changed_files_in_commit(REPO, commit_hash)

    changes = []
    for change in files:
        status = change[0]
        filepath = change[1]
        old_path = change[2] if status == "R" else None
        diff = get_file_diff(REPO, commit_hash, filepath)

        function_changes = []
        if status in ("A", "M") and filepath.endswith(".py"):
            function_changes = get_function_level_changes(REPO, commit_hash, filepath)

        changes.append((status, filepath, diff, old_path, function_changes))

    chunk_count = process_commit(commit, changes)
    print(f"History updated: commit {commit_hash[:8]}, {len(files)} file(s), {chunk_count} chunks.")


if __name__ == "__main__":
    process_latest_commit()