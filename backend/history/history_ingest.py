import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from config import get_repo_path, get_config

REPO = get_repo_path()
# Checkpoint file for the full backfill -- separate from update_index.py's
# .last_indexed_commit, which tracks a different collection's incremental
# progress. Commits are processed oldest-first and each commit's chunks are
# upserted as one atomic batch (see history_pipeline.process_commit), so
# checkpointing "last fully-processed commit" after each one is safe to
# resume from regardless of how the process was interrupted -- point IDs
# are deterministic (commit_hash + filepath + chunk_type + index), so even
# re-processing the same commit twice is a harmless no-op overwrite, not a
# duplicate.
PROGRESS_FILE = f"{REPO}/.history_ingest_progress"


def load_last_processed_commit():
    path = Path(PROGRESS_FILE)
    if path.exists():
        return path.read_text().strip()
    return None


def save_last_processed_commit(commit_hash: str):
    Path(PROGRESS_FILE).write_text(commit_hash)


def clear_progress():
    Path(PROGRESS_FILE).unlink(missing_ok=True)


def get_recent_commits(repo: str, limit: int = 50):
    """Get commit metadata for the most recent N commits."""
    result = subprocess.run(
        ["git", "-C", repo, "log", f"-{limit}",
         "--pretty=format:%H|%an|%ad|%s", "--date=short"],
        capture_output=True, text=True, check=True,
    )

    commits = []
    for line in result.stdout.strip().splitlines():
        commit_hash, author, date, subject = line.split("|", 3)
        commits.append({
            "hash": commit_hash,
            "author": author,
            "date": date,
            "subject": subject,
        })
    return commits


def get_changed_files_in_commit(repo: str, commit_hash: str):
    """Get files changed in a specific commit. Renames include both paths."""
    result = subprocess.run(
        ["git", "-C", repo, "show", "--name-status",
         "--pretty=format:", commit_hash],
        capture_output=True, text=True, errors="replace", check=True,
    )
    files = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]

        if status[0] == "R":
            if len(parts) >= 3:
                files.append(("R", parts[1], parts[2]))
            elif len(parts) == 2:
                # Malformed rename line (rare git edge case) — treat as a plain change
                files.append(("M", parts[1], None))
            # if len(parts) < 2, skip — nothing usable
        elif len(parts) >= 2:
            files.append((status[0], parts[1], None))
    return files


def get_file_diff(repo: str, commit_hash: str, filepath: str):
    """Get ONLY the diff content for one file in one commit (no header)."""
    result = subprocess.run(
        ["git", "-C", repo, "show", commit_hash, "--format=", "--", filepath],
        capture_output=True, text=True, errors="replace", check=True,
    )
    return result.stdout.strip()

def get_commit_metadata_full(repo: str, commit_hash: str):
    result = subprocess.run(
        ["git", "-C", repo, "show", "-s",
         "--pretty=format:%an|%ad|%s", "--date=short", commit_hash],
        capture_output=True, text=True, check=True,
    )
    author, date, subject = result.stdout.strip().split("|", 2)
    return {"hash": commit_hash, "author": author, "date": date, "subject": subject}

def get_file_content_at_commit(repo: str, commit_hash: str, filepath: str):
    """
    Get a file's full content as it existed at a specific commit.
    Returns None if the file didn't exist at that commit (e.g. it was
    just created, so there's no "before" version).
    """
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{commit_hash}:{filepath}"],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0:
        return None  # file didn't exist at this commit
    return result.stdout

def get_function_level_changes(repo: str, commit_hash: str, filepath: str):
    """
    Compare a file's functions before and after a commit.
    Returns a list of (function_name, change_type, body_text) tuples.
    body_text is the NEW body for added/modified, the OLD body for removed
    (so we always have something meaningful to store/embed).
    """
    from core.pipeline import get_function_map

    if not filepath.endswith(".py"):
        return []

    parent_hash = f"{commit_hash}~1"
    before_content = get_file_content_at_commit(repo, parent_hash, filepath)
    after_content = get_file_content_at_commit(repo, commit_hash, filepath)

    before_map = get_function_map(before_content) if before_content else {}
    after_map = get_function_map(after_content) if after_content else {}

    changes = []
    all_names = set(before_map.keys()) | set(after_map.keys())

    for name in all_names:
        in_before = name in before_map
        in_after = name in after_map

        if in_before and not in_after:
            changes.append((name, "removed", before_map[name]))
        elif in_after and not in_before:
            changes.append((name, "added", after_map[name]))
        elif before_map[name] != after_map[name]:
            changes.append((name, "modified", after_map[name]))

    return changes

if __name__ == "__main__":
    if not get_config().get("history", {}).get("enabled", True):
        print("history.enabled is false in config.yaml -- skipping history ingest. "
              "Current-state search works fully without it; set it back to true to backfill history.")
        sys.exit(0)

    from history.history_pipeline import create_history_collection, process_commit, HISTORY_COLLECTION
    from core.pipeline import get_client
    import subprocess

    last_processed = load_last_processed_commit()
    client = get_client()

    if last_processed is None or not client.collection_exists(HISTORY_COLLECTION):
        print("Starting a fresh full history ingest (no checkpoint found).")
        create_history_collection()
        last_processed = None
    else:
        print(f"Resuming from checkpoint: last processed commit {last_processed[:8]}")

    # Get ALL commit hashes, NEWEST first (git log's default order). Deliberately
    # not --reverse: most users care about recent history far more than deep
    # legacy commits, and processing newest-first means an interrupted run
    # (safe to do now that resume support exists -- see PROGRESS_FILE above)
    # leaves you with the most relevant history recorded, not the oldest.
    # Resume logic below is order-agnostic (just "everything after the
    # checkpoint in this list"), so this doesn't affect it.
    result = subprocess.run(
        ["git", "-C", REPO, "log", "--pretty=format:%H"],
        capture_output=True, text=True, check=True,
    )
    all_hashes = result.stdout.strip().splitlines()
    print(f"Found {len(all_hashes)} total commits.")

    start_index = 0
    if last_processed is not None:
        try:
            start_index = all_hashes.index(last_processed) + 1
        except ValueError:
            print(
                f"Checkpoint commit {last_processed[:8]} not found in current history "
                f"(repo may have been rewritten) -- starting over from scratch."
            )
            create_history_collection()
            start_index = 0
        else:
            print(f"Resuming from commit {start_index + 1}/{len(all_hashes)}.")

    total_chunks = 0
    commit_hash = None
    try:
        for i in range(start_index, len(all_hashes)):
            commit_hash = all_hashes[i]
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

            total_chunks += process_commit(commit, changes)
            save_last_processed_commit(commit_hash)  # only after the commit's chunks are safely upserted

            if (i + 1) % 100 == 0:
                print(f"Processed {i + 1}/{len(all_hashes)} commits, {total_chunks} chunks so far...")
    except KeyboardInterrupt:
        done = all_hashes.index(commit_hash) + 1 if commit_hash else start_index
        print(
            f"\nInterrupted after commit {done}/{len(all_hashes)}. "
            f"Progress is saved -- just re-run this script to resume from here."
        )
        raise

    print(f"Done. {len(all_hashes)} commits processed, {total_chunks} total chunks stored.")
    clear_progress()  # full run completed -- a future invocation should start fresh, not "resume" past the end