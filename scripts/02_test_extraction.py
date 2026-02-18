#!/usr/bin/env python3
"""
Test multi-witness extraction on sample documents.

Execution model
---------------
For each document:
  1. Docling (persistent warm process) + Vision (fresh process per doc) run in parallel.
     The Docling worker loads models ONCE at startup — no per-doc model loading overhead.
  2. Vision agreement score is computed page-by-page against Docling
  3a. Agreement ≥ threshold → Docling accepted, Tesseract skipped
  3b. Agreement < threshold → Tesseract runs in a child PROCESS

Text storage
------------
Each extractor's text is saved to data/texts/{item_key}/{extractor}.md|txt
The main JSON stores only metadata, quality scores, and file paths — not the
raw text blobs, which can be hundreds of thousands of characters each.

Usage:
    python scripts/02_test_extraction.py --samples 5
    python scripts/02_test_extraction.py --samples 30 --skip 2
    python scripts/02_test_extraction.py --samples 5 --threshold 0.5 --timeout 300
    python scripts/02_test_extraction.py --texts-dir data/extracted_texts
"""
import sys
import multiprocessing as mp
import queue as _queue
from pathlib import Path
import argparse
import json
import time
import subprocess
import os as _os

# ── psutil (optional — for memory monitoring) ─────────────────────────────────
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SRC = str(Path(__file__).parent.parent / 'src')
sys.path.insert(0, _SRC)

from dotenv import load_dotenv
from zotero_client import ZoteroLibrary
from extractors import DoclingExtractor, TesseractExtractor, VisionExtractor
from quality import compute_quality_metrics
from quality.similarity import compute_similarity
from language_detector import detect_and_format


# ── Config ────────────────────────────────────────────────────────────────────
EXTRACTOR_TIMEOUT       = 300    # seconds per document
DOCLING_STARTUP_TIMEOUT = 120    # seconds to wait for Docling models to load
VISION_THRESHOLD        = 0.5
VISION_PAGES            = 3
MEM_ABORT_GB            = 4.0
NOTIFY_TITLE            = "Islamic Cartography Pipeline"
_NTFY_TOPIC             = _os.getenv('NTFY_TOPIC', '')


# ── Memory helper ─────────────────────────────────────────────────────────────

def _mem_gb() -> float | None:
    """Current process RSS in GB, or None if psutil unavailable."""
    if not _PSUTIL:
        return None
    try:
        return psutil.Process().memory_info().rss / (1024 ** 3)
    except Exception:
        return None


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(message: str, subtitle: str = "", sound: str = "Ping"):
    full = f"{subtitle} — {message}" if subtitle else message
    if _NTFY_TOPIC:
        try:
            subprocess.run(
                ['curl', '-s', '-X', 'POST', f'https://ntfy.sh/{_NTFY_TOPIC}',
                 '-H', f'Title: {NOTIFY_TITLE}', '-H', 'Priority: default', '-d', full],
                check=False, capture_output=True, timeout=5
            )
        except Exception:
            pass
    script = (
        f'display notification {json.dumps(message)}'
        f' with title {json.dumps(NOTIFY_TITLE)}'
        + (f' subtitle {json.dumps(subtitle)}' if subtitle else '')
        + f' sound name "{sound}"'
    )
    try:
        subprocess.run(['osascript', '-e', script], check=False, capture_output=True)
    except FileNotFoundError:
        pass


# ── Result helpers ────────────────────────────────────────────────────────────

def _timeout_error(name, seconds):
    return {'text': None, 'confidence': 0.0, 'method': name,
            'metadata': {'success': False}, 'page_texts': {},
            'error': f'timeout after {seconds}s'}

def _exc_error(name, exc):
    return {'text': None, 'confidence': 0.0, 'method': name,
            'metadata': {'success': False}, 'page_texts': {},
            'error': str(exc)}


# ── Child-process workers (module-level → picklable with spawn) ───────────────

