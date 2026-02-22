#!/usr/bin/env python3
"""
Modal deployment for Heron layout enrichment (05c) — GPU-accelerated.

Runs 05c_layout_heron.py on a T4 GPU in the cloud, reads page images
from the repo, writes layout_elements.json back, then commits.

Usage (local → cloud):
    modal run scripts/modal_heron.py                          # all unenriched keys
    modal run scripts/modal_heron.py --keys "KEY1 KEY2 KEY3" # specific keys

Cost: T4 GPU @ ~$0.59/hr. Full corpus ≈ 30-60 min ≈ ~$0.30-0.60.

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
REPO_URL = "https://github.com/leifuss/scholion.git"
REPO_DIR = Path("/repo")

# ── Tuning ────────────────────────────────────────────────────────────────────
PER_KEY_TIMEOUT = 900      # 15 min per key — fast text assignment makes this generous
COMMIT_EVERY    = 5        # push progress every N keys (protects against timeout)
MAX_PAGES       = 800      # skip docs with more page images than this


def _push_progress(repo_dir: Path, message: str) -> bool:
    """Commit any staged changes and push. Returns True if pushed."""
    # Stage layout results — separate commands so a missing data/texts/ dir
    # doesn't cause git add to abort before staging collection texts
    subprocess.run("git add data/texts/ 2>/dev/null || true", cwd=repo_dir, shell=True)
    subprocess.run("git add data/collections/*/texts/ 2>/dev/null || true", cwd=repo_dir, shell=True)
    diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=repo_dir)
    if diff.returncode == 0:
        return False  # nothing to commit
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_dir, check=True,
    )
    for attempt in range(4):
        # Pull-rebase first so we're never behind main when pushing
        subprocess.run(["git", "pull", "--rebase"], cwd=repo_dir)
        result = subprocess.run(["git", "push"], cwd=repo_dir)
        if result.returncode == 0:
            return True
        wait = 2 ** (attempt + 1)
        print(f"  push failed, retrying in {wait}s…", flush=True)
        import time; time.sleep(wait)
    print("  ⚠ push failed after 4 attempts — will retry later", flush=True)
    return False


# ── Main function ─────────────────────────────────────────────────────────────

@app.function(
    gpu="T4",
    timeout=14400,         # 4 hours total budget
    secrets=[modal.Secret.from_name("islamic-cartography")],
)
def run_heron(keys: list[str] | None = None, github_token: str = "", repo_url: str = "",
              inventory: str = "", texts_root: str = "", force: bool = False):
    import json
    import os
    import subprocess
    import time
    from pathlib import Path

    clone_url = repo_url or REPO_URL

    # Clone the repo and pull LFS objects
    print(f"Cloning repo from {clone_url}...")
    subprocess.run(
        ["git", "clone", "--depth=1", clone_url, str(REPO_DIR)],
        check=True, capture_output=True
    )
    print("Pulling LFS objects (PDFs)...")
    subprocess.run(["git", "lfs", "pull"], cwd=REPO_DIR, check=True)

    # Configure git for committing results back
    subprocess.run(["git", "config", "user.name",  "modal-heron[bot]"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.email", "modal-heron@noreply"], cwd=REPO_DIR, check=True)

    # Set up push credentials early (so incremental pushes work)
    github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
    can_push = bool(github_token)
    if can_push:
        # Derive authenticated remote from clone URL
        auth_url = clone_url.replace("https://", f"https://x-access-token:{github_token}@")
        subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=REPO_DIR, check=True)
    else:
        print("ℹ No GITHUB_TOKEN — results will not be pushed.")

    # Build key list
    if not keys:
        inv = json.loads((REPO_DIR / "data/inventory.json").read_text())
        keys = [
            i["key"] for i in inv
            if i.get("extracted") and
               not (REPO_DIR / f"data/texts/{i['key']}/layout_elements.json").exists()
        ]

    total = len(keys)
    print(f"Keys to enrich: {total}")
    print(f"Per-key timeout: {PER_KEY_TIMEOUT}s  Max pages: {MAX_PAGES}")
    print(f"Incremental push: every {COMMIT_EVERY} keys" if can_push else "")

    # Process keys one at a time — 05c loads model once per subprocess,
    # but with fast text assignment each key is quick (Heron only, no Tesseract).
    failed = []
    ok_count = 0
    since_push = 0
    run_start = time.time()

    for n, key in enumerate(keys, 1):
        elapsed_total = int(time.time() - run_start)
        print(f"\n{'═'*50}\n[{n}/{total}] Layout: {key}  (elapsed {elapsed_total}s)\n{'═'*50}", flush=True)

        try:
            cmd = [sys.executable, "scripts/05c_layout_heron.py",
                   "--batch", "4",
                   "--max-pages", str(MAX_PAGES),
                   "--keys", key]
            if inventory:
                cmd += ["--inventory", inventory]
            if texts_root:
                cmd += ["--texts-root", texts_root]
            if force:
                cmd += ["--force"]
            result = subprocess.run(
                cmd,
                cwd=REPO_DIR,
                timeout=PER_KEY_TIMEOUT,
            )
            if result.returncode != 0:
                print(f"⚠ {key} failed (exit {result.returncode})", flush=True)
                failed.append(key)
            else:
                ok_count += 1
                since_push += 1
        except subprocess.TimeoutExpired:
            print(f"⚠ {key} timed out after {PER_KEY_TIMEOUT}s — skipping", flush=True)
            failed.append(key)

        # Incremental commit+push to save progress
        if can_push and since_push >= COMMIT_EVERY:
            print(f"\n  → Pushing progress ({ok_count} enriched so far)…", flush=True)
            pushed = _push_progress(
                REPO_DIR,
                f"layout(05c/modal): {ok_count} doc(s) enriched [skip ci]",
            )
            if pushed:
                since_push = 0
                print("  → Pushed.", flush=True)

    # Final push
    if can_push:
        print(f"\n{'═'*50}\nFinal push ({ok_count} enriched, {len(failed)} failed)…", flush=True)
        _push_progress(
            REPO_DIR,
            f"layout(05c/modal): {ok_count} doc(s) enriched on T4 GPU",
        )
        print("✓ Done.", flush=True)

    total_time = int(time.time() - run_start)
    print(f"\nDone in {total_time}s. OK: {ok_count}  Failed: {failed or 'none'}")
    return {"ok": ok_count, "failed": failed, "elapsed_s": total_time}


# ── Local entrypoint ─────────────────────────────────────────────────────────
@app.local_entrypoint()
def main(keys: str = "", inventory: str = "", texts_root: str = "", force: bool = False):
    """
    Run Heron enrichment on Modal T4 GPU.

    Args:
        --keys:       Space-separated doc keys. Leave blank for all unenriched.
        --inventory:  Path to inventory.json (default: data/inventory.json).
        --texts-root: Path to per-key text dirs (default: data/texts).
                      Pass data/collections/SLUG/texts for collection items.
        --force:      Re-enrich docs that already have _heron_version set.

    GITHUB_TOKEN is read from the local environment (set automatically in
    GitHub Actions, or set manually when running locally with a PAT).
    """
    import os
    key_list = keys.split() if keys.strip() else None
    github_token = os.environ.get("GITHUB_TOKEN", "")
    # Derive repo URL from GitHub Actions env (GITHUB_REPOSITORY = "owner/repo")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_url = f"https://github.com/{gh_repo}.git" if gh_repo else ""
    result = run_heron.remote(key_list, github_token=github_token, repo_url=repo_url,
                              inventory=inventory, texts_root=texts_root, force=force)
    print(f"\nResult: {result}")
