/* DXF 로컬 웹 시험판 — 외부 CDN/네트워크 의존성 없음. */
'use strict';
const $ = id => document.getElementById(id);
const fmt = n => Number(n || 0).toLocaleString('ko-KR');
const state = {
  id: 'demo', floor:'DEMO', scene:null, geom:null, texts:[], fills:[], hidden:new Set(),
  white:false,gray:false,showText:true,showFills:true,renderer:'webgl',lineWidth:1.4,
  zoom:1,tx:0,ty:0,drag:null,selected:null,renderPending:false,rendererError:'',lastDraw:0,
  cpuPaths:null,version:0, gl:null, glProgram:null, glBuffer:null, glUniforms:null,
  loadedInfo:null, root:'', importedTitle:''
};

async function request(path, type='json') {
  const response=await fetch(path, {cache:'no-store'});
  if(!response.ok){let msg='HTTP '+response.status;try{msg=(await response.json()).error||msg}catch(e){};throw new Error(msg)}
  return type==='bin' ? response.arrayBuffer() : response.json();
}
function params(obj){return new URLSearchParams(obj).toString()}
function busy(on,msg){$('busy').classList.toggle('visible',on);if(msg)$('busy').querySelector('strong').textContent=msg}
function message(msg,error=false){$('rootStatus').textContent=msg;$('rootStatus').style.color=error?'#ff979b':'#9db3cb'}

async function scan(root,auto=false) {
  try {
    if(!auto) message('기존 프로젝트 캐시 검색 중…');
    const result=await request('/api/catalog?'+params({root}));
    state.root=result.root;$('rootInput').value=result.root;
    const select=$('fileSelect');select.innerHTML='';
    if(state.importedTitle){const direct=document.createElement('option');direct.value='direct';direct.textContent='DXF 직접읽기 · '+state.importedTitle;select.appendChild(direct)}
    const demo=document.createElement('option');demo.value='demo';demo.textContent='시험용 샘플 (실제 CAD 아님)';select.appendChild(demo);
    for(const item of result.files){const opt=document.createElement('option');opt.value=item.id;opt.title=item.path;opt.textContent=item.name+' · '+item.sizeMB+' MB';select.appendChild(opt)}
    message(result.files.length ? `원본 캐시 ${result.files.length}개 발견. 상단에서 선택하세요.` : '캐시가 없습니다. 원래 프로그램 폴더를 확인하거나 샘플로 시험하세요.');
    if(state.importedTitle){select.value='direct';await chooseFile();return}
    if(result.files.length){
      const pick=await request('/api/recommended');
      if(pick.id!==null && pick.id!==undefined){
        select.value=pick.id;
        message(`사용 가능한 층별 벡터 캐시 연결 · ${result.files.length}개 검색`+(pick.skipped ? ` · 잘못된 캐시 ${pick.skipped}개 건너뜀` : ''));
        await chooseFile();
      }else{
        select.value='demo';await chooseFile();
        message(`검색된 ${result.files.length}개 중 층별 벡터 캐시가 없습니다. 기존 프로그램에서 CAD 도서읽기 후 다시 검색하세요.`+ (pick.skippedExamples?.length ? ' ('+pick.skippedExamples[0]+')' : ''),true);
      }
    }
    else {select.value='demo';await chooseFile()}
  } catch(e){message('캐시 검색 실패: '+e.message,true);$('fileSelect').value='demo';await chooseFile()}
}
async function chooseFile(){
  const id=$('fileSelect').value;
  try{
    const result=await request('/api/floors?'+params({id}));
    state.id=id;
    const select=$('floorSelect');select.innerHTML='';
    for(const f of result.floors){const option=document.createElement('option');option.textContent=f.name+' ('+fmt(f.segments)+')';option.value=f.name;select.appendChild(option)}
    if(!result.floors.length)throw new Error('저장된 층별 벡터가 없습니다. 원래 프로그램에서 CAD 도서읽기를 먼저 실행하세요.');
    $('drawingName').textContent=id==='demo'?'가상 주차장 샘플':(result.source?.split(/[\\/]/).pop() || result.name);
    $('statusTitle').textContent=id==='demo'?'DEMO · 비교 시험 준비':id==='direct'?'층별 DXF 디스크 캐시 · 읽기 전용':'원본 캐시 연결 · 읽기 전용';
    await chooseFloor();
  }catch(e){message('캐시 선택 실패: '+e.message,true);alert('캐시 선택 실패: '+e.message)}
}
async function chooseFloor(){
  const id=state.id,floor=$('floorSelect').value,version=++state.version;
  state.floor=floor;
  busy(true,'층별 벡터 로딩 중');
  try{
    const t0=performance.now();
    const qs=params({id,floor});
    const [scene,bin,texts,fills]=await Promise.all([
      request('/api/scene?'+qs),request('/api/geometry?'+qs,'bin'),request('/api/texts?'+qs),request('/api/fills?'+qs)
    ]);
    if(version!==state.version)return;
    if(bin.byteLength!==scene.totalLines*20)throw new Error('벡터 바이너리 길이가 일치하지 않습니다.');
    state.scene=scene;state.geom=new DataView(bin);state.texts=texts;state.fills=fills;state.hidden.clear();
    state.selected=null;state.cpuPaths=null;
    initGLData(bin);rebuildLayerList();resetSelection();
    $('floorName').textContent=floor;
    $('statLines').textContent=fmt(scene.totalLines);
    $('statTexts').textContent=fmt(scene.totalTexts);
    $('statFills').textContent=fmt(scene.totalFills);
    $('statLayers').textContent=fmt(scene.layers.length);
    $('lineCount').textContent=`선 ${fmt(scene.totalLines)}`;
    fitView();
    state.loadedInfo=`캐시 ${Math.round(performance.now()-t0)}ms`;scheduleDraw();
  }catch(e){alert('도면 표시 실패: '+e.message);console.error(e)}
  finally{if(version===state.version)busy(false)}
}
function rebuildLayerList(){
  const list=$('layerList');list.innerHTML='';
  const q=$('layerSearch').value.toLocaleLowerCase();
  const layers=(state.scene?.layers||[]);
  $('layerCount').textContent=fmt(layers.length);
  const frag=document.createDocumentFragment();
  for(const l of layers){
    if(!l.name.toLocaleLowerCase().includes(q))continue;
    const row=document.createElement('label');row.className='layerItem'+(state.hidden.has(l.name)?' hidden':'');
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=!state.hidden.has(l.name);
    cb.addEventListener('change',()=>{if(cb.checked)state.hidden.delete(l.name);else state.hidden.add(l.name);row.classList.toggle('hidden',!cb.checked);scheduleDraw()});
    const color=document.createElement('span');color.className='colorMark';
    const name=document.createElement('span');name.className='layerName';name.textContent=l.name;name.title=l.name;
    const count=document.createElement('span');count.className='layerCounter';count.textContent=fmt(l.count);
    row.append(cb,color,name,count);frag.appendChild(row);
  }
  list.append(frag);
  if(!list.children.length)list.innerHTML='<div class="emptyHint">일치하는 레이어가 없습니다.</div>';
}

