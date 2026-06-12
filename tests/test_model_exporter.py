import json
import struct

from app.model_exporter import GLB_MAGIC, GLB_VERSION, _build_glb


def test_build_glb_creates_valid_container():
    glb = _build_glb(
        [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)],
        [(0, 1, 2)],
    )

    magic, version, total_length = struct.unpack("<III", glb[:12])
    json_length, json_type = struct.unpack("<II", glb[12:20])
    document = json.loads(glb[20:20 + json_length].decode("utf-8"))

    assert magic == GLB_MAGIC
    assert version == GLB_VERSION
    assert total_length == len(glb)
    assert json_type == 0x4E4F534A
    assert document["asset"]["version"] == "2.0"
    assert document["meshes"][0]["primitives"][0]["attributes"] == {
        "POSITION": 0,
        "NORMAL": 1,
    }

