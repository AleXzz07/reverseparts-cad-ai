#!/usr/bin/env node
/* Exercise the actual DXF change handler in a small DOM/fetch harness. */
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync('frontend/index.html','utf8');
const marker="document.getElementById('flat-dxf-file').addEventListener('change', ";
const start=source.indexOf(marker);
const end=source.indexOf("\n    document.getElementById('flat-dxf-remove')",start);
assert(start>=0 && end>start,'DXF upload handler missing');
const handler=source.slice(start+marker.length,end).trim().replace(/\}\);$/,'}');
async function check(){
  const input={files:[{name:'sm07_reference.dxf'}],value:'selected'};
  const status={textContent:''};
  const fields={'flat-dxf-file':input,'flat-dxf-status':status,
    'material':{value:'acciaio'},'quantity':{value:'1'}};
  const originalStepResult={analysis:{step_sha256:'a'.repeat(64)},analysis_receipt:'token'};
  const originalQuote={analysis:{part_name:'SM07'},quote:{id:'original'}};
  let errorMessage=null;
  const context={document:{getElementById:id=>fields[id]},
    FormData:class {append(){}},
    originalStepResult,lastCadFile:{name:'SM07.step'},dxfRequestId:0,
    activeDxfFile:null,lastResult:originalQuote,
    parameterGroups:{laser:[],bending:[],general:[],material:[]},
    collectOverrides:()=>({}),weldConfigurations:[],
    fetchApi:async()=>{const e=new Error('HTTP 409');e.status=409;throw e;},
    setStatus:text=>{errorMessage=text;},
    window:{ReversePartsFlatViewer:{render(){throw new Error('not reached');}}},
    renderResult(){throw new Error('not reached');},showVisualPane(){throw new Error('not reached');}};
  const callback=vm.runInNewContext('('+handler+')',context);
  await callback({target:input});
  assert.equal(status.textContent,'DXF non verificato — analisi non associabile.');
  assert.equal(input.value,'');
  assert.equal(context.lastResult,originalQuote,'STEP result must survive the failed attach');
  assert.equal(context.activeDxfFile,null);
  assert.match(errorMessage,/409/);
  assert.match(source,/<button[^>]*id="flat-dxf-trigger"/);
  assert.match(source,/Carica DXF \(opzionale\)/);
  assert.match(source,/Carica DXF per completare lo sviluppo piano/);
  console.log('DXF upload button and HTTP 409 UI state: PASS; STEP result retained');
}
check().catch(error=>{console.error(error);process.exitCode=1;});
