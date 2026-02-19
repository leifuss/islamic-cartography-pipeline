#!/usr/bin/env python3
"""
RAG chat server for the Islamic Cartography corpus.

Hybrid retrieval: BM25 keyword search + semantic embedding similarity.
Uses layout_elements.json for structure-aware chunking (falls back to
fixed-window chunking when layout data is unavailable).

Usage:
    python scripts/rag_server.py            # runs on http://localhost:8001
    python scripts/rag_server.py --port 8002
    python scripts/rag_server.py --bm25-only  # skip embeddings (fast start)

Endpoints:
    GET  /api/status          → index stats
    POST /api/chat            → {query: str} → Server-Sent Events stream
    POST /api/search          → {query: str, k: int} → top-k chunks (debug)
"""

import json
import os
import re
import sys
import time
import hashlib
from pathlib import Path
from typing import Iterator

import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from rank_bm25 import BM25Okapi

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
TEXTS_ROOT = ROOT / "data" / "texts"
INV_PATH   = ROOT / "data" / "inventory.json"
EMB_CACHE  = ROOT / "data" / ".embedding_cache.npz"

CHUNK_WORDS   = 350   # target words per chunk (fixed-window fallback)
CHUNK_OVERLAP = 50    # words of overlap between chunks
TOP_K         = 6     # chunks to retrieve per query
MIN_SCORE     = 0.0   # minimum hybrid score threshold
MAX_CTX_WORDS = 3000  # max words sent to Claude

# Hybrid search weights (must sum to 1.0)
BM25_WEIGHT = 0.35
EMBED_WEIGHT = 0.65

# Embedding model — multilingual-e5-small handles Arabic/French/German/English
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── Chunking: structure-aware ─────────────────────────────────────────────────

def chunk_from_layout(layout_path: Path, key: str, max_words: int = CHUNK_WORDS,
                      overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Build chunks from layout_elements.json, splitting on section headers."""
    try:
        data = json.loads(layout_path.read_text())
    except Exception:
        return []

    # Collect all pages' elements in order
    elements = []
    for page_key in sorted(data.keys(), key=lambda k: int(k) if k.isdigit() else 0):
        if not page_key.isdigit():
            continue
        for el in data[page_key]:
            el["_page"] = int(page_key)
            elements.append(el)

    if not elements:
        return []

    # Group text between section headers
    sections = []
    current_heading = ""
    current_texts = []
    current_pages = set()

    for el in elements:
        label = el.get("label", "text")
        text = el.get("text", "").strip()
        page = el["_page"]
        if not text:
            continue

        if label == "section_header":
            # Flush current section
            if current_texts:
                sections.append({
                    "heading": current_heading,
                    "text": " ".join(current_texts),
                    "pages": sorted(current_pages),
                })
            current_heading = text
            current_texts = []
            current_pages = set()
        else:
            current_texts.append(text)
            current_pages.add(page)

    # Flush last section
    if current_texts:
        sections.append({
            "heading": current_heading,
            "text": " ".join(current_texts),
            "pages": sorted(current_pages),
        })

    # Convert sections to chunks (sub-chunk if too long)
    chunks = []
    for sec in sections:
        words = sec["text"].split()
        page_str = str(sec["pages"][0]) if sec["pages"] else "1"

        if len(words) <= max_words:
            chunks.append({
                "key": key,
                "page": page_str,
                "text": sec["text"],
                "heading": sec["heading"],
            })
        else:
            # Sub-chunk long sections with overlap
            step = max_words - overlap
            for i in range(0, len(words), step):
                chunk_text = " ".join(words[i:i + max_words])
                if len(chunk_text.split()) < 20:
                    continue
                chunks.append({
                    "key": key,
                    "page": page_str,
                    "text": chunk_text,
                    "heading": sec["heading"],
                })

    return chunks


def chunk_text(text: str, key: str, page: str, chunk_words: int = CHUNK_WORDS,
               overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Fixed-window chunking (fallback when no layout_elements.json)."""
    words = text.split()
    if not words:
        return []
    chunks = []
    step = chunk_words - overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_words])
        if len(chunk.split()) < 20:
            continue
        chunks.append({"key": key, "page": page, "text": chunk})
    return chunks


# ── Index building ─────────────────────────────────────────────────────────────

