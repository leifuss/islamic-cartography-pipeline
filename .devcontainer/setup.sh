#!/usr/bin/env bash
# Codespaces post-create setup script
# Runs once when the container is first created.
set -euo pipefail

echo "=== Setting up Islamic Cartography Pipeline environment ==="

# ── Tesseract OCR ─────────────────────────────────────────────────────────────
echo "→ Installing Tesseract OCR..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  tesseract-ocr \
  tesseract-ocr-eng \
  tesseract-ocr-ara \
  tesseract-ocr-fas \
  tesseract-ocr-tur \
  tesseract-ocr-deu \
  tesseract-ocr-fra

echo "  Tesseract $(tesseract --version 2>&1 | head -1)"

# ── Python virtual environment ────────────────────────────────────────────────
echo "→ Creating Python venv..."
python3.11 -m venv venv

echo "→ Installing Python packages..."
venv/bin/pip install --upgrade pip -q

# Install PyTorch CPU-only first (much smaller than GPU build)
venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q

# Install everything else
venv/bin/pip install -r requirements.txt -q

echo "  Python $(venv/bin/python --version)"

# ── Create .env from Codespace secrets ───────────────────────────────────────
echo "→ Writing .env..."
cat > .env << EOF
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
GEMINI_API_KEY=${GEMINI_API_KEY:-}
# Zotero group = "Islamic Cartography"; collection auto-named by CambridgeCore import
COLLECTION_NAME=CambridgeCore_Citation_04Nov2025
EOF

# ── Verify environment ────────────────────────────────────────────────────────
echo ""
echo "=== Environment check ==="
venv/bin/python -c "import pypdfium2; print('✓ pypdfium2')" 2>/dev/null || echo "✗ pypdfium2"
venv/bin/python -c "import pytesseract; print('✓ pytesseract')" 2>/dev/null || echo "✗ pytesseract"
venv/bin/python -c "from transformers import RTDetrV2ForObjectDetection; print('✓ transformers + Heron')" 2>/dev/null || echo "✗ transformers"
venv/bin/python -c "from rank_bm25 import BM25Okapi; print('✓ rank-bm25')" 2>/dev/null || echo "✗ rank-bm25"
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo "✓ ANTHROPIC_API_KEY set" || echo "⚠ ANTHROPIC_API_KEY not set (add to Codespace secrets)"

# ── Claude Code hint ──────────────────────────────────────────────────────────
echo ""
echo "=== Ready ==="
echo "To install Claude Code:  npm install -g @anthropic-ai/claude-code"
echo "To start Claude Code:    claude"
echo "To run extraction:       source venv/bin/activate && make extract-dry"
echo ""
