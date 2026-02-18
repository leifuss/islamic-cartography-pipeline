#!/usr/bin/env python3
"""
Batch-extract text from all embedded-font PDFs using parallel persistent
Docling workers.

Each worker loads ML models once, then processes documents from a shared queue —
no per-document model-reload overhead.  OCR is enabled but Docling's auto_ocr
model detects embedded-font pages and largely skips it; PIL's decompression-bomb
limit is raised so high-DPI images don't crash the OCR stage on the few pages
that do trigger it.  Completed saves are overlapped with extraction via a background
thread pool so the main loop is never blocked on disk I/O.

Outputs per document (under --texts-dir/{key}/):
  docling.md          full markdown text
  page_texts.json     {page_no: text} dict (1-based keys)
  meta.json           title, authors, year, page_count, text_quality, …

Results log: data/extract_results.json  (resume-safe, atomic writes)

Usage:
    python scripts/05_extract_embedded.py
    python scripts/05_extract_embedded.py --workers 3
    python scripts/05_extract_embedded.py --workers 1 --limit 10   # test run
"""
import sys
import gc
import os
import json
import time
import atexit
import multiprocessing as mp
import queue as _queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / 'src')
sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv(_ROOT / '.env')


# ── Quality diagnostics ───────────────────────────────────────────────────────

def compute_text_quality(full_text: str, page_texts: dict) -> dict:
    """
    Analyse extracted text for signs of garbled encoding.

    Indicators:
      pua_ratio         — fraction of Private Use Area codepoints (U+E000–U+F8FF).
                          High PUA means the font had custom/unencoded glyphs →
                          text will look like gibberish even though it's "embedded".
      replacement_ratio — fraction of U+FFFD replacement chars (failed decoding).
      chars_per_page    — average chars per page; very low = likely failed extraction.
      empty_pages       — pages with fewer than 50 chars after stripping whitespace.
      text_quality      — summary: "good" | "suspect" | "garbled"

    "Suspect" docs will be flagged for a Vision-API fallback in a later stage.
    "Garbled" docs should be re-classified as scanned and fed to the OCR pipeline.
    """
    n_pages = len(page_texts)
    if not full_text or not n_pages:
        return {
            'chars_per_page':    0.0,
            'pua_ratio':         1.0,
            'replacement_ratio': 1.0,
            'empty_pages':       n_pages,
            'text_quality':      'garbled',
        }

    total_chars = sum(len(t) for t in page_texts.values())
    chars_per_page = total_chars / n_pages

    n = len(full_text) or 1
    # Private Use Area: custom font glyphs that didn't map to Unicode
    pua_count  = sum(1 for c in full_text if '\ue000' <= c <= '\uf8ff')
    repl_count = full_text.count('\ufffd')
    pua_ratio  = pua_count  / n
    repl_ratio = repl_count / n

    empty_pages = sum(1 for t in page_texts.values() if len(t.strip()) < 50)

    if pua_ratio > 0.05 or repl_ratio > 0.05 or chars_per_page < 20:
        quality = 'garbled'
    elif pua_ratio > 0.01 or repl_ratio > 0.01 or chars_per_page < 100:
        quality = 'suspect'
    else:
        quality = 'good'

    return {
        'chars_per_page':    round(chars_per_page, 1),
        'pua_ratio':         round(pua_ratio, 4),
        'replacement_ratio': round(repl_ratio, 4),
        'empty_pages':       empty_pages,
        'text_quality':      quality,
    }


# ── Worker (module-level → picklable under spawn) ─────────────────────────────

