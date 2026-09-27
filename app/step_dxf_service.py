"""Crosschecked DXF quote adapter. Production STEP analysis is not rerun."""
from __future__ import annotations

from copy import deepcopy

from .dxf_workflow import analyze_dxf,validate_step_dxf
from .quote_engine import quote_from_cad,load_materials_config


def attach_supplied_dxf(*,analysis, dxf_bytes, quantity, material,
                        pricing_overrides=None, material_overrides=None, welds=None):
    parsed=analyze_dxf(dxf_bytes)
    validation=validate_step_dxf(analysis,parsed)
    combined=deepcopy(analysis)
    trusted_step_flat=bool((analysis.get('flat_pattern') or {}).get('usable_for_costing')
                           and (analysis.get('flat_pattern') or {}).get('validation',{}).get('passed'))
    provenance={'thickness_source':'step','bend_source':'step',
                'flat_geometry_source':'dxf' if validation['passed'] else 'unavailable',
                'laser_cut_length_source':'dxf' if validation['passed'] else 'unavailable',
                'flat_validation_source':'step_dxf_crosscheck' if validation['passed'] else 'unavailable'}
    if validation['passed'] and trusted_step_flat:
        provenance['flat_geometry_source']='step'
        provenance['laser_cut_length_source']='step'
        provenance['flat_validation_source']='step'
    elif validation['passed']:
        flat=combined['flat_pattern']
        flat.update(available=True,status='validated_estimate',usable_for_costing=True,
                    is_estimate=False,method='user-supplied DXF crosschecked against STEP',
                    confidence='high',net_developed_area_mm2=parsed['net_area_mm2'],
                    opening_area_mm2=parsed['openings_area_mm2'],
                    gross_blank_area_mm2=parsed['gross_area_mm2'],
                    blank_dimensions_mm=parsed['blank_dimensions_mm'],
                    outer_perimeter_mm=parsed['outer_perimeter_mm'],
                    inner_perimeter_mm=parsed['inner_perimeter_mm'],
                    total_cut_length_mm=parsed['total_cut_length_mm'],
                    propagated_opening_count=parsed['inner_contours'],
                    validation={'graph_connected':True,'topology_continuous':True,
                                'self_intersections':0,'overlap_area_mm2':0.,
                                'area_coherence_error_pct':validation['area_error_pct'],
                                'perimeter_coherence_error_pct':None,
                                'all_openings_propagated':True,'passed':True})
        material_conf=load_materials_config().get(material.lower())
        density=(material_overrides or {}).get('density_g_cm3') or (
            material_conf or {}).get('density_g_cm3')
        thickness=analysis.get('detected_thickness_mm')
        flat['blank_weight_kg']=(round(parsed['gross_area_mm2']*float(thickness)*float(density)/1_000_000,6)
                                 if thickness and density else None)
        combined['cutting'].update(total_cut_length_mm=parsed['total_cut_length_mm'],
                                   outer_cut_length_mm=parsed['outer_perimeter_mm'],
                                   inner_cut_length_mm=parsed['inner_perimeter_mm'],
                                   confidence='high')
    else:
        # A conflicting supplied DXF suspends costing until removed/replaced,
        # even when the STEP originally had its own valid flat.
        combined['flat_pattern'].update(status='partial',usable_for_costing=False)
        combined['flat_pattern']['validation']['passed']=False
        combined['cutting'].update(total_cut_length_mm=None,
                                   outer_cut_length_mm=None,inner_cut_length_mm=None)
    quote=quote_from_cad(combined,quantity=quantity,material=material,
                         pricing_overrides=pricing_overrides,
                         material_overrides=material_overrides,welds=welds)
    return {'analysis':combined,'quote':quote,'dxf_validation':validation,
            'workflow_mode':('validated_step_flat' if trusted_step_flat else 'validated_dxf_flat')
                            if validation['passed'] else 'no_valid_flat',
            'data_provenance':provenance,
            'flat_preview':{'contours':parsed['contours'],'thickness_mm':analysis.get('detected_thickness_mm')}
                            if validation['passed'] else None,
            'timings_sec':{'dxf_parse':parsed['parser_sec'],
                           'step_dxf_validation':validation['validation_sec']}}
