#!/usr/bin/env python3
"""
10a_vision_upsample_test.py — Test Vision accuracy vs upscale factor.

Sends the same page to Vision at 1×, 2×, and 3× resolution and compares
word counts and text quality. Does NOT write to page_texts.json.

Uses 1 API call per variant per page tested.

Usage:
    python scripts/10a_vision_upsample_test.py
    python scripts/10a_vision_upsample_test.py --key DUZKRZFQ --page 7
    python scripts/10a_vision_upsample_test.py --key CR7CQJJ8 --page 8 --lang-hints fa ar
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
_TEXTS = _ROOT / "data" / "texts"

_TESSERACT_CANDIDATES = [
    "tesseract", "/usr/local/bin/tesseract", "/opt/local/bin/tesseract",
]


def load_credentials() -> bool:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and \
            Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).exists():
        return True
    for env_path in [_ROOT / ".env", _ROOT / "data" / ".env"]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GOOGLE_APPLICATION_CREDENTIALS="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if Path(val).exists():
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = val
                    return True
    return False


def call_vision(img_path: Path, lang_hints: list[str] | None) -> str:
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()
    content = img_path.read_bytes()
    image = vision.Image(content=content)
    ctx = vision.ImageContext(language_hints=lang_hints) if lang_hints else None
    resp = client.document_text_detection(image=image, image_context=ctx)
    if resp.error.message:
        raise RuntimeError(resp.error.message)
    return resp.full_text_annotation.text.strip()


def upsample(src: Path, factor: int, tmp_dir: str) -> Path:
    img = Image.open(src)
    w, h = img.size
    out = Image.new("RGB", (w * factor, h * factor))
    out = img.resize((w * factor, h * factor), Image.LANCZOS)
    dest = Path(tmp_dir) / f"up{factor}_{src.name}"
    out.save(dest)
    return dest


def word_count(text: str) -> int:
    return len(text.split())


def snippet(text: str, n: int = 200) -> str:
    return text[:n].replace("\n", " ↵ ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="DUZKRZFQ")
    parser.add_argument("--page", type=int, default=7)
    parser.add_argument("--scales", nargs="+", type=int, default=[1, 2, 3],
                        help="Upscale factors to test (default: 1 2 3)")
    parser.add_argument("--lang-hints", nargs="+", default=None, metavar="LANG")
    args = parser.parse_args()

    if not load_credentials():
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS not found")
        sys.exit(1)

    img_path = _TEXTS / args.key / "pages" / f"{args.page:03d}.jpg"
    if not img_path.exists():
        print(f"ERROR: {img_path} not found")
        sys.exit(1)

    src_img = Image.open(img_path)
    w, h = src_img.size
    print(f"Image     : {img_path.name}  {w}×{h}px  (~{round(h/(297/25.4))} DPI est)")
    print(f"Lang hints: {args.lang_hints or 'auto-detect'}")
    print(f"Scales    : {args.scales}")
    print()

    results = {}

    with tempfile.TemporaryDirectory() as tmp_dir:
        for factor in args.scales:
            if factor == 1:
                src = img_path
                dims = f"{w}×{h}"
            else:
                src = upsample(img_path, factor, tmp_dir)
                dims = f"{w*factor}×{h*factor}"

            print(f"  {factor}× ({dims})... ", end="", flush=True)
            try:
                text = call_vision(src, args.lang_hints)
                wc = word_count(text)
                results[factor] = {"text": text, "words": wc}
                print(f"{wc} words")
            except Exception as e:
                print(f"ERROR: {e}")
                results[factor] = {"text": "", "words": 0}

    # Print comparison
    print()
    print("─" * 60)
    print(f"{'Scale':<8} {'Words':>8}   Snippet")
    print("─" * 60)
    for factor in args.scales:
        r = results.get(factor, {})
        snip = snippet(r.get("text", ""), 120)
        print(f"  {factor}×     {r.get('words', 0):>6}   {snip}")

    # Save full text outputs for manual inspection
    out_dir = _TEXTS / args.key / "ocr_test"
    out_dir.mkdir(exist_ok=True)
    for factor, r in results.items():
        out = out_dir / f"p{args.page:03d}_vision_{factor}x.txt"
        out.write_text(r.get("text", ""), encoding="utf-8")
        print(f"\nSaved {factor}× → {out.name}")

    print(f"\nInspect outputs in: {out_dir}")


if __name__ == "__main__":
    main()
