##
## Islamic Cartography Pipeline — Task Runner
##
## Usage:
##   make stage            Copy PDFs from Zotero → data/pdfs/{key}.pdf (with provenance)
##   make stage-dry        Preview staging without copying
##   make lfs-setup        One-time: install Git LFS and track data/pdfs/*.pdf
##   make extract          Run robust extraction in background (fire and forget)
##   make extract-dry      Preview what would be extracted (no writes)
##   make extract-vision   Extraction with Google Vision fallback enabled
##   make translate        Translate non-English docs (background)
##   make status           Show pipeline status (tail of log + status JSON)
##   make status-web       Open status.html in browser
##   make inventory        Re-scan Zotero and regenerate dashboard.html
##   make bibliography     Extract bibliography from all extracted docs
##   make rag              Start the RAG chat server on port 8001
##   make layout           Enrich layout_elements.json with Heron (background)
##   make layout-dry       Preview which docs need layout enrichment
##   make clean-locks      Remove stale .extract.lock files
##   make commit           Git add all and commit with a timestamp message
##

PYTHON  = venv/bin/python
LOG_DIR = /tmp/ic_pipeline
EXTRACT_LOG = $(LOG_DIR)/extract.log
TRANSLATE_LOG = $(LOG_DIR)/translate.log

.DEFAULT_GOAL := help

# ── Helpers ────────────────────────────────────────────────────────────────────

$(LOG_DIR):
	mkdir -p $(LOG_DIR)

.PHONY: help
help:
	@echo ""
	@echo "Islamic Cartography Pipeline"
	@echo "============================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Extraction ─────────────────────────────────────────────────────────────────

.PHONY: extract
extract: $(LOG_DIR)  ## Run extraction in background (pypdfium2 + Tesseract)
	@echo "Starting extraction in background…"
	@nohup $(PYTHON) scripts/05b_extract_robust.py --workers 3 \
	  > $(EXTRACT_LOG) 2>&1 &
	@echo "PID $$!"
	@echo "Log: $(EXTRACT_LOG)"
	@echo "Run 'make status' to monitor progress"

.PHONY: extract-dry
extract-dry:  ## Preview extraction plan (no writes)
	$(PYTHON) scripts/05b_extract_robust.py --dry-run

.PHONY: extract-vision
extract-vision: $(LOG_DIR)  ## Extraction with Google Vision fallback (costs ~$4)
	@echo "Starting extraction with Vision fallback…"
	@nohup $(PYTHON) scripts/05b_extract_robust.py --workers 2 --vision \
	  > $(EXTRACT_LOG) 2>&1 &
	@echo "PID $$!"
	@echo "Log: $(EXTRACT_LOG)"

.PHONY: extract-garbled
extract-garbled: $(LOG_DIR)  ## Re-process docs with garbled/scanned text using Vision
	@echo "Starting Vision OCR for garbled docs…"
	@$(PYTHON) -c "\
import json; inv = json.load(open('data/inventory.json')); \
keys = [x['key'] for x in inv if x.get('text_quality') in ('garbled','suspect')]; \
print(' '.join(keys))" > /tmp/garbled_keys.txt
	@KEYS=$$(cat /tmp/garbled_keys.txt); \
	nohup $(PYTHON) scripts/05b_extract_robust.py --vision --force --keys $$KEYS \
	  > $(EXTRACT_LOG) 2>&1 &
	@echo "PID $$!"

# ── Translation ────────────────────────────────────────────────────────────────

.PHONY: translate
translate: $(LOG_DIR)  ## Translate non-English docs in background
	@echo "Starting translation…"
	@nohup $(PYTHON) scripts/08_translate.py \
	  > $(TRANSLATE_LOG) 2>&1 &
	@echo "PID $$!"
	@echo "Log: $(TRANSLATE_LOG)"

# ── Status & monitoring ────────────────────────────────────────────────────────

.PHONY: status
status:  ## Show live extraction log + status summary
	@echo "=== Pipeline Status ==="
	@$(PYTHON) -c "\
import json, os; \
p = 'data/pipeline_status.json'; \
s = json.load(open(p)) if os.path.exists(p) else {}; \
docs = s.get('docs', {}); \
ok  = sum(1 for d in docs.values() if d.get('state') == 'ok'); \
err = sum(1 for d in docs.values() if d.get('state') == 'error'); \
run = sum(1 for d in docs.values() if d.get('state') == 'in_progress'); \
print(f'  ✓ OK: {ok}  ✗ Error: {err}  ⟳ Running: {run}'); \
[print(f'  ✗ {k}: {v.get(\"error\",\"?\")[:80]}') for k,v in docs.items() if v.get('state')=='error'] \
" 2>/dev/null || echo "  No status file yet."
	@echo ""
	@echo "=== Extract Log (last 20 lines) ==="
	@tail -20 $(EXTRACT_LOG) 2>/dev/null || echo "  No log yet. Run 'make extract' first."

