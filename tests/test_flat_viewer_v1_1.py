"""The browser camera test uses the preview of the verified SM07 reference DXF."""
import json
from pathlib import Path

from app.dxf_workflow import analyze_dxf


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/dataset/step_dxf_workflow'


def test_sm07_flat_viewer_preview_matches_verified_dxf():
    parsed = analyze_dxf((FIXTURE / 'sm07_reference.dxf').read_bytes())
    assert parsed['passed']
    assert (parsed['outer_contours'], parsed['inner_contours']) == (1, 1)
    preview = json.loads((FIXTURE / 'sm07_flat_preview.json').read_text())
    assert preview == {'contours': parsed['contours'], 'thickness_mm': 2.0}
    assert len(preview['contours'][1]['points']) >= 30
