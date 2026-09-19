import sys
import ast
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config` and sibling packages

from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from fastembed import SparseTextEmbedding
from qdrant_client.models import SparseVectorParams, SparseVector, VectorParams, Distance
import hashlib
from config import get_config, get_current_collection_name
from core.code_chunking import chunk_code_file
from core.symbol_index import find_referenced_symbols
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

def _cfg():
    return get_config()

COLLECTION_NAME = get_current_collection_name()
EMBEDDING_DIM = _cfg()["embedding"]["dimension"]

_model = None
_client = None


def generate_chunk_id(filepath: str, chunk_index: int) -> int:
    """
    Deterministic ID based on filepath + chunk position, so re-indexing
    the same file always maps to the same IDs instead of colliding
    with unrelated chunks.
    """
    key = f"{filepath}::{chunk_index}"
    hash_bytes = hashlib.md5(key.encode()).digest()
    return int.from_bytes(hash_bytes[:8], byteorder="big")

_sparse_model = None

def get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model

def get_model():
    global _model
    if _model is None:
        cfg = _cfg()["embedding"]
        _model = SentenceTransformer(cfg["model_name"], device="cuda")
        max_seq_length = cfg.get("max_seq_length")
        if max_seq_length:
            # Some models (e.g. Qwen3-Embedding, 32k context) will otherwise
            # attend over pathologically long chunks (a whole test class can
            # run to 20k+ tokens) at full quadratic cost -- verified this
            # stalls/nearly exhausts VRAM without a cap on constrained GPUs.
            _model.max_seq_length = max_seq_length
    return _model


def get_query_prompt_name():
    """
    Asymmetric embedding models (e.g. Qwen3-Embedding) need queries encoded
    with an instruction prefix but documents encoded plain. None for models
    with no named prompts (e.g. all-MiniLM-L6-v2) -- passing prompt_name=None
    to .encode() is equivalent to omitting it, so callers can pass this
    through unconditionally.
    """
    return _cfg()["embedding"].get("query_prompt_name")


def get_encode_batch_size():
    return _cfg()["embedding"].get("encode_batch_size", 32)


_reranker = None


def get_reranker():
    """
    Lazily loads the configured cross-encoder, or returns None if reranking
    is disabled. A cross-encoder scores a (query, candidate) pair jointly --
    slower and more precise than comparing independently-computed embedding
    vectors -- which is exactly why it's only ever applied to the modest
    candidate pool hybrid search has already narrowed things down to, not
    the whole collection.
    """
    global _reranker
    cfg = _cfg().get("reranking", {})
    if not cfg.get("enabled", True):
        return None
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(
            cfg.get("model_name", "cross-encoder/ms-marco-MiniLM-L-6-v2"), device="cuda"
        )
    return _reranker


def get_reranking_candidate_pool():
    return _cfg().get("reranking", {}).get("candidate_pool", 20)


def rerank(query: str, results: list) -> list:
    """
    Re-scores a hybrid-search candidate pool with the cross-encoder and
    returns it sorted by that score. Degrades to the original (RRF) order
    -- never raises -- if reranking is disabled or fails for any reason,
    same resilience pattern as history search degrading to current-state-
    only results rather than crashing the whole query.
    """
    reranker = get_reranker()
    if not reranker or not results:
        return results
    try:
        pairs = [(query, r["text"]) for r in results]
        scores = reranker.predict(pairs)
        for r, s in zip(results, scores):
            r["score"] = float(s)
        return sorted(results, key=lambda r: r["score"], reverse=True)
    except Exception as e:
        print(f"Warning: reranking failed ({e}); using original hybrid ranking.")
        return results


def get_client():
    global _client
    if _client is None:
        cfg = _cfg()["qdrant"]
        _client = QdrantClient(host=cfg["host"], port=cfg["port"])
    return _client


_MD_BADGE_LINE = re.compile(r'^\s*(\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\))\s*$')
_RST_IMAGE_DIRECTIVE = re.compile(r'^\s*\.\.\s+(image|figure)::')
_RST_DIRECTIVE_OPTION = re.compile(r'^\s+:\w+:')


def _strip_badge_noise(content: str) -> str:
    """
    Drop lines that are pure badge/image markup (PyPI/CI/coverage badges,
    RST image directives). These carry no descriptive meaning but their
    URL-heavy text dilutes the embedding of the real prose around them.
    """
    lines = content.splitlines()
    kept = []
    skip_directive_options = False
    for line in lines:
        if _RST_IMAGE_DIRECTIVE.match(line):
            skip_directive_options = True
            continue
        if skip_directive_options:
            if _RST_DIRECTIVE_OPTION.match(line):
                continue
            skip_directive_options = False
        if _MD_BADGE_LINE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


_MD_HEADING = re.compile(r'^(#{1,6})\s+\S')
_RST_UNDERLINE_CHARS = set('=-~^"*+#`:.\'_')