def _docling_persistent_worker(task_queue: mp.Queue, result_queue: mp.Queue):
    """
    Long-lived Docling worker: load models ONCE, then process documents on demand.

    Protocol:
      task_queue  ← (doc_id: int, file_path_str: str)  or  None (shutdown)
      result_queue → ('ready',    None,   None)         on successful init
                   → ('init_err', None,   error_str)    on init failure
                   → ('ok',       doc_id, result_dict)  per document
                   → ('err',      doc_id, error_str)    per document error
    """
    sys.path.insert(0, _SRC)
    load_dotenv()
    from extractors.docling_extractor import DoclingExtractor
    try:
        ext = DoclingExtractor()
        result_queue.put(('ready', None, None))
    except Exception as e:
        result_queue.put(('init_err', None, str(e)))
        return

    while True:
        msg = task_queue.get()
        if msg is None:          # shutdown sentinel
            break
        doc_id, file_path_str = msg
        try:
            result = ext.extract(Path(file_path_str))
            result_queue.put(('ok', doc_id, result))
        except Exception as e:
            result_queue.put(('err', doc_id, str(e)))


def _vision_worker(file_path_str: str, n_pages: int, result_queue: mp.Queue):
    try:
        sys.path.insert(0, _SRC)
        load_dotenv()
        from extractors.vision_extractor import VisionExtractor
        ext    = VisionExtractor()
        result = ext.extract(Path(file_path_str), page_indices=list(range(n_pages)))
        result_queue.put(('ok', result))
    except Exception as e:
        result_queue.put(('err', str(e)))


def _tesseract_worker(file_path_str: str, lang: str, result_queue: mp.Queue):
    try:
        sys.path.insert(0, _SRC)
        load_dotenv()
        from extractors.tesseract_extractor import TesseractExtractor
        ext    = TesseractExtractor()
        result = ext.extract(Path(file_path_str), lang=lang)
        result_queue.put(('ok', result))
    except Exception as e:
        result_queue.put(('err', str(e)))


# ── Persistent Docling worker manager ─────────────────────────────────────────

def start_docling_worker(startup_timeout: int = DOCLING_STARTUP_TIMEOUT):
    """
    Start the persistent Docling worker and block until models are loaded.
    Returns (process, task_queue, result_queue).
    Raises RuntimeError on failure or timeout.
    """
    task_q   = mp.Queue()
    result_q = mp.Queue()
    proc = mp.Process(
        target=_docling_persistent_worker,
        args=(task_q, result_q),
        daemon=True,
    )
    proc.start()
    t0 = time.time()
    print(f"  Docling worker starting (loading models)...", end="", flush=True)
    deadline = t0 + startup_timeout
    while time.time() < deadline:
        try:
            status, _, value = result_q.get(timeout=2)
            if status == 'ready':
                elapsed = int(time.time() - t0)
                print(f" ready ✓ [{elapsed}s]")
                return proc, task_q, result_q
            else:
                proc.terminate(); proc.join()
                raise RuntimeError(f"Docling worker init failed: {value}")
        except _queue.Empty:
            elapsed = int(time.time() - t0)
            print(f"\r  Docling worker starting [{elapsed}s]...", end="", flush=True)
    proc.terminate(); proc.join()
    raise RuntimeError(f"Docling worker did not start within {startup_timeout}s")


# ── Phase 1: Docling (persistent) + Vision (fresh process) in parallel ────────

