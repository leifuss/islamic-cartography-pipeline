#!/usr/bin/env python3
"""
Merge Docling layout labels back into Vision-processed layout_elements.json.

Vision OCR replaced all element labels with 'text', losing Docling's structural
labels (footnote, section_header, list_item, picture, etc.).  This script:

  1. Loads the Docling backup (layout_elements.docling.json) — has correct
     labels but possibly poorer OCR text.
  2. Loads the current Vision layout_elements.json — has 'text' labels but
     better OCR text.
  3. Normalises both sets of bboxes to [0,1] page fractions.
  4. For each Docling element, finds the best-matching Vision element by
     bbox centroid distance and IoU.  If a good match is found, the Vision
     element inherits the Docling label (and vice-versa for unmatched
     Docling elements that have an interesting label).
  5. Writes the updated layout_elements.json.

Usage:
    python scripts/12_merge_labels.py
    python scripts/12_merge_labels.py DUZKRZFQ HMPTGZID
    python scripts/12_merge_labels.py --dry-run
"""

import json
import sys
from pathlib import Path

TEXTS_ROOT = Path(__file__).parent.parent / "data" / "texts"

# Labels worth preserving from Docling (everything except plain 'text')
STRUCTURAL_LABELS = {
    "footnote", "section_header", "list_item", "picture",
    "caption", "title", "page_header", "page_footer", "formula",
}

# IoU threshold: Vision bbox must overlap at least this fraction with Docling bbox
# (both normalised to [0,1]) to accept the label.
IOU_THRESHOLD = 0.15
# Max centroid distance (normalised) to still consider a pair
MAX_CENTROID_DIST = 0.12


# ── Bbox normalisation helpers ────────────────────────────────────────────────

def norm_bbox(bb: dict, ps: dict) -> "dict | None":
    """Normalise a Docling-convention bbox {l,t,r,b} by page size {w,h} → [0,1]."""
    w, h = ps.get("w"), ps.get("h")
    if not w or not h:
        return None
    return {
        "l": bb["l"] / w,
        "t": bb["t"] / h,
        "r": bb["r"] / w,
        "b": bb["b"] / h,
    }


def centroid(nb: dict) -> tuple[float, float]:
    return ((nb["l"] + nb["r"]) / 2, (nb["t"] + nb["b"]) / 2)