def _rst_heading_underline(line: str, next_line: str) -> bool:
    if not line.strip() or line.startswith(" ") or line.startswith("\t"):
        return False
    stripped_next = next_line.strip()
    if len(stripped_next) < 3:
        return False
    chars = set(stripped_next)
    if len(chars) != 1 or next(iter(chars)) not in _RST_UNDERLINE_CHARS:
        return False
    return len(stripped_next) >= len(line.strip()) * 0.5


def _split_into_sections(content: str):
    """Split Markdown/RST content into (heading_text_or_None, lines) sections."""
    lines = content.splitlines()
    sections = []
    heading = None
    current = []

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        md_match = _MD_HEADING.match(line)
        is_rst = i + 1 < n and _rst_heading_underline(line, lines[i + 1])

        if md_match or is_rst:
            if current:
                sections.append((heading, current))
            heading = line.strip().lstrip("#").strip()
            current = [line]
            if is_rst:
                current.append(lines[i + 1])
                i += 1
        else:
            current.append(line)
        i += 1

    if current:
        sections.append((heading, current))

    # merge trivial leading fragments (e.g. a bare RST ".. _label:" anchor
    # before the first real heading) into the section that follows, instead
    # of leaving them as their own near-empty, spuriously-matching chunk.
    merged = []
    carry = []
    for h, sec_lines in sections:
        text = "\n".join(sec_lines).strip()
        if h is None and len(text) < 30:
            carry.extend(sec_lines)
            continue
        if carry:
            sec_lines = carry + sec_lines
            carry = []
        merged.append((h, sec_lines))
    if carry:
        if merged:
            h, l = merged[-1]
            merged[-1] = (h, l + carry)
        else:
            merged.append((None, carry))

    return merged


def chunk_prose(content: str, max_chunk_size: int = None):
    """
    Section-aware chunking for Markdown/RST docs. Splits on headers so each
    chunk stays within one coherent section instead of an arbitrary line
    window cutting through the middle of an example or explanation.
    Oversized sections are split by paragraph (blank-line separated),
    grouped up to max_chunk_size lines, with the section heading carried
    forward as context so a fragment still knows what section it's from.
    """
    cfg = _cfg()["chunking"]
    max_chunk_size = max_chunk_size or cfg.get("prose_max_chunk_size", 70)

    content = _strip_badge_noise(content)
    sections = _split_into_sections(content)

    if len(sections) <= 1 and sections and sections[0][0] is None:
        return chunk_text(content)

    chunks = []
    for heading, section_lines in sections:
        text = "\n".join(section_lines).strip()
        if not text:
            continue
        if len(section_lines) <= max_chunk_size:
            chunks.append(text)
            continue

        paragraphs = re.split(r'\n\s*\n', "\n".join(section_lines))
        group, group_len = [], 0
        for para in paragraphs:
            para_len = len(para.splitlines())
            if group and group_len + para_len > max_chunk_size:
                group_text = "\n\n".join(group)
                if heading and heading not in group_text:
                    group_text = f"{heading}\n{group_text}"
                chunks.append(group_text)
                group, group_len = [], 0
            group.append(para)
            group_len += para_len
        if group:
            group_text = "\n\n".join(group)
            if heading and heading not in group_text:
                group_text = f"{heading}\n{group_text}"
            chunks.append(group_text)

    return chunks


