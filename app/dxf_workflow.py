"""Optional DXF flat pipeline; no FreeCAD and no STEP Viewer code imports.

Only a single closed exterior with disjoint enclosed holes can supply laser
geometry. All other situations fail closed. Entity curves retain exact arc
area/perimeter; preview uses bounded sagitta sampling independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
import io
import math
import time

import ezdxf


@dataclass(frozen=True)
class DxfLimits:
    endpoint_mm: float = 0.0001
    area_pct: float = 3.0  # includes sheet-neutral allowance/model uncertainty
    hole_diameter_pct: float = 3.0
    preview_sagitta_mm: float = 0.02
    max_entities: int = 12000
    max_bytes: int = 12_000_000


LIMITS = DxfLimits()
UNITS_MM = {4:1., 1:25.4, 2:304.8, 5:10., 6:1000., 14:100.}
NON_CUT_LAYERS = {'BEND', 'BENDS', 'CENTER', 'CENTRE', 'CONSTRUCTION', 'ETCH', 'FOLD'}


@dataclass
class Segment:
    start: tuple[float,float]
    end: tuple[float,float]
    center: tuple[float,float] | None = None
    radius: float = 0.
    sweep: float = 0.  # signed radians; a full circle is 2*pi
    layer: str = '0'

    def reversed(self):
        return Segment(self.end,self.start,self.center,self.radius,-self.sweep,self.layer)

    def length(self):
        return abs(self.radius*self.sweep) if self.center else math.dist(self.start,self.end)

    def signed_area(self):
        if self.center is None:
            return (self.start[0]*self.end[1]-self.end[0]*self.start[1])/2
        cx,cy=self.center; a=math.atan2(self.start[1]-cy,self.start[0]-cx)
        b=a+self.sweep
        return (self.radius*cx*(math.sin(b)-math.sin(a))
                +self.radius*cy*(math.cos(a)-math.cos(b))
                +self.radius*self.radius*self.sweep)/2

    def sample(self,sagitta=LIMITS.preview_sagitta_mm):
        if self.center is None:return [self.start,self.end]
        step=2*math.acos(max(-1.,min(1.,1-sagitta/self.radius)))
        count=max(8 if abs(self.sweep)>6 else 1,math.ceil(abs(self.sweep)/max(step,1e-5)))
        if count>10000:raise ValueError('Curva troppo densa per la verifica')
        angle=math.atan2(self.start[1]-self.center[1],self.start[0]-self.center[0])
        points=[(self.center[0]+self.radius*math.cos(angle+self.sweep*i/count),
                 self.center[1]+self.radius*math.sin(angle+self.sweep*i/count))
                for i in range(count+1)]
        if abs(abs(self.sweep)-2*math.pi)>1e-8:points[-1]=self.end
        return points


def _arc(p,q,bulge,layer):
    if abs(bulge)<1e-12:return Segment(p,q,layer=layer)
    length=math.dist(p,q)
    if length<=LIMITS.endpoint_mm:raise ValueError('Arco con estremi coincidenti')
    sweep=4*math.atan(bulge)
    radius=length/(2*abs(math.sin(sweep/2)))
    mid=((p[0]+q[0])/2,(p[1]+q[1])/2)
    left=(-(q[1]-p[1])/length,(q[0]-p[0])/length)
    distance=length*(1-bulge*bulge)/(4*bulge)
    center=(mid[0]+left[0]*distance,mid[1]+left[1]*distance)
    return Segment(p,q,center,radius,sweep,layer)


def _parse_entities(doc,scale):
    segments=[]; warnings=[]; rejected=False
    entities=list(doc.modelspace())
    if len(entities)>LIMITS.max_entities:raise ValueError('Troppe entità DXF')
    for entity in entities:
        kind=entity.dxftype();layer=entity.dxf.layer.upper()
        if layer in NON_CUT_LAYERS:
            warnings.append('Layer non di taglio escluso: '+layer)
            continue
        extrusion=entity.dxf.get('extrusion',(0,0,1))
        if any(abs(float(a)-b)>1e-9 for a,b in zip(extrusion,(0,0,1))):
            rejected=True;warnings.append('Entità fuori piano XY');continue
        if kind=='LINE':
            a,b=entity.dxf.start,entity.dxf.end
            if abs(a.z)>LIMITS.endpoint_mm or abs(b.z)>LIMITS.endpoint_mm:
                rejected=True;warnings.append('LINE non planare');continue
            segments.append(Segment((a.x*scale,a.y*scale),(b.x*scale,b.y*scale),layer=layer))
        elif kind=='ARC':
            c=entity.dxf.center;r=float(entity.dxf.radius)*scale
            if abs(c.z)>LIMITS.endpoint_mm:
                rejected=True;warnings.append('ARC non planare');continue
            start=math.radians(float(entity.dxf.start_angle));end=math.radians(float(entity.dxf.end_angle))
            sweep=(end-start)%(2*math.pi)
            if r<=0 or sweep<1e-8:raise ValueError('Arco degenerato')
            center=(c.x*scale,c.y*scale)
            p=(center[0]+r*math.cos(start),center[1]+r*math.sin(start))
            q=(center[0]+r*math.cos(start+sweep),center[1]+r*math.sin(start+sweep))
            segments.append(Segment(p,q,center,r,sweep,layer))
        elif kind=='CIRCLE':
            c=entity.dxf.center;r=float(entity.dxf.radius)*scale
            if abs(c.z)>LIMITS.endpoint_mm:
                rejected=True;warnings.append('CIRCLE non planare');continue
            if r<=0:raise ValueError('Cerchio degenere')
            p=(c.x*scale+r,c.y*scale)
            segments.append(Segment(p,p,(c.x*scale,c.y*scale),r,2*math.pi,layer))
        elif kind in {'LWPOLYLINE','POLYLINE'}:
            if kind=='LWPOLYLINE':
                if abs(float(entity.dxf.get('elevation',0)))>LIMITS.endpoint_mm:
                    rejected=True;warnings.append('Polilinea non planare');continue
                pts=[(float(x)*scale,float(y)*scale,float(b)) for x,y,b in entity.get_points('xyb')]
            else:
                if not entity.is_2d_polyline:rejected=True;warnings.append('POLYLINE non 2D');continue
                if any(abs(float(v.dxf.location.z))>LIMITS.endpoint_mm for v in entity.vertices):
                    rejected=True;warnings.append('Polilinea non planare');continue
                pts=[(v.dxf.location.x*scale,v.dxf.location.y*scale,float(v.dxf.get('bulge',0))) for v in entity.vertices]
            if len(pts)<2:rejected=True;warnings.append('Polilinea degenerata');continue
            for i in range(len(pts) if entity.is_closed else len(pts)-1):
                p,q=pts[i],pts[(i+1)%len(pts)]
                segments.append(_arc((p[0],p[1]),(q[0],q[1]),p[2],layer))
            if not entity.is_closed:rejected=True;warnings.append('Polilinea aperta')
        elif kind=='SPLINE':
            rejected=True;warnings.append('SPLINE: verifica curve non supportata')
        else:
            rejected=True;warnings.append('Entità di taglio non supportata: '+kind)
    return segments,warnings,rejected


def _loops(segments):
    # Endpoint welding uses an exact tolerance and requires a degree-two graph;
    # duplicate edges and coincident full circles are rejected, never deleted.
    vertices=[]; adjacency=defaultdict(list); pending=[]; signatures=set();duplicates=0
    def node(p):
        for i,q in enumerate(vertices):
            if math.dist(p,q)<=LIMITS.endpoint_mm:return i
        vertices.append(p);return len(vertices)-1
    circles=[]
    for segment in segments:
        if segment.length()<LIMITS.endpoint_mm:raise ValueError('Segmento di lunghezza nulla')
        a=node(segment.start);b=node(segment.end)
        key=(min(a,b),max(a,b),round(segment.signed_area() if a==b else abs(segment.sweep),9),
             round(segment.radius,7),tuple(round(x,5) for x in segment.center) if segment.center else ())
        if key in signatures:duplicates+=1
        signatures.add(key)
        if a==b and abs(abs(segment.sweep)-2*math.pi)<1e-8:circles.append([segment]);continue
        if a==b:raise ValueError('Arco con autochiusura ambigua')
        index=len(pending);pending.append((segment,a,b))
        adjacency[a].append(index);adjacency[b].append(index)
    if duplicates:return [],len(adjacency),duplicates
    open_nodes=sum(len(items)!=2 for items in adjacency.values())
    if open_nodes:return [],open_nodes,0
    seen=set();out=list(circles)
    for start,(s,a,b) in enumerate(pending):
        if start in seen:continue
        current=a;edge=start;chain=[]
        while edge not in seen:
            seen.add(edge);item,left,right=pending[edge]
            chain.append(item if current==left else item.reversed())
            current=right if current==left else left
            remaining=[i for i in adjacency[current] if i not in seen]
            if not remaining:break
            edge=remaining[0]
        if current!=a:return [],1,0
        out.append(chain)
    return out,0,0


def _polygon(loop):
    result=[]
    for s in loop:result.extend(s.sample()[:-1])
    return result


def _contains(p,polygon):
    x,y=p;inside=False
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:inside=not inside
    return inside


def _intersects(a,b):
    def ccw(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    if max(a[0][0],a[1][0])+LIMITS.endpoint_mm<min(b[0][0],b[1][0]) or max(b[0][0],b[1][0])+LIMITS.endpoint_mm<min(a[0][0],a[1][0]):return False
    if max(a[0][1],a[1][1])+LIMITS.endpoint_mm<min(b[0][1],b[1][1]) or max(b[0][1],b[1][1])+LIMITS.endpoint_mm<min(a[0][1],a[1][1]):return False
    v=(ccw(a[0],a[1],b[0])*ccw(a[0],a[1],b[1])<=0 and
       ccw(b[0],b[1],a[0])*ccw(b[0],b[1],a[1])<=0)
    return v


def analyze_dxf(data:bytes):
    start=time.perf_counter(); result={'status':'invalid','passed':False,'warnings':[],
        'units':None,'closed_contours':0,'open_contours':0,'outer_contours':0,
        'inner_contours':0,'net_area_mm2':None,'gross_area_mm2':None,
        'openings_area_mm2':None,'outer_perimeter_mm':None,'inner_perimeter_mm':None,
        'total_cut_length_mm':None,'blank_dimensions_mm':None,'contours':[],
        'parser_sec':0.}
    def finish():result['parser_sec']=round(time.perf_counter()-start,5);return result
    if len(data)>LIMITS.max_bytes or not data:
        result['warnings'].append('Dimensione DXF non consentita');return finish()
    try:
        doc=ezdxf.read(io.StringIO(data.decode('utf-8-sig')))
        units=int(doc.header.get('$INSUNITS',0))
        result['units']=units
        if units not in UNITS_MM:
            result['warnings'].append('Unità DXF non dichiarate o non supportate');return finish()
        factor=UNITS_MM[units]
        segments,warnings,bad=_parse_entities(doc,factor)
        result['warnings']+=warnings
        if not segments or bad:return finish()
        contours,opened,duplicates=_loops(segments)
        result['open_contours']=opened
        if duplicates:result['warnings'].append('Entità di taglio duplicate')
        if opened:result['warnings'].append('Contorno aperto o nodo ambiguo')
        if opened or duplicates:return finish()
        result['closed_contours']=len(contours)
        signed=[sum(s.signed_area() for s in loop) for loop in contours]
        polys=[_polygon(loop) for loop in contours]
        if sum(len(p) for p in polys)>1800:
            result['warnings'].append('Contorno troppo denso per controllo intersezioni');return finish()
        if any(len(p)<3 or abs(area)<=LIMITS.endpoint_mm for p,area in zip(polys,signed)):
            result['warnings'].append('Contorno degenere');return finish()
        # Reject intersections/touching holes; false positives near shared
        # corners inside the same loop are excluded by their index distance.
        lines=[[ (p[i],p[(i+1)%len(p)]) for i in range(len(p))] for p in polys]
        for ci,contour in enumerate(lines):
            for i,a in enumerate(contour):
                for cj in range(ci,len(lines)):
                    for j,b in enumerate(lines[cj]):
                        if ci==cj and (abs(i-j)<=1 or {i,j}=={0,len(contour)-1}):continue
                        if _intersects(a,b):
                            result['warnings'].append('Contorni che si intersecano o toccano');return finish()
        outside=[i for i,p in enumerate(polys) if not any(
            _contains(p[0],other) for j,other in enumerate(polys) if j!=i)]
        result['outer_contours']=len(outside)
        if len(outside)!=1:
            result['warnings'].append('Numero contorni esterni diverso da uno');return finish()
        exterior=outside[0]
        holes=[i for i in range(len(polys)) if i!=exterior]
        if any(not _contains(polys[i][0],polys[exterior]) or
               any(_contains(polys[i][0],polys[j]) for j in holes if j!=i) for i in holes):
            result['warnings'].append('Isole o aperture annidate non supportate');return finish()
        outer=abs(signed[exterior]);opening=sum(abs(signed[i]) for i in holes)
        perim=sum(s.length() for s in contours[exterior])
        inside=sum(sum(s.length() for s in contours[i]) for i in holes)
        coords=[p for row in polys for p in row]
        width=max(p[0] for p in coords)-min(p[0] for p in coords)
        height=max(p[1] for p in coords)-min(p[1] for p in coords)
        if min(width,height)<=0 or outer<=opening:
            result['warnings'].append('Area o dimensioni non valide');return finish()
        ordered=[exterior]+holes
        openings=[]
        for i in holes:
            loop=contours[i]
            circle=(bool(loop) and all(s.center is not None and
                 math.dist(s.center,loop[0].center)<=LIMITS.endpoint_mm and
                 abs(s.radius-loop[0].radius)<=LIMITS.endpoint_mm for s in loop)
                 and abs(abs(sum(s.sweep for s in loop))-2*math.pi)<1e-5)
            px=[point[0] for point in polys[i]];py=[point[1] for point in polys[i]]
            openings.append({'type':'circular' if circle else 'other',
                'diameter_mm':round(loop[0].radius*2,5) if circle else None,
                'dimensions_mm':sorted((round(max(px)-min(px),5),round(max(py)-min(py),5)),reverse=True),
                'area_mm2':round(abs(signed[i]),5)})
        result.update(status='parsed',passed=True,inner_contours=len(holes),
            net_area_mm2=round(outer-opening,5),gross_area_mm2=round(outer,5),
            openings_area_mm2=round(opening,5),outer_perimeter_mm=round(perim,5),
            inner_perimeter_mm=round(inside,5),total_cut_length_mm=round(perim+inside,5),
            blank_dimensions_mm={'x':round(width,5),'y':round(height,5),'z':None},
            openings=openings,
            contours=[{'role':'outer' if k==exterior else 'inner','points':[[round(a,5),round(b,5)] for a,b in polys[k]],
                       'area_mm2':round(abs(signed[k]),5)} for k in ordered])
    except (UnicodeError,ValueError,ZeroDivisionError,OverflowError,ezdxf.DXFError) as exc:
        result['warnings'].append('DXF non analizzabile: '+type(exc).__name__)
    return finish()


def validate_step_dxf(analysis:dict, dxf:dict):
    start=time.perf_counter();record={'status':'rejected','passed':False,
        'units':dxf['units'],'closed_contours':dxf['closed_contours'],
        'open_contours':dxf['open_contours'],'outer_contours':dxf['outer_contours'],
        'inner_contours':dxf['inner_contours'],'net_area_mm2':dxf['net_area_mm2'],
        'step_material_area_mm2':None,'area_error_abs_mm2':None,'area_error_pct':None,
        'area_tolerance_pct':LIMITS.area_pct,'step_opening_count':None,
        'dxf_opening_count':dxf['inner_contours'],'warnings':list(dxf['warnings']),
        'validation_sec':0.}
    def done(status):
        record['status']=status;record['passed']=status=='verified'
        record['validation_sec']=round(time.perf_counter()-start,5)
        return record
    if not dxf['passed']:return done('rejected')
    flat=analysis.get('flat_pattern') or {}
    if (analysis.get('part_classification') or {}).get('category')!='sheet_metal':
        record['warnings'].append('STEP non classificato come singola lamiera');return done('rejected')
    if (analysis.get('geometry') or {}).get('solid_count')!=1:
        record['warnings'].append('Numero di solidi STEP non verificato');return done('rejected')
    thickness=analysis.get('detected_thickness_mm')
    if thickness is None or float(thickness)<=0 or analysis.get('thickness_confidence')!='high':
        record['warnings'].append('Spessore STEP non verificato con confidenza alta');return done('warning')
    volume=analysis.get('volume_cm3')
    if volume is None or float(volume)<=0:return done('warning')
    material=float(volume)*1000/float(thickness)
    error=abs(dxf['net_area_mm2']-material)
    pct=100*error/material
    record.update(step_material_area_mm2=round(material,5),area_error_abs_mm2=round(error,5),area_error_pct=round(pct,5))
    if pct>LIMITS.area_pct:
        factors=(10,25.4,1000)
        if any(abs(dxf['net_area_mm2']/(material*f*f)-1)<.04 or
               abs(dxf['net_area_mm2']*f*f/material-1)<.04 for f in factors):
            record['warnings'].append('Possibile errore di scala/unità DXF')
        else:record['warnings'].append('Area DXF incoerente con volume/spessore STEP')
        return done('rejected')
    holes=(analysis.get('holes') or {})
    count=holes.get('physical_openings_total')
    if count is None:
        record['warnings'].append('Numero aperture STEP non verificato');return done('warning')
    record['step_opening_count']=int(count)
    if int(count)!=dxf['inner_contours']:
        record['warnings'].append('Numero aperture DXF diverso dal STEP')
        return done('warning')
    # Circular hole diameter comparison only when there is a one-to-one,
    # unambiguous set of measured circular holes in both models.
    step_circular=[v.get('through_diameter_mm') or v.get('diameter_mm')
                   for v in holes.get('circular',[]) if v.get('through_diameter_mm') or v.get('diameter_mm')]
    dxf_circular=sorted(row['diameter_mm'] for row in dxf['openings'] if row['type']=='circular')
    measured=0
    if len(step_circular)==len(dxf_circular)>0:
        if any(abs(float(a)-b)>LIMITS.hole_diameter_pct*float(a)/100
               for a,b in zip(sorted(float(v) for v in step_circular),dxf_circular)):
            record['warnings'].append('Diametri circolari DXF incompatibili con STEP')
            return done('rejected')
        measured+=len(step_circular)
    elif step_circular or dxf_circular:
        record['warnings'].append('Diametri delle aperture non confrontabili in modo univoco')
        return done('warning')
    step_slots=[row for row in holes.get('elongated',[])
                if row.get('overall_length_mm') and row.get('width_mm')]
    dxf_others=[row for row in dxf['openings'] if row['type']!='circular']
    if step_slots:
        if len(step_slots)!=len(dxf_others):
            record['warnings'].append('Asole DXF non confrontabili con STEP')
            return done('warning')
        desired=sorted((sorted((float(row['overall_length_mm']),float(row['width_mm'])),reverse=True)
                        for row in step_slots))
        actual=sorted(row['dimensions_mm'] for row in dxf_others)
        if any(any(abs(a-b)>max(LIMITS.endpoint_mm,LIMITS.hole_diameter_pct*max(a,1)/100)
                   for a,b in zip(pair,geometry)) for pair,geometry in zip(desired,actual)):
            record['warnings'].append('Dimensioni asole DXF incompatibili con STEP')
            return done('rejected')
        measured+=len(step_slots)
    if int(count)>0 and measured!=int(count):
        record['warnings'].append('Aperture STEP senza dimensioni confrontabili nel DXF')
        return done('warning')
    if int(count)==0 and not (flat.get('validation') or {}).get('passed'):
        record['warnings'].append('Nessuna feature indipendente per associare il DXF allo STEP')
        return done('warning')
    return done('verified')
