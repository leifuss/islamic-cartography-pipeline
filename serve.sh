#!/usr/bin/env bash
# serve.sh — launch a local HTTP server so the corpus explorer and readers
#            work correctly (file:// URLs block some JS features browsers need).
#
# Usage:
#   ./serve.sh           → serves on http://localhost:8000/explore.html
#   ./serve.sh 9000      → uses port 9000 instead
#
PORT="${1:-8000}"
DATA_DIR="$(dirname "$0")/data"

echo "═══════════════════════════════════════════════════"
echo "  Islamic Cartography Corpus Explorer"
echo "  http://localhost:${PORT}/explore.html"
echo "  Press Ctrl-C to stop."
echo "═══════════════════════════════════════════════════"

# Open browser after a brief delay so the server can start
(sleep 1 && open "http://localhost:${PORT}/explore.html") &

python3 -m http.server "${PORT}" --directory "${DATA_DIR}"
