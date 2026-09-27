#!/usr/bin/env python3
"""Derive DXF contours only from the benchmark's validated unfolded STEP.

This fixture is a technical parser/workflow reference, not an industrial DXF.
Requires the real Docker FreeCAD runtime. No folded geometry is unfolded here.
"""
import argparse
import math
from pathlib import Path

import ezdxf

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/reference_unfold.step'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('/tmp/sm07_reference_regenerated.dxf'))
    args=parser.parse_args()
    from app.cad_analyzer import _configure_freecad_path
    _configure_freecad_path()
    import FreeCAD  # noqa: F401
    import Part
    shape=Part.Shape();shape.read(str(SOURCE))
    face=max((face for face in shape.Faces if face.Surface.TypeId=='Part::GeomPlane'),
             key=lambda face:float(face.Area))
    if len(face.Wires)!=2 or abs(float(face.Area)-7440.4955592149645)>.01:
        raise ValueError('La geometria di riferimento SM07 non coincide')
    doc=ezdxf.new('R2010');doc.header['$INSUNITS']=4
    doc.layers.new('CUT');model=doc.modelspace()
    for wire in face.Wires:
        for edge in wire.Edges:
            first,last=(float(x) for x in edge.ParameterRange)
            p=edge.valueAt(first);q=edge.valueAt(last)
            curve=edge.Curve
            name=type(curve).__name__
            if name in {'Line','LineSegment'}:
                model.add_line((p.x,p.y),(q.x,q.y),dxfattribs={'layer':'CUT'})
            elif name=='Circle':
                c=curve.Center;r=float(curve.Radius)
                start=math.degrees(math.atan2(p.y-c.y,p.x-c.x))
                end=math.degrees(math.atan2(q.y-c.y,q.x-c.x))
                sweep=math.degrees(float(edge.Length)/r)
                if abs((end-start)%360-sweep)<1e-4:angle=start
                elif abs((start-end)%360-sweep)<1e-4:angle=end
                else:raise ValueError('Orientamento arco SM07 non verificabile')
                model.add_arc((c.x,c.y),r,angle,angle+sweep,
                              dxfattribs={'layer':'CUT'})
            else:raise ValueError('Curva riferimento SM07 non supportata: '+name)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    doc.saveas(args.output)
    from app.dxf_workflow import analyze_dxf
    flat=analyze_dxf(args.output.read_bytes())
    if not flat['passed'] or abs(flat['net_area_mm2']-float(face.Area))>.01:
        raise ValueError('DXF generato non coincide con la faccia piana di riferimento')
    print('SM07 reference flat DXF generated and verified: area',flat['net_area_mm2'],
          'opening count',flat['inner_contours'])


if __name__=='__main__':main()
