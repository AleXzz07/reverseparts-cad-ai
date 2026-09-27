/* DXF flat rendering only. No STEP viewer code or backend calls. */
(function () {
  'use strict';
  const FOV = 34;
  const state = {renderer:null,scene:null,camera:null,controls:null,mesh:null,frame:null,
    observer:null,radius:1,animation:null,projection:'perspective',aspect:0,
    navItems:null,axesItems:null,navQuaternion:null};
  const el = id => document.getElementById(id);
  const rad = () => Math.tan(THREE.MathUtils.degToRad(FOV) / 2);

  // Rotate the camera and its up vector together across either pole.
  function createControls(camera, canvas) {
    const pointers = new Map();
    const controls = {
      object:camera, target:new THREE.Vector3(), minDistance:.01, maxDistance:100000,
      update() { this.object.lookAt(this.target); },
      rotate(dx,dy) {
        const view=this.object.position.clone().sub(this.target);
        if(view.lengthSq()<1e-12)return;
        const amount=2*Math.PI/Math.max(1,Math.min(canvas.clientWidth,canvas.clientHeight));
        const right=new THREE.Vector3(1,0,0).applyQuaternion(this.object.quaternion);
        const up=new THREE.Vector3(0,1,0).applyQuaternion(this.object.quaternion);
        const rotation=new THREE.Quaternion().setFromAxisAngle(up,-dx*amount)
          .multiply(new THREE.Quaternion().setFromAxisAngle(right,-dy*amount));
        view.applyQuaternion(rotation);
        this.object.up.applyQuaternion(rotation).normalize();
        this.object.position.copy(this.target).add(view);
        this.update();
        renderNavCube();
      },
      pan(dx,dy) {
        const view=this.object.position.clone().sub(this.target);
        const height=this.object.isOrthographicCamera
          ?(this.object.top-this.object.bottom)/this.object.zoom
          :2*view.length()*rad()/this.object.zoom;
        const move=new THREE.Vector3(-dx,dy,0).multiplyScalar(height/Math.max(canvas.clientHeight,1))
          .applyQuaternion(this.object.quaternion);
        this.object.position.add(move);this.target.add(move);this.update();
      },
      zoomBy(factor) {
        if(this.object.isOrthographicCamera){
          this.object.zoom=THREE.MathUtils.clamp(this.object.zoom*factor,.01,1000);
          this.object.updateProjectionMatrix();
        } else {
          const view=this.object.position.clone().sub(this.target);
          view.setLength(THREE.MathUtils.clamp(view.length()/factor,this.minDistance,this.maxDistance));
          this.object.position.copy(this.target).add(view);this.update();
        }
      },
    };
    canvas.addEventListener('contextmenu',event=>event.preventDefault());
    canvas.addEventListener('pointerdown',event=>{
      if(!state.mesh)return;
      event.preventDefault();
      pointers.set(event.pointerId,{x:event.clientX,y:event.clientY,
        pan:event.button===2||event.ctrlKey||event.metaKey||event.shiftKey});
      canvas.setPointerCapture?.(event.pointerId);
    });
    canvas.addEventListener('pointermove',event=>{
      const previous=pointers.get(event.pointerId);
      if(!previous||!state.mesh)return;
      event.preventDefault();
      const next={x:event.clientX,y:event.clientY,pan:previous.pan};
      const other=event.pointerType==='touch'&&pointers.size===2
        ?[...pointers.entries()].find(([id])=>id!==event.pointerId)?.[1]:null;
      if(other){
        controls.pan((next.x-previous.x)/2,(next.y-previous.y)/2);
        const before=Math.hypot(previous.x-other.x,previous.y-other.y);
        const after=Math.hypot(next.x-other.x,next.y-other.y);
        if(before>2&&after>2)controls.zoomBy(after/before);
      }else if(pointers.size===1){
        if(previous.pan)controls.pan(next.x-previous.x,next.y-previous.y);
        else controls.rotate(next.x-previous.x,next.y-previous.y);
      }
      pointers.set(event.pointerId,next);
    });
    const end=event=>{
      pointers.delete(event.pointerId);
      if(canvas.hasPointerCapture?.(event.pointerId))canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener('pointerup',end);
    canvas.addEventListener('pointercancel',end);
    canvas.addEventListener('lostpointercapture',event=>pointers.delete(event.pointerId));
    canvas.addEventListener('wheel',event=>{
      if(!state.mesh)return;
      event.preventDefault();
      const delta=event.deltaY*(event.deltaMode===1?16:1);
      controls.zoomBy(Math.exp(-THREE.MathUtils.clamp(delta,-500,500)*.001));
    },{passive:false});
    return controls;
  }

  function frustum(camera,halfHeight,aspect){
    camera.top=halfHeight;camera.bottom=-halfHeight;
    camera.left=-halfHeight*aspect;camera.right=halfHeight*aspect;
    camera.updateProjectionMatrix();
  }
  function resize(){
    if(!state.renderer||el('flat-pane').hidden)return;
    const width=Math.max(1,state.frame.clientWidth),height=Math.max(1,state.frame.clientHeight);
    const aspect=width/height;
    if(state.mesh&&state.aspect){
      const scale=Math.min(1,state.aspect)/Math.min(1,aspect);
      if(state.camera.isOrthographicCamera){
        frustum(state.camera,(state.camera.top-state.camera.bottom)*.5*scale,aspect);
      }else if(Math.abs(scale-1)>1e-6){
        state.camera.position.sub(state.controls.target).multiplyScalar(scale).add(state.controls.target);
        state.controls.update();
      }
    }
    state.renderer.setSize(width,height,false);
    if(state.camera.isPerspectiveCamera)state.camera.aspect=aspect;
    state.camera.updateProjectionMatrix();state.aspect=aspect;
  }
  function setProjection(mode){
    if(!['perspective','orthographic'].includes(mode)||mode===state.projection)return;
    if(!state.camera){state.projection=mode;return;}
    const old=state.camera,target=state.controls.target;
    const offset=old.position.clone().sub(target),distance=Math.max(offset.length(),.01);
    const aspect=state.aspect||1;
    let camera;
    if(mode==='orthographic'){
      const halfHeight=distance*rad()/old.zoom;
      camera=new THREE.OrthographicCamera(-halfHeight*aspect,halfHeight*aspect,
        halfHeight,-halfHeight,old.near,old.far);
      camera.position.copy(old.position);
    }else{
      const halfHeight=(old.top-old.bottom)/(2*old.zoom);
      const newDistance=halfHeight/rad();
      camera=new THREE.PerspectiveCamera(FOV,aspect,
        Math.min(old.near,Math.max(.0001,newDistance*.01)),
        Math.max(old.far,newDistance+state.radius*4));
      camera.position.copy(target).add(offset.multiplyScalar(newDistance/distance));
    }
    camera.up.copy(old.up);camera.quaternion.copy(old.quaternion);
    state.camera=camera;state.controls.object=camera;state.projection=mode;
    state.controls.update();state.navQuaternion=null;renderNavCube();
  }
  function setDirection(direction){
    if(!state.mesh)return;
    direction.normalize();
    // Flat XY face is seen from +Z. The front edge is on -Y.
    const up=new THREE.Vector3(0,0,1);
    const vertical=up.dot(direction);
    if(Math.abs(vertical)>.999)up.set(0,vertical>0?1:-1,0);
    else up.addScaledVector(direction,-vertical).normalize();
    const camera=state.camera,aspect=Math.max(state.aspect||1,.01);
    const distance=Math.max(state.radius*2.35,state.radius/rad()*1.18/Math.min(1,aspect));
    camera.up.copy(up);camera.position.copy(direction).multiplyScalar(distance);
    camera.zoom=1;
    if(camera.isOrthographicCamera)frustum(camera,state.radius*1.18/Math.min(1,aspect),aspect);
    camera.near=Math.max(state.radius/1000,.001);
    camera.far=Math.max(state.radius*100,1000);
    camera.updateProjectionMatrix();
    state.controls.target.set(0,0,0);state.controls.update();
    state.navQuaternion=null;renderNavCube();
  }
  const directions={isometric:[1,-1,1],top:[0,0,1],bottom:[0,0,-1],
    front:[0,-1,0],rear:[0,1,0],right:[1,0,0],left:[-1,0,0]};
  function fit(view='isometric'){
    if(view==='fit')view='isometric';
    setDirection(new THREE.Vector3(...(directions[view]||directions.isometric)));
  }
  function ensure(){
    if(state.renderer)return;
    const canvas=el('flat-canvas');state.frame=el('flat-frame');
    state.renderer=new THREE.WebGLRenderer({canvas,antialias:true});
    state.renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
    state.renderer.setClearColor(0xe8edf2,1);
    state.scene=new THREE.Scene();
    state.camera=new THREE.PerspectiveCamera(FOV,1,.01,100000);
    state.camera.up.set(0,0,1);
    state.controls=createControls(state.camera,canvas);
    state.scene.add(new THREE.HemisphereLight(0xffffff,0x687786,1.5));
    const sun=new THREE.DirectionalLight(0xffffff,.75);sun.position.set(2,-3,5);state.scene.add(sun);
    initNavCube();initAxes();
    if(window.ResizeObserver){state.observer=new ResizeObserver(resize);state.observer.observe(state.frame);}
    window.addEventListener('resize',resize);
    const animate=()=>{
      state.animation=requestAnimationFrame(animate);
      if(!el('flat-pane').hidden){state.controls.update();renderNavCube();
        state.renderer.render(state.scene,state.camera);}
    };
    animate();
  }
  function clear(){
    if(state.mesh){state.scene.remove(state.mesh);state.mesh.traverse(obj=>{
      obj.geometry?.dispose();
      if(obj.material){const materials=Array.isArray(obj.material)?obj.material:[obj.material];
        materials.forEach(material=>material.dispose());}
    });state.mesh=null;}
    el('flat-canvas').hidden=true;el('flat-empty').hidden=false;
    ['flat-toolbar','flat-fullscreen','flat-navcube-shell','flat-orientation','flat-help']
      .forEach(id=>{el(id).hidden=true;});
    el('flat-status').textContent='Sviluppo 3D disponibile dopo verifica DXF.';
  }
  function path(points,shape){
    shape.moveTo(points[0][0],points[0][1]);
    for(let i=1;i<points.length;i++)shape.lineTo(points[i][0],points[i][1]);
    shape.closePath();
  }
  function render(preview){
    if(!preview?.contours?.length||!(preview.thickness_mm>0)){if(state.mesh)clear();return;}
    ensure();if(state.mesh)clear();
    const outside=preview.contours.find(c=>c.role==='outer');
    if(!outside?.points?.length)return;
    const shape=new THREE.Shape();path(outside.points,shape);
    for(const hole of preview.contours.filter(c=>c.role==='inner')){
      const inset=new THREE.Path();path(hole.points,inset);shape.holes.push(inset);
    }
    const thickness=Number(preview.thickness_mm);
    // Preserve the verified DXF extrusion and normals exactly.
    const geometry=new THREE.ExtrudeGeometry(shape,{depth:thickness,bevelEnabled:false,curveSegments:32});
    geometry.computeVertexNormals();
    const group=new THREE.Group();
    const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({
      color:0xbfcbd7,metalness:.12,roughness:.75,side:THREE.DoubleSide}));
    group.add(mesh);
    // Outline only existing creases of that same extrusion; no STEP edges or new contour sampling.
    const edgeGeometry=new THREE.EdgesGeometry(geometry,30);
    const edges=new THREE.LineSegments(edgeGeometry,new THREE.LineBasicMaterial({
      color:0x65798d,transparent:false,depthWrite:false}));
    edges.renderOrder=1;group.add(edges);
    const bounds=new THREE.Box3().setFromObject(group),center=bounds.getCenter(new THREE.Vector3());
    group.position.sub(center);
    state.radius=Math.max(bounds.getBoundingSphere(new THREE.Sphere()).radius,thickness,1);
    state.scene.add(group);state.mesh=group;
    el('flat-canvas').hidden=false;el('flat-empty').hidden=true;
    ['flat-toolbar','flat-fullscreen','flat-navcube-shell','flat-orientation','flat-help']
      .forEach(id=>{el(id).hidden=false;});
    el('flat-status').textContent='Lamiera piatta 3D dal contorno DXF verificato; spessore dal modello STEP.';
    resize();requestAnimationFrame(()=>fit());
  }
  function syncFullscreen(){
    const frame=el('flat-frame');
    const expanded=document.fullscreenElement===frame||frame.classList.contains('viewer-expanded');
    const button=el('flat-fullscreen'),label=expanded?'Esci da schermo intero':'Schermo intero';
    button.querySelector('span').textContent=label;
    button.setAttribute('aria-label',label);button.setAttribute('aria-pressed',String(expanded));
    requestAnimationFrame(resize);
  }
  function useExpanded(){
    el('flat-frame').classList.add('viewer-expanded');
    document.body.classList.add('viewer-expanded');syncFullscreen();
  }
  function fullscreen(){
    const frame=el('flat-frame');
    if(document.fullscreenElement===frame){document.exitFullscreen();return;}
    if(frame.classList.contains('viewer-expanded')){exitFullscreen();return;}
    if(frame.requestFullscreen)frame.requestFullscreen().then(syncFullscreen).catch(useExpanded);
    else useExpanded();
  }
  function exitFullscreen(){
    const frame=el('flat-frame');
    if(document.fullscreenElement===frame)document.exitFullscreen();
    if(frame.classList.contains('viewer-expanded')){
      frame.classList.remove('viewer-expanded');document.body.classList.remove('viewer-expanded');
      syncFullscreen();
    }
  }
  function initialize(){
    document.querySelectorAll('[data-flat-view]').forEach(button=>
      button.addEventListener('click',()=>fit(button.dataset.flatView)));
    el('flat-projection').addEventListener('change',event=>setProjection(event.target.value));
    el('flat-fullscreen').addEventListener('click',fullscreen);
    document.addEventListener('fullscreenchange',syncFullscreen);
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&el('flat-frame').classList.contains('viewer-expanded'))exitFullscreen();
    });
  }

  /* CAMERA ORIENTATION GIZMOS */
    function initNavCube() {
      const svg = document.getElementById("flat-navcube");
      const namespace = "http://www.w3.org/2000/svg";
      const element = (tag, attributes = {}) => {
        const node = document.createElementNS(namespace, tag);
        Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
        return node;
      };
      const faceLayer = element("g");
      const edgeLayer = element("g");
      const cornerLayer = element("g");
      svg.append(faceLayer, edgeLayer, cornerLayer);
      const faces = [];
      const edges = [];
      const corners = [];
      const namedFaces = {
        "1,0,0": ["right", "Destra"], "-1,0,0": ["left", "Sinistra"],
        "0,1,0": ["rear", "Posteriore"], "0,-1,0": ["front", "Frontale"],
        "0,0,1": ["top", "Alto"], "0,0,-1": ["bottom", "Basso"],
      };
      const captionBackground = element("rect", {
        class: "nav-caption-bg", x: 3, y: 117, width: 130, height: 16, rx: 4,
      });
      const caption = element("text", {class: "nav-caption", x: 68, y: 126});
      caption.textContent = "Seleziona una vista";
      const makeTarget = (group, label, direction, action) => {
        group.setAttribute("role", "button");
        group.setAttribute("tabindex", "0");
        group.setAttribute("aria-label", `Vista ${label}`);
        group.dataset.navView = label;
        const title = element("title");
        title.textContent = `Vista ${label}`;
        group.append(title);
        group.addEventListener("click", event => {
          event.stopPropagation();
          if (state.mesh) action(direction);
        });
        group.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (state.mesh) action(direction);
          }
        });
        group.addEventListener("pointerenter", () => { caption.textContent = label; });
        group.addEventListener("pointerleave", () => { caption.textContent = "Seleziona una vista"; });
        group.addEventListener("focus", () => { caption.textContent = label; });
        group.addEventListener("blur", () => { caption.textContent = "Seleziona una vista"; });
      };
      for (let axis = 0; axis < 3; axis += 1) {
        const others = [0, 1, 2].filter(item => item !== axis);
        for (const sign of [-1, 1]) {
          const normal = [0, 0, 0];
          normal[axis] = sign;
          const [key, label] = namedFaces[normal.join(",")];
          const group = element("g", {class: "nav-face"});
          const polygon = element("polygon");
          const textNode = element("text");
          textNode.textContent = label.toUpperCase();
          group.append(polygon, textNode);
          makeTarget(group, label, key, fit);
          faceLayer.append(group);
          const cornersOnFace = [[-1,-1],[1,-1],[1,1],[-1,1]].map(([first, second]) => {
            const vertex = normal.slice();
            vertex[others[0]] = first;
            vertex[others[1]] = second;
            return vertex;
          });
          faces.push({group, polygon, textNode, normal, vertices: cornersOnFace});
        }
      }
      for (let axis = 0; axis < 3; axis += 1) {
        const others = [0, 1, 2].filter(item => item !== axis);
        for (const first of [-1, 1]) for (const second of [-1, 1]) {
          const center = [0, 0, 0];
          center[others[0]] = first;
          center[others[1]] = second;
          const names = others.map(index => namedFaces[
            [0,1,2].map(item => item === index ? center[item] : 0).join(",")][1]);
          const label = names.join(" / ");
          const group = element("g", {class: "nav-edge"});
          const outline = element("line", {class: "outline"});
          const hit = element("line", {class: "hit"});
          group.append(outline, hit);
          makeTarget(group, label, center, value =>
            setDirection(new THREE.Vector3(...value)));
          edgeLayer.append(group);
          const ends = [-1, 1].map(sign => {
            const vertex = center.slice();
            vertex[axis] = sign;
            return vertex;
          });
          edges.push({group, outline, hit, center, ends, others});
        }
      }
      for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) {
        const vertex = [x, y, z];
        const label = vertex.map((sign, axis) => namedFaces[
          [0,1,2].map(item => item === axis ? sign : 0).join(",")][1]).join(" / ");
        const group = element("g", {class: "nav-corner"});
        const mark = element("circle", {class: "mark", r: 4});
        const hit = element("circle", {class: "hit", r: 10});
        group.append(mark, hit);
        makeTarget(group, label, vertex, value =>
          setDirection(new THREE.Vector3(...value)));
        cornerLayer.append(group);
        corners.push({group, mark, hit, vertex});
      }
      svg.append(captionBackground, caption);
      state.navItems = {faces, edges, corners, faceLayer, edgeLayer, cornerLayer};
    }

    function initAxes() {
      const svg = document.getElementById("flat-axes");
      const namespace = "http://www.w3.org/2000/svg";
      state.axesItems = [
        ["X", [1, 0, 0], "#d94545"],
        ["Y", [0, 1, 0], "#279050"],
        ["Z", [0, 0, 1], "#2969d5"],
      ].map(([label, vector, color]) => {
        const group = document.createElementNS(namespace, "g");
        const line = document.createElementNS(namespace, "line");
        const tip = document.createElementNS(namespace, "circle");
        const text = document.createElementNS(namespace, "text");
        line.setAttribute("x1", "42");
        line.setAttribute("y1", "42");
        line.setAttribute("stroke", color);
        tip.setAttribute("r", "3");
        tip.setAttribute("fill", color);
        text.setAttribute("fill", color);
        text.textContent = label;
        group.append(line, tip, text);
        svg.append(group);
        return {group, line, tip, text, vector};
      });
    }

    function renderNavCube() {
      if (!state.mesh || !state.navItems) return;
      const inverse = state.camera.quaternion.clone().invert();
      if (state.navQuaternion && 1 - Math.abs(state.navQuaternion.dot(inverse)) < 1e-10) return;
      state.navQuaternion = inverse.clone();
      const {faces, edges, corners, faceLayer} = state.navItems;
      const projected = coordinates => {
        const point = new THREE.Vector3(...coordinates).applyQuaternion(inverse);
        return {x: 68 + point.x * 28, y: 67 - point.y * 28, depth: point.z};
      };
      const visibleNormal = normal => new THREE.Vector3(...normal)
        .applyQuaternion(inverse).z > 1e-5;
      faces.sort((a, b) => projected(a.normal).depth - projected(b.normal).depth);
      faces.forEach(face => {
        const visible = visibleNormal(face.normal);
        face.group.style.display = visible ? "" : "none";
        if (!visible) return;
        const points = face.vertices.map(projected);
        face.polygon.setAttribute("points", points.map(point => `${point.x},${point.y}`).join(" "));
        face.textNode.setAttribute("x", String(points.reduce((sum, point) => sum + point.x, 0) / 4));
        face.textNode.setAttribute("y", String(points.reduce((sum, point) => sum + point.y, 0) / 4));
        faceLayer.append(face.group);
      });
      edges.forEach(edge => {
        const visible = edge.others.some(index => {
          const normal = [0, 0, 0];
          normal[index] = edge.center[index];
          return visibleNormal(normal);
        });
        edge.group.style.display = visible ? "" : "none";
        if (!visible) return;
        const [start, end] = edge.ends.map(projected);
        [edge.outline, edge.hit].forEach(line => {
          line.setAttribute("x1", String(start.x)); line.setAttribute("y1", String(start.y));
          line.setAttribute("x2", String(end.x)); line.setAttribute("y2", String(end.y));
        });
      });
      corners.forEach(corner => {
        const visible = corner.vertex.some((sign, index) => {
          const normal = [0, 0, 0];
          normal[index] = sign;
          return visibleNormal(normal);
        });
        corner.group.style.display = visible ? "" : "none";
        if (!visible) return;
        const point = projected(corner.vertex);
        [corner.mark, corner.hit].forEach(circle => {
          circle.setAttribute("cx", String(point.x));
          circle.setAttribute("cy", String(point.y));
        });
      });
      const axisSvg = document.getElementById("flat-axes");
      state.axesItems.sort((first, second) =>
        new THREE.Vector3(...first.vector).applyQuaternion(inverse).z
        - new THREE.Vector3(...second.vector).applyQuaternion(inverse).z);
      state.axesItems.forEach(axis => {
        const direction = new THREE.Vector3(...axis.vector).applyQuaternion(inverse);
        const x = 42 + direction.x * 23;
        const y = 42 - direction.y * 23;
        axis.line.setAttribute("x2", String(x));
        axis.line.setAttribute("y2", String(y));
        axis.tip.setAttribute("cx", String(x));
        axis.tip.setAttribute("cy", String(y));
        axis.text.setAttribute("x", String(x + (Math.abs(direction.x) > .3 ? Math.sign(direction.x) * 8 : 8)));
        axis.text.setAttribute("y", String(y + (Math.abs(direction.y) > .3 ? -Math.sign(direction.y) * 8 : 9)));
        axisSvg.append(axis.group);
      });
    }

  window.ReversePartsFlatViewer={render,clear,resize,fit,setProjection,initialize,exitFullscreen};
})();
