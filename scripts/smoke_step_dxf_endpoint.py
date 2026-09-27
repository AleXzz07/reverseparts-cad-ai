#!/usr/bin/env python3
"""Live SM07 API probe; uploads an existing validated reference DXF.

Run against a locally started candidate container, never against Render.
"""
import argparse
import json
from pathlib import Path
import time

import httpx


ROOT=Path(__file__).resolve().parents[1]
STEP=ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step'
DXF=ROOT/'tests/dataset/step_dxf_workflow/sm07_reference.dxf'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    with httpx.Client(base_url=args.url,timeout=180.) as client:
        started=time.perf_counter()
        response=client.post('/analyze-and-quote',data={'material':'acciaio','quantity':'1'},
            files={'file':('sm07.step',STEP.read_bytes(),'application/octet-stream')})
        response.raise_for_status();baseline=response.json()
        analysis_sec=time.perf_counter()-started
        assert baseline['analysis']['step_sha256'] and baseline['analysis_receipt']
        start=time.perf_counter()
        attached=client.post('/attach-flat-dxf',
            data={'material':'acciaio','quantity':'1',
                  'analysis_json':json.dumps(baseline['analysis']),
                  'receipt':baseline['analysis_receipt']},
            files={'file':('reference.dxf',DXF.read_bytes(),'application/dxf'),
                   'step_file':('sm07.step',STEP.read_bytes(),'application/octet-stream')})
        attached.raise_for_status();body=attached.json()
        attach_sec=time.perf_counter()-start
        assert body['dxf_validation']['passed'],body['dxf_validation']
        assert body['quote']['estimated_internal_cost_eur']['laser'] is not None
        assert body['flat_preview']['contours']
        wrong=client.post('/attach-flat-dxf',
            data={'material':'acciaio','quantity':'1',
                  'analysis_json':json.dumps(baseline['analysis']),
                  'receipt':baseline['analysis_receipt']},
            files={'file':('reference.dxf',DXF.read_bytes(),'application/dxf'),
                   'step_file':('other.step',b'incorrect STEP','application/octet-stream')})
        assert wrong.status_code==409,wrong.text
        markup=client.get('/').text
        assert 'id="flat-tab"' in markup and 'id="viewer-tab"' in markup
        assert client.get('/vendor/flat_viewer.js').status_code==200
        print(json.dumps({'step_analysis_sec':round(analysis_sec,4),
            'dxf_request_sec':round(attach_sec,4),'dxf_steps_sec':body['timings_sec'],
            'dxf_status':body['dxf_validation']['status'],
            'workflow_mode':body['workflow_mode'],'flat_contours':len(body['flat_preview']['contours']),
            'wrong_step_http':wrong.status_code,'dual_viewer_assets_served':True},indent=2))


if __name__=='__main__':main()
