import sys
import hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from core.pipeline import get_model, get_client, get_sparse_model, get_encode_batch_size
from qdrant_client.models import (
    PointStruct, VectorParams, SparseVectorParams, Distance, SparseVector
)
from config import get_config, get_history_collection_name

EMBEDDING_DIM = get_config()["embedding"]["dimension"]
HISTORY_COLLECTION = get_history_collection_name()
DIFF_CHUNK_THRESHOLD = get_config()["chunking"]["diff_chunk_threshold"]


def create_history_collection():
    client = get_client()
    if client.collection_exists(HISTORY_COLLECTION):
        client.delete_collection(HISTORY_COLLECTION)
    client.create_collection(
        collection_name=HISTORY_COLLECTION,
        vectors_config={
            "dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )


def generate_history_id(commit_hash: str, filepath: str, chunk_type: str, index: int) -> int:
    key = f"{commit_hash}::{filepath}::{chunk_type}::{index}"
    hash_bytes = hashlib.md5(key.encode()).digest()
    return int.from_bytes(hash_bytes[:8], byteorder="big")


def _split_diff_if_needed(diff: str):
    from core.pipeline import chunk_text
    if len(diff) > DIFF_CHUNK_THRESHOLD:
        return chunk_text(diff, chunk_size=40, overlap=5)
    return [diff]


def process_commit(commit: dict, changes: list):
    """
    Process ALL file changes for one commit in a single batch.
    changes: list of (status, filepath, diff, old_path, function_changes)
    function_changes: list of (function_name, change_type, body_text)
    Returns number of chunks stored.
    """
    model = get_model()
    sparse_model = get_sparse_model()
    client = get_client()

    pending = []  # list of (id, payload, text)

    for status, filepath, diff, old_path, function_changes in changes:
        base_payload = {
            "commit_hash": commit["hash"],
            "date": commit["date"],
            "author": commit["author"],
            "filepath": filepath,
            "status": status,
            "old_path": old_path,
        }

        # Existing: commit message chunk (once per file changed)
        if commit["subject"].strip():
            pending.append((
                generate_history_id(commit["hash"], filepath, "message", 0),
                {**base_payload, "chunk_type": "message", "text": commit["subject"]},
                commit["subject"],
            ))

        # Existing: file-level diff chunk(s)
        if diff.strip():
            for i, chunk in enumerate(_split_diff_if_needed(diff)):
                if not chunk.strip():
                    continue
                pending.append((
                    generate_history_id(commit["hash"], filepath, "diff", i),
                    {**base_payload, "chunk_type": "diff", "text": chunk},
                    chunk,
                ))

        # NEW: function-level chunks, one per changed function
        for func_name, change_type, body in function_changes:
            if not body.strip():
                continue
            func_id_key = f"func::{func_name}"
            pending.append((
                generate_history_id(commit["hash"], filepath, func_id_key, 0),
                {
                    **base_payload,
                    "chunk_type": "function_change",
                    "function_name": func_name,
                    "function_change_type": change_type,
                    "text": body,
                },
                body,
            ))

    if not pending:
        return 0

    texts = [p[2] for p in pending]
    dense_embeddings = model.encode(texts, batch_size=get_encode_batch_size(), show_progress_bar=False)
    sparse_embeddings = list(sparse_model.embed(texts))

    points = []
    for (point_id, payload, _), dense_vec, sparse_vec in zip(pending, dense_embeddings, sparse_embeddings):
        points.append(PointStruct(
            id=point_id,
            vector={
                "dense": dense_vec.tolist(),
                "sparse": SparseVector(
                    indices=sparse_vec.indices.tolist(),
                    values=sparse_vec.values.tolist(),
                ),
            },
            payload=payload,
        ))

    # Batched, not one upsert for the whole commit: even with chunk_text's
    # max_chars backstop, a commit that touches many files at once (a mass
    # reformat, a vendored dependency bump) can still accumulate enough
    # points to approach Qdrant's request size limit. Batch size is a point
    # count, not a byte count, but at max_chars=4000 per chunk this is a
    # generous margin under the limit even in the worst case.
    UPSERT_BATCH_SIZE = 200
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        client.upsert(collection_name=HISTORY_COLLECTION, points=points[i:i + UPSERT_BATCH_SIZE])
    return len(points)