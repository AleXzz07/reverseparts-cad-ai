#!/usr/bin/env python3
"""Read-only GLB SHA and raw-buffer comparison across frozen Docker images."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from app.model_exporter import export_step_to_glb
from scripts.compare_viewer_normals_glb import _buffers

ROOT=Path.cwd()
CASES={'plate':ROOT/'tests/dataset/lamiera_piana_test_1/input.stp',
       'simple':ROOT/'tests/test_files/STAFFA TEST 1.stp',
       'sm07':ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step',
       'complex':ROOT/'tests/dataset/staffa_16_pieghe_stress_test/input.stp'}


def fingerprint(case):
    path=CASES[case]
    os.environ.update(VIEWER_MODEL_MAX_TRIANGLES='50000',
        VIEWER_MODEL_TESSELLATION_RATIO='300',VIEWER_MODEL_CURVED_FACE_REFINEMENT='0.8')
    payload=export_step_to_glb(str(path))
    if not payload.get('available'):raise RuntimeError('Fixture Viewer non esportato')
    blob=base64.b64decode(payload['model_base64'])
    return {'case':case,'step_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'glb_sha256':hashlib.sha256(blob).hexdigest(),
            'buffer_sha256':{name:hashlib.sha256(raw).hexdigest() for name,raw in _buffers(blob).items()}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=CASES,default='complex')
    parser.add_argument('--compare',nargs=2,type=Path)
    args=parser.parse_args()
    if args.compare:
        a,b=(json.loads(p.read_text()) for p in args.compare)
        identical=a==b
        print(json.dumps({'same_glb_and_buffers':identical,'baseline':a,'candidate':b},indent=2))
        if not identical:raise SystemExit(2)
    else:print(json.dumps(fingerprint(args.case),indent=2))


if __name__=='__main__':main()
