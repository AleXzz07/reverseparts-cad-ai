"""DXF is a separate, optional source; wrong flats never unlock laser cost."""
import copy
import hashlib
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import ezdxf
from app.dxf_workflow import analyze_dxf,validate_step_dxf
from app.step_dxf_service import attach_supplied_dxf
from app.quote_engine import quote_from_cad
from app.step_dxf_receipt import analysis_receipt,valid_analysis_receipt


ROOT=Path(__file__).resolve().parents[1]
DXF=ROOT/'tests/dataset/step_dxf_workflow'
BENCHMARK=ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/reference_unfold.step'


def sm07_analysis():
    return {'part_name':'SM07','part_classification':{'category':'sheet_metal'},
        'geometry':{'solid_count':1},'detected_thickness_mm':2.,
        'thickness_confidence':'high','volume_cm3':14.9061239,
        'holes':{'physical_openings_total':1,'circular':[],
                 'elongated':[{'type':'elongated','overall_length_mm':24.,'width_mm':8.}]},
        'bends':{'count':1},'flat_pattern':{'status':'partial',
        'usable_for_costing':False,'validation':{'passed':False}},'cutting':{}}


def browser_json(analysis):
    """Model JSON.parse/stringify number changes, without requiring Node in Docker.

    The live Node smoke script performs the real JavaScript serialization.
    """
    def js_number(value):
        if isinstance(value,dict):return {k:js_number(v) for k,v in value.items()}
        if isinstance(value,list):return [js_number(v) for v in value]
        if isinstance(value,float) and value==0:return 0
        if isinstance(value,float) and value.is_integer() and abs(value)<=2**53-1:
            return int(value)
        return value
    return json.dumps(js_number(json.loads(json.dumps(analysis,allow_nan=False))),
                      ensure_ascii=False,separators=(',',':'))


