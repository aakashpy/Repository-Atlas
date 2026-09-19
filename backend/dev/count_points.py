import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from core.pipeline import get_client, COLLECTION_NAME

client = get_client()
info = client.get_collection(COLLECTION_NAME)
print(f"Point count: {info.points_count}")