/* WebGL2: 선마다 1개 instance, 화면 픽셀 기준 굵기의 안티앨리어싱 스트로크 */
const VERT = `#version 300 es
precision highp float;
layout(location=0) in vec4 aSeg;
layout(location=1) in vec4 aRGB;
uniform vec2 uPan;
uniform vec2 uSize;
uniform float uZoom;
uniform float uWidth;
out vec4 vColor;
out float vDist;
void main(){
  vec2 p0=vec2(uPan.x + aSeg.x*uZoom, uPan.y - aSeg.y*uZoom);
  vec2 p1=vec2(uPan.x + aSeg.z*uZoom, uPan.y - aSeg.w*uZoom);
  vec2 axis=p1-p0; float lengthPx=max(length(axis),0.001);
  vec2 normal=vec2(-axis.y,axis.x)/lengthPx;
  int v=gl_VertexID;
  float side=(v==0||v==2)?-1.0:1.0;
  vec2 endPoint=(v<2)?p0:p1;
  float dist=side*(uWidth*.5+0.9);
  vec2 pos=endPoint+normal*dist;
  gl_Position=vec4((pos.x/uSize.x)*2.0-1.0,1.0-(pos.y/uSize.y)*2.0,0.0,1.0);
  vColor=aRGB;
  vDist=dist;
}`;
const FRAG=`#version 300 es
precision mediump float;
in vec4 vColor;
in float vDist;
uniform float uWidth;
uniform bool uGray;
uniform bool uLight;
out vec4 outColor;
void main(){
  float a=1.0-smoothstep(max(0.0,uWidth*.5-.6),uWidth*.5+.85,abs(vDist));
  vec3 c=vColor.rgb;
  // Qt CachedVectorBackgroundItem._color(): white CAD background = RGB inversion.
  // Treat grayscale exactly like Qt (150 on black / 105 on white).
  if(uGray) c=vec3(150.0/255.0);
  if(uLight) c=vec3(1.0)-c;
  outColor=vec4(c,max(0.0,min(1.0,a)));
}`;
function shader(gl,type,source){const x=gl.createShader(type);gl.shaderSource(x,source);gl.compileShader(x);if(!gl.getShaderParameter(x,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(x));return x}
function initGL(){
  const c=$('glCanvas');
  try{
    const gl=c.getContext('webgl2',{alpha:true,antialias:true,preserveDrawingBuffer:false,powerPreference:'high-performance'});
    if(!gl)throw new Error('이 브라우저는 WebGL2를 사용할 수 없습니다.');
    const program=gl.createProgram();gl.attachShader(program,shader(gl,gl.VERTEX_SHADER,VERT));gl.attachShader(program,shader(gl,gl.FRAGMENT_SHADER,FRAG));gl.linkProgram(program);
    if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.enableVertexAttribArray(0);gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(0,4,gl.FLOAT,false,20,0);
    gl.vertexAttribPointer(1,4,gl.UNSIGNED_BYTE,true,20,16);
    gl.vertexAttribDivisor(0,1);gl.vertexAttribDivisor(1,1);
    gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
    state.gl=gl;state.glBuffer=buffer;state.glProgram=program;
    state.glUniforms={pan:gl.getUniformLocation(program,'uPan'),size:gl.getUniformLocation(program,'uSize'),zoom:gl.getUniformLocation(program,'uZoom'),width:gl.getUniformLocation(program,'uWidth'),gray:gl.getUniformLocation(program,'uGray'),light:gl.getUniformLocation(program,'uLight')};
    c.addEventListener('webglcontextlost',event=>{event.preventDefault();state.gl=null;selectRenderer('canvas');message('GPU 컨텍스트가 해제되어 Canvas2D로 전환했습니다.',true)});
    return true;
  }catch(e){state.rendererError=e.message;state.gl=null;$('rendererSelect').querySelector('option[value="webgl"]').disabled=true;$('rendererSelect').value='canvas';state.renderer='canvas';return false}
}
function initGLData(bin){if(!state.gl)return;const gl=state.gl;gl.bindBuffer(gl.ARRAY_BUFFER,state.glBuffer);gl.bufferData(gl.ARRAY_BUFFER,bin,gl.STATIC_DRAW)}
function drawGL(){
  const gl=state.gl;if(!gl||!state.scene)return;
  gl.useProgram(state.glProgram);gl.bindBuffer(gl.ARRAY_BUFFER,state.glBuffer);
  gl.viewport(0,0,gl.canvas.width,gl.canvas.height);
  gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
  const u=state.glUniforms,c=gl.canvas,w=c.clientWidth,h=c.clientHeight;
  gl.uniform2f(u.pan,state.tx,state.ty);gl.uniform2f(u.size,w,h);gl.uniform1f(u.zoom,state.zoom);
  gl.uniform1f(u.width,state.lineWidth);gl.uniform1i(u.gray,state.gray?1:0);gl.uniform1i(u.light,state.white?1:0);
  for(const layer of state.scene.layers){
    if(state.hidden.has(layer.name)||!layer.count)continue;
    gl.vertexAttribPointer(0,4,gl.FLOAT,false,20,layer.offset*20);
    gl.vertexAttribPointer(1,4,gl.UNSIGNED_BYTE,true,20,layer.offset*20+16);
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP,0,4,layer.count);
  }
}

