#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"
docker build -t reverseparts-cad-ai .

if [ ! -f tests/output/staffa_test_1_actual.json ]; then
  sh scripts/analyze_staffa.sh
fi

docker run --rm \
  -v "$PROJECT_DIR:/workspace" \
  -w /workspace \
  reverseparts-cad-ai \
  python3 -m app.evaluator \
  --actual /workspace/tests/output/staffa_test_1_actual.json \
  --expected /workspace/tests/ground_truth/staffa_test_1_expected.json \
  --output /workspace/tests/output/staffa_test_1_evaluation.json
