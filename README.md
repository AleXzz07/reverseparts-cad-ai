# reverseparts-cad-ai

Backend Python per REVERSEPARTS che analizza file CAD STEP/STP di componenti meccanici e restituisce un JSON tecnico verificabile.

## Stack

- Python
- FastAPI
- FreeCAD Python API
- Docker
- pytest

## API

### `GET /health`

Checks service status and whether the FreeCAD Python API can be imported.

```json
{
  "status": "ok",
  "freecad_available": true,
  "freecad_error": null
}
```

### `POST /analyze-cad`

Input `multipart/form-data`:

- `file`: `.stp` or `.step`
- `material`: optional string
- `density_g_cm3`: optional number
- `declared_thickness_mm`: optional number
- `quantity`: optional integer, defaults to `1`

The response separates declared values, measured CAD values, and estimated values. Unreliable features are returned as `null`, `[]`, or low confidence with a warning.

The CAD response also includes a preliminary `cutting` section with outer, inner, and total laser cut length estimates when planar wire geometry and detected features are reliable enough.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest
```

FreeCAD is required for real STEP analysis. Without FreeCAD, `/health` reports `freecad_available: false` and `/analyze-cad` returns an HTTP 503.

## Docker

```powershell
docker build -t reverseparts-cad-ai .
docker run --rm -p 8000:8000 reverseparts-cad-ai
```

The container uses Ubuntu 22.04 LTS, installs `freecad` and `python3-freecad` when available, configures `PYTHONPATH` for FreeCAD Python modules, verifies `import FreeCAD, Part` during build, and starts Uvicorn on port `8000`.

## Test reale STAFFA TEST 1

`tests/test_files/STAFFA TEST 1.stp` is the real STEP fixture used to verify CAD analysis with FreeCAD. The expected comparison data lives in `tests/ground_truth/staffa_test_1_expected.json`.

Run the real test inside Docker, where FreeCAD is installed:

```powershell
.\scripts\test_real_staffa.ps1
```

On Linux/macOS:

```sh
sh scripts/test_real_staffa.sh
```

The scripts run:

```sh
docker build -t reverseparts-cad-ai .
docker run --rm reverseparts-cad-ai pytest tests -v
```

Outside Docker, the real STAFFA test is skipped when FreeCAD is not available. Inside Docker, FreeCAD is required and the test fails if it cannot be imported.

Generate and print the real analysis JSON:

```powershell
.\scripts\analyze_staffa.ps1
```

This writes `tests/output/staffa_test_1_actual.json` and prints the formatted JSON to the console.

Generate the validation report against the AutoForm ground truth:

```powershell
.\scripts\evaluate_staffa.ps1
```

This compares `tests/output/staffa_test_1_actual.json` with `tests/ground_truth/staffa_test_1_expected.json`, writes `tests/output/staffa_test_1_evaluation.json`, and prints the formatted report.

Generate the preliminary process/cost quote:

```powershell
.\scripts\quote_staffa.ps1
```

Examples with requested quantities:

```powershell
.\scripts\quote_staffa.ps1 -Quantity 1
.\scripts\quote_staffa.ps1 -Quantity 25
.\scripts\quote_staffa.ps1 -Quantity 100
```

This reads `tests/output/staffa_test_1_actual.json`, writes `tests/output/staffa_test_1_quote.json`, and prints a preliminary internal-cost estimate based on:

- `config/pricing_default.json`
- `config/materials.json`

The quote separates `estimated_internal_cost_eur` from `commercial_guidance`. The engine shows the configured minimum order value and minimum billable guidance, but it does not apply margin or decide the final commercial price. When CAD analysis provides `cutting.total_cut_length_mm`, laser time is estimated from cut length; otherwise the quote falls back to the feature-based heuristic.

## Dataset Multi Pezzo

Real CAD test parts can be organized under `tests/dataset/`, one folder per part:

```text
tests/dataset/<case_name>/
  input.stp
  expected.json
  actual.json
  evaluation.json
  quote.json
```

`tests/dataset/staffa_test_1/` mirrors the existing STAFFA TEST 1 fixture in this scalable format.

Process every dataset folder inside Docker:

```powershell
.\scripts\analyze_dataset.ps1
.\scripts\evaluate_dataset.ps1
.\scripts\quote_dataset.ps1
```

The older `analyze_staffa`, `evaluate_staffa`, and `quote_staffa` scripts are kept for direct STAFFA TEST 1 compatibility.

## Test Fixtures

`tests/test_files/` is intentionally kept ready for real CAD files such as `STAFFA TEST 1.stp`. Ground-truth JSON files can be added under `tests/ground_truth/`.
