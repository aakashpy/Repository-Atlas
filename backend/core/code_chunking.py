"""
Tree-sitter-based, multi-language chunking. Generalizes pipeline.py's old
Python-ast-only chunk_python_file() to any language tree-sitter-language-pack
supports: same granularity (only TOP-LEVEL definitions become their own
chunk -- a class's methods stay inside its one chunk, exactly like the old
ast-based chunker), but works across languages instead of being hard-wired
to Python's own `ast` module.

Node-type names below were verified empirically against real parses (see
docs/DECISIONS.md) rather than assumed from memory -- tree-sitter grammar
internals (which field holds a name, how exports/decorators wrap a
definition) vary in real ways worth confirming.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config`

from tree_sitter_language_pack import get_parser
from config import get_config

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

CHUNK_NODE_TYPES = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "javascript": {
        "function_declaration", "class_declaration", "lexical_declaration", "export_statement",
        "method_definition",  # nested inside a class_body -- never a direct top-level child, only found when splitting an oversized class
    },
    "typescript": {
        "function_declaration", "class_declaration", "interface_declaration",
        "type_alias_declaration", "lexical_declaration", "export_statement",
        "method_definition",
    },
    "tsx": {
        "function_declaration", "class_declaration", "interface_declaration",
        "type_alias_declaration", "lexical_declaration", "export_statement",
        "method_definition",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "java": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "method_declaration",  # nested inside a class_body -- same reasoning as JS's method_definition above
    },
    "c": {"function_definition", "struct_specifier", "enum_specifier", "union_specifier"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier", "namespace_definition"},
}


def _get_language_for_file(filepath: str):
    return LANGUAGE_BY_EXTENSION.get(Path(filepath).suffix.lower())


def _first_child_of_type(node, types):
    for child in node.children:
        if child.type in types:
            return child
    return None


def _find_first_identifier(node):
    """DFS for the first identifier-like node -- used for C/C++ function
    names, which live inside a (possibly pointer-wrapped) declarator rather
    than a simple 'name' field."""
    if node is None:
        return None
    if node.type in ("identifier", "type_identifier", "field_identifier"):
        return node.text.decode()
    for child in node.children:
        found = _find_first_identifier(child)
        if found:
            return found
    return None


def _extract_name(node):
    name_field = node.child_by_field_name("name")
    if name_field is not None:
        return name_field.text.decode()

    if node.type == "decorated_definition":
        inner = _first_child_of_type(node, ("function_definition", "class_definition"))
        return _extract_name(inner) if inner else None

    if node.type == "export_statement":
        inner = _first_child_of_type(
            node,
            ("function_declaration", "class_declaration", "interface_declaration",
             "type_alias_declaration", "lexical_declaration"),
        )
        return _extract_name(inner) if inner else None

    if node.type == "lexical_declaration":
        declarator = _first_child_of_type(node, ("variable_declarator",))
        if declarator is not None:
            name_field = declarator.child_by_field_name("name")
            return name_field.text.decode() if name_field else None
        return None

    if node.type == "type_declaration":  # Go: type X struct {...}
        spec = _first_child_of_type(node, ("type_spec",))
        if spec is not None:
            name_field = spec.child_by_field_name("name")
            return name_field.text.decode() if name_field else None
        return None

    if node.type == "function_definition":  # C/C++: name is inside 'declarator'
        return _find_first_identifier(node.child_by_field_name("declarator"))

    return None


def _char_slice(text: str, max_chars: int):
    """Hard backstop: raw fixed-size slicing, no syntax awareness at all --
    same trick pipeline.py::chunk_text() uses for oversized diffs. Only
    reached once there's no further tree structure left to split on."""
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] if text else []


