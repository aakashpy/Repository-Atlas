import yaml
from pathlib import Path

_config = None


def get_config():
    """Load config.yaml once, reuse afterward (singleton pattern)."""
    global _config
    if _config is None:
        config_path = Path(__file__).parent / "config.yaml"
        with open(config_path) as f:
            _config = yaml.safe_load(f)
    return _config


# Convenience derived values, computed once
def get_current_collection_name():
    return f"{get_config()['project']['name']}_poc"


def get_history_collection_name():
    return f"{get_config()['project']['name']}_history"


def get_repo_path() -> str:
    """
    Resolve config.yaml's project.repo_path relative to the project root
    (one level up from backend/), NOT relative to the caller's working
    directory. This keeps scripts runnable regardless of whether they're
    invoked as `python3 backend/ingest.py` from the project root or
    `python3 ingest.py` from inside backend/.
    """
    repo_path = get_config()["project"]["repo_path"]
    return str((Path(__file__).parent.parent / repo_path).resolve())