def load_inventory() -> dict[str, dict]:
    if not INV_PATH.exists():
        return {}
    items = json.loads(INV_PATH.read_text())
    return {it["key"]: it for it in items}


def _chunks_fingerprint(chunks: list[dict]) -> str:
    """Hash chunk texts to detect if re-embedding is needed."""
    h = hashlib.md5()
    for c in chunks:
        h.update(c["text"][:200].encode("utf-8", errors="replace"))
    return h.hexdigest()


def build_index(use_embeddings: bool = True) -> tuple[list[dict], "BM25Okapi", dict, np.ndarray | None]:
    """
    Scan all processed docs, build chunks list + BM25 + embedding index.
    Prefers layout_elements.json for semantic chunking; falls back to page_texts.json.
    Returns (chunks, bm25, stats, embeddings_matrix_or_None).
    """
    inventory = load_inventory()
    all_chunks: list[dict] = []
    stats: dict = {"docs": 0, "pages": 0, "chunks": 0, "words": 0,
                   "semantic_chunks": 0, "fallback_chunks": 0}

    for doc_dir in sorted(TEXTS_ROOT.iterdir()):
        if not doc_dir.is_dir():
            continue
        key = doc_dir.name

        inv_item = inventory.get(key, {})
        meta = {
            "title":   inv_item.get("title", key),
            "authors": inv_item.get("authors", ""),
            "year":    inv_item.get("year", ""),
        }

        # Try semantic chunking from layout_elements.json first
        layout_path = doc_dir / "layout_elements.json"
        doc_chunks = []

        if layout_path.exists():
            doc_chunks = chunk_from_layout(layout_path, key)
            if doc_chunks:
                stats["semantic_chunks"] += len(doc_chunks)

        # Fallback to page_texts.json
        if not doc_chunks:
            # Prefer translation for non-English docs
            transl_path = doc_dir / "translation.json"
            pt_path     = doc_dir / "page_texts.json"
            page_texts: dict[str, str] = {}

            if transl_path.exists():
                try:
                    t = json.loads(transl_path.read_text())
                    if t.get("page_texts"):
                        page_texts = t["page_texts"]
                except Exception:
                    pass

            if not page_texts and pt_path.exists():
                try:
                    page_texts = json.loads(pt_path.read_text())
                except Exception:
                    pass

            for page, text in page_texts.items():
                if not isinstance(text, str) or not text.strip():
                    continue
                chunks = chunk_text(text, key, page)
                doc_chunks.extend(chunks)
                stats["pages"] += 1
                stats["words"] += len(text.split())

            stats["fallback_chunks"] += len(doc_chunks)

        # Attach metadata
        for c in doc_chunks:
            c.update(meta)
        all_chunks.extend(doc_chunks)

        if doc_chunks:
            stats["docs"] += 1
        stats["chunks"] += len(doc_chunks)

    # Build BM25 index
    tokenised = [
        re.findall(r"[a-zA-ZÀ-ÿ\u0600-\u06FF']{2,}", c["text"].lower())
        for c in all_chunks
    ]
    bm25 = BM25Okapi(tokenised) if tokenised else None

    # Build embedding index
    embeddings = None
    if use_embeddings and all_chunks:
        embeddings = _build_embeddings(all_chunks)

    mode = "hybrid" if embeddings is not None else "BM25-only"
    print(f"Index ({mode}): {stats['docs']} docs · {stats['chunks']} chunks "
          f"({stats['semantic_chunks']} semantic + {stats['fallback_chunks']} fallback) · "
          f"{stats.get('words', 0):,} words", flush=True)

    return all_chunks, bm25, stats, embeddings


def _build_embeddings(chunks: list[dict]) -> np.ndarray | None:
    """Compute or load cached embeddings for all chunks."""
    fingerprint = _chunks_fingerprint(chunks)

    # Try loading cache
    if EMB_CACHE.exists():
        try:
            cached = np.load(EMB_CACHE, allow_pickle=True)
            if cached["fingerprint"].item() == fingerprint:
                print(f"  Loaded cached embeddings ({len(cached['embeddings'])} vectors)", flush=True)
                return cached["embeddings"]
        except Exception:
            pass

    print(f"  Computing embeddings for {len(chunks)} chunks…", flush=True)
    t0 = time.time()

    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=EMBED_MODEL)
        texts = [c["text"][:512] for c in chunks]  # truncate to model max
        embeddings = np.array(list(model.embed(texts)), dtype=np.float32)

        # Cache to disk
        np.savez_compressed(EMB_CACHE, embeddings=embeddings, fingerprint=fingerprint)
        print(f"  Embeddings: {embeddings.shape} in {time.time()-t0:.1f}s (cached)", flush=True)
        return embeddings
    except ImportError:
        print("  fastembed not installed — running BM25-only", flush=True)
        return None
    except Exception as e:
        print(f"  Embedding error: {e} — running BM25-only", flush=True)
        return None


