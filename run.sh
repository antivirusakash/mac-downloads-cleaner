#!/bin/sh
# Scan a folder (default ~/Downloads), check the report, and render preview.html.
# If private/labels exists, also score the report against your own labels.
set -e
cd "$(dirname "$0")"
DIR="${DIR:-$HOME/Downloads}"
python3 -I declutter.py --out out "$DIR" > /dev/null
python3 -I check.py out/report.json | tail -1
if [ -d private/labels ]; then
  python3 -I eval.py out/report.json private/labels --log private/decision.tsv --note "${1:-run}" --show "${SHOW:-0}"
fi
TOTAL=$(du -sk "$DIR" | awk '{print $1*1024}')
python3 -I render.py out/report.json preview.html --total-bytes "$TOTAL" --label "$DIR" > /dev/null
echo "open preview.html"
