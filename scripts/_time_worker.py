#!/usr/bin/env python3
"""Timing test for worker startup."""
import sys, time, multiprocessing as mp
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / 'src')

def worker(q):
    sys.path.insert(0, _SRC)
    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env')
    t0 = time.time()
    try:
        from PIL import Image as _P; _P.MAX_IMAGE_PIXELS = None
        print(f'  PIL import:     {time.time()-t0:.1f}s', flush=True)
    except: pass
    from extractors.docling_extractor import DoclingExtractor
    print(f'  Module import:  {time.time()-t0:.1f}s', flush=True)
    ext = DoclingExtractor(do_ocr=True)
    print(f'  Extractor init: {time.time()-t0:.1f}s', flush=True)
    q.put('ready')

if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    q  = mp.Queue()
    t0 = time.time()
    p  = mp.Process(target=worker, args=(q,))
    p.start()
    try:
        msg = q.get(timeout=300)
        print(f'Total wall time: {time.time()-t0:.1f}s')
    except Exception as e:
        print(f'TIMEOUT/ERROR: {e}')
    p.join(timeout=5)