def run_phase1_parallel(
    attachment_path: Path,
    n_pages: int,
    timeout: int,
    docling_task_q: mp.Queue,
    docling_result_q: mp.Queue,
    doc_id: int,
    vision_available: bool,
) -> dict:
    """
    Submit a task to the persistent Docling worker and run Vision in a fresh
    process simultaneously.  Both results are collected within `timeout` seconds.

    Docling timeouts do NOT kill the persistent worker — the stale result is
    discarded the next time we check the result queue (doc_id mismatch).
    """
    # Send task to warm Docling worker
    docling_task_q.put((doc_id, str(attachment_path)))
    docling_done   = False
    docling_result = None

    # Start Vision in a fresh process (quick startup — no heavy models)
    vision_done   = not vision_available
    vision_result = None
    vision_proc   = None
    vision_q      = None
    if vision_available:
        vision_q    = mp.Queue()
        vision_proc = mp.Process(
            target=_vision_worker,
            args=(str(attachment_path), n_pages, vision_q),
            daemon=True,
        )
        vision_proc.start()

    start    = time.time()
    deadline = start + timeout

    while True:
        elapsed   = int(time.time() - start)
        timed_out = time.time() >= deadline

        # Drain Docling result queue — discard stale results from prev timed-out docs
        if not docling_done:
            while True:
                try:
                    status, rid, value = docling_result_q.get_nowait()
                    if rid == doc_id:
                        docling_result = value if status == 'ok' else _exc_error('docling', value)
                        docling_done   = True
                        break
                    # else: stale result from a previous timeout — discard
                except _queue.Empty:
                    break

        # Check Vision process
        if not vision_done and vision_proc and not vision_proc.is_alive():
            vision_proc.join()
            try:
                status, value = vision_q.get_nowait()
                vision_result = value if status == 'ok' else _exc_error('vision', value)
            except _queue.Empty:
                vision_result = _exc_error('vision', 'process exited without result')
            vision_done = True

        # Handle timeouts
        if timed_out:
            if not docling_done:
                docling_result = _timeout_error('docling', timeout)
                docling_done   = True
                # Persistent worker keeps running; stale result discarded next round
            if not vision_done:
                if vision_proc and vision_proc.is_alive():
                    vision_proc.terminate()
                    vision_proc.join(3)
                    if vision_proc.is_alive():
                        vision_proc.kill()
                        vision_proc.join()
                vision_result = _timeout_error('vision', timeout)
                vision_done   = True

        # Status line
        mem     = _mem_gb()
        mem_str = f"  [{mem:.1f}GB RAM]" if mem is not None else ""
        parts   = []

        if docling_done:
            err  = (docling_result or {}).get('error') or ''
            icon = 'TIMEOUT' if 'timeout' in err else ('✗' if err else '✓')
            parts.append(f"Docling {icon}[{elapsed}s]")
        else:
            parts.append(f"Docling ⟳[{elapsed}s]")

        if vision_available:
            if vision_done:
                err  = (vision_result or {}).get('error') or ''
                icon = 'TIMEOUT' if 'timeout' in err else ('✗' if err else '✓')
                parts.append(f"Vision {icon}[{elapsed}s]")
            else:
                parts.append(f"Vision ⟳[{elapsed}s]")

        print(f"\r  {' | '.join(parts)}{mem_str}", end="", flush=True)

        if docling_done and vision_done:
            print()
            break

        time.sleep(1)

    results = {'docling': docling_result or _exc_error('docling', 'no result')}
    if vision_available:
        results['vision'] = vision_result or _exc_error('vision', 'no result')
    return results


# ── Tesseract runner ──────────────────────────────────────────────────────────

def run_tesseract_in_process(file_path: Path, lang: str | None, timeout: int) -> dict:
    """Run Tesseract in a child process with a hard kill on timeout."""
    q = mp.Queue()
    p = mp.Process(
        target=_tesseract_worker,
        args=(str(file_path), lang or 'eng', q),
        daemon=True,
    )
    p.start()
    start = time.time()

    while p.is_alive():
        elapsed = int(time.time() - start)
        mem     = _mem_gb()
        mem_str = f"  {mem:.1f}GB RAM" if mem else ""
        print(f"\r  Tesseract... [{elapsed}s{mem_str}]", end="", flush=True)

        if elapsed >= timeout:
            print(f"\r  Tesseract... TIMEOUT ({timeout}s) — killing          ")
            p.terminate()
            p.join(timeout=3)
            if p.is_alive():
                p.kill()
                p.join()
            return _timeout_error('tesseract', timeout)

        if mem and mem > MEM_ABORT_GB:
            print(f"\r  Tesseract... MEM ABORT ({mem:.1f}GB > {MEM_ABORT_GB}GB) — killing")
            p.terminate()
            p.join(timeout=3)
            if p.is_alive():
                p.kill()
                p.join()
            return _exc_error('tesseract', f'memory limit {MEM_ABORT_GB}GB exceeded')

        time.sleep(1)

    p.join()
    print(f"\r  Tesseract... ", end="", flush=True)

    if not q.empty():
        status, value = q.get_nowait()
        return value if status == 'ok' else _exc_error('tesseract', value)
    return _exc_error('tesseract', 'process exited without result')


