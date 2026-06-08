$ErrorActionPreference = "Stop"

docker build -t reverseparts-cad-ai .
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

docker run --rm reverseparts-cad-ai pytest tests -v
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
