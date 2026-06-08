$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputDir = Join-Path $ProjectRoot "tests\output"
$TempScript = Join-Path $OutputDir "_analyze_staffa_tmp.py"
New-Item -ItemType Directory -Force $OutputDir | Out-Null

docker build -t reverseparts-cad-ai $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$PythonCode = @'
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, "/app")
from app.main import app


root = Path("/workspace")
input_path = root / "tests" / "test_files" / "STAFFA TEST 1.stp"
output_path = root / "tests" / "output" / "staffa_test_1_actual.json"

client = TestClient(app)
with input_path.open("rb") as step_file:
    response = client.post(
        "/analyze-cad",
        data={
            "material": "alluminio",
            "density_g_cm3": "2.70",
            "declared_thickness_mm": "2.0",
            "quantity": "1",
        },
        files={"file": ("STAFFA TEST 1.stp", step_file, "application/step")},
    )

if response.status_code != 200:
    print(response.text)
    raise SystemExit(response.status_code)

payload = response.json()
formatted = json.dumps(payload, indent=2, ensure_ascii=False)
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(formatted + "\n", encoding="utf-8")
print(formatted)
'@

Set-Content -Path $TempScript -Value $PythonCode -Encoding UTF8

docker run --rm `
    -v "${ProjectRoot}:/workspace" `
    -w /workspace `
    reverseparts-cad-ai `
    python3 /workspace/tests/output/_analyze_staffa_tmp.py
$RunExitCode = $LASTEXITCODE
Remove-Item -LiteralPath $TempScript -Force

if ($RunExitCode -ne 0) {
    exit $RunExitCode
}
