from __future__ import annotations

import argparse
import json
from pathlib import Path

from .model_exporter import export_step_to_glb


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated STEP to GLB worker.")
    parser.add_argument("step_path")
    parser.add_argument("output_path")
    args = parser.parse_args()

    result = export_step_to_glb(args.step_path)
    Path(args.output_path).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
