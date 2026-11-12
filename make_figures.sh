#!/usr/bin/env bash
# Regenerate the static analysis tables and every figure from the included data (no database).
# Output goes to out/; the figures used in the paper are also in figures/ for comparison.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-python}"
export PYTHONIOENCODING=utf-8
mkdir -p out/figures
"$PY" chop.py > out/static_tables.txt
(cd runtime && "$PY" paper_figs.py ../data --out ../out/figures \
             && "$PY" plot_fig12_predictors.py ../data --minmax --out ../out/figures)
echo "wrote out/static_tables.txt and out/figures/"
