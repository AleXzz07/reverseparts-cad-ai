from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .model_exporter import export_step_to_glb


class WorkerProgress:
    """Persist bounded-staleness progress without a disk write per CAD face.

    Major phases always reach disk. During per-face work the last completed
    checkpoint is at most 0.2 s old when callbacks are regularly arriving.
    A blocking OCC call cannot be interrupted by a Python progress callback.
    """

    ALWAYS_WRITE = frozenset({"worker_started", "freecad_import", "step_load",
                              "shape_loaded", "brep_edges", "glb_assembly",
                              "base64_encoding", "export_completed",
                              "export_failed", "result_serialization",
                              "worker_completed"})
    INTERVAL_SEC = 0.2

    def __init__(self, path: Path | None, started: float):
        self.path = path
        self.started = started
        self.state: dict = {}
        self.last_write = float("-inf")

    def __call__(self, phase: str, metrics: dict) -> bool:
        if self.path is None:
            return False
        self.state.update(metrics)
        now = time.monotonic()
        first_face_phase = (phase in {"tessellation", "occ_normals"}
                            and metrics.get("face_index") == 1)
        if (phase not in self.ALWAYS_WRITE and not first_face_phase
                and now - self.last_write < self.INTERVAL_SEC):
            return False
        try:
            import resource
            peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
        except ImportError:
            peak_rss_mib = None
        payload = {"phase": phase, "worker_elapsed_sec": round(now - self.started, 4),
                   "worker_peak_rss_mib": peak_rss_mib, **self.state}
        temporary = self.path.with_suffix(".partial")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            # Diagnostics must never prevent an otherwise valid GLB export.
            return False
        self.last_write = now
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated STEP to GLB worker.")
    parser.add_argument("step_path")
    parser.add_argument("output_path")
    parser.add_argument("--status-path", default=None)
    args = parser.parse_args()

    started = time.monotonic()
    progress = WorkerProgress(Path(args.status_path) if args.status_path else None,
                              started)

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
