$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ActualPath = Join-Path $ProjectRoot "tests\output\staffa_test_1_actual.json"
$Quantity = 1

if ($args.Count -gt 0) {
    $Quantity = [int]$args[0]
}

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
    python3 -m app.quote_engine `
    --actual /workspace/tests/output/staffa_test_1_actual.json `
    --output /workspace/tests/output/staffa_test_1_quote.json `
    --quantity $Quantity

exit $LASTEXITCODE