# ── Vision gate ───────────────────────────────────────────────────────────────

def vision_gate_score(docling_result: dict, vision_result: dict) -> float:
    """Page-aligned mean similarity between Vision samples and Docling pages."""
    vision_pages  = vision_result.get('page_texts', {})
    docling_pages = docling_result.get('page_texts', {})
    scores = []
    for idx, v_text in vision_pages.items():
        d_text = docling_pages.get(idx + 1, '')
        if v_text and d_text:
            scores.append(compute_similarity(v_text, d_text))
    return sum(scores) / len(scores) if scores else 0.0


# ── Text file storage ─────────────────────────────────────────────────────────

def save_text(item_key: str, extractor_name: str, text: str, texts_dir: Path) -> Path:
    doc_dir = texts_dir / item_key
    doc_dir.mkdir(parents=True, exist_ok=True)
    suffix  = 'md' if extractor_name == 'docling' else 'txt'
    path    = doc_dir / f"{extractor_name}.{suffix}"
    path.write_text(text, encoding='utf-8')
    return path


def save_witness_texts(witnesses: dict, item_key: str, texts_dir: Path) -> dict:
    slimmed = {}
    for name, result in witnesses.items():
        r    = dict(result)
        text = r.pop('text', None)
        r.pop('page_texts', None)
        if text:
            path           = save_text(item_key, name, text, texts_dir)
            r['text_path'] = str(path)
        slimmed[name] = r
    return slimmed


# ── Save ──────────────────────────────────────────────────────────────────────

