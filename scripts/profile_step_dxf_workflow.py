#!/usr/bin/env python3
"""Profile STEP-only and optional DXF stages without rendering/Viewer work."""
import importlib.util
import json
from pathlib import Path
import resource
import time

ROOT=Path.cwd()
STEP=ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step'
DXF=ROOT/'tests/dataset/step_dxf_workflow/sm07_reference.dxf'


def main():
    from app.cad_analyzer import analyze_step_file
    sha=__import__('hashlib').sha256(STEP.read_bytes()).hexdigest()
    start=time.perf_counter()
    analysis=analyze_step_file(file_bytes=STEP.read_bytes(),source_file='fixture.step',
                               material='acciaio',density_g_cm3=7.85).model_dump()
    step_sec=time.perf_counter()-start
    result={'step_sha256':sha,'step_analysis_sec':round(step_sec,5),
            'step_only_flat_status':analysis['flat_pattern']['status'],
            'dxf_parse_sec':None,'crosscheck_sec':None,'flat_preview_sec':None,
            'peak_rss_mib':None}
    if importlib.util.find_spec('app.dxf_workflow'):
        from app.dxf_workflow import analyze_dxf,validate_step_dxf
        start=time.perf_counter();parsed=analyze_dxf(DXF.read_bytes())
        result['dxf_parse_sec']=round(time.perf_counter()-start,5)
        start=time.perf_counter();verified=validate_step_dxf(analysis,parsed)
        result['crosscheck_sec']=round(time.perf_counter()-start,5)
        start=time.perf_counter();preview=parsed['contours'] if verified['passed'] else None
        result['flat_preview_sec']=round(time.perf_counter()-start,5)
        result['dxf_validation_status']=verified['status']
        result['flat_preview_contours']=len(preview or [])
    result['peak_rss_mib']=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