# ── Retrieval ──────────────────────────────────────────────────────────────────

def _embed_query(query: str) -> np.ndarray | None:
    """Embed a single query string."""
    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=EMBED_MODEL)
        return np.array(list(model.embed([query[:512]]))[0], dtype=np.float32)
    except Exception:
        return None


def retrieve(query: str, chunks: list[dict], bm25: BM25Okapi | None,
             embeddings: np.ndarray | None,
             k: int = TOP_K, min_score: float = MIN_SCORE) -> list[dict]:
    """Hybrid retrieval: weighted combination of BM25 + cosine similarity."""
    if not chunks:
        return []

    n = len(chunks)

    # BM25 scores (normalised to 0-1)
    bm25_scores = np.zeros(n)
    if bm25 is not None:
        tokens = re.findall(r"[a-zA-ZÀ-ÿ\u0600-\u06FF']{2,}", query.lower())
        if tokens:
            raw = bm25.get_scores(tokens)
            max_bm25 = max(raw) if max(raw) > 0 else 1.0
            bm25_scores = np.array(raw) / max_bm25

    # Embedding cosine similarity (already normalised to 0-1 range)
    embed_scores = np.zeros(n)
    if embeddings is not None:
        q_emb = _embed_query(query)
        if q_emb is not None:
            # Cosine similarity (embeddings are already L2-normalised by fastembed)
            sims = embeddings @ q_emb
            # Shift to 0-1 range (cosine ranges from -1 to 1)
            embed_scores = (sims + 1) / 2

    # Weighted hybrid score
    if embeddings is not None:
        combined = BM25_WEIGHT * bm25_scores + EMBED_WEIGHT * embed_scores
    else:
        combined = bm25_scores

    # Top-k with deduplication
    top_idx = np.argsort(combined)[::-1]
    results = []
    seen_keys = set()
    for i in top_idx:
        if combined[i] < min_score:
            break
        c = chunks[i]
        uid = (c["key"], c["page"])
        if uid in seen_keys:
            continue
        seen_keys.add(uid)
        results.append({**c, "score": float(combined[i]),
                        "bm25": float(bm25_scores[i]),
                        "semantic": float(embed_scores[i])})
        if len(results) >= k:
            break

    return results


def format_context(hits: list[dict], max_words: int = MAX_CTX_WORDS) -> str:
    parts = []
    total = 0
    for h in hits:
        author = h.get("authors", "") or h["key"]
        year   = h.get("year", "n.d.")
        doc_label = f"[{author}, {year}, p.{h['page']}]"
        chunk_words = len(h["text"].split())
        if total + chunk_words > max_words:
            remaining = max_words - total
            if remaining < 50:
                break
            snippet = " ".join(h["text"].split()[:remaining])
            parts.append(f"{doc_label}\n{snippet}…")
            break
        parts.append(f"{doc_label}\n{h['text']}")
        total += chunk_words
    return "\n\n---\n\n".join(parts)


# ── LLM streaming (Gemini / OpenAI / Anthropic — uses first available key) ─────

SYSTEM_PROMPT = """\
You are a research assistant specialising in Islamic cartography, historical geography,
and medieval Arabic/Persian geographical literature. Answer questions using ONLY the
provided corpus excerpts. Cite sources inline using Harvard author-date-page format,
e.g. (Tibbetts, 1992, p.14) — match exactly the author and year shown in the excerpt
labels. If the excerpts do not contain enough information to answer, say so clearly.
Be concise but scholarly."""