def _serializable(obj):
    if isinstance(obj, dict):
        return {k: _serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serializable(i) for i in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def save_results(results, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix('.tmp.json')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_serializable(results), f, indent=2, ensure_ascii=False)
    tmp.replace(output_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument('--samples',      type=int,   default=5)
    parser.add_argument('--skip',         type=int,   default=0,
                        help='Skip first N documents by position')
    parser.add_argument('--output',       type=str,   default='data/test_results.json')
    parser.add_argument('--texts-dir',    type=str,   default='data/texts',
                        help='Directory for per-document extracted text files (default: data/texts)')
    parser.add_argument('--timeout',      type=int,   default=EXTRACTOR_TIMEOUT,
                        help=f'Per-document timeout in seconds (default {EXTRACTOR_TIMEOUT})')
    parser.add_argument('--threshold',    type=float, default=VISION_THRESHOLD,
                        help=f'Vision/Docling gate threshold (default {VISION_THRESHOLD})')
    parser.add_argument('--vision-pages', type=int,   default=VISION_PAGES,
                        help=f'Spread pages for Vision to sample (default {VISION_PAGES})')
    parser.add_argument('--mem-limit',    type=float, default=MEM_ABORT_GB,
                        help=f'RAM limit in GB before aborting Tesseract (default {MEM_ABORT_GB})')
    args = parser.parse_args()

    texts_dir = Path(args.texts_dir)

    mem_note = f"psutil ✓ (abort >{args.mem_limit}GB)" if _PSUTIL else "psutil ✗ (no memory monitoring)"
    print(f"Samples: {args.samples}  |  Skip: {args.skip}  |  Timeout: {args.timeout}s  "
          f"|  Threshold: {args.threshold}  |  {mem_note}")
    print("Flow: Docling(persistent) ║ Vision(first N pages) → gate → Tesseract[process] only if needed")
    print(f"Texts: {texts_dir}/{{item_key}}/{{extractor}}.md|txt")
    print("="*60)

    library = ZoteroLibrary()
    items   = library.get_all_items()
    if not items:
        print("ERROR: No items found in Zotero")
        notify("ERROR: No items found in Zotero", sound="Basso")
        return 1

    samples = []
    for item in items:
        if len(samples) >= args.samples + args.skip:
            break
        path = library.get_attachment_path(item)
        if path and path.exists():
            samples.append((item, path))

    if args.skip:
        skipped = samples[:args.skip]
        samples  = samples[args.skip:]
        print(f"Skipping first {args.skip} document(s): "
              + ", ".join(i.get('data', {}).get('title', '?')[:30] for i, _ in skipped))

    if not samples:
        print("ERROR: No items to process after skip")
        return 1

    # Validate extractors (quick import/init check in main process)
    print("\nInitialising extractors...")
    docling_available = tesseract_available = vision_available = False

    try:
        DoclingExtractor();  print("✓ Docling");       docling_available = True
    except Exception as e:
        print(f"⚠ Docling: {e}")
    try:
        TesseractExtractor(); print("✓ Tesseract");    tesseract_available = True
    except Exception as e:
        print(f"⚠ Tesseract: {e}")
    try:
        VisionExtractor();    print("✓ Google Vision"); vision_available = True
    except Exception as e:
        print(f"⚠ Google Vision: {e}")

    if not docling_available:
        print("\nERROR: Docling is required"); return 1

    # Start persistent Docling worker — loads models once for all documents
    print("\nStarting persistent Docling worker...")
    try:
        docling_proc, docling_task_q, docling_result_q = start_docling_worker()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1

    # Resume from existing results
    output_path = Path(args.output)
    if output_path.exists():
        with open(output_path, encoding='utf-8') as f:
            results = json.load(f)
        done_keys = {r['item_key'] for r in results}
        print(f"\nResuming: {len(done_keys)} document(s) already done\n")
    else:
        results, done_keys = [], set()

    # ── Document loop ─────────────────────────────────────────────────────────
    doc_id = 0   # monotonically increasing — used to discard stale Docling results

    try:
        for idx, (item, attachment_path) in enumerate(samples, 1):
            title = item.get('data', {}).get('title', 'Untitled')
            key   = item.get('key', 'unknown')

            print(f"\nDocument {idx}/{len(samples)}: {title[:60]}")
            print(f"File: {attachment_path.name}")
            print("-"*60)

            if key in done_keys:
                print("  (already processed, skipping)")
                continue

            witnesses      = {}
            used_tesseract = False

            # ── Phase 1: Docling (persistent) + Vision in parallel ─────────────
            phase1 = run_phase1_parallel(
                attachment_path  = attachment_path,
                n_pages          = args.vision_pages,
                timeout          = args.timeout,
                docling_task_q   = docling_task_q,
                docling_result_q = docling_result_q,
                doc_id           = doc_id,
                vision_available = vision_available,
            )
            doc_id += 1

            docling_result = phase1['docling']
            vision_result  = phase1.get('vision')

            for name, res in phase1.items():
                if res.get('error'):
                    print(f"  {name.capitalize()}: ✗ {res['error']}")
                else:
                    pages_info = ''
                    if name == 'vision' and res.get('metadata', {}).get('pages_sampled'):
                        pages_info = f" (pages {res['metadata']['pages_sampled']})"
                    print(f"  {name.capitalize()}: ✓ {len(res.get('text') or '')} chars{pages_info}")

            witnesses['docling'] = docling_result

            # Language detection from Docling output
            docling_text  = docling_result.get('text') or ''
            detected_lang = detect_and_format(docling_text) if docling_text else None
            if detected_lang:
                print(f"  [languages: {detected_lang}]")

            # ── Phase 2: Vision gate ───────────────────────────────────────────
            needs_tesseract = not vision_available

            if vision_result and not vision_result.get('error') and docling_text:
                gate_score = vision_gate_score(docling_result, vision_result)
                witnesses['vision'] = vision_result

                if gate_score >= args.threshold:
                    print(f"  Vision gate: {gate_score:.2f} ≥ {args.threshold} "
                          f"→ Docling accepted ✓  (Tesseract skipped)")
                else:
                    print(f"  Vision gate: {gate_score:.2f} < {args.threshold} "
                          f"→ running Tesseract...")
                    needs_tesseract = True

            # ── Phase 3: Tesseract in a child PROCESS ─────────────────────────
            if needs_tesseract and tesseract_available:
                tess_result = run_tesseract_in_process(attachment_path, detected_lang, args.timeout)
                witnesses['tesseract'] = tess_result
                used_tesseract = True
                if tess_result.get('error'):
                    print(f"✗ {tess_result['error']}")
                else:
                    print(f"✓ {len(tess_result.get('text') or '')} chars, "
                          f"conf={tess_result.get('confidence', 0):.2f}")

            # ── Quality assessment ─────────────────────────────────────────────
            # For pairwise similarity, compare only the first VISION_PAGES pages
            # of Docling against Vision — comparing the full doc to 3 sampled
            # pages via Levenshtein always scores low due to length mismatch.
            witnesses_for_quality = dict(witnesses)
            docling_page_texts = docling_result.get('page_texts', {})
            if docling_page_texts:
                first_pages = sorted(docling_page_texts)[:args.vision_pages]
                docling_excerpt = ' '.join(docling_page_texts[p] for p in first_pages)
                witnesses_for_quality['docling'] = {**docling_result, 'text': docling_excerpt}

            quality = compute_quality_metrics(witnesses_for_quality)

            print(f"\n  Quality:")
            print(f"    Agreement:   {quality['agreement_score']:.2f}")
            print(f"    Cleanliness: {quality['cleanliness_score']:.2f}")
            print(f"    Score:       {quality['score']:.2f}")
            print(f"    → {quality['recommendation'].upper()}"
                  + (" [+Tesseract]" if used_tesseract else ""))

            if quality['similarity']['pairs']:
                print(f"\n  Similarity:")
                for pair, score in quality['similarity']['pairs'].items():
                    print(f"    {pair}: {score:.2f}")

            # ── Save text blobs to per-document files ──────────────────────────
            witnesses_slim = save_witness_texts(witnesses, key, texts_dir)
            saved_paths = {name: r.get('text_path', '(none)')
                           for name, r in witnesses_slim.items()}
            print(f"\n  Texts saved:")
            for name, path in saved_paths.items():
                print(f"    {name}: {path}")

            # ── Append slim result to JSON ─────────────────────────────────────
            results.append({
                'item_key':       key,
                'title':          title,
                'file':           str(attachment_path),
                'witnesses':      witnesses_slim,
                'quality':        quality,
                'used_tesseract': used_tesseract,
            })

            save_results(results, output_path)
            print(f"\n  [saved → {output_path}]")
            notify(
                f"{idx}/{len(samples)} done — {quality['recommendation'].upper()}"
                + (" (+Tesseract)" if used_tesseract else ""),
                subtitle=title[:60], sound="Ping"
            )

    finally:
        # Shut down the persistent Docling worker cleanly
        docling_task_q.put(None)
        docling_proc.join(timeout=5)
        if docling_proc.is_alive():
            docling_proc.terminate()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    recs = {}
    for r in results:
        rec = r['quality']['recommendation']
        recs[rec] = recs.get(rec, 0) + 1
    for rec, count in sorted(recs.items()):
        print(f"  {rec.upper()}: {count}/{len(results)}")

    tess_count = sum(1 for r in results if r.get('used_tesseract'))
    print(f"\n  Tesseract invoked: {tess_count}/{len(results)} documents")
    if results:
        avg = sum(r['quality']['score'] for r in results) / len(results)
        print(f"  Average quality score: {avg:.2f}")

    print(f"\n  Results saved to:  {output_path}")
    print(f"  Text files under:  {texts_dir}/")

    avg = sum(r['quality']['score'] for r in results) / len(results) if results else 0
    notify(
        f"All {len(results)} done — avg quality {avg:.2f}, "
        f"Tesseract used {tess_count}/{len(results)}",
        subtitle=str(output_path), sound="Glass"
    )
    return 0


if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)   # safe on macOS; avoids fork+Objective-C crashes
    sys.exit(main())
