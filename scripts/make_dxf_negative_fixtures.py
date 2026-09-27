#!/usr/bin/env python3
"""Create deterministic parser/crosscheck negatives from SM07 reference DXF."""
from pathlib import Path
import ezdxf

ROOT=Path(__file__).resolve().parents[1]
directory=ROOT/'tests/dataset/step_dxf_workflow'
source=directory/'sm07_reference.dxf'


def main():
    for variant in ('wrong_units','open','missing_opening','modified_geometry','duplicate'):
        doc=ezdxf.readfile(source);model=doc.modelspace();entities=list(model)
        if variant=='wrong_units':doc.header['$INSUNITS']=1
        elif variant=='open':model.delete_entity(entities[0])
        elif variant=='missing_opening':
            # Source is four outer edges followed by five arcs/lines of slot.
            for entity in entities[4:]:model.delete_entity(entity)
        elif variant=='modified_geometry':
            for entity in entities:
                if entity.dxftype()=='LINE':
                    entity.dxf.start=entity.dxf.start*1.05
                    entity.dxf.end=entity.dxf.end*1.05
                elif entity.dxftype()=='ARC':
                    entity.dxf.center=entity.dxf.center*1.05
                    entity.dxf.radius=entity.dxf.radius*1.05
        elif variant=='duplicate':
            model.add_line(entities[0].dxf.start,entities[0].dxf.end,
                           dxfattribs={'layer':'CUT'})
        doc.saveas(directory/f'sm07_{variant}.dxf')


if __name__=='__main__':main()
