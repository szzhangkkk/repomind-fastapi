"""Build Milvus Lite index for FastAPI source code.

Usage:
    python pe/build_index.py

Scans all .py files under fastapi/, extracts function/class definitions via AST,
embeds with BGE-base-zh-v1.5, and stores in Milvus Lite collection 'fastapi_chunks'.
"""

import ast
import sys
import time
from pathlib import Path

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
    utility,
)
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FASTAPI_DIR = PROJECT_ROOT / "fastapi"
PE_DIR = PROJECT_ROOT / "pe"
MILVUS_DB = PE_DIR / "milvus_lite.db"
COLLECTION_NAME = "fastapi_chunks"
EMBED_DIM = 768  # bge-base-zh-v1.5
MAX_SOURCE_CHARS = 60000  # fit within Milvus VARCHAR(65535)

# ---------------------------------------------------------------------------
# Step 1: AST-based chunking
# ---------------------------------------------------------------------------

# Sentinel for module-level docstring detection
_MODULE_DOCSTRING_SENTINEL = "__module_docstring__"


def _get_imports(filepath: Path) -> str:
    """Extract import statements from a Python file (lines 1..last_import)."""
    src = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    last_import_line = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_line = max(last_import_line, node.end_lineno or node.lineno)
    if last_import_line == 0:
        return ""
    lines = src.splitlines()
    return "\n".join(lines[:last_import_line])


def _get_source_lines(filepath: Path, start: int, end: int) -> str:
    """Extract source lines from file (1-indexed, inclusive).

    Truncates to MAX_SOURCE_CHARS to fit Milvus VARCHAR(65535) limit.
    """
    lines = filepath.read_text(encoding="utf-8").splitlines()
    selected = lines[start - 1 : end]
    src = "\n".join(selected)
    if len(src) > MAX_SOURCE_CHARS:
        src = src[:MAX_SOURCE_CHARS] + "\n# ... [truncated]"
    return src


def _get_docstring_summary(node: ast.AST) -> str:
    """Get first line of the docstring from a function/class body."""
    body = getattr(node, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, (ast.Constant, ast.Str)):
        doc = body[0].value.value if isinstance(body[0].value, ast.Constant) else body[0].value.s
        if doc:
            # Return first non-empty line
            for line in doc.strip().splitlines():
                stripped = line.strip()
                if stripped:
                    return stripped
    return ""


def _collect_nodes(nodes) -> list:
    """Collect all FunctionDef, AsyncFunctionDef, ClassDef nodes recursively.

    Walks into if/else/try/with blocks to find nested definitions.
    """
    result = []
    for node in list(nodes):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.append(node)
        elif isinstance(node, (ast.If, ast.Try)):
            result.extend(_collect_nodes(node.body))
            if isinstance(node, ast.If):
                result.extend(_collect_nodes(node.orelse))
    return result


def _walk_functions(tree: ast.AST, file_rel: str, filepath: Path, extra_context: str):
    """Walk AST yielding function/class chunks.

    Handles top-level and conditionally-defined (if PYDANTIC_V2) functions.
    """
    chunks = []
    top_nodes = _collect_nodes(ast.iter_child_nodes(tree))

    for node in top_nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunk = _make_chunk(node, file_rel, filepath, extra_context, parent_name=None)
            if chunk:
                chunks.append(chunk)
        elif isinstance(node, ast.ClassDef):
            # Class itself as a chunk
            class_chunk = _make_chunk(node, file_rel, filepath, extra_context, parent_name=None)
            if class_chunk:
                chunks.append(class_chunk)
            # Methods inside the class
            for sub in _collect_nodes(node.body):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chunk = _make_chunk(
                        sub, file_rel, filepath, extra_context, parent_name=node.name
                    )
                    if chunk:
                        chunks.append(chunk)

    return chunks


def _make_chunk(node, file_rel: str, filepath: Path, extra_context: str, parent_name: str | None):
    """Build a single chunk dict from an AST node."""
    name = node.name
    line = node.lineno
    end_line = node.end_lineno or node.lineno
    source = _get_source_lines(filepath, line, end_line)
    doc_summary = _get_docstring_summary(node)
    node_type = type(node).__name__

    if parent_name:
        full_name = f"{parent_name}.{name}"
        id_str = f"{file_rel}:{line}"
    else:
        full_name = name
        id_str = f"{file_rel}:{line}"

    # Include source code in text_for_embedding for better discrimination
    # BGE-base max length is 512 tokens; limit source to ~1500 chars (~375 tokens)
    source_excerpt = source[:1500]
    text_for_embedding = f"{full_name}({file_rel}:{line})"
    if doc_summary:
        text_for_embedding += f": {doc_summary}"
    text_for_embedding += f"\n{source_excerpt}"

    return {
        "id": id_str,
        "file": file_rel,
        "line": line,
        "name": full_name,
        "source": source,
        "context": extra_context,
        "text_for_embedding": text_for_embedding,
        "node_type": node_type,
    }