def _docling_worker(worker_id: int, task_q: mp.Queue, result_q: mp.Queue):
    """
    Persistent Docling worker — OCR disabled (embedded fonts only).

    Pulls (doc_id, key, pdf_path_str) from task_q.
    Pushes:
      ('ready',    worker_id, None,   None, None)      on init success
      ('init_err', worker_id, None,   None, err_str)   on init failure
      ('started',  worker_id, doc_id, key,  None)      when extraction begins
      ('ok',       worker_id, doc_id, key,  result)    on success
      ('err',      worker_id, doc_id, key,  err_str)   on failure
    """
    sys.path.insert(0, _SRC)
    load_dotenv(_ROOT / '.env')

    # Safety net: even without OCR, Docling may render pages for layout
    # analysis.  Disable PIL's decompression-bomb guard for trusted PDFs.
    try:
        from PIL import Image as _PILImage
        _PILImage.MAX_IMAGE_PIXELS = None
    except ImportError:
        pass

    from extractors.docling_extractor import DoclingExtractor

    try:
        # Use the default converter (OCR enabled but auto-skipped for embedded-font
        # pages by Docling's auto_ocr_model).  Explicitly disabling OCR via
        # PdfFormatOption loads a different (heavier) model set — slower startup.
        # The PIL MAX_IMAGE_PIXELS fix above is the critical guard for large pages.
        ext = DoclingExtractor(do_ocr=True)
        result_q.put(('ready', worker_id, None, None, None))
    except Exception as e:
        result_q.put(('init_err', worker_id, None, None, str(e)))
        return

    while True:
        msg = task_q.get()
        if msg is None:
            break
        doc_id, key, pdf_path_str = msg
        result_q.put(('started', worker_id, doc_id, key, None))
        try:
            result = ext.extract(Path(pdf_path_str))
            result_q.put(('ok', worker_id, doc_id, key, result))
            del result          # release large extraction result before next doc
        except Exception as e:
            result_q.put(('err', worker_id, doc_id, key, str(e)))
        finally:
            gc.collect()        # reclaim memory now, before loading next document


# ── Save helpers ──────────────────────────────────────────────────────────────

_save_lock = threading.Lock()   # guards result-log writes across save threads