.PHONY: status-web
status-web:  ## Open status.html in browser
	open data/status.html

.PHONY: ps-extract
ps-extract:  ## Show running extraction processes
	@ps aux | grep '05b_extract_robust' | grep -v grep || echo "No extraction running"

.PHONY: kill-extract
kill-extract:  ## Kill any running extraction processes
	@pkill -f '05b_extract_robust' && echo "Killed." || echo "No process found."

# ── Inventory & bibliography ───────────────────────────────────────────────────

# ── PDF staging ───────────────────────────────────────────────────────────────

.PHONY: stage
stage:  ## Copy PDFs from Zotero/downloads → data/pdfs/{key}.pdf with provenance
	$(PYTHON) scripts/00_stage_pdfs.py

.PHONY: stage-dry
stage-dry:  ## Preview PDF staging without copying
	$(PYTHON) scripts/00_stage_pdfs.py --dry-run

.PHONY: lfs-setup
lfs-setup:  ## One-time: install Git LFS and track data/pdfs/*.pdf
	git lfs install
	git lfs track "data/pdfs/*.pdf"
	git add .gitattributes
	@echo "Git LFS configured. Now run 'make stage' then 'git add data/pdfs/' to push PDFs."

# ── Inventory & bibliography ───────────────────────────────────────────────────

.PHONY: flags
flags:  ## Sync extracted/has_reader flags from disk into inventory.json
	$(PYTHON) scripts/00_update_inventory_flags.py

.PHONY: inventory
inventory:  ## Re-scan Zotero and regenerate dashboard.html
	$(PYTHON) scripts/03_inventory.py
	$(PYTHON) scripts/00_update_inventory_flags.py

.PHONY: bibliography
bibliography:  ## Extract bibliography from all extracted docs
	$(PYTHON) scripts/06_extract_bibliography.py

# ── Layout enrichment ──────────────────────────────────────────────────────────

LAYOUT_LOG = $(LOG_DIR)/layout.log

.PHONY: layout
layout: $(LOG_DIR)  ## Enrich layout_elements.json with Heron (cloud-friendly, no PDF needed)
	@echo "Starting Heron layout enrichment in background…"
	@nohup $(PYTHON) scripts/05c_layout_heron.py \
	  > $(LAYOUT_LOG) 2>&1 &
	@echo "PID $$!"
	@echo "Log: $(LAYOUT_LOG)"
	@echo "Run 'tail -f $(LAYOUT_LOG)' to monitor"

.PHONY: layout-dry
layout-dry:  ## Preview which docs need layout enrichment
	$(PYTHON) scripts/05c_layout_heron.py --dry-run

# ── RAG server ─────────────────────────────────────────────────────────────────

.PHONY: rag
rag:  ## Start RAG chat server on localhost:8001
	$(PYTHON) scripts/rag_server.py --port 8001

.PHONY: rag-bg
rag-bg: $(LOG_DIR)  ## Start RAG server in background
	@nohup $(PYTHON) scripts/rag_server.py --port 8001 \
	  > $(LOG_DIR)/rag.log 2>&1 &
	@echo "RAG server PID $$! — http://localhost:8001"

# ── Maintenance ────────────────────────────────────────────────────────────────

.PHONY: clean-locks
clean-locks:  ## Remove stale lock files
	rm -f data/.extract.lock
	@echo "Lock files cleared."

.PHONY: check
check:  ## Verify environment (pypdfium2, tesseract, optional Vision)
	@$(PYTHON) -c "import pypdfium2; print('✓ pypdfium2', pypdfium2.__version__)" 2>/dev/null || echo "✗ pypdfium2 missing"
	@$(PYTHON) -c "import pytesseract; print('✓ pytesseract', pytesseract.get_tesseract_version())" 2>/dev/null || echo "✗ pytesseract/tesseract missing"
	@$(PYTHON) -c "from google.cloud import vision; print('✓ google-cloud-vision')" 2>/dev/null || echo "– google-cloud-vision not installed (optional)"
	@$(PYTHON) -c "from rank_bm25 import BM25Okapi; print('✓ rank-bm25')" 2>/dev/null || echo "✗ rank-bm25 missing (needed for RAG)"
	@$(PYTHON) -c "import fastapi; print('✓ fastapi', fastapi.__version__)" 2>/dev/null || echo "✗ fastapi missing (needed for RAG server)"

.PHONY: commit
commit:  ## Commit all changes with a timestamp
	git add -A
	git commit -m "WIP: pipeline update $$(date '+%Y-%m-%d %H:%M')"
