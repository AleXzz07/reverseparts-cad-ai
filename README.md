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

### `POST /quote`

Input JSON:

```json
{
  "analysis": {},
  "quantity": 37,
  "material": "alluminio"
}
```

Generates the quote JSON from an existing CAD analysis. `quantity` must be greater than `0`, and `material` must exist in `config/materials.json`.

### `POST /analyze-and-quote`

Input `multipart/form-data`:

- `file`: `.stp` or `.step`
- `material`: required material key from `config/materials.json`
- `quantity`: required integer greater than `0`
- `declared_thickness_mm`: optional number

The endpoint analyzes the STEP file, then returns:

```json
{
  "analysis": {},
  "quote": {}
}
```

### `POST /quote-pdf`

Input JSON:

```json
{
  "analysis": {},
  "quote": {}
}
```

Returns a downloadable `application/pdf` document titled `REVERSEPARTS - Preventivo tecnico interno`. The PDF contains part data, internal cost lines, technical timing details, and the preliminary quote note for commercial review.

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
.\scripts\quote_staffa.ps1 -Quantity 37 -Material alluminio
.\scripts\quote_staffa.ps1 -Quantity 37 -Material acciaio
.\scripts\quote_staffa.ps1 -Quantity 37 -Material inox
```

This reads `tests/output/staffa_test_1_actual.json`, writes `tests/output/staffa_test_1_quote.json`, and prints a preliminary internal-cost estimate based on:

- `config/pricing_default.json`
- `config/materials.json`

Use `-Material` to choose a material key from `config/materials.json`. If the selected material exists and `volume_cm3` is available, the quote recalculates weight as `volume_cm3 * density_g_cm3 / 1000` and marks `material.weight_source` as `recalculated_from_volume`. Unknown materials return a clear error with the available material keys. Each material can also define a `laser` profile with `cut_speed_mm_min` and `pierce_time_sec`; `laser_details.material_laser_profile_used` shows whether the material profile was used or the quote fell back to `config/pricing_default.json`.

The quote separates `estimated_internal_cost_eur` from `commercial_guidance`. The engine shows the configured minimum order value and minimum billable guidance, but it does not apply margin or decide the final commercial price. When CAD analysis provides `cutting.total_cut_length_mm`, laser time uses the configured cut speed, pierce time, pierce count, and extra handling time per piece; otherwise the quote falls back to the feature-based heuristic. The resulting quote includes `laser_details` with `cut_length_mm`, `cut_speed_mm_min`, `pierce_count`, `pierce_time_sec`, and `laser_time_min_per_piece`.

## Web App

The frontend and backend can run together for local development:

```powershell
docker run --rm -p 8000:8000 reverseparts-cad-ai
```

Open `http://localhost:8000/`. FastAPI exposes `GET /app-config.js`; if `API_BASE_URL` is not set, it sets `window.REVERSEPARTS_API_BASE_URL` to an empty string and the frontend uses relative requests.

The page runs the complete STEP analysis, quote, and PDF flow. Material and pricing defaults are loaded from `config/materials.json` and `config/pricing_default.json`. Values edited in the page are sent as `material_overrides` and `pricing_overrides`, apply only to that request, and never modify the config files. The quote reports `overrides_used` and exposes the effective values under `config_used`.

`POST /quote` accepts optional JSON objects named `pricing_overrides` and `material_overrides`. `POST /analyze-and-quote` accepts the same fields as JSON-encoded multipart form values.

Bending time uses `bending_setup_time_min` once per order plus a per-piece time based on `bending_time_sec_per_bend` and `bending_extra_handling_sec_per_piece`. If the CAD analysis does not provide `bends.count`, the engine uses the previous heuristic and adds a warning. The quote includes `bending_details` with the configured inputs and calculated per-piece and total bending time.

## Production Deployment

Deploy only the static frontend to Vercel. The Vercel build reads `API_BASE_URL` and writes both `public/index.html` and `public/app-config.js`:

```powershell
$env:API_BASE_URL="https://reverseparts-cad-api.onrender.com"
npm run build
```

In the Vercel project settings, configure:

```text
API_BASE_URL=https://reverseparts-cad-api.onrender.com
```

The value is written to `app-config.js` at build time, so redeploy the frontend after changing it. Verify the deployed value at:

```text
https://reverseparts-cad-ai.vercel.app/app-config.js
```

It must contain:

```javascript
window.REVERSEPARTS_API_BASE_URL = "https://reverseparts-cad-api.onrender.com";
```

`vercel.json` already selects `npm run build` and the `public` output directory. The HTML loads `/app-config.js` before the application script, and every API request is built through `apiUrl(path)`.

The web app header shows the effective backend as `API backend: ...` (or `relative` locally). Browser console diagnostics also print the configured base URL and the complete URL used for each API request. Network errors shown in the page include the request URL, HTTP method, and browser error message.

The FastAPI backend must run from the repository Dockerfile on a service with Docker and enough resources for FreeCAD, such as Render, Railway, Fly.io, or a VPS. Vercel must not run the FreeCAD backend.

The backend allows `https://reverseparts-cad-ai.vercel.app`, `localhost`, and `127.0.0.1` by default. To replace or extend the explicit production origins on Render, configure:

```text
CORS_ALLOW_ORIGINS=https://your-reverseparts-frontend.vercel.app
```

Multiple origins can be comma-separated. For a temporary unrestricted integration test, set `CORS_ALLOW_ORIGINS=*`; restrict it again before normal production use.

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

Current dataset cases:

- `tests/dataset/staffa_test_1/`: bent sheet-metal bracket with laser features and two flanges.
- `tests/dataset/lamiera_piana_test_1/`: flat sheet-metal laser-only part with circular holes and no bending operation.
- `tests/dataset/staffa_1_piega_test_1/`: simple sheet-metal bracket with two circular holes and one bending operation.
- `tests/dataset/staffa_u_test_1/`: U-shaped sheet-metal bracket with four circular holes and two bending operations.
- `tests/dataset/staffa_16_pieghe_stress_test/`: complex sheet-metal stress test without rigid geometric evaluation; it validates robust analysis, complexity warnings, and quote generation.

CAD feature tolerances are configurable in `config/analysis_default.json`. The current settings control circular-hole center, diameter, and axis matching plus bend surface pairing. Stress-test folders may omit `expected.json` and `evaluation.json`; the dataset evaluator skips those cases while analysis and quote generation still process them.

Hole analysis separates circular, elongated, polygonal, and formed openings. Formed openings include raised collars, embossed holes, and shaped apertures represented by non-circular inner loops. The analysis JSON reports per-category counts and `total_holes`; the quote uses `total_holes + 1` as the laser pierce count when cut-length data is available.

Process every dataset folder inside Docker:

```powershell
.\scripts\analyze_dataset.ps1
.\scripts\evaluate_dataset.ps1
.\scripts\quote_dataset.ps1
```

The older `analyze_staffa`, `evaluate_staffa`, and `quote_staffa` scripts are kept for direct STAFFA TEST 1 compatibility.

## Test Fixtures

`tests/test_files/` is intentionally kept ready for real CAD files such as `STAFFA TEST 1.stp`. Ground-truth JSON files can be added under `tests/ground_truth/`.
