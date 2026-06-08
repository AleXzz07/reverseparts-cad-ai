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

The container uses Ubuntu LTS, installs `python3-freecad`, configures `PYTHONPATH`, and starts Uvicorn on port `8000`.

## Test Fixtures

`tests/test_files/` is intentionally kept ready for real CAD files such as `STAFFA TEST 1.stp`. Ground-truth JSON files can be added under `tests/ground_truth/`.
