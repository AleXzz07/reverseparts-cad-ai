from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .model_exporter import export_step_to_glb


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated STEP to GLB worker.")
    parser.add_argument("step_path")
    parser.add_argument("output_path")
    parser.add_argument("--status-path", default=None)
    args = parser.parse_args()

    started = time.monotonic()
    status_path = Path(args.status_path) if args.status_path else None
    state: dict = {}

    def progress(phase: str, metrics: dict) -> None:
        if status_path is None:
            return
        state.update(metrics)
        try:
            import resource
            peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
        except ImportError:
            peak_rss_mib = None
        payload = {"phase": phase, "worker_elapsed_sec": round(time.monotonic() - started, 4),
                   "worker_peak_rss_mib": peak_rss_mib, **state}
        temporary = status_path.with_suffix(".partial")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, status_path)
        except OSError:
            # Diagnostics must never prevent an otherwise valid GLB export.
            pass

    progress("worker_started", {})
    result = export_step_to_glb(args.step_path, progress=progress)
    progress("result_serialization", {"available": bool(result.get("available"))})
    serialize_started = time.monotonic()
    encoded = json.dumps(result)
    Path(args.output_path).write_text(encoded, encoding="utf-8")
    progress("worker_completed", {"available": bool(result.get("available")),
                                  "result_json_bytes": len(encoded.encode("utf-8")),
                                  "result_serialization_sec": round(
                                      time.monotonic() - serialize_started, 4)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
