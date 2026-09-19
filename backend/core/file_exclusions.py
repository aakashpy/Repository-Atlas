"""
Decides which files must NEVER be read into the knowledge base, beyond the
basic IGNORE_DIRS/ALLOWED_EXTENSIONS filtering ingest.py already did.

Two sources, both applied:
1. Everything the target repo's own .gitignore (plus any nested
   .gitignore, .git/info/exclude, global excludes) already excludes --
   evaluated with git itself (`git check-ignore`), not a hand-rolled
   reimplementation. Nested .gitignore files and negation patterns are
   genuinely tricky to get right otherwise, and this repo may not be the
   one this code lives in -- it's whatever the user pointed
   project.repo_path at, node_modules/.env/build output and all.
2. Whatever extra patterns the user lists in config.yaml's
   ingestion.exclude_patterns -- for anything that should stay out of the
   index regardless of the target repo's own git configuration, including
   files that ARE tracked/committed but shouldn't be searched (a vendored
   SDK checked into the repo, a generated-code directory, etc).

Used by both ingest.py (full walk) and update_index.py (incremental), so
a file is never treated differently depending on which one runs.
"""
import sys
import subprocess
import fnmatch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config`

from config import get_config


def get_gitignored_relpaths(repo_path: str, candidate_relpaths: list) -> set:
    """
    Given candidate file paths relative to repo_path, returns the subset
    git itself considers ignored. Degrades to "none ignored" (rather than
    crashing ingestion) if repo_path isn't a real git repo or git isn't
    available -- same graceful-degradation pattern already used for
    Ctags in symbol_index.py.
    """
    if not candidate_relpaths:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "check-ignore", "--stdin", "-z"],
            input="\0".join(candidate_relpaths),
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return set()
    # check-ignore exits 1 when NONE of the given paths are ignored --
    # that's a normal outcome, not an error. Anything else (128 = not a
    # git repo, etc.) means we can't evaluate gitignore rules at all.
    if result.returncode not in (0, 1):
        return set()
    return {p for p in result.stdout.split("\0") if p}


def get_custom_exclude_patterns() -> list:
    return get_config().get("ingestion", {}).get("exclude_patterns", [])


def matches_custom_pattern(relpath: str, patterns: list) -> bool:
    """
    Simple glob-style matching (fnmatch) against user-specified patterns --
    e.g. "*.pem", "secrets/*", "*.local.py". Checked against both the full
    relative path and the bare filename, so a pattern like "*.env" matches
    regardless of which directory the file is in.
    """
    if not patterns:
        return False
    name = Path(relpath).name
    return any(fnmatch.fnmatch(relpath, p) or fnmatch.fnmatch(name, p) for p in patterns)


def filter_excluded(repo_path: str, candidate_relpaths: list) -> set:
    """
    The one entry point both ingest.py and update_index.py call: returns
    the subset of candidate_relpaths that should be SKIPPED (gitignored
    by the target repo, or matching a custom exclude_patterns entry).
    """
    excluded = set(get_gitignored_relpaths(repo_path, candidate_relpaths))
    patterns = get_custom_exclude_patterns()
    if patterns:
        excluded |= {p for p in candidate_relpaths if matches_custom_pattern(p, patterns)}
    return excluded
