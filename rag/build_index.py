"""Build/update a local vector index over the converted regulatory markdown files.

Incremental: tracks each source file's modification time in manifest.json.
On each run, only new or changed files get (re-)chunked and (re-)embedded;
everything else is carried over from the existing index unchanged. Files that
disappeared from the knowledge base folder are dropped from the index too.

The first run (no existing index yet) embeds everything, same as before.

Run this after every bot.py conversion run (new/changed files get re-indexed;
unchanged ones cost nothing).
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import yaml
from sentence_transformers import SentenceTransformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import KB_ROOT as KB_DIR, SYSTEM_DIR  # noqa: E402
INDEX_DIR = SYSTEM_DIR / "rag_index"
EMBEDDINGS_FILE = INDEX_DIR / "embeddings.npy"
CHUNKS_FILE = INDEX_DIR / "chunks.json"
MANIFEST_FILE = INDEX_DIR / "manifest.json"  # source_file -> mtime at last embed

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # small, fast, runs fine on CPU
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2's output size, used for empty-array fallbacks

CHUNK_WORDS = 220
CHUNK_OVERLAP_WORDS = 40


def parse_markdown_file(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)  # the frontmatter's closing line, not a table's |---| row
    if end == -1:
        return {}, text
    frontmatter = yaml.safe_load(text[4:end]) or {}
    return frontmatter, text[end + 5:].strip()


def chunk_table_text(body):
    """Chunk a body that contains markdown tables by whole rows, repeating the
    table's header at the top of every chunk so a row is never cut loose from
    its column names. Non-table lines are chunked as they come."""
    chunks, current, size, header = [], [], 0, []

    def flush():
        nonlocal current, size
        if current:
            has_header = bool(header) and current[0] == header[0]
            chunks.append("\n".join(current if has_header or not header else header + current))
        current, size = [], 0

    lines = body.split("\n")
    for i, line in enumerate(lines):
        is_row = line.startswith("|")
        if is_row and i + 1 < len(lines) and lines[i + 1].startswith("|---"):
            flush()
            header = [line, lines[i + 1]]  # a new table starts here
        elif not is_row:
            if header:
                flush()
            header = []
        words = len(line.split())
        if size + words > CHUNK_WORDS and current:
            flush()
        current.append(line)
        size += words
    flush()
    return [c for c in chunks if c.strip()]


def chunk_text(body):
    if re.search(r"^\|---", body, re.M):
        return chunk_table_text(body)
    words = body.split()
    if not words:
        return []
    chunks = []
    step = CHUNK_WORDS - CHUNK_OVERLAP_WORDS
    for start in range(0, len(words), step):
        chunk_words = words[start : start + CHUNK_WORDS]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


def source_key(path):
    """Path relative to the knowledge-base folder (top-level files keep their plain name)."""
    return path.relative_to(KB_DIR).as_posix()


def kb_markdown_files():
    skip_dirs = {"_system", "snapshots"}
    return sorted(
        p for p in KB_DIR.rglob("*.md")
        if p.name not in ("README.md", "NEEDS_ATTENTION.md") and not (skip_dirs & set(p.relative_to(KB_DIR).parts))
    )


def chunks_for_file(path):
    frontmatter, body = parse_markdown_file(path)
    body = re.sub(r"\n{3,}", "\n\n", body)
    pieces = chunk_text(body)
    return [
        {
            "text": piece,
            "source_file": source_key(path),
            "type": frontmatter.get("type") or frontmatter.get("source", ""),
            "title": frontmatter.get("title") or frontmatter.get("substation") or frontmatter.get("application_id", ""),
            "date": frontmatter.get("date", ""),
            "doc_reference": frontmatter.get("doc_reference", ""),
            "original_url": frontmatter.get("original_url", ""),
            "needs_review": frontmatter.get("needs_review", False),
            "chunk_index": i,
        }
        for i, piece in enumerate(pieces)
    ]


def load_existing_index():
    if EMBEDDINGS_FILE.exists() and CHUNKS_FILE.exists() and MANIFEST_FILE.exists():
        embeddings = np.load(EMBEDDINGS_FILE)
        chunks = json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        return embeddings, chunks, manifest
    return np.zeros((0, EMBEDDING_DIM), dtype=np.float32), [], {}


def build():
    md_files = kb_markdown_files()
    print(f"Found {len(md_files)} markdown files in {KB_DIR}")

    existing_embeddings, existing_chunks, manifest = load_existing_index()
    current_mtimes = {source_key(p): p.stat().st_mtime for p in md_files}

    changed_files = [
        p for p in md_files
        if source_key(p) not in manifest or manifest[source_key(p)] != current_mtimes[source_key(p)]
    ]
    removed_names = set(manifest) - set(current_mtimes)

    if not changed_files and not removed_names:
        print("No new or changed files - index is already up to date.")
        return

    print(f"{len(changed_files)} new/changed file(s), {len(removed_names)} removed file(s).")

    stale_names = {source_key(p) for p in changed_files} | removed_names
    keep_mask = np.array([c["source_file"] not in stale_names for c in existing_chunks], dtype=bool)
    kept_chunks = [c for c, keep in zip(existing_chunks, keep_mask) if keep]
    kept_embeddings = existing_embeddings[keep_mask] if len(existing_chunks) else existing_embeddings

    new_chunks = []
    for p in changed_files:
        new_chunks.extend(chunks_for_file(p))

    if new_chunks:
        print(f"Embedding {len(new_chunks)} chunk(s) from {len(changed_files)} file(s)...")
        model = SentenceTransformer(EMBEDDING_MODEL)
        texts = [c["text"] for c in new_chunks]
        new_embeddings = np.asarray(
            model.encode(texts, show_progress_bar=True, batch_size=32, normalize_embeddings=True),
            dtype=np.float32,
        )
    else:
        new_embeddings = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    all_chunks = kept_chunks + new_chunks
    all_embeddings = (
        np.vstack([kept_embeddings, new_embeddings]) if (len(kept_chunks) or len(new_chunks)) else new_embeddings
    )

    for name in removed_names:
        manifest.pop(name, None)
    for p in changed_files:
        manifest[source_key(p)] = current_mtimes[source_key(p)]

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(EMBEDDINGS_FILE, all_embeddings)
    CHUNKS_FILE.write_text(json.dumps(all_chunks, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Saved index: {EMBEDDINGS_FILE} ({all_embeddings.shape}), {CHUNKS_FILE} ({len(all_chunks)} chunks)")


if __name__ == "__main__":
    build()
