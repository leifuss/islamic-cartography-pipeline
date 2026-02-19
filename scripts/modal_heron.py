#!/usr/bin/env python3
"""
Modal deployment for Heron layout enrichment (05c) — GPU-accelerated.

Runs 05c_layout_heron.py on a T4 GPU in the cloud, reads page images
from the repo, writes layout_elements.json back, then commits.

Usage (local → cloud):
    modal run scripts/modal_heron.py                          # all unenriched keys
    modal run scripts/modal_heron.py --keys "KEY1 KEY2 KEY3" # specific keys

Cost: T4 GPU @ ~$0.59/hr. Full 10-doc batch ≈ 15-20 min ≈ ~$0.15.

Setup (one-time):
    pip install modal
    modal setup            # authenticate
    modal secret create islamic-cartography \
        ANTHROPIC_API_KEY=sk-ant-...
"""

import subprocess
import sys
from pathlib import Path

import modal

# ── Image ─────────────────────────────────────────────────────────────────────
# Build a container image with all the deps 05c needs
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "tesseract-ocr",
        "tesseract-ocr-eng",
        "tesseract-ocr-ara",
        "tesseract-ocr-fas",
        "tesseract-ocr-tur",
        "tesseract-ocr-deu",
        "tesseract-ocr-fra",
        "git",
        "git-lfs",
        "libgl1",
        "libglib2.0-0",
    )
    .pip_install(
        "torch",
        "torchvision",
        "transformers",
        "Pillow",
        "pytesseract",
        "python-dotenv",
        "tqdm",
        "anthropic",
    )
)

app = modal.App("islamic-cartography-heron", image=image)

# ── Repo volume — cloned fresh each run (LFS included) ───────────────────────
REPO_URL = "https://github.com/leifuss/islamic-cartography-pipeline.git"
REPO_DIR = Path("/repo")

# ── Main function ─────────────────────────────────────────────────────────────
@app.function(
    gpu="T4",
    timeout=3600,          # 1 hour — generous for large batches
    secrets=[modal.Secret.from_name("islamic-cartography")],
)
def run_heron(keys: list[str] | None = None):
    import json
    import os
    import subprocess
    from pathlib import Path

    # Clone the repo (with LFS for PDFs if needed)
    print("Cloning repo...")
    subprocess.run(
        ["git", "clone", "--depth=1", REPO_URL, str(REPO_DIR)],
        check=True, capture_output=True
    )

    # Configure git for committing results back
    subprocess.run(["git", "config", "user.name",  "modal-heron[bot]"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.email", "modal-heron@noreply"], cwd=REPO_DIR, check=True)

    # Build key list
    if not keys:
        inv = json.loads((REPO_DIR / "data/inventory.json").read_text())
        keys = [
            i["key"] for i in inv
            if i.get("extracted") and
               not (REPO_DIR / f"data/texts/{i['key']}/layout_elements.json").exists()
        ]

    print(f"Keys to enrich: {keys}")

    failed = []
    for key in keys:
        print(f"\n{'═'*40}\nLayout: {key}\n{'═'*40}")
        result = subprocess.run(
            [sys.executable, "scripts/05c_layout_heron.py", "--batch", "4", "--keys", key],
            cwd=REPO_DIR,
        )
        if result.returncode != 0:
            print(f"⚠ {key} failed (exit {result.returncode})")
            failed.append(key)

    # Commit results back using a deploy token if set, else skip
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        remote = f"https://x-access-token:{github_token}@github.com/leifuss/islamic-cartography-pipeline.git"
        subprocess.run(["git", "remote", "set-url", "origin", remote], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "add", "data/texts/"], cwd=REPO_DIR, check=True)
        result = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=REPO_DIR)
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", f"layout(05c/modal): {len(keys)-len(failed)} doc(s) enriched on T4 GPU [skip ci]"],
                cwd=REPO_DIR, check=True
            )
            subprocess.run(["git", "push"], cwd=REPO_DIR, check=True)
            print("✓ Committed and pushed layout results.")
    else:
        print("ℹ No GITHUB_TOKEN — results not pushed. Run locally with: modal volume get ...")

    print(f"\nDone. Failed: {failed or 'none'}")
    return {"ok": len(keys) - len(failed), "failed": failed}


# ── Local entrypoint ─────────────────────────────────────────────────────────
@app.local_entrypoint()
def main(keys: str = ""):
    """
    Run Heron enrichment on Modal T4 GPU.

    Args:
        --keys: Space-separated doc keys. Leave blank for all unenriched.
    """
    key_list = keys.split() if keys.strip() else None
    result = run_heron.remote(key_list)
    print(f"\nResult: {result}")
