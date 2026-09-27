#!/usr/bin/env node
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {performance}=require('node:perf_hooks');
const threeContext={};
vm.runInNewContext(fs.readFileSync('frontend/vendor/three.min.js','utf8'),threeContext);
const THREE=threeContext.THREE;
const preview=JSON.parse(fs.readFileSync('tests/dataset/step_dxf_workflow/sm07_flat_preview.json'));
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.events={};this.dataset={};this.style={};
    this.attrs={};this.hidden=false;this.clientWidth=800;this.clientHeight=600;this.textContent='';
    this.values=new Set();this.classList={add:k=>this.values.add(k),remove:k=>this.values.delete(k),contains:k=>this.values.has(k),
      toggle:(k,enabled)=>{if(enabled===undefined? !this.values.has(k):enabled)this.values.add(k);else this.values.delete(k);}};}
  append(...children){for(const child of children){if(child.parent){const siblings=child.parent.children;
      siblings.splice(siblings.indexOf(child),1);}child.parent=this;this.children.push(child);}}
  setAttribute(k,v){this.attrs[k]=String(v);}
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
  emit(name,extra={}){for(const fn of this.events[name]??[])fn({target:this,preventDefault(){},stopPropagation(){},...extra});}
  querySelector(selector){return selector==='span'?this.children.find(node=>node.tag==='span'):null;}
  setPointerCapture(){}hasPointerCapture(){return false;}
}
const ids=['flat-pane','flat-frame','flat-canvas','flat-empty','flat-toolbar','flat-fullscreen','flat-status',
  'flat-projection','flat-navcube','flat-navcube-shell','flat-axes','flat-orientation','flat-help'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const buttons=Object.fromEntries(['isometric','top','front','right','fit'].map(name=>{
  const node=new Element('button');node.dataset.flatView=name;return [name,node];}));
elements['flat-fullscreen'].append(new Element('span'));
let renderer,frames=[],backendCalls=0;
THREE.WebGLRenderer=class{constructor(){renderer=this;this.sizes=[];}setPixelRatio(){}setClearColor(){}
  setSize(w,h){this.sizes.push([w,h]);}render(scene,camera){this.scene=scene;this.camera=camera;}};
const document={body:new Element(),fullscreenElement:null,events:{},
  getElementById:id=>{assert(elements[id],`Missing ${id}`);return elements[id];},
  createElementNS:(ns,tag)=>new Element(tag),
  querySelectorAll:selector=>selector==='[data-flat-view]'?Object.values(buttons):[],
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);},
  emit(name,event){for(const fn of this.events[name]??[])fn(event);}};
const requestAnimationFrame=fn=>(frames.push(fn),frames.length);
const window={devicePixelRatio:1,ResizeObserver:class{observe(){}},requestAnimationFrame,
  addEventListener(){},fetch(){backendCalls++;throw Error('unexpected fetch');}};
vm.runInNewContext(fs.readFileSync('frontend/vendor/flat_viewer.js','utf8'),
  {THREE,document,window,ResizeObserver:window.ResizeObserver,requestAnimationFrame,console});
const viewer=window.ReversePartsFlatViewer;
function frame(){const scheduled=frames;frames=[];scheduled.forEach(fn=>fn());}
function near(a,b,tol=1e-5){assert(Math.abs(a-b)<tol,`${a} != ${b}`);}
function same(a,b){assert.deepEqual(Array.from(a),Array.from(b));}
function originalGeometry(){const shape=new THREE.Shape();function path(points,out){out.moveTo(...points[0]);
  for(const point of points.slice(1))out.lineTo(...point);out.closePath();}
  path(preview.contours.find(c=>c.role==='outer').points,shape);
  for(const hole of preview.contours.filter(c=>c.role==='inner')){
    const inset=new THREE.Path();path(hole.points,inset);shape.holes.push(inset);}
  const geometry=new THREE.ExtrudeGeometry(shape,{depth:preview.thickness_mm,bevelEnabled:false,curveSegments:32});
  geometry.computeVertexNormals();return geometry;}
const baseline=originalGeometry();
// The real attach handler renders while STEP is visible, then selects the flat tab.
elements['flat-pane'].hidden=true;
const initStart=performance.now();viewer.initialize();viewer.render(preview);
elements['flat-pane'].hidden=false;viewer.resize();frame();
const initialization=performance.now()-initStart;
assert(!elements['flat-canvas'].hidden&&!elements['flat-orientation'].hidden&&!elements['flat-navcube-shell'].hidden);
const group=renderer.scene.children.find(child=>child.isGroup);
const mesh=group.children.find(child=>child.isMesh);
const edges=group.children.find(child=>child.isLineSegments);
assert(mesh&&edges&&edges.geometry.attributes.position.count);
const geometry=mesh.geometry;
for(const name of ['position','normal'])same(geometry.attributes[name].array,baseline.attributes[name].array);
if(baseline.index)same(geometry.index.array,baseline.index.array);
const saved=geometry.attributes.position.array.slice();
const oldCamera=renderer.camera,orientation=oldCamera.quaternion.clone();
const t1=performance.now();elements['flat-projection'].emit('change',{target:{value:'orthographic'}});frame();
assert(renderer.camera.isOrthographicCamera);near(renderer.camera.quaternion.angleTo(orientation),0);
const height=(renderer.camera.top-renderer.camera.bottom)/renderer.camera.zoom;
elements['flat-projection'].emit('change',{target:{value:'perspective'}});frame();
assert(renderer.camera.isPerspectiveCamera);near(renderer.camera.quaternion.angleTo(orientation),0);
near(2*renderer.camera.position.length()*Math.tan(THREE.MathUtils.degToRad(34)/2),height);
const projection=performance.now()-t1;
const t2=performance.now();buttons.top.emit('click');frame();
assert(renderer.camera.position.z>0&&Math.abs(renderer.camera.position.x)<1e-8);
const targets=[];function visit(node){if(node.dataset.navView)targets.push(node);node.children.forEach(visit);}
visit(elements['flat-navcube']);assert.equal(targets.length,26);
for(const name of ['Frontale','Destra','Alto','Destra / Alto','Destra / Frontale / Alto']){
  const target=targets.find(node=>node.dataset.navView===name);assert(target,`Missing ${name}`);
  target.emit('click');frame();}