class StepDxfWorkflowTest(unittest.TestCase):
    def test_real_saved_cad_analysis_with_signed_zeros_survives_browser(self):
        saved=json.loads((ROOT/'tests/dataset/staffa_u_test_1/actual.json').read_text())
        def count_signed_zeros(value):
            if isinstance(value,float):
                return int(value==0 and math.copysign(1,value)<0)
            if isinstance(value,list):return sum(map(count_signed_zeros,value))
            if isinstance(value,dict):return sum(map(count_signed_zeros,value.values()))
            return 0
        self.assertGreater(count_signed_zeros(saved),0)
        self.assertTrue(valid_analysis_receipt(json.loads(browser_json(saved)),
                                               analysis_receipt(saved)))

    def test_analysis_receipt_rejects_tampered_step_features(self):
        analysis={**sm07_analysis(),'step_sha256':'a'*64}
        receipt=analysis_receipt(analysis)
        self.assertTrue(valid_analysis_receipt(analysis,receipt))
        for field,value in [('volume_cm3',99),('detected_thickness_mm',3),
                            ('step_sha256','b'*64)]:
            with self.subTest(field=field):
                forged=copy.deepcopy(analysis);forged[field]=value
                self.assertFalse(valid_analysis_receipt(forged,receipt))
        for field,value in [('holes',0),('flat_pattern',True)]:
            with self.subTest(field=field):
                forged=copy.deepcopy(analysis)
                if field=='holes':forged['holes']['physical_openings_total']=value
                else:forged['flat_pattern']['validation']['passed']=value
                self.assertFalse(valid_analysis_receipt(forged,receipt))

    def test_receipt_survives_signed_zero_and_browser_numeric_roundtrip(self):
        analysis={**sm07_analysis(),'step_sha256':'a'*64}
        analysis['geometry']['center_of_mass_mm']={'x':-0.0,'y':1.0,'z':1e-7}
        analysis['numeric_roundtrip_probe']=1e21
        signed=analysis_receipt(analysis)
        browser=browser_json(analysis)
        self.assertIn('"x":0',browser)
        self.assertIn('"y":1',browser)
        self.assertIsInstance(json.loads(browser)['geometry']['center_of_mass_mm']['x'],int)
        self.assertTrue(valid_analysis_receipt(json.loads(browser),signed))
        self.assertFalse(valid_analysis_receipt({**analysis,'step_sha256':'b'*64},signed))

    def test_receipt_rejects_integer_not_exact_in_javascript(self):
        with self.assertRaises(ValueError):
            analysis_receipt({'numeric':2**53+1})
        self.assertTrue(valid_analysis_receipt({'numeric':10**21},
                                                analysis_receipt({'numeric':1e21})))

    def test_positive_reference_geometry_and_cost(self):
        flat=analyze_dxf((DXF/'sm07_reference.dxf').read_bytes())
        self.assertTrue(flat['passed'],flat['warnings'])
        self.assertEqual((flat['outer_contours'],flat['inner_contours']), (1,1))
        self.assertAlmostEqual(flat['net_area_mm2'],7440.4955592149645,places=3)
        self.assertAlmostEqual(flat['outer_perimeter_mm'],373.9587,places=3)
        self.assertAlmostEqual(flat['inner_perimeter_mm'],32+8*3.141592653589793,places=3)
        self.assertAlmostEqual(flat['blank_dimensions_mm']['x'],126.97935,places=3)
        self.assertEqual(flat['blank_dimensions_mm']['y'],60.)
        base=sm07_analysis()
        result=attach_supplied_dxf(analysis=base,dxf_bytes=(DXF/'sm07_reference.dxf').read_bytes(),
                                   quantity=1,material='acciaio')
        self.assertTrue(result['dxf_validation']['passed'])
        self.assertEqual(result['workflow_mode'],'validated_dxf_flat')
        self.assertIsNotNone(result['quote']['estimated_internal_cost_eur']['laser'])
        self.assertEqual(result['data_provenance']['bend_source'],'step')
        self.assertFalse(base['flat_pattern']['usable_for_costing'])

    def test_reference_flat_is_authoritative_docker_freecad(self):
        try:
            from app.cad_analyzer import _configure_freecad_path
            _configure_freecad_path()
            import FreeCAD  # noqa: F401
            import Part
        except ImportError:
            self.skipTest('Requires real Docker FreeCAD runtime')
        shape=Part.Shape();shape.read(str(BENCHMARK))
        face=max((f for f in shape.Faces if f.Surface.TypeId=='Part::GeomPlane'),
                 key=lambda f:float(f.Area))
        dxf=analyze_dxf((DXF/'sm07_reference.dxf').read_bytes())
        self.assertAlmostEqual(dxf['net_area_mm2'],float(face.Area),delta=.01)
        self.assertEqual(len(face.Wires)-1,dxf['inner_contours'])
        self.assertAlmostEqual(float(face.OuterWire.Length),dxf['outer_perimeter_mm'],delta=.01)

    def test_actual_sm07_step_analysis_crosschecks_reference_dxf(self):
        try:
            from app.cad_analyzer import analyze_step_file,_configure_freecad_path
            _configure_freecad_path()
            import FreeCAD  # noqa: F401
        except ImportError:
            self.skipTest('Requires real Docker FreeCAD runtime')
        folded=ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step'
        analysis=analyze_step_file(file_bytes=folded.read_bytes(),
            source_file='sm07.step',material='acciaio',density_g_cm3=7.85).model_dump()
        checked=attach_supplied_dxf(analysis=analysis,
            dxf_bytes=(DXF/'sm07_reference.dxf').read_bytes(),quantity=1,material='acciaio')
        self.assertEqual(checked['dxf_validation']['status'],'verified',checked['dxf_validation'])
        self.assertEqual(checked['dxf_validation']['step_opening_count'],1)
        self.assertIsNotNone(checked['quote']['estimated_internal_cost_eur']['laser'])
        from app.schemas import CadAnalysisResponse
        analysis['step_sha256']=hashlib.sha256(folded.read_bytes()).hexdigest()
        analysis=CadAnalysisResponse.model_validate(analysis).model_dump()
        browser=browser_json(analysis)
        self.assertTrue(valid_analysis_receipt(json.loads(browser),analysis_receipt(analysis)))
        # The former canonical JSON signed number spelling (e.g. 2.0 vs 2).
        former=json.dumps(analysis,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        prior_browser=json.dumps(json.loads(browser),sort_keys=True,ensure_ascii=False,separators=(',',':'))
        self.assertNotEqual(former,prior_browser)
        def signed_zeros(value):
            if isinstance(value,float):return int(value==0 and math.copysign(1,value)<0)
            if isinstance(value,list):return sum(map(signed_zeros,value))
            if isinstance(value,dict):return sum(map(signed_zeros,value.values()))
            return 0
        print('SM07 numeric browser round-trip: legacy spelling differs; signed zeros:',
              signed_zeros(analysis))
        from fastapi.testclient import TestClient
        from app.main import app
        with patch('app.main.run_isolated_cad_analysis',side_effect=AssertionError('unexpected STEP rerun')):
            response=TestClient(app).post('/attach-flat-dxf',
                data={'analysis_json':browser,'receipt':analysis_receipt(analysis),
                      'material':'acciaio','quantity':'1'},
                files={'file':('flat.dxf',(DXF/'sm07_reference.dxf').read_bytes(),'application/dxf'),
                       'step_file':('folded.step',folded.read_bytes(),'application/octet-stream')})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['dxf_validation']['status'],'verified')

    def test_negative_fixtures_suspend_costing(self):
        statuses={'wrong_units':'rejected','open':'rejected','missing_opening':'warning',
                  'modified_geometry':'rejected','duplicate':'rejected'}
        for case,status in statuses.items():
            with self.subTest(case=case):
                result=attach_supplied_dxf(analysis=sm07_analysis(),
                    dxf_bytes=(DXF/f'sm07_{case}.dxf').read_bytes(),
                    quantity=1,material='acciaio')
                self.assertEqual(result['dxf_validation']['status'],status)
                self.assertFalse(result['analysis']['flat_pattern']['usable_for_costing'])
                self.assertIsNone(result['analysis']['cutting']['total_cut_length_mm'])
                self.assertIsNone(result['quote']['estimated_internal_cost_eur']['laser'])
                self.assertIsNone(result['flat_preview'])

    def test_dxf_of_other_step_rejected(self):
        other=sm07_analysis();other['volume_cm3']=16.215  # SM06 benchmark
        record=validate_step_dxf(other,analyze_dxf((DXF/'sm07_reference.dxf').read_bytes()))
        self.assertEqual(record['status'],'rejected')

    def test_existing_validated_step_quote_unchanged_if_dxf_matches(self):
        base=sm07_analysis();base['flat_pattern'].update(status='validated_estimate',
            usable_for_costing=True,confidence='high',gross_blank_area_mm2=7618.76104,
            total_cut_length_mm=431.09144,
            validation={'passed':True,'all_openings_propagated':True})
        base['cutting']['total_cut_length_mm']=431.09144
        previous=quote_from_cad(base,quantity=1,material='acciaio')
        checked=attach_supplied_dxf(analysis=base,dxf_bytes=(DXF/'sm07_reference.dxf').read_bytes(),
                                    quantity=1,material='acciaio')
        self.assertEqual(checked['workflow_mode'],'validated_step_flat')
        self.assertEqual(checked['quote'],previous)
        conflict=attach_supplied_dxf(analysis=base,dxf_bytes=(DXF/'sm07_modified_geometry.dxf').read_bytes(),
                                     quantity=1,material='acciaio')
        self.assertIsNone(conflict['quote']['estimated_internal_cost_eur']['laser'])

    def test_basic_polyline_bulge_circle_and_units(self):
        doc=ezdxf.new('R2010');doc.header['$INSUNITS']=4
        m=doc.modelspace();m.add_lwpolyline([(0,0),(100,0),(100,60),(0,60)],close=True)
        m.add_circle((30,30),5)
        import io
        output=io.StringIO();doc.write(output)
        flat=analyze_dxf(output.getvalue().encode('utf-8'))
        self.assertTrue(flat['passed'],flat['warnings'])
        self.assertAlmostEqual(flat['net_area_mm2'],6000-25*3.141592653589793,places=3)
        self.assertEqual(flat['openings'][0]['type'],'circular')
        self.assertEqual(flat['openings'][0]['diameter_mm'],10.)

    def test_measured_circular_diameter_conflict_rejected(self):
        import io
        doc=ezdxf.new('R2010');doc.header['$INSUNITS']=4
        doc.modelspace().add_lwpolyline([(0,0),(100,0),(100,60),(0,60)],close=True)
        doc.modelspace().add_circle((30,30),5)
        output=io.StringIO();doc.write(output)
        sample=sm07_analysis()
        sample['volume_cm3']=(6000-25*3.141592653589793)*2/1000
        sample['holes']={'physical_openings_total':1,
                         'circular':[{'through_diameter_mm':12.}], 'elongated':[]}
        record=validate_step_dxf(sample,analyze_dxf(output.getvalue().encode()))
        self.assertEqual(record['status'],'rejected')
        self.assertIn('Diametri circolari DXF incompatibili con STEP',record['warnings'])

    def test_closed_legacy_polyline_and_nonplanar_entity(self):
        import io
        doc=ezdxf.new('R2010');doc.header['$INSUNITS']=4
        model=doc.modelspace()
        model.add_polyline2d([(0,0),(20,0),(20,10),(0,10)],close=True)
        model.add_circle((5,5),2)
        output=io.StringIO();doc.write(output)
        data=analyze_dxf(output.getvalue().encode())
        self.assertTrue(data['passed'],data['warnings'])
        self.assertAlmostEqual(data['net_area_mm2'],200-4*3.141592653589793,places=3)
        model.add_line((0,0,1),(2,3,1))
        invalid=io.StringIO();doc.write(invalid)
        flat=analyze_dxf(invalid.getvalue().encode())
        self.assertFalse(flat['passed'])
        self.assertIn('LINE non planare',flat['warnings'])

    def test_real_complex_step_remains_without_flat(self):
        try:
            from app.cad_analyzer import analyze_step_file,_configure_freecad_path
            _configure_freecad_path()
            import FreeCAD  # noqa: F401
        except ImportError:
            self.skipTest('Requires real Docker FreeCAD runtime')
        source=ROOT/'tests/dataset/staffa_16_pieghe_stress_test/input.stp'
        analysis=analyze_step_file(file_bytes=source.read_bytes(),source_file='fixture.step',
                                   material='acciaio',density_g_cm3=7.85).model_dump()
        self.assertEqual(analysis['bends']['count'],17)
        self.assertEqual(analysis['holes']['physical_openings_total'],12)
        self.assertFalse(analysis['flat_pattern']['usable_for_costing'])
        self.assertIsNone(quote_from_cad(analysis,material='acciaio')['estimated_internal_cost_eur']['laser'])

    def test_optional_dxf_endpoint_checks_step_identity_without_analysis_worker(self):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            from app.schemas import CadAnalysisResponse
            from app.step_dxf_receipt import analysis_receipt
        except ImportError:
            self.skipTest('Requires Docker FastAPI dependencies')
        folded=(ROOT/'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step').read_bytes()
        analysis=CadAnalysisResponse.model_validate({**sm07_analysis(),
            'step_sha256':hashlib.sha256(folded).hexdigest()}).model_dump()
        analysis['geometry']['center_of_mass_mm']={'x':-0.0,'y':1.0,'z':1e-7}
        fields={'analysis_json':browser_json(analysis),'receipt':analysis_receipt(analysis),
                'material':'acciaio','quantity':'1'}
        file_bytes=(DXF/'sm07_reference.dxf').read_bytes()
        client=TestClient(app)
        with patch('app.main.run_isolated_cad_analysis',side_effect=AssertionError('unexpected STEP rerun')):
            request=lambda step:client.post('/attach-flat-dxf',data=fields,files={
                'file':('flat.dxf',file_bytes,'application/dxf'),
                'step_file':('folded.step',step,'application/octet-stream')})
            correct=request(folded)
            self.assertEqual(correct.status_code,200,correct.text)
            self.assertTrue(correct.json()['dxf_validation']['passed'])
            self.assertEqual(request(b'wrong STEP').status_code,409)
            changed=copy.deepcopy(analysis);changed['volume_cm3']=999
            altered={**fields,'analysis_json':json.dumps(changed)}
            forged=client.post('/attach-flat-dxf',data=altered,files={
                'file':('flat.dxf',file_bytes,'application/dxf'),
                'step_file':('folded.step',folded,'application/octet-stream')})
            self.assertEqual(forged.status_code,409)


if __name__=='__main__':unittest.main()
