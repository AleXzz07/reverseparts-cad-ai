param(
    [Parameter(Position = 0)]
    [int]$Quantity = 1,

    [string]$Material = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

docker build -t reverseparts-cad-ai $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$QuoteArgs = @(
    "quote",
    "--dataset-dir", "/workspace/tests/dataset",
    "--quantity", "$Quantity"
)

if (-not [string]::IsNullOrWhiteSpace($Material)) {
    $QuoteArgs += @("--material", $Material)
}

docker run --rm `
    -v "${ProjectRoot}:/workspace" `
    -w /workspace `
    reverseparts-cad-ai `
    python3 -m app.dataset_runner @QuoteArgs

exit $LASTEXITCODE