// Same RGB color transform as the original Qt vector background (preview.py).
// In white mode *all* CAD RGB channels are inverted, not only bright colors.
// This keeps white CAD lines black, while preserving alpha/transparency.
function cadRGB(rgb, gray=state.gray, white=state.white){
  let [r,g,b]=gray?[150,150,150]:rgb;
  return white?[255-r,255-g,255-b]:[r,g,b];
}
function colors(rgb, alpha=1){
  const [r,g,b]=cadRGB(rgb);
  return `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},${alpha})`;
}
function createPaths(){
  if(!state.scene||!state.geom)return;
  const groups=[];const geom=state.geom;
  for(const l of state.scene.layers){
    const byColor=new Map();
    for(let j=l.offset;j<l.offset+l.count;j++){
      const o=j*20;const rr=geom.getUint8(o+16),gg=geom.getUint8(o+17),bb=geom.getUint8(o+18);
      const key=`${rr},${gg},${bb}`;
      let path=byColor.get(key);if(!path){path=new Path2D();byColor.set(key,path)}
      path.moveTo(geom.getFloat32(o,true),geom.getFloat32(o+4,true));
      path.lineTo(geom.getFloat32(o+8,true),geom.getFloat32(o+12,true));
    }
    groups.push({name:l.name,paths:[...byColor].map(([k,path])=>({rgb:k.split(',').map(Number),path}))});
  }
  state.cpuPaths=groups;
}
function drawCPU(){
  const c=$('cpuCanvas'), ctx=c.getContext('2d');
  ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,c.width,c.height);
  if(!state.scene)return;
  if(!state.cpuPaths)createPaths();
  const dpr=window.devicePixelRatio||1;
  ctx.setTransform(dpr*state.zoom,0,0,-dpr*state.zoom,dpr*state.tx,dpr*state.ty);
  ctx.lineWidth=state.lineWidth/state.zoom;
  ctx.lineCap='butt';ctx.lineJoin='miter';
  for(const group of state.cpuPaths){
    if(state.hidden.has(group.name))continue;
    for(const item of group.paths){ctx.strokeStyle=colors(item.rgb);ctx.stroke(item.path)}
  }
}
function resizeCanvases(){
  const el=$('canvasWrap'),dpr=Math.min(window.devicePixelRatio||1,3);
  for(const id of ['fillCanvas','glCanvas','cpuCanvas','textCanvas']){
    const c=$(id),w=Math.max(1,Math.round(el.clientWidth*dpr)),h=Math.max(1,Math.round(el.clientHeight*dpr));
    if(c.width!==w||c.height!==h){c.width=w;c.height=h}
  }
}
function worldToScreen(x,y){return {x:state.tx+x*state.zoom,y:state.ty-y*state.zoom}}
function screenToWorld(x,y){return {x:(x-state.tx)/state.zoom,y:(state.ty-y)/state.zoom}}
function fitView(){
  if(!state.scene)return;
  const rect=state.scene.rect,w=$('canvasWrap').clientWidth,h=$('canvasWrap').clientHeight;
  const ww=Math.max(1,rect[2]-rect[0]),hh=Math.max(1,rect[3]-rect[1]);
  state.zoom=Math.max(.000001,Math.min((w-76)/ww,(h-76)/hh));
  state.tx=(w-ww*state.zoom)/2-rect[0]*state.zoom;
  state.ty=(h+hh*state.zoom)/2+rect[1]*state.zoom;
  scheduleDraw();
}
function drawFills(){
  const c=$('fillCanvas'),ctx=c.getContext('2d'),dpr=Math.min(window.devicePixelRatio||1,3);
  ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,c.width,c.height);
  if(!state.showFills||!state.scene)return;
  ctx.setTransform(dpr*state.zoom,0,0,-dpr*state.zoom,dpr*state.tx,dpr*state.ty);
  for(const item of state.fills){
    if(state.hidden.has(item.layer))continue;
    const pts=item.points||[];if(pts.length<3)continue;
    const path=new Path2D();path.moveTo(pts[0][0],pts[0][1]);
    for(let i=1;i<pts.length;i++)path.lineTo(pts[i][0],pts[i][1]);path.closePath();
    ctx.fillStyle=colors(item.rgb,.42);ctx.fill(path);
  }
}
function drawText(){
  const c=$('textCanvas'),ctx=c.getContext('2d'),dpr=Math.min(window.devicePixelRatio||1,3);
  ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,c.width,c.height);
  if(!state.scene)return;
  const cw=c.clientWidth,ch=c.clientHeight;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  if(state.showText){
    ctx.textBaseline='alphabetic';
    for(const item of state.texts){
      if(state.hidden.has(item.layer))continue;
      const p=worldToScreen(item.x,item.y);
      let fontSize=item.height*state.zoom;
      if(fontSize<3.3||p.x< -200||p.x>cw+200||p.y< -200||p.y>ch+200)continue;
      fontSize=Math.min(fontSize,160);
      ctx.save();ctx.translate(p.x,p.y);ctx.rotate(-item.rotation*Math.PI/180);
      ctx.font=`500 ${fontSize}px "Malgun Gothic", Arial, sans-serif`;
      const halign=Number(item.halign||0);
      ctx.textAlign=halign===1||halign===4?'center':halign===2?'right':'left';
      ctx.fillStyle=colors(item.rgb,1);
      for(const [i,line] of String(item.text).replace(/\\P/g,'\n').split('\n').entries()){
        if(line)ctx.fillText(line,0,i*fontSize*1.15);
      }
      ctx.restore();
    }
  }
  if(state.selected){
    const s=state.selected;const a=worldToScreen(s.x1,s.y1),b=worldToScreen(s.x2,s.y2);
    ctx.save();ctx.strokeStyle='#ffcf69';ctx.lineWidth=3;ctx.shadowColor='#ffb144';ctx.shadowBlur=12;
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();ctx.restore();
    ctx.fillStyle='#ffcf69';ctx.beginPath();ctx.arc(a.x,a.y,3,0,Math.PI*2);ctx.arc(b.x,b.y,3,0,Math.PI*2);ctx.fill();
  }
}
function scheduleDraw(){if(!state.renderPending){state.renderPending=true;requestAnimationFrame(draw)}}
function draw(){
  state.renderPending=false;
  resizeCanvases();
  const t0=performance.now();
  $('canvasWrap').style.background=state.white?'#ffffff':'#000000';
  $('gridHint').style.color=state.white?'#a1acbe':'#4c6380';
  drawFills();
  if(state.renderer==='canvas'||!state.gl){$('glCanvas').style.display='none';$('cpuCanvas').style.display='block';drawCPU()}
  else{$('cpuCanvas').style.display='none';$('glCanvas').style.display='block';drawGL()}
  drawText();
  const ms=performance.now()-t0;
  state.lastDraw=ms;
  $('renderTime').textContent=`프레임 제출 ${ms.toFixed(1)}ms`;
  $('performance').textContent=`${state.renderer==='webgl'?'GPU':'CPU'} · ${state.loadedInfo||'준비'} · ${ms.toFixed(1)}ms`;
  const base=state.scene?Math.min(($('canvasWrap').clientWidth-76)/(state.scene.rect[2]-state.scene.rect[0]),($('canvasWrap').clientHeight-76)/(state.scene.rect[3]-state.scene.rect[1])):1;
  $('zoomLevel').textContent=`${Math.round(state.zoom/Math.max(base,.0000001)*100)}%`;
}
function updateBackgroundButton(){
  const btn=$('btnBackground');
  btn.classList.toggle('active',state.white);
  btn.setAttribute('aria-pressed',String(state.white));
  btn.title=state.white?'흰색 CAD 배경 (클릭 시 검정)':'검정 CAD 배경 (클릭 시 흰색)';
  btn.querySelector('span').textContent=state.white?'흰색':'검정';
}
function selectRenderer(mode){state.renderer=(mode==='webgl'&&state.gl)?'webgl':'canvas';$('rendererSelect').value=state.renderer;scheduleDraw()}
function resetSelection(){$('selectedInfo').innerHTML='<h3>선분을 선택하세요</h3><p>도면 위의 선을 클릭하면 레이어·길이·블록 정보가 표시됩니다.</p>';$('selectionDetails').innerHTML='';}
function showSelection(s){
  state.selected=s;
  if(!s){resetSelection();scheduleDraw();return}
  $('selectedInfo').innerHTML='<h3>선분 선택됨</h3><p>기존 CAD 캐시의 객체 정보 (읽기 전용)</p>';
  const rows=[['구분',s.kind],['레이어',s.layer],['길이',fmt(s.length)+' mm'],['객체',s.entity],['블록',s.block||'—'],['핸들',s.handle||'—']];
  $('selectionDetails').innerHTML='';
  for(const [k,v] of rows){const d=document.createElement('div');d.className='detail';const a=document.createElement('span');a.textContent=k;const b=document.createElement('b');b.textContent=String(v);b.title=String(v);d.append(a,b);$('selectionDetails').append(d)}
  scheduleDraw();
}
async function pickAt(x,y){
  if(!state.scene)return;
  const p=screenToWorld(x,y);
  if(state.id==='demo'){
    const d=state.geom;let best=null,dist=Infinity;
    for(const l of state.scene.layers){if(state.hidden.has(l.name))continue;
      for(let i=l.offset;i<l.offset+l.count;i++){
        const o=i*20,ax=d.getFloat32(o,true),ay=d.getFloat32(o+4,true),bx=d.getFloat32(o+8,true),by=d.getFloat32(o+12,true);
        const vx=bx-ax,vy=by-ay,t=Math.max(0,Math.min(1,((p.x-ax)*vx+(p.y-ay)*vy)/Math.max(1,vx*vx+vy*vy)));
        const xx=ax+t*vx,yy=ay+t*vy,dd=(p.x-xx)**2+(p.y-yy)**2;
        if(dd<dist){dist=dd;best={kind:'LINE',layer:l.name,x1:ax,y1:ay,x2:bx,y2:by,length:Math.hypot(vx,vy).toFixed(2),handle:'DEMO',entity:'LINE'}}
      }
    }
    showSelection(dist<Math.pow(10/state.zoom,2)?best:null);return;
  }
  try{
    const hidden=[...state.hidden].join('\x1f');
    const q=params({id:state.id,floor:state.floor,x:p.x,y:p.y,tol:Math.min(1500,Math.max(5,10/state.zoom)),hidden});
    const data=await request('/api/pick?'+q);showSelection(data.selected);
  }catch(e){message('객체 선택 실패: '+e.message,true)}
}


