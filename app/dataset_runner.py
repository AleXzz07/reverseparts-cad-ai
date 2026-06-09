from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .evaluator import evaluate_files
from .main import app
from .quote_engine import DEFAULT_MATERIALS_CONFIG_PATH, load_materials_config, quote_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_DIR = PROJECT_ROOT / "tests" / "dataset"


def iter_dataset_cases(dataset_dir: Path = DEFAULT_DATASET_DIR) -> list[Path]:
    if not dataset_dir.exists():
        return []
    return sorted(path for path in dataset_dir.iterdir() if path.is_dir())


def _read_expected(case_dir: Path) -> dict[str, Any]:
    expected_path = case_dir / "expected.json"
    if not expected_path.exists():
        return {}
    return json.loads(expected_path.read_text(encoding="utf-8"))


def _material_key(expected: dict[str, Any], materials: dict[str, dict[str, float]]) -> str | None:
    explicit = expected.get("material_key")
    if explicit:
        return str(explicit).lower()

    material_description = str(expected.get("material", "")).lower()
    for material_name in materials:
        if material_name.lower() in material_description:
            return material_name.lower()
    return None


def _case_display_name(case_dir: Path, expected: dict[str, Any]) -> str:
    if expected.get("display_name"):
        return str(expected["display_name"])
    return case_dir.name.replace("_", " ").upper()


def analyze_case(case_dir: Path, quantity: int = 1) -> dict[str, Any]:
    input_path = case_dir / "input.stp"
    output_path = case_dir / "actual.json"
    expected = _read_expected(case_dir)
    materials = load_materials_config(DEFAULT_MATERIALS_CONFIG_PATH)
    material = _material_key(expected, materials)
    material_config = materials.get(material or "")

    if not input_path.exists():
        raise FileNotFoundError(f"Missing dataset input file: {input_path}")

    form_data = {"quantity": str(max(int(quantity), 1))}
    if material:
        form_data["material"] = material
    if material_config:
        form_data["density_g_cm3"] = str(material_config["density_g_cm3"])
    if expected.get("declared_thickness_mm") is not None:
        form_data["declared_thickness_mm"] = str(expected["declared_thickness_mm"])

    client = TestClient(app)
    with input_path.open("rb") as step_file:
        response = client.post(
            "/analyze-cad",
            data=form_data,
            files={"file": (input_path.name, step_file, "application/step")},
        )

    if response.status_code != 200:
        raise RuntimeError(f"Analysis failed for {case_dir.name}: {response.text}")

    payload = response.json()
    payload["part_name"] = _case_display_name(case_dir, expected)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def analyze_dataset(dataset_dir: Path = DEFAULT_DATASET_DIR, quantity: int = 1) -> list[dict[str, Any]]:
    return [
        {"case": case_dir.name, "actual": analyze_case(case_dir, quantity=quantity)}
        for case_dir in iter_dataset_cases(dataset_dir)
    ]


def evaluate_dataset(dataset_dir: Path = DEFAULT_DATASET_DIR) -> list[dict[str, Any]]:
    reports = []
    for case_dir in iter_dataset_cases(dataset_dir):
        report = evaluate_files(
            case_dir / "actual.json",
            case_dir / "expected.json",
            case_dir / "evaluation.json",
        )
        reports.append({"case": case_dir.name, "evaluation": report})
    return reports


def quote_dataset(dataset_dir: Path = DEFAULT_DATASET_DIR, quantity: int = 1, material: str | None = None) -> list[dict[str, Any]]:
    quotes = []
    for case_dir in iter_dataset_cases(dataset_dir):
        quote = quote_files(
            case_dir / "actual.json",
            case_dir / "quote.json",
            quantity=quantity,
            material=material,
        )
        quotes.append({"case": case_dir.name, "quote": quote})
    return quotes


def main() -> None:
    parser = argparse.ArgumentParser(description="Process REVERSEPARTS CAD AI dataset folders.")
    parser.add_argument("command", choices=("analyze", "evaluate", "quote"))
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--material", type=str, default=None)
    args = parser.parse_args()

    try:
        if args.command == "analyze":
            result = analyze_dataset(args.dataset_dir, quantity=args.quantity)
        elif args.command == "evaluate":
            result = evaluate_dataset(args.dataset_dir)
        else:
            result = quote_dataset(args.dataset_dir, quantity=args.quantity, material=args.material)
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