def _find_nested_definitions(node, node_types):
    """
    DFS for descendant nodes (not `node` itself) whose type is one of this
    language's own CHUNK_NODE_TYPES -- e.g. a method inside a class body.
    Several grammars reuse the exact same node type for a nested definition
    as for a top-level one (Python's function_definition/decorated_definition,
    C/C++'s function_definition); others use a distinct nested-only type
    (JS/TS's method_definition, Java's method_declaration) that's already
    folded into the same per-language set above -- verified empirically
    against real parses, not assumed (see docs/DECISIONS.md). Doesn't
    recurse into a node once it's matched, so this only ever splits one
    level deeper than the oversized chunk itself.
    """
    found = []

    def walk(n):
        for child in n.children:
            if child.type in node_types:
                found.append(child)
            else:
                walk(child)

    walk(node)
    found.sort(key=lambda n: n.start_point[0])
    return found


def _split_oversized_chunk(node, lines, node_types, max_chars, enclosing_name):
    """
    Splits one top-level chunk whose full text already exceeded max_chars
    into smaller pieces -- e.g. a class with many methods, all bundled into
    a single chunk today, which then gets silently truncated by both the
    embedding model and the reranker (measured: see docs/DECISIONS.md,
    "Reranker's Own Assumptions Validated"):

    1. Find nested definitions inside it (methods inside the class).
    2. One piece per nested definition, labeled with the enclosing
       definition's name for context, plus one piece for whatever comes
       before the first nested definition (signature/docstring/fields).
    3. Any piece still over max_chars (a single huge method, or no nested
       definitions found at all for this language/construct) gets sliced
       into raw fixed-size pieces as a final backstop.

    Returns a list of (text, symbol_name) tuples.
    """
    start, end = node.start_point[0], node.end_point[0] + 1
    nested = _find_nested_definitions(node, node_types)

    if not nested:
        full_text = "\n".join(lines[start:end]).strip()
        return [(piece, enclosing_name) for piece in _char_slice(full_text, max_chars)]

    header = f"[Inside {enclosing_name}]\n" if enclosing_name else ""
    pieces = []

    first_start = nested[0].start_point[0]
    if first_start > start:
        head_text = "\n".join(lines[start:first_start]).strip()
        if head_text:
            full = header + head_text
            pieces.extend(
                (piece, enclosing_name)
                for piece in ([full] if len(full) <= max_chars else _char_slice(full, max_chars))
            )

    for i, sub in enumerate(nested):
        sub_start = sub.start_point[0]
        sub_end = nested[i + 1].start_point[0] if i + 1 < len(nested) else end
        sub_text = "\n".join(lines[sub_start:sub_end]).strip()
        if not sub_text:
            continue
        sub_name = _extract_name(sub) or enclosing_name
        full = header + sub_text
        if len(full) <= max_chars:
            pieces.append((full, sub_name))
        else:
            pieces.extend((piece, sub_name) for piece in _char_slice(full, max_chars))

    return pieces


def chunk_code_file(filepath: str, content: str):
    """
    Returns a list of {"text": str, "symbol_name": str | None} dicts, or
    None if this file's language isn't supported / it fails to parse --
    callers should fall back to the generic line-window chunker in that case.
    """
    language = _get_language_for_file(filepath)
    if language is None:
        return None

    try:
        parser = get_parser(language)
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return None

    max_chars = get_config()["chunking"].get("max_chunk_chars", 4000)
    lines = content.splitlines()
    node_types = CHUNK_NODE_TYPES.get(language, set())
    chunks = []
    covered_lines = set()

    for node in tree.root_node.children:
        if node.type in node_types:
            start, end = node.start_point[0], node.end_point[0] + 1
            text = "\n".join(lines[start:end]).strip()
            if not text:
                continue
            covered_lines.update(range(start, end))
            name = _extract_name(node)
            if len(text) <= max_chars:
                chunks.append({"text": text, "symbol_name": name})
            else:
                for piece_text, piece_name in _split_oversized_chunk(node, lines, node_types, max_chars, name):
                    if piece_text.strip():
                        chunks.append({"text": piece_text, "symbol_name": piece_name})

    leftover_lines = [line for i, line in enumerate(lines) if i not in covered_lines]
    leftover_text = "\n".join(leftover_lines).strip()
    if leftover_text:
        if len(leftover_text) <= max_chars:
            chunks.append({"text": leftover_text, "symbol_name": None})
        else:
            for piece_text in _char_slice(leftover_text, max_chars):
                chunks.append({"text": piece_text, "symbol_name": None})

    return chunks