/* Logs are generated by the Python DXF analysis job, not just visible UI text. */
async function getReadLog(){
  const response=await fetch('/api/dxf/log',{cache:'no-store'});
  if(!response.ok)throw new Error('읽기 로그 요청 실패 (HTTP '+response.status+')');
  return response.text();
}
function logNotice(message,isError=false){
  const el=$('dxfLogStatus');el.textContent=message;
  el.style.color=isError?'#ff979b':'#82e6bb';
}
async function copyReadLog(){
  try{
    const text=await getReadLog();
    if(navigator.clipboard?.writeText){
      try{await navigator.clipboard.writeText(text)}
      catch(e){legacyCopyLog(text)}
    }else legacyCopyLog(text);
    logNotice('로그 복사 완료');
  }catch(e){logNotice('복사 실패: '+e.message,true)}
}
function legacyCopyLog(text){
  const area=document.createElement('textarea');area.value=text;
  area.style.cssText='position:fixed;left:-10000px;top:0;opacity:0';
  document.body.appendChild(area);area.focus();area.select();
  const ok=document.execCommand('copy');area.remove();
  if(!ok)throw new Error('클립보드 복사 권한이 없습니다. TXT 저장을 사용하세요.');
}
async function saveReadLog(){
  try{
    const text=await getReadLog();
    const now=new Date();
    const stamp=[now.getFullYear(),String(now.getMonth()+1).padStart(2,'0'),String(now.getDate()).padStart(2,'0')].join('')+'_'+[now.getHours(),now.getMinutes(),now.getSeconds()].map(x=>String(x).padStart(2,'0')).join('');
    const blob=new Blob(['\uFEFF',text.replace(/^\uFEFF/,'')],{type:'text/plain;charset=utf-8'});
    const url=URL.createObjectURL(blob);
    const link=document.createElement('a');link.href=url;link.download=`DXF_Read_Log_${stamp}.txt`;
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),2000);
    logNotice('TXT 저장 요청 완료');
  }catch(e){logNotice('TXT 저장 실패: '+e.message,true)}
}