def chunk_text(content: str, chunk_size: int = None, overlap: int = None):
    cfg = _cfg()["chunking"]
    chunk_size = chunk_size or cfg["text_chunk_size"]
    overlap = overlap or cfg["text_chunk_overlap"]
    max_chars = cfg.get("max_chunk_chars", 4000)
    lines = content.splitlines()
    chunks = []
    start = 0
    while start < len(lines):
        end = start + chunk_size
        chunk = "\n".join(lines[start:end])
        # A line-count window doesn't bound byte size: a handful of
        # pathologically long lines (a binary file diffed as text, a
        # minified bundle, a generated data dump -- all realistic in any
        # real codebase) can make one "40-line chunk" many megabytes,
        # oversized both for embedding and for a single Qdrant upsert
        # request (verified directly: a vendored .ai binary's diff hit
        # this and crashed history_ingest.py with a 400 "payload too
        # large" error). Slice any chunk over max_chars into raw
        # character-length pieces as a hard backstop.
        if len(chunk) > max_chars:
            chunks.extend(chunk[i:i + max_chars] for i in range(0, len(chunk), max_chars))
        else:
            chunks.append(chunk)
        start = end - overlap
    return chunks


def chunk_file(filepath: str, content: str):
    """
    Dispatches to the right chunker and normalizes every result to the same
    {"text", "symbol_name"} shape, so embed_and_upsert_file doesn't need to
    know which chunker produced a given chunk. Docs (.md/.rst) never have a
    symbol_name; code files get one via tree-sitter when the language is
    supported and parsing succeeds, else fall back to the generic
    line-window chunker (also symbol_name=None).
    """
    if filepath.endswith((".md", ".rst")):
        return [{"text": c, "symbol_name": None} for c in chunk_prose(content)]

    code_chunks = chunk_code_file(filepath, content)
    if code_chunks is not None:
        return code_chunks

    return [{"text": c, "symbol_name": None} for c in chunk_text(content)]


def delete_file_chunks(filepath: str):
    """Remove all existing chunks for a given filepath from Qdrant."""
    client = get_client()
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="filepath", match=MatchValue(value=filepath))]
        ),
    )


def embed_and_upsert_file(filepath: str, content: str, symbol_index: dict = None):
    """
    Chunk, embed (dense + sparse), and store a file's content in Qdrant.
    symbol_index (from symbol_index.build_symbol_index), when given, tags
    each chunk with the names of other repo-defined symbols it references
    -- resolved to actual definitions later, live, at generation time
    (search.py::_expand_referenced_symbols), not baked in here.
    """
    model = get_model()
    sparse_model = get_sparse_model()
    client = get_client()

    chunks = chunk_file(filepath, content)
    valid_chunks = [c for c in chunks if c["text"].strip()]

    if not valid_chunks:
        return

    texts = [c["text"] for c in valid_chunks]
    dense_embeddings = model.encode(texts, batch_size=get_encode_batch_size(), show_progress_bar=False)
    sparse_embeddings = list(sparse_model.embed(texts))

    points = []
    for i, chunk in enumerate(valid_chunks):
        sparse = sparse_embeddings[i]
        payload = {
            "filepath": filepath,
            "text": chunk["text"],
            "symbol_name": chunk["symbol_name"],
            "referenced_symbols": find_referenced_symbols(
                chunk["text"], chunk["symbol_name"], symbol_index
            ) if symbol_index else [],
        }
        points.append(
            PointStruct(
                id=generate_chunk_id(filepath, i),
                vector={
                    "dense": dense_embeddings[i].tolist(),
                    "sparse": SparseVector(
                        indices=sparse.indices.tolist(),
                        values=sparse.values.tolist(),
                    ),
                },
                payload=payload,
            )
        )

    client.upsert(collection_name=COLLECTION_NAME, points=points)

def create_hybrid_collection():
    client = get_client()

    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )

def get_function_map(content: str) -> dict:
    """
    Parse Python content and return {function_or_class_name: body_text}
    for every top-level function/class. Used to compare two versions
    of a file and detect exactly which functions changed.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}

    lines = content.splitlines()
    result = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno - 1
            end = node.end_lineno
            body = "\n".join(lines[start:end])
            result[node.name] = body

    return result