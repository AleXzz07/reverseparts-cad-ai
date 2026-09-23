"""Export matching V1.1-before/after GLBs for local browser visual review.

Run once for each mode in separate Docker processes. This script never calls
CAD analysis, the quote engine, or the static preview renderer.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("before", "after"), required=True)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "before":
        from scripts import _viewer_v1_1_before_shading as exporter
    else:
        from app import model_exporter as exporter
    result = exporter.export_step_to_glb(str(args.step))
    if not result["available"]:
        parser.error("; ".join(result["warnings"]))
    glb = base64.b64decode(result["model_base64"], validate=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(glb)
    print(f"{args.mode}: {args.step} -> {args.output} ({len(glb)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
