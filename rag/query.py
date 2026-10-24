"""Query the regulatory knowledge base's local vector index.

Usage:
    python query.py "your question here" [--top-k 8]

Prints the most relevant chunks (with source file, title, date, similarity
score) as JSON to stdout. Intended to be called by an LLM (e.g. a Claude
Code skill) which reads the chunks and synthesizes an answer - this script
only does retrieval, no generation.
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
from sentence_transformers import SentenceTransformer

INDEX_DIR = Path(__file__).resolve().parent / "index"
EMBEDDINGS_FILE = INDEX_DIR / "embeddings.npy"
CHUNKS_FILE = INDEX_DIR / "chunks.json"

EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_model = None


def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def load_index():
    if not EMBEDDINGS_FILE.exists() or not CHUNKS_FILE.exists():
        raise FileNotFoundError(
            f"Index not found at {INDEX_DIR}. Run build_index.py first."
        )
    embeddings = np.load(EMBEDDINGS_FILE)
    chunks = json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))
    return embeddings, chunks


def search(question, top_k=8):
    embeddings, chunks = load_index()
    model = get_model()
    query_vec = model.encode([question], normalize_embeddings=True)[0]

    scores = embeddings @ query_vec  # cosine similarity (embeddings are normalized)
    top_indices = np.argsort(-scores)[:top_k]

    results = []
    for idx in top_indices:
        chunk = chunks[int(idx)]
        results.append(
            {
                "score": round(float(scores[idx]), 4),
                "source_file": chunk["source_file"],
                "title": chunk["title"],
                "date": chunk["date"],
                "doc_reference": chunk["doc_reference"],
                "original_url": chunk["original_url"],
                "needs_review": chunk["needs_review"],
                "text": chunk["text"],
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    results = search(args.question, top_k=args.top_k)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
