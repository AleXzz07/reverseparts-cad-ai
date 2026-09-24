#!/usr/bin/env python3
"""Compare complete GLB and each raw accessor for the real profiler STEP files.

The frozen module is an unedited copy of the previous published exporter.
Exit 2 means at least one byte differs: inspect the per-accessor results.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as current  # noqa: E402
from scripts import _viewer_normals_before_optimization as before  # noqa: E402
from scripts.profile_viewer_v1 import DEFAULT_CASES  # noqa: E402


def _buffers(glb):
    size = struct.unpack_from("<I", glb, 12)[0]
    document = json.loads(glb[20:20 + size])
    start = 20 + size + 8
    names = ("positions", "normals", "indices", "brep_edges")
    return {name: glb[start + view["byteOffset"]:
                      start + view["byteOffset"] + view["byteLength"]]
            for name, view in zip(names, document["bufferViews"])}


def _normal_angles(left, right):
    if len(left) != len(right):
        return None
    counts = 0
    values = []
    for offset in range(0, len(left), 12):
        if left[offset:offset + 12] != right[offset:offset + 12]:
            counts += 1
        a = struct.unpack_from("<3f", left, offset)
        b = struct.unpack_from("<3f", right, offset)
        dot = sum(x * y for x, y in zip(a, b))
        length = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
        values.append(math.degrees(math.acos(max(-1., min(1., dot / length)))))
    values.sort()
    return {"changed_vertices": counts,
            "maximum_angle_deg": round(values[-1], 7) if values else 0,
            "p99_angle_deg": round(values[int(0.99 * (len(values) - 1))], 7) if values else 0}


def _export(module, path):
    payload = module.export_step_to_glb(str(path))
    if not payload.get("available"):
        raise RuntimeError("Exporter unavailable for benchmark input")
    return base64.b64decode(payload["model_base64"])


def compare(case, path, complexity):
    keys = ("VIEWER_MODEL_MAX_TRIANGLES", "VIEWER_MODEL_TESSELLATION_RATIO",
            "VIEWER_MODEL_CURVED_FACE_REFINEMENT")
    original = {key: os.environ.get(key) for key in keys}
    settings = (("50000", "300", "0.8") if complexity == "high" else
                ("120000", "650", "0.65"))
    try:
        os.environ.update(zip(keys, settings))
        old = _export(before, path)
        new = _export(current, path)
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    left, right = _buffers(old), _buffers(new)
    result = {"case": case, "whole_glb_byte_equal": old == new,
              "whole_glb_sha256_before": hashlib.sha256(old).hexdigest(),
              "whole_glb_sha256_after": hashlib.sha256(new).hexdigest(),
              "buffers": {}}
    for name in left.keys() | right.keys():
        a, b = left.get(name, b""), right.get(name, b"")
        result["buffers"][name] = {
            "byte_equal": a == b, "size_before": len(a), "size_after": len(b),
            "sha256_before": hashlib.sha256(a).hexdigest(),
            "sha256_after": hashlib.sha256(b).hexdigest(),
        }
    result["occ_normal_differences"] = _normal_angles(left["normals"], right["normals"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", choices=[case for case, _, _ in DEFAULT_CASES])
    args = parser.parse_args()
    cases = [item for item in DEFAULT_CASES if not args.case or item[0] in args.case]
    results = [compare(*entry) for entry in cases]
    payload = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    if any(not row["whole_glb_byte_equal"] for row in results):
        sys.exit(2)


if __name__ == "__main__":
    main()
