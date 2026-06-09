param(
    [Parameter(Position = 0)]
    [int]$Quantity = 1,

    [string]$Material = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ActualPath = Join-Path $ProjectRoot "tests\output\staffa_test_1_actual.json"

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

$QuoteArgs = @(
    "--actual", "/workspace/tests/output/staffa_test_1_actual.json",
    "--output", "/workspace/tests/output/staffa_test_1_quote.json",
    "--quantity", "$Quantity"
)

if (-not [string]::IsNullOrWhiteSpace($Material)) {
    $QuoteArgs += @("--material", $Material)
}

docker run --rm `
    -v "${ProjectRoot}:/workspace" `
    -w /workspace `
    reverseparts-cad-ai `
    python3 -m app.quote_engine @QuoteArgs

exit $LASTEXITCODE
