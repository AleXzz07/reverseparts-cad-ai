from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .preview_renderer import generate_step_previews


def _write_checkpoint(output_path: Path, result: dict) -> None:
    temporary_path = Path(f"{output_path}.tmp")
    temporary_path.write_text(json.dumps(result), encoding="utf-8")
    temporary_path.replace(output_path)


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

    output_path = Path(args.output_path)
    result = generate_step_previews(
        args.step_path,
        max_views=args.max_views,
        view_names=args.views,
        progress_callback=lambda payload: _write_checkpoint(output_path, payload),
    )
    _write_checkpoint(output_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
