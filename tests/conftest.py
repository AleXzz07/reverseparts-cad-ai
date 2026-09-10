"""Pytest bootstrap for native FreeCAD modules.

The production API deliberately keeps FreeCAD out of the Uvicorn process.
Some geometry unit tests still exercise FreeCAD helpers directly, so Docker
must load the native modules on pytest's main thread before TestClient starts
worker threads. A late first import of Part can otherwise crash inside the
native extension.
"""

from __future__ import annotations

import importlib
import os


if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
    from app.cad_analyzer import _configure_freecad_path

    _configure_freecad_path()
    importlib.import_module("FreeCAD")
    importlib.import_module("Part")
