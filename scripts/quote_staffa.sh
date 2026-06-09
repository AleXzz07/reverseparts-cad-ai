#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
QUANTITY="${1:-1}"
MATERIAL="${2:-}"

cd "$PROJECT_DIR"
docker build -t reverseparts-cad-ai .

if [ ! -f tests/output/staffa_test_1_actual.json ]; then
  sh scripts/analyze_staffa.sh
fi

if [ -n "$MATERIAL" ]; then
  docker run --rm \
    -v "$PROJECT_DIR:/workspace" \
    -w /workspace \
    reverseparts-cad-ai \
    python3 -m app.quote_engine \
    --actual /workspace/tests/output/staffa_test_1_actual.json \
    --output /workspace/tests/output/staffa_test_1_quote.json \
    --quantity "$QUANTITY" \
    --material "$MATERIAL"
else
  docker run --rm \
    -v "$PROJECT_DIR:/workspace" \
    -w /workspace \
    reverseparts-cad-ai \
    python3 -m app.quote_engine \
    --actual /workspace/tests/output/staffa_test_1_actual.json \
    --output /workspace/tests/output/staffa_test_1_quote.json \
    --quantity "$QUANTITY"
fi
