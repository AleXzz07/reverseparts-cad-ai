$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Quantity = 1

if ($args.Count -gt 0) {
    $Quantity = [int]$args[0]
}

docker build -t reverseparts-cad-ai $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

docker run --rm `
    -v "${ProjectRoot}:/workspace" `
    -w /workspace `
    reverseparts-cad-ai `
    python3 -m app.dataset_runner analyze `
    --dataset-dir /workspace/tests/dataset `
    --quantity $Quantity

exit $LASTEXITCODE