function dxfProgress(text, error=false){
  const el=$('dxfJobStatus'); el.textContent=text; el.style.color=error?'#ff9999':'#9ebdd6';
}
function elapsedLabel(seconds){
  const total=Math.max(0,Math.floor(Number(seconds)||0));
  return `${Math.floor(total/60)}분 ${String(total%60).padStart(2,'0')}초`;
}
function showDxfElapsed(text){$('dxfElapsed').textContent=text}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function watchDxfJob(job, filename, clickStartedAt){
  for(;;){
    const result=await request('/api/dxf/job?'+params({id:job.id}));
    const parseTime=elapsedLabel(result.elapsedSeconds);
    if(result.state==='error'){
      showDxfElapsed('실패 · 경과 '+parseTime);
      throw new Error((result.error||'DXF 변환 실패')+' · 경과 '+parseTime);
    }
    if(result.state==='done'){
      const info=result.result||{};
      dxfProgress(info.cacheHit?'기존 디스크 캐시에서 화면 불러오는 중':'DXF 분석 완료 · 선택층 표시 중');
      showDxfElapsed(info.cacheHit?'캐시 확인 '+parseTime:'읽기·캐시 '+parseTime);
      state.importedTitle=filename;
      const select=$('fileSelect');let opt=select.querySelector('option[value="direct"]');
      if(!opt){opt=document.createElement('option');opt.value='direct';select.prepend(opt)}
      opt.textContent='DXF 층별 캐시 · '+filename;select.value='direct';
      await chooseFile();
      busy(false);
      const totalTime=elapsedLabel((performance.now()-clickStartedAt)/1000);
      showDxfElapsed(`전체 ${totalTime} · ${info.cacheHit?'캐시 재사용':'DXF 읽기·캐시 '+parseTime}`);
      dxfProgress(`${info.cacheHit?'기존 디스크 캐시 재사용 (재분석 없음)':'층별 디스크 캐시 저장 완료'} · `+
        `${info.floors?.length||0}개 층/영역 · 원본 선 ${fmt(info.segments)}개`+
        (info.frameDetection?.autoAdded?` · 도곽 자동인식 +${info.frameDetection.autoAdded}개`:'')+
        (info.notice?' · '+info.notice:'')+
        (info.unresolvedXrefs?.length?` · 미해결 XREF ${info.unresolvedXrefs.join(', ')}`:'')+
        (info.emptyBlockRefs?.length?` · 빈 블록 ${info.emptyBlockRefs.join(', ')}`:''));
      return;
    }
    dxfProgress(result.message);busy(true,result.message);
    showDxfElapsed('분석 중 '+parseTime);
    await sleep(400);
  }
}
async function openDxf(){
  const file=$('dxfFile').files[0];
  if(!file){dxfProgress('먼저 .dxf 파일을 선택하세요.',true);return}
  if(!file.name.toLowerCase().endsWith('.dxf')){dxfProgress('DXF 파일만 지원합니다.',true);return}
  const clickStartedAt=performance.now();
  showDxfElapsed('업로드 중…');
  const button=$('btnOpenDxf');button.disabled=true;
  busy(true,'DXF 업로드 중');dxfProgress(`${file.name} · 로컬 서버에 전달 중`);
  try{
    const response=await fetch('/api/dxf/upload',{
      method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file
    });
    const job=await response.json();if(!response.ok)throw new Error(job.error||`HTTP ${response.status}`);
    await watchDxfJob(job,file.name,clickStartedAt);
  }catch(e){busy(false);dxfProgress('실패: '+e.message,true);message('DXF 읽기 실패: '+e.message,true)}
  finally{button.disabled=false}
}
async function openDxfPath(){
  const pathname=$('dxfLocalPath').value.trim().replace(/^"|"$/g,'');
  if(!pathname.toLowerCase().endsWith('.dxf')){dxfProgress('PC의 DXF 파일 전체 경로를 입력하세요.',true);return}
  const clickStartedAt=performance.now();
  showDxfElapsed('분석 준비…');
  const button=$('btnOpenDxfPath');button.disabled=true;
  busy(true,'로컬 DXF 읽기 준비');dxfProgress('원본을 복사하지 않고 PC 경로에서 직접 분석합니다.');
  try{
    const response=await fetch('/api/dxf/open-path',{
      method:'POST',headers:{'Content-Type':'application/octet-stream'},body:new TextEncoder().encode(pathname)
    });
    const job=await response.json();if(!response.ok)throw new Error(job.error||`HTTP ${response.status}`);
    await watchDxfJob(job,pathname.split(/[\\/]/).pop(),clickStartedAt);
  }catch(e){busy(false);dxfProgress('실패: '+e.message,true);message('DXF 읽기 실패: '+e.message,true)}
  finally{button.disabled=false}
}