def dist(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def iou(a: dict, b: dict) -> float:
    """Intersection over Union for two normalised [0,1] bboxes."""
    ix_l = max(a["l"], b["l"])
    ix_r = min(a["r"], b["r"])
    ix_b = max(a["b"], b["b"])
    ix_t = min(a["t"], b["t"])
    if ix_r <= ix_l or ix_t <= ix_b:
        return 0.0
    inter = (ix_r - ix_l) * (ix_t - ix_b)
    area_a = (a["r"] - a["l"]) * (a["t"] - a["b"])
    area_b = (b["r"] - b["l"]) * (b["t"] - b["b"])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ── Per-doc merge ─────────────────────────────────────────────────────────────

def merge_doc(doc_dir: Path, dry_run: bool = False) -> dict:
    le_path   = doc_dir / "layout_elements.json"
    doc_path  = doc_dir / "layout_elements.docling.json"

    if not le_path.exists():
        return {"key": doc_dir.name, "status": "no_layout_elements"}
    if not doc_path.exists():
        return {"key": doc_dir.name, "status": "no_docling_backup"}

    with open(le_path)  as f: vision_data  = json.load(f)
    with open(doc_path) as f: docling_data = json.load(f)

    vision_ps  = vision_data.get("_page_sizes",  {})   # pixel dims
    docling_ps = docling_data.get("_page_sizes", {})    # PDF-point dims

    stats = {"relabelled": 0, "pages": 0, "non_text_in_docling": 0, "injected": 0}

    updated = {}  # page_str → new element list

    all_pages = set(k for k in vision_data  if k != "_page_sizes") | \
                set(k for k in docling_data if k != "_page_sizes")

    for pg in sorted(all_pages, key=lambda x: int(x)):
        v_els = vision_data.get(pg, [])
        d_els = docling_data.get(pg, [])

        vps = vision_ps.get(pg)    # pixel page size
        dps = docling_ps.get(pg)   # pdf-point page size

        if not v_els:
            updated[pg] = v_els
            continue

        # Count structural labels in Docling page
        n_structural = sum(
            1 for el in d_els
            if (el.get("label") or "").lower() in STRUCTURAL_LABELS
        )
        stats["non_text_in_docling"] += n_structural

        if not d_els or not vps or not dps:
            updated[pg] = v_els
            continue

        # Normalise all bboxes
        v_norm = []
        for el in v_els:
            nb = norm_bbox(el.get("bbox", {}), vps) if el.get("bbox") else None
            v_norm.append(nb)

        d_norm = []
        for el in d_els:
            nb = norm_bbox(el.get("bbox", {}), dps) if el.get("bbox") else None
            d_norm.append(nb)

        # For each Docling element with a structural label, find best Vision match
        new_v_els = [dict(el) for el in v_els]  # deep-ish copy

        used_vision_idx = set()
        matched_docling_idx = set()

        for di, del_ in enumerate(d_els):
            lbl = (del_.get("label") or "text").lower()
            if lbl not in STRUCTURAL_LABELS:
                continue
            dnb = d_norm[di]
            if dnb is None:
                continue
            dc = centroid(dnb)

            best_vi, best_score = None, -1.0
            for vi, vel in enumerate(new_v_els):
                vnb = v_norm[vi]
                if vnb is None:
                    continue
                vc = centroid(vnb)
                d_dist = dist(dc, vc)
                if d_dist > MAX_CENTROID_DIST:
                    continue
                score = iou(dnb, vnb) - d_dist
                if score > best_score:
                    best_score = score
                    best_vi = vi

            if best_vi is not None and iou(d_norm[di], v_norm[best_vi]) >= IOU_THRESHOLD:
                matched_docling_idx.add(di)
                if new_v_els[best_vi]["label"] != lbl:
                    new_v_els[best_vi]["label"] = lbl
                    stats["relabelled"] += 1
                    used_vision_idx.add(best_vi)

        # Inject Docling-only visual elements (picture/table) that Vision missed
        # entirely — these have no matching Vision element at all
        VISUAL_LABELS = {"picture", "table"}
        for di, del_ in enumerate(d_els):
            if di in matched_docling_idx:
                continue
            lbl = (del_.get("label") or "text").lower()
            if lbl not in VISUAL_LABELS:
                continue
            if not del_.get("bbox"):
                continue
            # Convert Docling bbox (in pdf-point space) to pixel space
            # so it's consistent with Vision elements
            db = del_["bbox"]
            if dps and vps:
                # Scale from pdf-point space to pixel space
                sx = vps["w"] / dps["w"]
                sy = vps["h"] / dps["h"]
                pixel_bbox = {
                    "l": round(db["l"] * sx, 2),
                    "t": round(db["t"] * sy, 2),
                    "r": round(db["r"] * sx, 2),
                    "b": round(db["b"] * sy, 2),
                }
            else:
                pixel_bbox = db  # best effort
            new_v_els.append({
                "label": lbl,
                "text": del_.get("text", ""),
                "bbox": pixel_bbox,
                "page": del_.get("page", int(pg)),
                "_source": "docling",
            })
            stats["injected"] = stats.get("injected", 0) + 1

        updated[pg] = new_v_els
        stats["pages"] += 1

    if dry_run:
        return {"key": doc_dir.name, "status": "dry_run", **stats}

    # Write updated data (preserve _page_sizes and other top-level keys)
    out = {k: v for k, v in vision_data.items() if k == "_page_sizes"}
    out.update(updated)
    with open(le_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    return {"key": doc_dir.name, "status": "merged", **stats}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Merge Docling labels into Vision layout_elements")
    parser.add_argument("keys", nargs="*", help="Doc keys (default: all with .docling.json backup)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.keys:
        doc_dirs = [TEXTS_ROOT / k for k in args.keys]
    else:
        doc_dirs = sorted(
            d for d in TEXTS_ROOT.iterdir()
            if d.is_dir() and (d / "layout_elements.docling.json").exists()
        )

    for doc_dir in doc_dirs:
        result = merge_doc(doc_dir, dry_run=args.dry_run)
        status = result["status"]
        key    = result["key"]
        if status in ("merged", "dry_run"):
            prefix = "[dry] " if status == "dry_run" else "✓ "
            injected = result.get('injected', 0)
            inj_str = f", {injected} injected" if injected else ""
            print(f"{prefix}{key}: {result['relabelled']} relabelled{inj_str} "
                  f"({result['non_text_in_docling']} structural in Docling, "
                  f"{result['pages']} pages)")
        else:
            print(f"  {key}: {status}")


if __name__ == "__main__":
    main()
