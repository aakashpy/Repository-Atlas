"""
Cross-file symbol resolution via Universal Ctags. Builds one map of every
definable symbol name -> where it's defined, used at ingest time to tag
each chunk with the *names* it references (not their full definitions --
see docs/DECISIONS.md for why baking full definitions into chunk text was
rejected: staleness risk). Definitions are resolved live at generation
time instead (search.py::_expand_referenced_symbols), so a chunk's stored
reference list never goes stale even if the referenced code changes.
"""
import sys
import json
import re
import subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config`

from config import get_config


def _ctags_binary() -> str:
    return get_config().get("ctags", {}).get("binary", "ctags")


def build_symbol_index(repo_path: str) -> dict:
    """
    Run ctags across the whole repo and return {symbol_name: [{"file",
    "line"}, ...]}. Always rebuilt from scratch on every ingest/update run
    rather than patched incrementally -- ctags runs in low single-digit
    seconds even on medium repos, and correctly patching a cross-file
    symbol map incrementally is a much larger problem than just re-running
    it. If ctags isn't installed, cross-file resolution is silently
    disabled (empty index) rather than failing ingestion outright.
    """
    binary = _ctags_binary()
    try:
        result = subprocess.run(
            [binary, "-R", "--output-format=json", "--fields=+n", "-f", "-", repo_path],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print(
            f"Warning: '{binary}' not found -- cross-file symbol resolution "
            f"disabled. Install with: sudo apt-get install universal-ctags"
        )
        return {}

    index = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag":
            continue
        name = tag.get("name")
        if not name:
            continue
        index.setdefault(name, []).append({"file": tag.get("path"), "line": tag.get("line")})

    return index


_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")


def find_referenced_symbols(chunk_text: str, own_name, symbol_index: dict, limit: int = 8) -> list:
    """
    Language-agnostic: scan a chunk's text for identifier-shaped tokens that
    match a real symbol defined elsewhere in the repo, excluding the
    chunk's own name. Some false positives are expected (a common word
    happens to match another symbol's name) and acceptable -- resolution
    only runs for chunks actually retrieved for a real query, so it's
    cheap and self-limiting, not a correctness bug.
    """
    if not symbol_index:
        return []

    found = []
    for token in _IDENTIFIER_RE.findall(chunk_text):
        if token == own_name or token in found:
            continue
        if token in symbol_index:
            found.append(token)
            if len(found) >= limit:
                break
    return found