async function showFrameInfo(){
  const modal=$('frameModal'), container=$('frameDetails');
  modal.classList.add('visible');container.replaceChildren();
  $('frameSummary').textContent='도곽 판정 결과 불러오는 중…';
  try{
    const result=await request('/api/dxf/frames');
    const added=result.added||[], unresolved=result.unresolved||[];
    $('frameSummary').textContent=`기존 층 ${result.existing||0}개 · 자동 추가 ${result.autoAdded||0}개 · 사각 후보 ${result.rectCandidates||0}개 · 평면도 제목 ${result.planTitles||0}개 · 보류 ${result.unresolvedCount||0}개`;
    function row(title,desc,status,uncertain=false){
      const el=document.createElement('div');el.className='frameRow';
      const left=document.createElement('div');
      const strong=document.createElement('strong');strong.textContent=title;
      const detail=document.createElement('small');detail.textContent=desc;
      const right=document.createElement('span');right.className='frameStatus'+(uncertain?' uncertain':'');right.textContent=status;
      left.append(strong,detail);el.append(left,right);container.append(el);
    }
    const old=result.existingFrames||[];
    if(!added.length && !unresolved.length && !old.length)row('자동 판정 없음','기존 도곽 레이어 지정 결과만 사용했거나 평면도 제목을 찾지 못했습니다.','기존 유지');
    for(const f of old)row((f.floors||[]).join(', '),`${f.title} · 기존 도곽 지정`, '기존 유지');
    for(const f of added)row((f.floors||[]).join(', '),`${f.title} · ${f.source} · ${f.layer} · [${f.rect?.map(x=>Math.round(x)).join(', ')}]`,f.confidence||'자동');
    for(const f of unresolved)row((f.floors||[]).join(', ')||f.title,`${f.title} · ${f.reason}`,'판정 보류',true);
  }catch(e){$('frameSummary').textContent='도곽 결과를 읽을 수 없습니다: '+e.message;}
}

