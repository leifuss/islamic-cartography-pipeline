#!/usr/bin/env python3
"""
09_ocr_test.py — Multi-engine OCR quality comparison.

Runs Tesseract, EasyOCR, and Google Vision on page images and compares
results against the existing page_texts.json (Docling embedded-text extraction).
Produces a side-by-side HTML report showing all engines together.

Usage:
    # Clean English page (5 pages, all engines)
    python scripts/09_ocr_test.py --key PRZTK6C7 --pages 1-5

    # Arabic/garbled page (single page comparison)
    python scripts/09_ocr_test.py --key CR7CQJJ8 --pages 3 --lang ara

    # Specific engines only
    python scripts/09_ocr_test.py --key PRZTK6C7 --pages 1-5 --engines tesseract vision

    # Tesseract with upsampling
    python scripts/09_ocr_test.py --key PRZTK6C7 --pages 1-5 --upsample 3
"""

import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_TEXTS = _ROOT / "data" / "texts"

_TESSERACT_CANDIDATES = [
    "tesseract",
    "/usr/local/bin/tesseract",
    "/opt/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]

# ── Utility ────────────────────────────────────────────────────────────────

def find_tesseract() -> str | None:
    for c in _TESSERACT_CANDIDATES:
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return c
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return None


def parse_page_range(spec: str) -> list[int]:
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def load_google_credentials() -> str | None:
    """Return path to Google credentials JSON, or None."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and Path(path).exists():
        return path
    for env_path in [_ROOT / ".env", _ROOT / "data" / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("GOOGLE_APPLICATION_CREDENTIALS="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if Path(val).exists():
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = val
                        return val
    return None


# ── OCR Engines ────────────────────────────────────────────────────────────

def run_tesseract(img_path: Path, lang: str = "eng", upsample: int = 1,
                  tess_bin: str = "tesseract", ocr_dir: Path = None) -> str:
    if upsample > 1:
        img = Image.open(img_path)
        w, h = img.size
        img = img.resize((w * upsample, h * upsample), Image.LANCZOS)
        tmp = (ocr_dir or img_path.parent) / f"_up{upsample}_{img_path.name}"
        img.save(tmp, dpi=(96 * upsample, 96 * upsample))
        src = tmp
    else:
        src = img_path

    result = subprocess.run(
        [tess_bin, str(src), "stdout", "-l", lang, "--oem", "1", "--psm", "3"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Tesseract error: {result.stderr.strip()}")
    return result.stdout.strip()


def run_easyocr(img_path: Path, reader) -> str:
    results = reader.readtext(str(img_path), detail=0, paragraph=True)
    return "\n".join(results)


def run_google_vision(img_path: Path) -> str:
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()
    content = img_path.read_bytes()
    image = vision.Image(content=content)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")
    return response.full_text_annotation.text.strip()


# ── Scoring ────────────────────────────────────────────────────────────────

def word_set(text: str) -> set[str]:
    import re
    return set(w.lower() for w in re.findall(r"[a-zA-Z\u0600-\u06FF']+", text) if len(w) > 2)


def f1_score(ref: set, hyp: set) -> float:
    if not ref or not hyp:
        return 0.0
    tp = len(ref & hyp)
    p = tp / len(hyp)
    r = tp / len(ref)
    return 2 * p * r / (p + r) if (p + r) else 0.0


# ── Aligned diff ──────────────────────────────────────────────────────────

def aligned_diff(a: str, b: str) -> list[tuple[str, str, str]]:
    tok_a = a.split()
    tok_b = b.split()
    rows: list[tuple[str, str, str]] = []
    chunk = 20
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, tok_a, tok_b, autojunk=False).get_opcodes():
        if tag == "equal":
            words = tok_a[i1:i2]
            for s in range(0, len(words), chunk):
                rows.append((" ".join(words[s:s + chunk]),
                              " ".join(words[s:s + chunk]), "equal"))
        elif tag == "replace":
            rows.append((" ".join(tok_a[i1:i2]), " ".join(tok_b[j1:j2]), "replace"))
        elif tag == "delete":
            rows.append((" ".join(tok_a[i1:i2]), "", "delete"))
        elif tag == "insert":
            rows.append(("", " ".join(tok_b[j1:j2]), "insert"))
    return rows


# ── HTML helpers ───────────────────────────────────────────────────────────

def html_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def score_badge(score: float, label: str) -> str:
    cls = "good" if score >= 0.80 else "ok" if score >= 0.55 else "poor"
    return f"<span class='badge {cls}'>{html_esc(label)}: {score*100:.1f}%</span>"


def diff_table(ref: str, hyp: str, ref_label: str, hyp_label: str) -> str:
    rows = ""
    for left, right, tag in aligned_diff(ref, hyp):
        if tag == "equal":
            rows += (f"<tr class='eq'><td>{html_esc(left)}</td>"
                     f"<td>{html_esc(right)}</td></tr>\n")
        elif tag == "replace":
            rows += (f"<tr class='chg'><td class='del'>{html_esc(left)}</td>"
                     f"<td class='ins'>{html_esc(right)}</td></tr>\n")
        elif tag == "delete":
            rows += f"<tr class='chg'><td class='del'>{html_esc(left)}</td><td></td></tr>\n"
        elif tag == "insert":
            rows += f"<tr class='chg'><td></td><td class='ins'>{html_esc(right)}</td></tr>\n"
    return (
        f"<table class='diff'>"
        f"<thead><tr><th>{html_esc(ref_label)}</th><th>{html_esc(hyp_label)}</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


# ── HTML report ────────────────────────────────────────────────────────────

CSS = """
body{font-family:system-ui,sans-serif;max-width:1300px;margin:2em auto;padding:0 1em;color:#222}
h1{border-bottom:2px solid #333;padding-bottom:.4em}
.summary{background:#f5f5f5;border-radius:6px;padding:1em 1.5em;margin:1.5em 0}
.summary dt{font-weight:600;color:#555;float:left;width:160px}
.summary dd{margin-left:170px;margin-bottom:.4em}
.scoreboard{display:flex;gap:1em;flex-wrap:wrap;margin:1.5em 0}
.scorecard{border:1px solid #ddd;border-radius:6px;padding:.8em 1.2em;min-width:160px;text-align:center}
.scorecard h3{margin:0 0 .3em;font-size:.9em;color:#555}
.scorecard .avg{font-size:1.6em;font-weight:700}
.good-bg{background:#d4edda}.ok-bg{background:#fff3cd}.poor-bg{background:#f8d7da}
.page-block{margin:2em 0;border:1px solid #ddd;border-radius:6px;overflow:hidden}
.page-block h2{margin:0;background:#f0f0f0;padding:.6em 1em;font-size:1.1em;border-bottom:1px solid #ddd}
.meta{font-size:.85em;color:#666;padding:.4em 1em;background:#fafafa;border-bottom:1px solid #eee}
.engine-tabs{display:flex;gap:0;background:#f0f0f0;padding:.5em 1em 0;border-bottom:1px solid #ccc}
.engine-tab{padding:.4em .9em;cursor:pointer;border:1px solid transparent;
  border-radius:4px 4px 0 0;margin-right:.3em;font-size:.85em;font-weight:500;background:#e8e8e8}
.engine-tab.active{background:#fff;border-color:#ccc;border-bottom-color:#fff;
  margin-bottom:-1px;position:relative}
.engine-panel{display:none}.engine-panel.active{display:block}
table.diff{width:100%;border-collapse:collapse;font-size:.82em;font-family:monospace}
table.diff th{background:#e8e8e8;padding:.35em .6em;text-align:left;border-bottom:1px solid #ccc}
table.diff td{padding:.25em .6em;vertical-align:top;border-bottom:1px solid #eee;
  width:50%;word-break:break-word;white-space:pre-wrap}
table.diff tr.eq td{color:#999}
td.del{background:#ffeef0;color:#c0392b}
td.ins{background:#e6ffed;color:#196127}
.badge{display:inline-block;font-size:.75em;padding:.2em .55em;border-radius:12px;
  font-weight:600;margin-left:.4em}
.good{background:#d4edda;color:#155724}
.ok{background:#fff3cd;color:#856404}
.poor{background:#f8d7da;color:#721c24}
label{font-size:.85em;color:#666;cursor:pointer}
"""

JS = """
function switchTab(pageId, engine) {
  const block = document.getElementById(pageId);
  block.querySelectorAll('.engine-tab').forEach(
    t => t.classList.toggle('active', t.dataset.eng === engine));
  block.querySelectorAll('.engine-panel').forEach(
    p => p.classList.toggle('active', p.dataset.eng === engine));
}
function toggleEqual(cb) {
  cb.closest('.page-block').querySelectorAll('tr.eq').forEach(
    r => r.style.display = cb.checked ? 'none' : '');
}
document.addEventListener('DOMContentLoaded', () =>
  document.querySelectorAll('tr.eq').forEach(r => r.style.display = 'none'));
"""


def make_report(key: str, pages_results: list[dict], output_path: Path,
                engine_names: list[str], tess_upsample: int) -> None:
    # Scoreboard
    avgs = {n: 0.0 for n in engine_names}
    for r in pages_results:
        for n in engine_names:
            avgs[n] += r["engines"].get(n, {}).get("score", 0.0)
    for n in engine_names:
        avgs[n] = avgs[n] / len(pages_results) if pages_results else 0.0

    scorecard_html = ""
    for n in engine_names:
        avg = avgs[n]
        bg = "good-bg" if avg >= 0.80 else "ok-bg" if avg >= 0.55 else "poor-bg"
        scorecard_html += (
            f"<div class='scorecard {bg}'><h3>{html_esc(n)}</h3>"
            f"<div class='avg'>{avg*100:.1f}%</div>"
            f"<div style='font-size:.75em;color:#666'>avg word-F1</div></div>"
        )

    pages_html = ""
    for r in pages_results:
        pid = f"page-{r['page']}"
        badges = "".join(score_badge(r["engines"].get(n, {}).get("score", 0), n)
                         for n in engine_names)

        tabs = "".join(
            f"<div class='engine-tab{' active' if i == 0 else ''}' "
            f"data-eng='{html_esc(n)}' "
            f"onclick='switchTab(\"{pid}\",\"{html_esc(n)}\")'>"
            f"{html_esc(n)}</div>"
            for i, n in enumerate(engine_names)
        )

        panels = ""
        for i, n in enumerate(engine_names):
            d = r["engines"].get(n, {})
            hyp = d.get("text", "(not run)")
            tbl = diff_table(r["docling"], hyp, "Docling (ref)", n)
            panels += (
                f"<div class='engine-panel{' active' if i == 0 else ''}' "
                f"data-eng='{html_esc(n)}'>{tbl}</div>"
            )

        pages_html += f"""
        <section class="page-block" id="{pid}">
          <h2>Page {r['page']} {badges}</h2>
          <div class="meta">
            Docling words: {r['docling_words']}
            &nbsp;·&nbsp;
            <label><input type="checkbox" onchange="toggleEqual(this)" checked>
            hide matching rows</label>
          </div>
          <div class="engine-tabs">{tabs}</div>
          {panels}
        </section>"""

    upsample_note = (f"Tesseract {tess_upsample}× upsampled, OEM 1, PSM 3"
                     if tess_upsample > 1 else "Tesseract raw, OEM 1, PSM 3")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>OCR Comparison — {key}</title>
<style>{CSS}</style>
<script>{JS}</script>
</head>
<body>
<h1>OCR Engine Comparison — {key}</h1>
<div class="summary"><dl>
  <dt>Document</dt><dd>{key}</dd>
  <dt>Pages</dt><dd>{', '.join(str(r['page']) for r in pages_results)}</dd>
  <dt>Engines</dt><dd>{', '.join(engine_names)}</dd>
  <dt>Tesseract</dt><dd>{upsample_note}</dd>
  <dt>Reference</dt><dd>Docling embedded-text extraction (word-F1 vs this)</dd>
</dl></div>
<div class="scoreboard">{scorecard_html}</div>
{pages_html}
</body></html>"""

    output_path.write_text(html, encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-engine OCR comparison")
    parser.add_argument("--key", default="PRZTK6C7")
    parser.add_argument("--pages", default="1-5")
    parser.add_argument("--engines", nargs="+",
                        default=["tesseract", "easyocr", "vision"],
                        choices=["tesseract", "easyocr", "vision"])
    parser.add_argument("--lang", default="eng",
                        help="Tesseract language (eng/ara). EasyOCR auto-detects from this.")
    parser.add_argument("--upsample", type=int, default=1,
                        help="Upsample factor before Tesseract (e.g. 3)")
    args = parser.parse_args()

    arabic_mode = args.lang in ("ara", "ar")
    easy_langs = ["ar", "en"] if arabic_mode else ["en"]

    doc_dir = _TEXTS / args.key
    pages_dir = doc_dir / "pages"
    ocr_dir = doc_dir / "ocr_test"
    ocr_dir.mkdir(exist_ok=True)

    pt_path = doc_dir / "page_texts.json"
    if not pt_path.exists():
        print(f"ERROR: {pt_path} not found")
        sys.exit(1)
    page_texts = json.loads(pt_path.read_text())
    page_nums = parse_page_range(args.pages)

    engine_names: list[str] = []

    tess_label = (f"Tesseract-{args.lang}"
                  + (f"-{args.upsample}x" if args.upsample > 1 else ""))
    tess_bin = None
    if "tesseract" in args.engines:
        tess_bin = find_tesseract()
        if tess_bin:
            engine_names.append(tess_label)
            print(f"Tesseract : {tess_bin}")
        else:
            print("WARNING: tesseract not found, skipping")

    vision_ok = False
    if "vision" in args.engines:
        creds = load_google_credentials()
        if creds:
            try:
                from google.cloud import vision as _v  # noqa
                engine_names.append("Google Vision")
                vision_ok = True
                print(f"Vision    : credentials OK")
            except ImportError:
                print("WARNING: google-cloud-vision not installed, skipping")
        else:
            print("WARNING: GOOGLE_APPLICATION_CREDENTIALS not set, skipping Vision")

    easyocr_ok = False
    easy_label = f"EasyOCR-{'+'.join(easy_langs)}"
    easy_reader = None
    if "easyocr" in args.engines:
        try:
            import easyocr
            easyocr_ok = True
            engine_names.append(easy_label)
            print(f"EasyOCR   : loading model (langs={easy_langs})...", end=" ", flush=True)
            easy_reader = easyocr.Reader(easy_langs, gpu=False, verbose=False)
            print("ready")
        except ImportError:
            print("WARNING: easyocr not installed, skipping")

    if not engine_names:
        print("ERROR: no engines available")
        sys.exit(1)

    print(f"\nDocument  : {args.key}  pages {args.pages}")
    print(f"Engines   : {', '.join(engine_names)}\n")

    pages_results: list[dict] = []

    for page_num in page_nums:
        img_path = pages_dir / f"{page_num:03d}.jpg"
        if not img_path.exists():
            print(f"Page {page_num}: image not found, skipping")
            continue

        docling_text = page_texts.get(str(page_num), "")
        ref_words = word_set(docling_text)
        result = {
            "page": page_num,
            "docling": docling_text,
            "docling_words": len(ref_words),
            "engines": {},
        }

        print(f"Page {page_num}:")

        if tess_bin:
            print(f"  {tess_label:<35}", end=" ", flush=True)
            try:
                text = run_tesseract(img_path, lang=args.lang,
                                     upsample=args.upsample, tess_bin=tess_bin,
                                     ocr_dir=ocr_dir)
                score = f1_score(ref_words, word_set(text))
                result["engines"][tess_label] = {"text": text, "score": score,
                                                  "words": len(word_set(text))}
                (ocr_dir / f"p{page_num:03d}_{tess_label}.txt").write_text(text)
                print(f"F1={score*100:.1f}%")
            except Exception as e:
                print(f"ERROR: {e}")
                result["engines"][tess_label] = {"text": f"ERROR: {e}", "score": 0.0, "words": 0}

        if vision_ok:
            print(f"  {'Google Vision':<35}", end=" ", flush=True)
            try:
                text = run_google_vision(img_path)
                score = f1_score(ref_words, word_set(text))
                result["engines"]["Google Vision"] = {"text": text, "score": score,
                                                       "words": len(word_set(text))}
                (ocr_dir / f"p{page_num:03d}_vision.txt").write_text(text)
                print(f"F1={score*100:.1f}%")
            except Exception as e:
                print(f"ERROR: {e}")
                result["engines"]["Google Vision"] = {"text": f"ERROR: {e}", "score": 0.0, "words": 0}

        if easyocr_ok:
            print(f"  {easy_label:<35}", end=" ", flush=True)
            try:
                text = run_easyocr(img_path, easy_reader)
                score = f1_score(ref_words, word_set(text))
                result["engines"][easy_label] = {"text": text, "score": score,
                                                  "words": len(word_set(text))}
                (ocr_dir / f"p{page_num:03d}_easyocr.txt").write_text(text)
                print(f"F1={score*100:.1f}%")
            except Exception as e:
                print(f"ERROR: {e}")
                result["engines"][easy_label] = {"text": f"ERROR: {e}", "score": 0.0, "words": 0}

        pages_results.append(result)

    # Summary table
    print()
    print(f"{'Engine':<35} {'Avg word-F1':>12}")
    print("-" * 50)
    for n in engine_names:
        scores = [r["engines"].get(n, {}).get("score", 0) for r in pages_results]
        avg = sum(scores) / len(scores) if scores else 0
        print(f"{n:<35} {avg*100:>11.1f}%")

    (ocr_dir / "summary.json").write_text(json.dumps({
        "key": args.key, "engines": engine_names,
        "pages": [{"page": r["page"], "engines": {
            n: {"score": round(d.get("score", 0), 4), "words": d.get("words", 0)}
            for n, d in r["engines"].items()}} for r in pages_results]
    }, indent=2))

    report_path = ocr_dir / "report.html"
    make_report(args.key, pages_results, report_path, engine_names, args.upsample)
    print(f"\nReport → {report_path}")
    print(f"open \"{report_path}\"")


if __name__ == "__main__":
    main()
