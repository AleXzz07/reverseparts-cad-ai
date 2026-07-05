from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .preview_renderer import generate_step_previews


def main() -> int:
    logging.basicConfig(
        level=os.getenv("PREVIEW_LOG_LEVEL", "INFO"),
        format="%(message)s",
    )
    parser = argparse.ArgumentParser(description="Isolated STEP preview worker.")
    parser.add_argument("step_path")
    parser.add_argument("output_path")
    parser.add_argument("--max-views", type=int, default=4)
    parser.add_argument("--views", nargs="*")
    args = parser.parse_args()

    result = generate_step_previews(
        args.step_path,
        max_views=args.max_views,
        view_names=args.views,
    )
    Path(args.output_path).write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
