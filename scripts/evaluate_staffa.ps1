$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ActualPath = Join-Path $ProjectRoot "tests\output\staffa_test_1_actual.json"
$ExpectedPath = Join-Path $ProjectRoot "tests\ground_truth\staffa_test_1_expected.json"
$OutputPath = Join-Path $ProjectRoot "tests\output\staffa_test_1_evaluation.json"

docker build -t reverseparts-cad-ai $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not (Test-Path $ActualPath)) {
    & (Join-Path $PSScriptRoot "analyze_staffa.ps1")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

docker run --rm `
    -v "${ProjectRoot}:/workspace" `
    -w /workspace `
    reverseparts-cad-ai `
    python3 -m app.evaluator `
    --actual /workspace/tests/output/staffa_test_1_actual.json `
    --expected /workspace/tests/ground_truth/staffa_test_1_expected.json `
    --output /workspace/tests/output/staffa_test_1_evaluation.json

exit $LASTEXITCODE