assert(renderer.camera.position.x>0&&renderer.camera.position.y<0&&renderer.camera.position.z>0);
const preset=performance.now()-t2;
assert.strictEqual(group.children.find(child=>child.isMesh),mesh);same(geometry.attributes.position.array,saved);
const axes=elements['flat-axes'];assert.equal(axes.children.length,3);
const x=axes.children.find(node=>node.children[2]?.textContent==='X');
const axisBefore=x.children[1].attrs.cy;
const canvas=elements['flat-canvas'];canvas.emit('pointerdown',{pointerId:1,clientX:50,clientY:50,button:0});
for(let i=1;i<=8;i++)canvas.emit('pointermove',{pointerId:1,clientX:50,clientY:50+i*140,button:0});
canvas.emit('pointerup',{pointerId:1});frame();
assert.notEqual(x.children[1].attrs.cy,axisBefore,'triad follows orbit');
const before=renderer.camera.position.clone();
canvas.emit('pointerdown',{pointerId:2,clientX:30,clientY:30,button:2});
canvas.emit('pointermove',{pointerId:2,clientX:50,clientY:40,button:2});canvas.emit('pointerup',{pointerId:2});frame();
assert(renderer.camera.position.distanceTo(before)>0,'pan');
const distance=renderer.camera.position.length();canvas.emit('wheel',{deltaY:80,deltaMode:0});frame();
assert.notEqual(distance,renderer.camera.position.length(),'perspective zoom');
viewer.setProjection('orthographic');frame();const zoom=renderer.camera.zoom;
canvas.emit('wheel',{deltaY:-80,deltaMode:0});frame();assert.notEqual(zoom,renderer.camera.zoom,'orthographic zoom');
buttons.fit.emit('click');frame();assert.equal(renderer.camera.zoom,1);
function visible(){
  const camera=renderer.camera;camera.updateMatrixWorld();
  const box=new THREE.Box3().setFromObject(group);
  for(const x of [box.min.x,box.max.x])for(const y of [box.min.y,box.max.y])
    for(const z of [box.min.z,box.max.z]){
      const p=new THREE.Vector3(x,y,z).project(camera);
      assert(Math.abs(p.x)<=1.00001&&Math.abs(p.y)<=1.00001&&p.z>=-1&&p.z<=1,
        `Adatta must keep the entire extrusion in view: ${p.toArray()}`);
    }
}
visible();
elements['flat-frame'].clientWidth=500;elements['flat-frame'].clientHeight=700;
viewer.resize();frame();
near((renderer.camera.right-renderer.camera.left)/(renderer.camera.top-renderer.camera.bottom),500/700);
visible();
elements['flat-fullscreen'].emit('click');frame();
assert(elements['flat-frame'].classList.contains('viewer-expanded'));
assert(!elements['flat-orientation'].hidden&&!elements['flat-navcube-shell'].hidden);
document.emit('keydown',{key:'Escape'});frame();assert(!elements['flat-frame'].classList.contains('viewer-expanded'));
const t3=performance.now();elements['flat-pane'].hidden=true;elements['flat-pane'].hidden=false;viewer.resize();frame();
const toggle=performance.now()-t3;
assert.strictEqual(renderer.scene.children.find(child=>child.isGroup),group);
same(geometry.attributes.position.array,saved);assert.equal(backendCalls,0);
const html=fs.readFileSync('frontend/index.html','utf8');
for(const id of ['flat-projection','flat-navcube-shell','flat-orientation'])assert(html.includes(`id="${id}"`));
const start=html.indexOf('    function showVisualPane(mode) {');
const end=html.indexOf('    function setViewerStatus(',start);
assert(start>0&&end>start);
const toggleDocument={...document,getElementById:id=>elements[id]??(elements[id]=new Element())};
const toggleContext={document:toggleDocument,window:{ReversePartsFlatViewer:viewer},
  viewerState:{renderer:null},exitViewerFullscreen(){},resizeViewer(){},fetch(){backendCalls++;throw Error('fetch');}};
vm.runInNewContext(html.slice(start,end)+'; this.toggle=showVisualPane;',toggleContext);
const t4=performance.now();toggleContext.toggle('viewer');toggleContext.toggle('flat');frame();
const actualToggle=performance.now()-t4;
assert(!elements['flat-pane'].hidden);
assert.strictEqual(renderer.scene.children.find(child=>child.isGroup),group);
assert.equal(backendCalls,0);
console.log('SM07 DXF headless camera/mesh regression PASS; 26 cube targets, XYZ, fullscreen, no backend calls');
console.log(JSON.stringify({headless_ms:{initialization:+initialization.toFixed(3),projection_roundtrip:+projection.toFixed(3),presets_cube:+preset.toFixed(3),toggle:+actualToggle.toFixed(3)},vertices:geometry.attributes.position.count,flat_edge_vertices:edges.geometry.attributes.position.count,backend_calls:backendCalls}));