def save_document(key: str, result: dict, meta: dict, texts_dir: Path) -> dict:
    """
    Write markdown, page_texts JSON, and meta JSON (with quality diagnostics).
    Returns a dict of the paths written, suitable for the results log.
    Thread-safe (each key gets its own directory; log writes are locked).
    """
    doc_dir = texts_dir / key
    doc_dir.mkdir(parents=True, exist_ok=True)

    paths = {}

    text = result.get('text') or ''
    if text:
        md_path = doc_dir / 'docling.md'
        md_path.write_text(text, encoding='utf-8')
        paths['docling_md'] = str(md_path)

    page_texts = result.get('page_texts') or {}
    if page_texts:
        pt_path = doc_dir / 'page_texts.json'
        pt_path.write_text(
            json.dumps(page_texts, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        paths['page_texts'] = str(pt_path)

    layout_elements = result.get('layout_elements') or {}
    if layout_elements:
        le_path = doc_dir / 'layout_elements.json'
        le_path.write_text(
            json.dumps(layout_elements, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        paths['layout_elements'] = str(le_path)

    raw_pc     = result.get('metadata', {}).get('page_count')
    page_count = raw_pc() if callable(raw_pc) else raw_pc

    quality = compute_text_quality(text, page_texts)

    meta_out = {
        **meta,
        'page_count':   page_count,
        'confidence':   result.get('confidence'),
        'method':       result.get('method'),
        'do_ocr':       result.get('do_ocr', False),
        'extracted_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        **quality,          # chars_per_page, pua_ratio, … text_quality
    }
    meta_path = doc_dir / 'meta.json'
    meta_path.write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    paths['meta']         = str(meta_path)
    paths['text_quality'] = quality['text_quality']
    return paths


def save_log(results: list, path: Path):
    """Atomic JSON write — safe to call from multiple threads (caller holds lock)."""
    tmp = path.with_suffix('.tmp.json')
    tmp.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers',   type=int, default=1,
                        help='Parallel Docling workers (default 1; each needs ~500MB RAM; '
                             'increase only if you have >2GB free RAM per extra worker)')
    parser.add_argument('--limit',     type=int, default=0,
                        help='Stop after N documents (0 = all)')
    parser.add_argument('--texts-dir', default='data/texts')
    parser.add_argument('--inventory', default='data/inventory.json')
    parser.add_argument('--results',   default='data/extract_results.json')
    parser.add_argument('--timeout',   type=int, default=180,
                        help='Per-document timeout in seconds (default 180; '
                             'OCR-off extractions are much faster)')
    parser.add_argument('--keys', nargs='+', default=[],
                        help='Only process these specific document keys (space-separated)')
    parser.add_argument('--retry-timeouts', action='store_true',
                        help='Also retry previously timed-out documents')
    parser.add_argument('--direct', action='store_true',
                        help='Run Docling in the main process (no subprocess). '
                             'Slower but avoids macOS spawn/ML-library crashes.')
    parser.add_argument('--force', action='store_true',
                        help='Re-process docs even if already marked OK in the results log. '
                             'Use with --keys to re-extract specific documents.')
    args = parser.parse_args()

    texts_dir  = _ROOT / args.texts_dir
    inv_path   = _ROOT / args.inventory
    res_path   = _ROOT / args.results

    # ── Lock file: prevent concurrent extraction runs ──────────────────────────
    # Two simultaneous Docling workers each load ~500 MB of ML models.
    # On a machine with limited RAM this causes catastrophic memory pressure.
    lock_path = _ROOT / 'data' / '.extract.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        lock_fh = open(lock_path, 'w')
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
    except ImportError:
        # fcntl not available (Windows) — skip locking
        lock_fh = None
    except OSError:
        print(
            f"\n⛔  Another extraction run is already in progress "
            f"(lock file: {lock_path}).\n"
            f"   If that process is gone, delete the lock file and retry.\n"
        )
        return

    def _release_lock():
        try:
            if lock_fh:
                import fcntl as _f
                _f.flock(lock_fh, _f.LOCK_UN)
                lock_fh.close()
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(_release_lock)
    # ──────────────────────────────────────────────────────────────────────────

    # Load inventory — embedded-font docs with a PDF
    inventory  = json.loads(inv_path.read_text(encoding='utf-8'))
    candidates = [
        r for r in inventory
        if r['doc_type'] == 'embedded'
        and r['pdf_status'] in ('stored', 'downloaded')
        and r.get('pdf_path')
    ]
    print(f"Embedded-font PDFs available: {len(candidates)}")

    # Resume: skip already-extracted docs
    if res_path.exists():
        prior = json.loads(res_path.read_text(encoding='utf-8'))
        done  = {r['key'] for r in prior if r.get('status') == 'ok'}
        print(f"Already extracted: {len(done)}  →  resuming")
    else:
        prior, done = [], set()

    if not args.force:
        candidates = [r for r in candidates if r['key'] not in done]
    else:
        print(f"--force: skipping done-check ({len(done)} previously OK docs may be re-processed)")

    # --keys filter: only process specified keys (re-queues even timed-out ones)
    if args.keys:
        key_set = set(args.keys)
        candidates = [r for r in candidates if r['key'] in key_set]
        print(f"Key filter active: {args.keys}")

    if args.limit:
        candidates = candidates[:args.limit]

    if not candidates:
        print("Nothing to do.")
        return

    print(f"To extract: {len(candidates)}  |  "
          + (f"Mode: DIRECT  |  Timeout: {args.timeout}s/doc"
             if args.direct else
             f"Workers: {args.workers}  |  Timeout: {args.timeout}s/doc"))
    print("=" * 60)

    # ── DIRECT MODE: single-process extraction (bypasses broken subprocess) ────
    if args.direct:
        try:
            from PIL import Image as _PILImage
            _PILImage.MAX_IMAGE_PIXELS = None
        except ImportError:
            pass

        from extractors.docling_extractor import DoclingExtractor

        print("\nLoading Docling models…", flush=True)
        t0  = time.time()
        ext = DoclingExtractor(do_ocr=True)
        print(f"Models ready in {int(time.time()-t0)}s\n")

        results = list(prior)
        total   = len(candidates)

        for idx, item in enumerate(candidates, 1):
            key   = item['key']
            title = item.get('title', key)
            print(f"  ⟳ [{idx:3d}/{total}] {title[:55]}", flush=True)
            t_start = time.time()
            try:
                result = ext.extract(Path(item['pdf_path']))
                elapsed = int(time.time() - t_start)

                meta = {
                    'key':     key,
                    'title':   item.get('title', ''),
                    'authors': item.get('authors', ''),
                    'year':    item.get('year',    ''),
                }
                paths = save_document(key, result, meta, texts_dir)
                tq    = paths.get('text_quality', '?')
                icon  = {'good': '✓', 'suspect': '⚠', 'garbled': '✗'}.get(tq, '?')
                chars = len(result.get('text') or '')
                pages = len(result.get('page_texts') or {})
                print(f"       {chars:,} chars · {pages} pages · "
                      f"quality: {icon} {tq} · {elapsed}s\n")
                entry = {
                    'key':          key,
                    'title':        title,
                    'status':       'ok',
                    'chars':        chars,
                    'pages':        pages,
                    'text_quality': tq,
                    **{k: v for k, v in paths.items() if k != 'text_quality'},
                }
                del result
            except Exception as exc:
                elapsed = int(time.time() - t_start)
                print(f"       ERROR ({elapsed}s): {str(exc)[:100]}\n")
                entry = {
                    'key':    key,
                    'title':  title,
                    'status': 'error',
                    'error':  str(exc)[:200],
                }

            results.append(entry)
            save_log(results, res_path)
            gc.collect()

        # Summary
        ok_results = [r for r in results if r.get('status') == 'ok']
        ok       = len(ok_results)
        errors   = sum(1 for r in results if r.get('status') == 'error')
        skipped  = len(done)
        q_counts = {}
        for r in ok_results:
            q = r.get('text_quality', 'unknown')
            q_counts[q] = q_counts.get(q, 0) + 1
        print("=" * 60)
        print(f"✓ Extracted:  {ok}")
        if ok and q_counts:
            for q, n in sorted(q_counts.items()):
                icc = {'good': '✓', 'suspect': '⚠', 'garbled': '✗'}.get(q, '?')
                print(f"    {icc} {q}: {n}")
        print(f"✗ Errors:     {errors}")
        print(f"– Skipped:    {skipped} (already done)")
        print(f"\nTexts → {texts_dir}/")
        print(f"Log   → {res_path}")
        return
    # ──────────────────────────────────────────────────────────────────────────

    n_workers = min(args.workers, len(candidates))
    task_q    = mp.Queue()
    result_q  = mp.Queue()

    # Staggered startup: spawn one worker at a time and wait for its 'ready'
    # signal before spawning the next.  Each worker loads ~400-500 MB of ML
    # models; simultaneous spawning causes severe memory-pressure / paging on
    # machines with limited free RAM.  Sequential startup is slower in wall-
    # clock terms but reliable regardless of system memory state.
    print(f"\nStarting {n_workers} Docling worker(s) — loading models…", flush=True)
    workers = []
    t0 = time.time()

    for wid in range(n_workers):
        p = mp.Process(target=_docling_worker, args=(wid, task_q, result_q), daemon=True)
        p.start()
        workers.append(p)
        try:
            status, w_id, *_ = result_q.get(timeout=240)
            if status == 'ready':
                print(f"  Worker {w_id} ready  [{int(time.time()-t0)}s]")
            elif status == 'init_err':
                print(f"  Worker {w_id} FAILED to init — exiting")
                for _ in workers:
                    task_q.put(None)
                return
        except _queue.Empty:
            print(f"  ERROR: Worker {wid} did not start within 240s — exiting")
            for _ in workers:
                task_q.put(None)
            return

    print()

    # Enqueue all tasks
    for doc_id, item in enumerate(candidates):
        task_q.put((doc_id, item['key'], item['pdf_path']))
    for _ in range(n_workers):      # sentinel per worker
        task_q.put(None)

    # ── Collect results, saving in background threads ─────────────────────────
    results     = list(prior)
    completed   = 0
    total       = len(candidates)
    key_to_item = {item['key']: item for item in candidates}
    start_times: dict[str, float] = {}   # populated on 'started', not at enqueue

    print(f"Processing {total} documents…\n")

    # I/O thread pool: disk writes don't stall the result-collection loop
    save_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='save')

    while completed < total:
        # ── Timeout check (only started docs) ─────────────────────────────────
        now = time.time()
        for key, t_start in list(start_times.items()):
            if now - t_start > args.timeout:
                item = key_to_item.get(key, {})
                print(f"  ⚠  TIMEOUT ({args.timeout}s): {item.get('title','?')[:55]}")
                entry = {
                    'key':    key,
                    'title':  item.get('title', key),
                    'status': 'timeout',
                    'error':  f'exceeded {args.timeout}s',
                }
                del start_times[key]
                completed += 1
                with _save_lock:
                    results.append(entry)
                    save_log(results, res_path)

        # ── Drain result queue ─────────────────────────────────────────────────
        try:
            status, wid, doc_id, key, payload = result_q.get(timeout=0.5)
        except _queue.Empty:
            continue

        if key is None:     # safety: ignore malformed messages
            continue

        item  = key_to_item.get(key, {})
        title = item.get('title', key) if isinstance(item, dict) else key

        if status == 'started':
            start_times[key] = time.time()
            print(f"  ⟳  [{len(start_times):2d} active] {title[:55]}  (worker {wid})")
            continue

        # Doc finished — cancel its timer
        start_times.pop(key, None)

        # Skip if already counted as timed-out
        with _save_lock:
            already = any(r['key'] == key for r in results)
        if already:
            continue

        if status == 'ok':
            chars = len(payload.get('text') or '')
            pages = len(payload.get('page_texts') or {})

            meta = {
                'key':     key,
                'title':   item.get('title', '') if isinstance(item, dict) else '',
                'authors': item.get('authors', '') if isinstance(item, dict) else '',
                'year':    item.get('year',    '') if isinstance(item, dict) else '',
            }

            # Kick off disk save in background; build result entry immediately
            future = save_pool.submit(save_document, key, payload, meta, texts_dir)

            def _on_save_done(fut, _key=key, _title=title, _chars=chars,
                              _pages=pages, _completed=completed + 1, _total=total):
                try:
                    paths = fut.result()
                    tq    = paths.get('text_quality', '?')
                    quality_icon = {'good': '✓', 'suspect': '⚠', 'garbled': '✗'}.get(tq, '?')
                    print(f"  ✓ [{_completed:3d}/{_total}] {_title[:55]}")
                    print(f"      {_chars:,} chars · {_pages} pages · "
                          f"quality: {quality_icon} {tq}\n")
                    entry = {
                        'key':          _key,
                        'title':        _title,
                        'status':       'ok',
                        'chars':        _chars,
                        'pages':        _pages,
                        'text_quality': tq,
                        **{k: v for k, v in paths.items() if k != 'text_quality'},
                    }
                except Exception as exc:
                    print(f"  ⚠  Save failed for {_key}: {exc}\n")
                    entry = {'key': _key, 'title': _title,
                             'status': 'save_error', 'error': str(exc)}
                with _save_lock:
                    results.append(entry)
                    save_log(results, res_path)

            future.add_done_callback(_on_save_done)

        else:   # err
            print(f"  ✗ [{completed+1:3d}/{total}] {title[:55]}")
            print(f"      ERROR: {str(payload)[:100]}\n")
            entry = {
                'key':    key,
                'title':  title,
                'status': 'error',
                'error':  str(payload)[:200],
            }
            with _save_lock:
                results.append(entry)
                save_log(results, res_path)

        completed += 1

    # Wait for any in-flight saves before shutting down
    save_pool.shutdown(wait=True)

    # Shut down workers
    for p in workers:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()

    # ── Summary ───────────────────────────────────────────────────────────────
    ok_results = [r for r in results if r.get('status') == 'ok']
    ok       = len(ok_results)
    errors   = sum(1 for r in results if r.get('status') == 'error')
    timeouts = sum(1 for r in results if r.get('status') == 'timeout')
    skipped  = len(done)

    # Quality breakdown
    q_counts = {}
    for r in ok_results:
        q = r.get('text_quality', 'unknown')
        q_counts[q] = q_counts.get(q, 0) + 1

    print("=" * 60)
    print(f"✓ Extracted:  {ok}")
    if ok and q_counts:
        for q, n in sorted(q_counts.items()):
            icon = {'good': '✓', 'suspect': '⚠', 'garbled': '✗'}.get(q, '?')
            print(f"    {icon} {q}: {n}")
    print(f"✗ Errors:     {errors}")
    print(f"⚠ Timeouts:   {timeouts}")
    print(f"– Skipped:    {skipped} (already done)")
    print(f"\nTexts → {texts_dir}/")
    print(f"Log   → {res_path}")


if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    main()
