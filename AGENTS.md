# Reverseparts CAD AI Agent Notes

This repository contains a Python FastAPI backend for REVERSEPARTS CAD analysis.

## Principles

- Do not invent geometric or manufacturing data.
- Keep declared, measured, and estimated values separate.
- Return `null`, empty arrays, low confidence, and warnings when a feature is not reliable.
- Treat STEP/STP parsing as a best-effort operation backed by FreeCAD.
- Keep API responses deterministic and schema-compatible.

## Useful Commands

```powershell
pytest
docker build -t reverseparts-cad-ai .
docker run --rm -p 8000:8000 reverseparts-cad-ai
```

## Test Data

Place real STEP fixtures in `tests/test_files/`. The project is prepared for a real `STAFFA TEST 1.stp` fixture, but no proprietary CAD file is committed by default.
