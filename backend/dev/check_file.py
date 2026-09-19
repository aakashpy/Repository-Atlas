import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # dev/ tools import from backend/ proper

from core.pipeline import get_client, COLLECTION_NAME
from qdrant_client.models import Filter, FieldCondition, MatchText

client = get_client()
query_text = sys.argv[1] if len(sys.argv) > 1 else "README"

results, _ = client.scroll(
    collection_name=COLLECTION_NAME,
    scroll_filter=Filter(
        must=[FieldCondition(key="filepath", match=MatchText(text=query_text))]
    ),
    limit=20,
)

for point in results:
    print(point.payload["filepath"])