def build_chunks() -> list[dict]:
    """Build all chunks from the FastAPI source."""
    py_files = sorted(FASTAPI_DIR.rglob("*.py"))
    all_chunks = []

    for filepath in py_files:
        rel = filepath.relative_to(PROJECT_ROOT).as_posix()
        extra_context = _get_imports(filepath)
        src = filepath.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            print(f"  [SKIP] Syntax error in {rel}: {e}")
            continue
        chunks = _walk_functions(tree, rel, filepath, extra_context)
        all_chunks.extend(chunks)

    return all_chunks


# ---------------------------------------------------------------------------
# Step 2: Embedding
# ---------------------------------------------------------------------------

def load_embedding_model() -> SentenceTransformer:
    """Load BGE-base-zh-v1.5 model (768 dim) from local cache on /mnt."""
    print("[Embedding] Loading BAAI/bge-base-zh-v1.5 from local cache...")
    model = SentenceTransformer(str(PE_DIR / "model_cache" / "bge-base-zh"))
    dim = model.get_sentence_embedding_dimension()
    print(f"[Embedding] Model dim: {dim}")
    assert dim == EMBED_DIM, f"Dim mismatch: expected {EMBED_DIM}, got {dim}"
    return model


def embed_chunks(model: SentenceTransformer, chunks: list[dict], batch_size: int = 64) -> list[list[float]]:
    """Embed chunk texts in batches."""
    texts = [c["text_for_embedding"] for c in chunks]
    print(f"[Embedding] Encoding {len(texts)} texts (batch_size={batch_size})...")
    # BGE models benefit from adding the instruction prefix for retrieval
    # But since we're just encoding, we pass raw text
    # normalize_embeddings=True for cosine similarity
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    return embeddings.tolist()


# ---------------------------------------------------------------------------
# Step 3: Milvus Lite collection
# ---------------------------------------------------------------------------

def create_milvus_collection() -> Collection:
    """Create/load the 'fastapi_chunks' collection in Milvus Lite."""
    print(f"[Milvus] Database: {MILVUS_DB}")

    # Connect with Milvus Lite (file-based)
    connections.connect(
        alias="default",
        uri=str(MILVUS_DB),
    )

    # Drop existing collection if it exists
    if utility.has_collection(COLLECTION_NAME):
        utility.drop_collection(COLLECTION_NAME)
        print(f"[Milvus] Dropped existing collection '{COLLECTION_NAME}'")

    # Define schema
    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
        FieldSchema(name="file", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="line", dtype=DataType.INT64),
        FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name="context", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name="text_for_embedding", dtype=DataType.VARCHAR, max_length=4096),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBED_DIM),
    ]
    schema = CollectionSchema(fields, description="FastAPI source code chunks")
    collection = Collection(name=COLLECTION_NAME, schema=schema)

    print(f"[Milvus] Created collection '{COLLECTION_NAME}' with dim={EMBED_DIM}")

    # Create IVF_FLAT index on embedding field for ANN search
    index_params = {
        "metric_type": "COSINE",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128},
    }
    collection.create_index(field_name="embedding", index_params=index_params)
    print(f"[Milvus] Created IVF_FLAT index (cosine)")

    return collection


def insert_chunks(collection: Collection, chunks: list[dict], embeddings: list[list[float]]):
    """Insert chunks with embeddings into Milvus collection."""
    entities = [
        [c["id"] for c in chunks],
        [c["file"] for c in chunks],
        [c["line"] for c in chunks],
        [c["name"] for c in chunks],
        [c["source"] for c in chunks],
        [c["context"] for c in chunks],
        [c["text_for_embedding"] for c in chunks],
        embeddings,
    ]
    collection.insert(entities)
    collection.flush()
    print(f"[Milvus] Inserted {len(chunks)} entities")


# ---------------------------------------------------------------------------
# Step 4: Verification
# ---------------------------------------------------------------------------

def verify_search(collection: Collection):
    """Verify that key functions can be retrieved."""
    collection.load()

    test_cases = [
        ("get_dependant", "get_dependant"),
        ("analyze_param", "analyze_param"),
        ("get_body_field", "get_body_field"),
    ]

    print("\n[Verify] Searching for key functions...")
    for query, expected_name in test_cases:
        # Use a simple embedding-free search by expression when possible,
        # or do a dummy vector search
        results = collection.query(
            expr=f'name == "{expected_name}"',
            output_fields=["id", "file", "line", "name"],
            limit=1,
        )
        if results:
            r = results[0]
            print(f"  ✓ {expected_name}: {r['id']}")
        else:
            print(f"  ✗ {expected_name}: NOT FOUND")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()

    print("=" * 50)
    print("RepoMind Index Builder v6")
    print("=" * 50)

    # Step 1: Chunk
    print("\n[Step 1] AST chunking...")
    chunks = build_chunks()
    print(f"  Total chunks: {len(chunks)}")

    # Step 2: Embed
    print("\n[Step 2] Embedding...")
    model = load_embedding_model()
    embeddings = embed_chunks(model, chunks)

    # Step 3: Milvus
    print("\n[Step 3] Milvus Lite...")
    collection = create_milvus_collection()
    insert_chunks(collection, chunks, embeddings)

    # Step 4: Verify
    print("\n[Step 4] Verification...")
    verify_search(collection)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. DB: {MILVUS_DB}")
    return chunks


if __name__ == "__main__":
    chunks = main()
    sys.exit(0)
