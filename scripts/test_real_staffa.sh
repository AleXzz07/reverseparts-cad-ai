#!/usr/bin/env sh
set -eu

docker build -t reverseparts-cad-ai .
docker run --rm reverseparts-cad-ai pytest tests -v
