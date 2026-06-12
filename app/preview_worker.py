from __future__ import annotations

import argparse
import json
from pathlib import Path

from .preview_renderer import generate_step_previews


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated STEP preview worker.")
    parser.add_argument("step_path")
    parser.add_argument("output_path")
    parser.add_argument("--max-views", type=int, default=4)
    args = parser.parse_args()

    result = generate_step_previews(
        args.step_path,
        max_views=args.max_views,
    )
    Path(args.output_path).write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
