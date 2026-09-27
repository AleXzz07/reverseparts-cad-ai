#!/usr/bin/env node
// Real JSON.parse/JSON.stringify path used by the browser; Node 18+.
// Run against the candidate container, never against Render.
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const base=(process.argv[2]||'http://127.0.0.1:8000').replace(/\/$/,'');
const step=await readFile(resolve(root,'tests/dataset/sheetmetal_benchmark_v2/SM07/folded.step'));
const dxf=await readFile(resolve(root,'tests/dataset/step_dxf_workflow/sm07_reference.dxf'));
const upload=(data,receipt,stepBytes=step)=>{
  const form=new FormData();
  form.append('analysis_json',data);
  form.append('receipt',receipt);
  form.append('material','acciaio');
  form.append('quantity','1');
  form.append('file',new Blob([dxf]),'flat.dxf');
  form.append('step_file',new Blob([stepBytes]),'sm07.step');
  return fetch(`${base}/attach-flat-dxf`,{method:'POST',body:form});
};
const analysisForm=new FormData();
analysisForm.append('material','acciaio');
analysisForm.append('quantity','1');
analysisForm.append('file',new Blob([step]),'sm07.step');
const analysisResponse=await fetch(`${base}/analyze-and-quote`,{method:'POST',body:analysisForm});
if(!analysisResponse.ok)throw new Error(`Analisi STEP HTTP ${analysisResponse.status}: ${await analysisResponse.text()}`);
const result=JSON.parse(await analysisResponse.text());
const signedZeros=value=>{
  if(Object.is(value,-0))return 1;
  if(Array.isArray(value))return value.reduce((count,v)=>count+signedZeros(v),0);
  if(value&&typeof value==='object')return Object.values(value).reduce((count,v)=>count+signedZeros(v),0);
  return 0;
};
const browserAnalysis=JSON.stringify(result.analysis);
const ok=await upload(browserAnalysis,result.analysis_receipt);
const body=await ok.json();
if(ok.status!==200||body.dxf_validation?.status!=='verified')
  throw new Error(`Round-trip reale STEP SM07/DXF HTTP ${ok.status}: ${JSON.stringify(body)}`);
const forged=JSON.parse(browserAnalysis);forged.volume_cm3+=1;
const tampered=await upload(JSON.stringify(forged),result.analysis_receipt);
if(tampered.status!==409)throw new Error(`Volume modificato: atteso HTTP 409, ottenuto ${tampered.status}`);
const wrongStep=await upload(browserAnalysis,result.analysis_receipt,new TextEncoder().encode('wrong STEP'));
if(wrongStep.status!==409)throw new Error(`STEP diverso: atteso HTTP 409, ottenuto ${wrongStep.status}`);
console.log(JSON.stringify({browser_json_roundtrip:'JSON.parse + JSON.stringify',
  signed_zeros_in_real_sm07:signedZeros(result.analysis),dxf_status:body.dxf_validation.status,
  laser_available:body.quote.estimated_internal_cost_eur.laser!==null,
  tampered_volume_http:tampered.status,wrong_step_http:wrongStep.status},null,2));