function bindUI(){
  $('dxfFile').addEventListener('change',e=>{$('dxfSelected').textContent=e.target.files[0]?.name||'선택된 DXF 없음'});
  $('btnOpenDxf').addEventListener('click',openDxf);
  $('btnOpenDxfPath').addEventListener('click',openDxfPath);
  $('btnFrameInfo').addEventListener('click',showFrameInfo);
  $('btnCopyLog').addEventListener('click',copyReadLog);
  $('btnSaveLog').addEventListener('click',saveReadLog);
  $('closeFrameInfo').addEventListener('click',()=> $('frameModal').classList.remove('visible'));
  $('frameModal').addEventListener('click',e=>{if(e.target===$('frameModal'))$('frameModal').classList.remove('visible')});
  $('dxfLocalPath').addEventListener('keydown',e=>{if(e.key==='Enter')openDxfPath()});
  $('btnScan').addEventListener('click',()=>scan($('rootInput').value));
  $('rootInput').addEventListener('keydown',e=>{if(e.key==='Enter')scan(e.target.value)});
  $('fileSelect').addEventListener('change',chooseFile);
  $('floorSelect').addEventListener('change',chooseFloor);
  $('layerSearch').addEventListener('input',rebuildLayerList);
  $('layerAll').addEventListener('click',()=>{state.hidden.clear();rebuildLayerList();scheduleDraw()});
  $('layerNone').addEventListener('click',()=>{state.hidden=new Set(state.scene?.layers.map(l=>l.name)||[]);rebuildLayerList();scheduleDraw()});
  $('btnFit').addEventListener('click',fitView);
  $('btnView').addEventListener('click',()=>{$('btnView').classList.add('active');$('btnSelect').classList.remove('active')});
  $('btnSelect').addEventListener('click',()=>{$('btnSelect').classList.add('active');$('btnView').classList.remove('active')});
  $('rendererSelect').addEventListener('change',e=>selectRenderer(e.target.value));
  $('lineWidth').addEventListener('input',e=>{state.lineWidth=Number(e.target.value);$('widthLabel').textContent=state.lineWidth.toFixed(1)+'px';scheduleDraw()});
  $('btnBackground').addEventListener('click',()=>{
    state.white=!state.white;
    updateBackgroundButton();
    scheduleDraw();
  });
  $('btnGray').addEventListener('click',()=>{state.gray=!state.gray;$('btnGray').classList.toggle('active',state.gray);$('btnGray span').textContent=state.gray?'회색':'원색';scheduleDraw()});
  $('btnText').addEventListener('click',()=>{state.showText=!state.showText;$('btnText').classList.toggle('active',state.showText);scheduleDraw()});
  $('btnFills').addEventListener('click',()=>{state.showFills=!state.showFills;$('btnFills').classList.toggle('active',state.showFills);scheduleDraw()});
  const modal=$('helpModal');$('btnHelp').onclick=()=>modal.classList.add('visible');$('closeHelp').onclick=$('closeHelp2').onclick=()=>modal.classList.remove('visible');
  const wrap=$('canvasWrap');
  wrap.addEventListener('pointerdown',e=>{if(e.button!==0&&e.button!==1)return;e.preventDefault();wrap.setPointerCapture(e.pointerId);state.drag={x:e.clientX,y:e.clientY,tx:state.tx,ty:state.ty,dist:0};wrap.classList.add('dragging')});
  wrap.addEventListener('pointermove',e=>{
    const rect=wrap.getBoundingClientRect(),p=screenToWorld(e.clientX-rect.left,e.clientY-rect.top);
    if(state.scene){$('coords').textContent=`X: ${fmt((p.x+state.scene.origin[0]).toFixed(1))} · Y: ${fmt((p.y+state.scene.origin[1]).toFixed(1))}`}
    if(!state.drag)return;
    const dx=e.clientX-state.drag.x,dy=e.clientY-state.drag.y;state.drag.dist=Math.max(state.drag.dist,Math.hypot(dx,dy));
    state.tx=state.drag.tx+dx;state.ty=state.drag.ty+dy;scheduleDraw();
  });
  wrap.addEventListener('pointerup',e=>{if(!state.drag)return;const dist=state.drag.dist;state.drag=null;wrap.classList.remove('dragging');if(dist<=4&&$('btnSelect').classList.contains('active')){const rect=wrap.getBoundingClientRect();pickAt(e.clientX-rect.left,e.clientY-rect.top)}});
  wrap.addEventListener('pointercancel',()=>{state.drag=null;wrap.classList.remove('dragging')});
  wrap.addEventListener('wheel',e=>{e.preventDefault();if(!state.scene)return;const rect=wrap.getBoundingClientRect();const px=e.clientX-rect.left,py=e.clientY-rect.top;const world=screenToWorld(px,py);const newZoom=Math.min(.5,Math.max(1e-7,state.zoom*Math.exp(-e.deltaY*.0013)));state.zoom=newZoom;state.tx=px-world.x*newZoom;state.ty=py+world.y*newZoom;scheduleDraw()},{passive:false});
  document.addEventListener('keydown',e=>{if(e.target.matches('input,select'))return;if(e.key.toLowerCase()==='f')fitView()});
  new ResizeObserver(()=>scheduleDraw()).observe(wrap);
}
async function main(){
  bindUI();updateBackgroundButton();initGL();selectRenderer(state.gl?'webgl':'canvas');
  const c=await request('/api/config');$('rootInput').value=c.root||'';
  state.importedTitle=c.direct||'';
  await scan(c.root||'',true);
}
main().catch(e=>{busy(false);message(e.message,true);console.error(e)});