def _load_env_key(var: str) -> str:
    """Return key from os.environ, falling back to .env file."""
    val = os.environ.get(var, "")
    if val:
        return val
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _stream_gemini(user_msg: str) -> Iterator[str]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_load_env_key("GEMINI_API_KEY"))
    for chunk in client.models.generate_content_stream(
        model="gemini-2.0-flash",
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=1024,
        ),
    ):
        if chunk.text:
            yield chunk.text


def _stream_openai(user_msg: str) -> Iterator[str]:
    from openai import OpenAI
    client = OpenAI(api_key=_load_env_key("OPENAI_API_KEY"))
    with client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        stream=True,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
    ) as stream:
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text:
                yield text


def _stream_anthropic(user_msg: str) -> Iterator[str]:
    import anthropic
    client = anthropic.Anthropic(api_key=_load_env_key("ANTHROPIC_API_KEY"))
    with client.messages.stream(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def stream_llm(query: str, context: str, sources: list[dict]) -> Iterator[str]:
    """Auto-select provider based on which API key is available."""
    source_list = [{"key": h["key"], "page": h["page"],
                    "title": h.get("title", h["key"]),
                    "authors": h.get("authors", ""),
                    "year": h.get("year", ""),
                    "score": round(h["score"], 3)}
                   for h in sources]
    yield f"data: {json.dumps({'type': 'sources', 'sources': source_list})}\n\n"

    user_msg = f"Corpus excerpts:\n\n{context}\n\n---\n\nQuestion: {query}"

    if _load_env_key("GEMINI_API_KEY"):
        provider, streamer = "Gemini", _stream_gemini
    elif _load_env_key("OPENAI_API_KEY"):
        provider, streamer = "OpenAI", _stream_openai
    elif _load_env_key("ANTHROPIC_API_KEY"):
        provider, streamer = "Anthropic", _stream_anthropic
    else:
        yield f"data: {json.dumps({'type': 'token', 'text': 'Error: no API key found. Set GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY in .env'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    print(f"  → using {provider}", flush=True)

    try:
        for text in streamer(user_msg):
            yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'token', 'text': f'Error ({provider}): {e}'})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="Islamic Cartography RAG")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Deferred index build
CHUNKS: list[dict] = []
BM25_INDEX: BM25Okapi | None = None
EMBEDDINGS: np.ndarray | None = None
INDEX_STATS: dict = {}
_INDEX_BUILT = False
_USE_EMBEDDINGS = True


def _ensure_index():
    global CHUNKS, BM25_INDEX, EMBEDDINGS, INDEX_STATS, _INDEX_BUILT
    if _INDEX_BUILT:
        return
    CHUNKS, BM25_INDEX, INDEX_STATS, EMBEDDINGS = build_index(use_embeddings=_USE_EMBEDDINGS)
    _INDEX_BUILT = True


@app.on_event("startup")
def startup():
    _ensure_index()


@app.get("/api/status")
def status():
    _ensure_index()
    return {
        "status": "ok",
        "index": INDEX_STATS,
        "mode": "hybrid" if EMBEDDINGS is not None else "bm25",
    }


@app.post("/api/search")
def search(body: dict):
    _ensure_index()
    query = body.get("query", "")
    k     = int(body.get("k", TOP_K))
    hits  = retrieve(query, CHUNKS, BM25_INDEX, EMBEDDINGS, k=k)
    return {"query": query, "mode": "hybrid" if EMBEDDINGS is not None else "bm25",
            "hits": [
        {"key": h["key"], "page": h["page"], "score": h["score"],
         "bm25": h.get("bm25", 0), "semantic": h.get("semantic", 0),
         "snippet": h["text"][:200]}
        for h in hits
    ]}


@app.post("/api/chat")
def chat(body: dict):
    _ensure_index()
    query = body.get("query", "").strip()
    if not query:
        return {"error": "empty query"}
    hits    = retrieve(query, CHUNKS, BM25_INDEX, EMBEDDINGS)
    context = format_context(hits)
    return StreamingResponse(
        stream_llm(query, context, hits),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static files (data/) — must be mounted after API routes ────────────────────
app.mount("/", StaticFiles(directory=str(ROOT / "data"), html=True), name="static")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bm25-only", action="store_true",
                        help="Skip embedding computation (faster startup)")
    args = parser.parse_args()
    _USE_EMBEDDINGS = not args.bm25_only
    uvicorn.run(app, host=args.host, port=args.port)
