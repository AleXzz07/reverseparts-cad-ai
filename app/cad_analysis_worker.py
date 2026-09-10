from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _probe(output_path: Path) -> int:
    from .cad_analyzer import get_freecad_status

    status = get_freecad_status()
    _write_json(
        output_path,
        {
            "status": "available" if status.available else "unavailable",
            "error": status.error,
        },
    )
    return 0


def _analyze(request_path: Path, output_path: Path) -> int:
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Worker request must be a JSON object.")
        step_path = Path(str(request["step_path"]))

        from .cad_analyzer import analyze_step_file, get_freecad_status

        freecad_status = get_freecad_status()
        if not freecad_status.available:
            _write_json(
                output_path,
                {
                    "status": "error",
                    "error_type": "freecad_unavailable",
                    "message": freecad_status.error or "FreeCAD is unavailable.",
                },
            )
            return 0

        result = analyze_step_file(
            file_bytes=step_path.read_bytes(),
            source_file=str(request.get("source_file") or "input.step"),
            material=request.get("material"),
            density_g_cm3=request.get("density_g_cm3"),
            declared_thickness_mm=request.get("declared_thickness_mm"),
            quantity=int(request.get("quantity", 1)),
            k_factor=request.get("k_factor"),
        )
        payload = (
            result.model_dump()
            if hasattr(result, "model_dump")
            else result.dict()
        )
        _write_json(output_path, {"status": "ok", "analysis": payload})
        return 0
    except Exception as exc:
        _write_json(
            output_path,
            {
                "status": "error",
                "error_type": "technical_error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated STEP analysis worker.")
    parser.add_argument("request_path", nargs="?")
    parser.add_argument("output_path")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    output_path = Path(args.output_path)
    if args.probe:
        return _probe(output_path)
    if not args.request_path:
        parser.error("request_path is required for CAD analysis")
    return _analyze(Path(args.request_path), output_path)


if __name__ == "__main__":
    raise SystemExit(main())
