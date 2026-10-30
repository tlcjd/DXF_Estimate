# -*- coding: utf-8 -*-
"""DXF 견적프로그램: 기존 2단 벡터 캐시를 읽는 오프라인 브라우저 시험판.

표준 라이브러리만 사용. CAD/원본 캐시는 절대 수정하지 않는다.
주의: pickle은 신뢰할 수 있는 자기 PC에서 생성한 캐시만 열어야 한다.
"""
from __future__ import annotations

import argparse
import tempfile
import uuid
import traceback
import gzip
import hashlib
import json
import math
import os
import pickle
import struct
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT.parent
STATIC = ROOT / 'static'
if str(APP_ROOT) not in sys.path: sys.path.insert(0, str(APP_ROOT))
from web_trial.browser_window import launch_app_window
from web_trial import read_log, file_dialog
from web_trial.disk_cache import (CACHE_ROOT, upload_signature, signature_for_path,
    load_index, latest_index, write_cache, floor_scene, floor_order, pick_disk)
MAX_CACHE_FILES = 350
MAX_CACHE_BYTES = 1024 * 1024 * 1024


def tojson(v):
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def clean_num(v, default=0.0):
    try:
        n = float(v)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default


def color3(raw):
    try:
        values = list(raw or ())
        return tuple(max(0, min(255, int(values[i]))) for i in range(3))
    except (ValueError, TypeError, IndexError):
        return (168, 180, 192)


def layer_of_segment(raw):
    if len(raw) >= 10 and isinstance(raw[9], dict):
        return str(raw[9].get('layer') or '레이어 미지정')
    return '레이어 미지정'


def layer_of_item(raw):
    meta = raw.get('meta') or {}
    return str(meta.get('layer') or raw.get('layer') or '레이어 미지정')


def scan_cache_files(root: Path):
    """기존 프로그램 임시/프로젝트 캐시를 원본 변경 없이 인덱싱."""
    root = root.expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f'폴더가 없습니다: {root}')
    # 여기서는 사용자파일/프로젝트 아래만 탐색. 소스, 가상환경을 탐색하지 않는다.
    candidates = []
    paths = [root]
    if (root / '사용자파일' / '프로젝트').is_dir():
        paths = [root / '사용자파일' / '프로젝트']
    elif (root / '프로젝트').is_dir():
        paths = [root / '프로젝트']
    for base in paths:
        for folder, dirs, names in os.walk(base):
            depth = len(Path(folder).relative_to(base).parts)
            if depth > 9:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in ('.venv','__pycache__','.git','node_modules')]
            for name in names:
                if name.lower().endswith('.pkl') and (name == '원본도형_캐시.pkl' or '_source_geometry' in Path(folder).parts):
                    f = Path(folder) / name
                    try:
                        if 100 <= f.stat().st_size <= MAX_CACHE_BYTES:
                            candidates.append(f)
                    except OSError:
                        pass
                    if len(candidates) >= MAX_CACHE_FILES:
                        return sorted(candidates, key=lambda p: str(p))
    return sorted(candidates, key=lambda p: str(p))


class Catalog:
    def __init__(self):
        self.lock = threading.RLock()
        self.paths = {}
        self.root = ''
        self.cache_path = None
        self.cache_payload = None
        self.scene = None
        self.scene_key = None
        self.direct_payload = None
        self.direct_index = None
        self.direct_title = ''

    def set_direct_cache(self, index):
        # Restore the last selected DXF across server restarts.
        from web_trial import disk_cache as dc
        try:
            dc.CACHE_ROOT.mkdir(parents=True,exist_ok=True)
            (dc.CACHE_ROOT/'last_opened.txt').write_text(index['signature'],encoding='ascii')
        except (OSError,KeyError):pass
        with self.lock:
            self.direct_payload = None
            self.direct_index = index
            self.direct_title = str(index.get('title') or '')
            self.scene = None
            self.scene_key = None

    def scan(self, root: str):
        rootpath = Path(root).expanduser()
        candidates = scan_cache_files(rootpath)
        with self.lock:
            self.paths = {str(i): p for i, p in enumerate(candidates)}
            self.root = str(rootpath)
            self.cache_payload = None
            self.cache_path = None
            self.scene = None
            self.scene_key = None
        return {'root': self.root, 'files': [
            {'id': str(i), 'path': str(p), 'name':p.parent.parent.name if p.name=='원본도형_캐시.pkl' else p.stem,
             'sizeMB': round(p.stat().st_size / (1024*1024), 2)}
            for i,p in enumerate(candidates)]}

    def payload(self, id_):
        with self.lock:
            if id_ == 'direct':
                raise ValueError('직접 DXF의 벡터는 층별 디스크 캐시에서 읽습니다.')
            path = self.paths.get(id_)
            if not path:
                raise ValueError('목록에서 원본 벡터 캐시를 선택하세요.')
            if self.cache_path == path and self.cache_payload is not None:
                return self.cache_payload
            # 기존 캐시 포맷은 자체 Python pickle. 신뢰하는 자신의 캐시만 허용.
            with path.open('rb') as f:
                data = pickle.load(f)
            if not isinstance(data, dict) or not isinstance(data.get('floors'), dict):
                raise ValueError('층별 벡터 정보(floors)가 없습니다. 다른 캐시를 선택하거나 기존 프로그램에서 CAD 도서읽기를 실행하세요.')
            self.cache_payload = data
            self.cache_path = path
            self.scene = None
            self.scene_key = None
            return data

    def recommended(self):
        """보유 캐시 중 실제 층별 벡터가 있는 파일을 골라준다.

        검색된 마지막 파일이 유효하다고 가정하지 않는다. 프로젝트에 저장된
        구버전/미완성 캐시와 최적화 캐시가 혼재할 수 있다.
        """
        with self.lock:
            ordered = sorted(self.paths.items(),
                             key=lambda kv: kv[1].stat().st_mtime_ns if kv[1].exists() else 0,
                             reverse=True)
        rejected = []
        for id_, path in ordered:
            try:
                data = self.payload(id_)
                floor_map = data.get('floors') or {}
                if not any(isinstance(v, dict) and (
                    v.get('segments') or v.get('annotation_segments') or v.get('texts') or v.get('fills')
                ) for v in floor_map.values()):
                    raise ValueError('층별 도형 데이터 없음')
                return {'id': id_, 'name': path.name, 'skipped': len(rejected),
                        'skippedExamples': rejected[:3]}
            except (Exception) as exc:
                rejected.append(f'{path.name}: {type(exc).__name__}: {exc}')
                continue
        return {'id': None, 'skipped':len(rejected), 'skippedExamples':rejected[:3]}

    def floors(self, id_):
        if id_ == 'direct':
            with self.lock:
                index=self.direct_index
            if not index:raise ValueError('DXF 캐시가 없습니다. DXF를 먼저 읽으세요.')
            return {'name':index['title'],'version':index['cacheVersion'],
                    'source':index['title'], 'diskCache':True,
                    'spatialFallback':index.get('spatialFallback',False),
                    'floors':[{'name':f['name'],'segments':f['segments'],
                              'texts':f['texts'],'fills':f['fills']}
                              for f in index['floors']]}
        data = self.payload(id_)
        floors = data.get('floors') or {}
        title = self.direct_title if id_ == 'direct' else self.cache_path.name
        return {'name': title, 'version':data.get('version'),
                'source':data.get('source_path') or '',
                'floors':[{'name':str(name), 'segments':len(v.get('segments') or [])+len(v.get('annotation_segments') or []),
                           'texts':len(v.get('texts') or []), 'fills':len(v.get('fills') or [])}
                          for name,v in sorted(floors.items(), key=lambda a:floor_order(a[0])) if isinstance(v, dict)]}

    def prepare_scene(self, id_, floor):
        with self.lock:
            key = (id_,floor)
            if self.scene_key == key and self.scene is not None:
                return self.scene
            if id_ == 'direct':
                if not self.direct_index:raise ValueError('DXF 캐시를 먼저 읽으세요.')
                self.scene=floor_scene(self.direct_index, floor)
                self.scene_key=key
                return self.scene
            data = self.payload(id_)
            rawfloor = (data.get('floors') or {}).get(floor)
            if not isinstance(rawfloor, dict):
                raise ValueError(f'없는 층입니다: {floor}')
            rect = rawfloor.get('crop_rect') or (0,0,100000,100000)
            if not isinstance(rect,(list,tuple)) or len(rect)!=4:
                rect = (0,0,100000,100000)
            rect = tuple(clean_num(v) for v in rect)
            ox,oy = rect[:2]
            # 큰 도면에서도 원본 선분 목록을 통째로 복제하지 않는다.
            lines = rawfloor.get('segments') or ()
            annotation = rawfloor.get('annotation_segments') or ()
            # (선분, 종류) 튜플을 선분마다 생성하지 않고 레이어별 참조만 모은다.
            grouping = defaultdict(lambda: ([], []))
            for flag, coll in ((0, lines), (1, annotation)):
                for seg in coll:
                    try:
                        if len(seg) < 4: continue
                        grouping[layer_of_segment(seg)][flag].append(seg)
                    except Exception:
                        continue
            text_by_layer = defaultdict(list)
            alltext = []
            for item in rawfloor.get('texts') or []:
                try:
                    layer = layer_of_item(item)
                    text = str(item.get('text') or '')
                    if not text:continue
                    rgb=color3(item.get('rgb'))
                    packed = {'x':clean_num(item.get('x'))-ox, 'y':clean_num(item.get('y'))-oy,
                              'height':max(1,clean_num(item.get('height'),150)),
                              'rotation':clean_num(item.get('rotation')),
                              'text':text[:1000], 'rgb':rgb, 'layer':layer,
                              'halign':item.get('halign',0),'valign':item.get('valign',0),
                              'attachment':item.get('attachment',0)}
                    text_by_layer[layer].append(packed)
                    alltext.append(packed)
                except Exception:
                    continue
            allfills = []
            for item in rawfloor.get('fills') or []:
                try:
                    pts = [[clean_num(p[0])-ox,clean_num(p[1])-oy] for p in (item.get('points') or [])]
                    if len(pts) >= 3:
                        allfills.append({'layer':layer_of_item(item), 'rgb':color3(item.get('rgb')),
                                         'points':pts[:2000]})
                except Exception:pass
            buf = bytearray()
            index = []
            offset = 0
            for layer in sorted(set(grouping)|set(text_by_layer)|{f['layer'] for f in allfills}):
                normal, annotated = grouping[layer]
                count = len(normal) + len(annotated)
                idx = {'name':layer,'count':count,'offset':offset,'texts':len(text_by_layer[layer])}
                for flag, entries in ((0, normal), (1, annotated)):
                    for seg in entries:
                        try:
                            rgb=color3(seg[4:7]) if len(seg)>=7 else (170,170,170)
                            buf.extend(struct.pack('<4f4B', clean_num(seg[0])-ox, clean_num(seg[1])-oy,
                                                   clean_num(seg[2])-ox, clean_num(seg[3])-oy,*rgb, flag))
                        except (OverflowError,struct.error):
                            buf.extend(struct.pack('<4f4B',0,0,0,0,100,100,100,flag))
                offset += count
                index.append(idx)
            scene = {'id':id_, 'floor':floor,'rect':[0,0,rect[2]-ox,rect[3]-oy],
                     'origin':[ox,oy], 'layers':index, 'totalLines':offset,
                     'totalTexts':len(alltext),'totalFills':len(allfills),
                     'bin':buf,'texts':alltext,'fills':allfills, 'raw':rawfloor,
                     'built':time.perf_counter()}
            self.scene_key=key
            self.scene=scene
            return scene

CATALOG = Catalog()

def safe_data(scene):
    return {k:scene[k] for k in ('id','floor','rect','origin','layers','totalLines','totalTexts','totalFills')}


def sample_scene():
    # 실제 캐시가 없는 PC에서도 UI 조작을 확인할 수 있도록 간단한 벡터 예제 제공.
    # '샘플'임을 화면에 명확히 표시하고 실제 CAD 성능 검증용으로 사용하지 않는다.
    rect=(0,0,54000,38000)
    groups=defaultdict(list)
    def seg(layer,x1,y1,x2,y2,rgb):
        groups[layer].append((x1,y1,x2,y2,*rgb,0))
    for x in range(0,54001,6000):seg('S-축열',x,0,x,38000,(65,90,120))
    for y in range(0,38001,6000):seg('S-축열',0,y,54000,y,(65,90,120))
    for y in (2000,35000):
        seg('A-WALL',2000,y,52000,y,(230,218,95))
    for x in (2000,52000):seg('A-WALL',x,2000,x,35000,(230,218,95))
    for y in (10500,21500):
        for x in range(4500,50500,6200):
            seg('A-PARK',x,y,x+5300,y,(62,205,140));seg('A-PARK',x,y+9000,x+5300,y+9000,(62,205,140))
            seg('A-PARK',x,y,x,y+9000,(62,205,140));seg('A-PARK',x+5300,y,x+5300,y+9000,(62,205,140))
    for x in (3500,51000):
        for y in (6000,18000,30000):
            for dx,dy,ex,ey in [(0,0,1700,0),(1700,0,1700,1500),(1700,1500,0,1500),(0,1500,0,0)]:
                seg('S-COLUMN',x+dx,y+dy,x+ex,y+ey,(255,193,100))
    buf=bytearray();layers=[];offset=0
    for layer,items in groups.items():
        layers.append({'name':layer,'offset':offset,'count':len(items),'texts':0})
        for s in items:buf.extend(struct.pack('<4f4B',*s))
        offset+=len(items)
    return {'id':'demo','floor':'DEMO','rect':rect,'origin':[0,0], 'layers':layers,
            'totalLines':offset,'totalTexts':2,'totalFills':0,'bin':bytes(buf),
            'texts':[{'x':2000,'y':36500,'height':1100,'rotation':0,'text':'DEMO / 지하주차장 (실제 CAD 캐시 아님)','rgb':[220,230,242],'layer':'DEMO-TEXT'},
                     {'x':15000,'y':17700,'height':650,'rotation':0,'text':'DXF 로컬 웹 뷰어 시험판','rgb':[220,230,242],'layer':'DEMO-TEXT'}],
            'fills':[], 'raw':{}}

DEMO=sample_scene()


def pick(scene, x, y, visible):
    # 시험판 선택: 데이터 전체를 서버에서 검사. 편집/원본 변경 없음.
    raw = scene.get('raw') or {}
    best=None;best_d2=float('inf');maxrad= max(10, min(1500, float(visible.get('tol') or 150)))
    sx, sy = x+scene['origin'][0],y+scene['origin'][1]
    for kind,items in [('LINE',raw.get('segments') or []),('DIMENSION',raw.get('annotation_segments') or [])]:
        for s in items:
            try:
                layer=layer_of_segment(s)
                if layer in visible['hidden']:continue
                ax,ay,bx,by=map(float,s[:4])
                if (sx < min(ax,bx)-maxrad or sx>max(ax,bx)+maxrad or sy<min(ay,by)-maxrad or sy>max(ay,by)+maxrad):continue
                ux,uy=bx-ax,by-ay
                t=max(0,min(1,((sx-ax)*ux+(sy-ay)*uy)/(ux*ux+uy*uy))) if ux*ux+uy*uy else 0
                px,py=ax+t*ux,ay+t*uy
                d2=(sx-px)**2+(sy-py)**2
                if d2<best_d2:
                    best_d2=d2
                    m=s[9] if len(s)>9 and isinstance(s[9],dict) else {}
                    best={'kind':kind,'layer':layer,'handle':str(s[8]) if len(s)>8 else '',
                          'block':str(m.get('block_name') or ''),'entity':str(m.get('entity_type') or kind),
                          'x1':ax-scene['origin'][0],'y1':ay-scene['origin'][1],
                          'x2':bx-scene['origin'][0],'y2':by-scene['origin'][1],
                          'length':round(math.hypot(ux,uy),2), 'distance':round(math.sqrt(d2),2)}
            except Exception:continue
    return best if best_d2<maxrad*maxrad else None



class DXFJobs:
    """Single active DXF parse job; source files never modified."""
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None

    def begin(self, source: Path, title: str, remove_after: bool, key: str, force: bool = False):
        started_perf = time.perf_counter()
        with self.lock:
            if self.job and self.job.get('state') == 'running':
                raise ValueError('다른 DXF를 읽는 중입니다. 완료 후 다시 시도하세요.')
            existing=None if force else load_index(key)
            if existing:
                CATALOG.set_direct_cache(existing)
                job={'id':uuid.uuid4().hex,'state':'done','message':'저장된 층별 캐시 재사용 · DXF 재분석 없음',
                     'title':title,'stage':100,'error':'','result':dict(existing['stats'],cacheHit=True),
                     'started':time.time(), '_start_perf':started_perf, '_end_perf':time.perf_counter()}
                read_log.start_log(job,source,cache_hit=True)
                read_log.record(job,'기존 디스크 캐시 재사용 · DXF 재분석 없음',stage=100)
                self.job=job
                read_log.persist(job)
                if remove_after:source.unlink(missing_ok=True)
                return {'id':job['id']}
            job = {'id': uuid.uuid4().hex, 'state':'running', 'message':'DXF 읽기 준비',
                   'title':title,'stage':0,'error':'','result':None,'started':time.time(),
                   '_start_perf':started_perf, '_end_perf':None}
            read_log.start_log(job,source,cache_hit=False)
            if force:
                read_log.record(job, "강제 재분석 선택 · 기존 캐시가 있어도 새로 분석", stage=0)
            self.job=job
            read_log.persist(job)
        threading.Thread(target=self._run,args=(job['id'],source,title,remove_after,key,force),daemon=True).start()
        return {'id':job['id']}

    def update(self,id_,**kwargs):
        with self.lock:
            if self.job and self.job['id']==id_:
                if kwargs.get('state') in ('done', 'error'):
                    self.job['_end_perf'] = time.perf_counter()
                self.job.update(kwargs)
                if 'message' in kwargs:
                    read_log.record(self.job,kwargs['message'],stage=kwargs.get('stage',self.job.get('stage')))
                read_log.persist(self.job)

    def log_text(self):
        with self.lock:
            if self.job:return read_log.render(self.job)
            return read_log.latest_text()

    def status(self,id_):
        with self.lock:
            if not self.job or self.job['id']!=id_:
                raise ValueError('작업 기록을 찾을 수 없습니다.')
            result = {k:v for k,v in self.job.items() if not k.startswith('_')}
            end = self.job['_end_perf']
            result['elapsedSeconds'] = round(max(0.0, (end if end is not None else time.perf_counter())
                                                  - self.job['_start_perf']), 1)
            return result

    def _run(self,id_,path,title,remove_after,key,force=False):
        try:
            index, stats = import_dxf(path, title, key, lambda step,msg:self.update(id_,stage=step,message=msg), force=force)
            CATALOG.set_direct_cache(index)
            self.update(id_,state='done',stage=100,message='층별 디스크 캐시 저장 완료',result=stats)
        except Exception as exc:
            print('[DXF 읽기 실패]', traceback.format_exc(),flush=True)
            self.update(id_,state='error',message='DXF 읽기 실패',error=f'{type(exc).__name__}: {exc}',
                        _traceback=traceback.format_exc())
        finally:
            if remove_after:
                try:path.unlink(missing_ok=True)
                except OSError:pass

def _choice_path(key):
    if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('캐시 식별자가 올바르지 않습니다.')
    return CACHE_ROOT/'frame_choices'/f'{key}.json'


def _read_choices(key):
    path=_choice_path(key)
    try:
        raw=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if not isinstance(raw,dict):return {}
        return {str(f):list(rect) for f,rect in raw.items() if isinstance(rect,list) and len(rect)==4}
    except (ValueError,OSError):return {}


def _save_choices(key, choices):
    target=_choice_path(key)
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.tmp.'+uuid.uuid4().hex)
    try:
        temp.write_text(json.dumps(choices,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temp,target)
    finally:temp.unlink(missing_ok=True)


DXF_JOBS = DXFJobs()


def import_dxf(path:Path, title:str, key:str, progress=None, *, force=False):
    """V0.5: existing DXF core -> crop per floor -> persisted binary sidecars.

    The original DXF is read-only, and native CAD COM / Qt are not required.
    """
    try:import ezdxf
    except ImportError as exc:
        raise RuntimeError('DXF 읽기에는 ezdxf가 필요합니다. python -m pip install ezdxf') from exc
    from app.rooms.core import read_drawing_divisions, read_cad_background
    from web_trial.auto_frames import discover_plan_frames
    from app.rooms.background_source_cache import floor_frame_rects
    tell=progress or (lambda step,msg:None)
    tell(10,'DXF 파일 읽는 중')
    doc=ezdxf.readfile(path);msp=doc.modelspace()
    tell(25,'평면도 도곽 및 층 제목 판정 중')
    frames, frame_report = discover_plan_frames(msp, read_drawing_divisions(msp),
                                                  preferred=_read_choices(key))
    tell(30, f"도곽 자동인식 {frame_report['autoAdded']}개 추가 · 기존 {frame_report['existing']}개 층")
    rects=floor_frame_rects(frames)
    crops=list({tuple(v) for v in rects.values()})
    tell(35,'기존 DXF 엔진으로 블록·XCLIP·선분 해석 중')
    segments,extra=read_cad_background(doc,crop_rects=crops or None)
    extra['frame_detection'] = frame_report
    annotations=extra.get('annotation_segments') or []
    texts=extra.get('texts') or []
    fills=extra.get('fills') or []
    if not segments and not annotations:
        raise ValueError('도면 선분이 없습니다. DXF에 외부참조가 포함되어 있는지 확인하세요.')
    tell(80, f'층별 벡터 분할·디스크 저장 준비 · 원본 {len(segments)+len(annotations):,} 선분')
    index=write_cache(key,title,path,frames,rects,segments,texts=texts,annotations=annotations,fills=fills,extra=extra,progress=tell,force=force)
    stats=index['stats']
    if frame_report.get('manualSelections'):
        stats['notice']=f"사용자가 선택한 도곽 {len(frame_report['manualSelections'])}개 적용"
    elif frame_report['autoAdded']:
        stats['notice']=f"평면 도곽 {frame_report['autoAdded']}개 자동 판정 · 결과 확인 가능"
    elif frame_report['unresolvedCount']:
        stats['notice']=f"평면도 제목 {frame_report['unresolvedCount']}개는 도곽 확정 보류 · 결과 확인 필요"
    if index.get('spatialFallback'):
        stats['notice']='평면도 도곽/층 제목을 확정하지 못해 공간별 영역으로 나눴습니다. 층 이름은 자동 추측하지 않습니다.'
    tell(100,'층별 디스크 캐시 저장 완료 · 웹은 선택한 층만 불러옵니다')
    return index,stats

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and not self.path.startswith('/api/geometry'):
            print('[WEB]',fmt%args)
    def send_bytes(self, payload, mime='application/json', status=200, compress=True):
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Cache-Control','no-store')
        if compress and 'gzip' in self.headers.get('Accept-Encoding','') and len(payload)>2048:
            payload=gzip.compress(payload,compresslevel=3)
            self.send_header('Content-Encoding','gzip')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        try:self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass
    def respond(self,obj,status=200):self.send_bytes(tojson(obj),status=status)
    def do_POST(self):
        parsed=urlparse(self.path)
        if parsed.path not in ('/api/dxf/browse','/api/dxf/upload','/api/dxf/open-path','/api/dxf/cache/delete','/api/dxf/frame/choose','/api/dxf/frame/reset'):
            self.respond({'error':'지원하지 않는 요청'},404);return
        try:
            if parsed.path=='/api/dxf/cache/delete':
                if DXF_JOBS.job and DXF_JOBS.job.get('state')=='running':
                    raise ValueError('DXF 분석 중에는 캐시를 삭제할 수 없습니다.')
                with CATALOG.lock:
                    index=CATALOG.direct_index
                    if index is None:
                        raise ValueError('삭제할 현재 DXF 캐시가 없습니다.')
                    sig=str(index.get('signature') or '')
                    if len(sig)!=64 or any(c not in '0123456789abcdef' for c in sig):
                        raise ValueError('캐시 식별자가 올바르지 않습니다.')
                    target=CACHE_ROOT/sig
                    # Only delete generated disk caches under our own cache root.
                    if target.exists():
                        import shutil
                        shutil.rmtree(target)
                    CATALOG.direct_index=None
                    CATALOG.direct_payload=None
                    CATALOG.direct_title=''
                    CATALOG.scene=None
                    CATALOG.scene_key=None
                    last=CACHE_ROOT/'last_opened.txt'
                    if last.is_file() and last.read_text(encoding='ascii').strip()==sig:
                        last.unlink()
                self.respond({'deleted':True,'signature':sig})
                return
            if parsed.path=='/api/dxf/browse':
                # 브라우저 업로드(복사) 대신 PC의 파일 선택 창으로 원본 경로를 받아 직접 읽는다.
                if DXF_JOBS.job and DXF_JOBS.job.get('state')=='running':
                    raise ValueError('이미 DXF 분석 중입니다.')
                length=int(self.headers.get('Content-Length','0'))
                if length>512:raise ValueError('요청이 올바르지 않습니다.')
                req=json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                with CATALOG.lock:index=CATALOG.direct_index
                last=Path(str((index or {}).get('sourcePath') or '')).parent
                path=file_dialog.ask_dxf_path(str(last) if str(last)!='.' and last.is_dir() else '')
                if path is None:
                    self.respond({'canceled':True});return
                if path.suffix.lower()!='.dxf' or not path.is_file():
                    raise ValueError('DXF 파일만 열 수 있습니다.')
                key=signature_for_path(path)
                if req.get('carryChoices') and index:
                    # 업로드 캐시에서 고른 도곽 선택을 같은 도면의 경로 캐시로 이어 받는다.
                    old=_read_choices(str(index.get('signature') or ''))
                    if old or _read_choices(key):_save_choices(key,old)
                result=DXF_JOBS.begin(path,path.name,False,key,force=bool(req.get('force')))
                result['title']=path.name
                self.respond(result,202)
                return
            if parsed.path=='/api/dxf/frame/reset':
                if DXF_JOBS.job and DXF_JOBS.job.get('state')=='running':
                    raise ValueError('DXF 분석 중에는 도곽 선택을 해제할 수 없습니다.')
                with CATALOG.lock:index=CATALOG.direct_index
                if not index:raise ValueError('현재 DXF가 없습니다.')
                length=int(self.headers.get('Content-Length','0'))
                if length<=0 or length>512:raise ValueError('요청이 올바르지 않습니다.')
                data=json.loads(self.rfile.read(length).decode('utf-8'))
                floor=str(data.get('floor') or '').strip()
                key=str(index.get('signature') or '')
                choices=_read_choices(key)
                if floor not in choices:raise ValueError('사용자 지정된 도곽이 없습니다.')
                del choices[floor]
                _save_choices(key,choices)
                source=Path(str(index.get('sourcePath') or ''))
                if not source.is_file():
                    self.respond({'needsUpload':True,'floor':floor},200);return
                job=DXF_JOBS.begin(source,source.name,False,key,force=True)
                self.respond(job,202)
                return
            if parsed.path=='/api/dxf/frame/choose':
                if DXF_JOBS.job and DXF_JOBS.job.get('state')=='running':
                    raise ValueError('DXF 분석 중에는 도곽을 변경할 수 없습니다.')
                with CATALOG.lock:
                    index=CATALOG.direct_index
                if not index:raise ValueError('현재 열린 DXF가 없습니다.')
                source=Path(str(index.get('sourcePath') or ''))
                # Browser uploads are intentionally removed after analysis.
                # If absent, record the choice and ask browser to re-upload the
                # original File input; never keep a secret copy of the DXF.
                needs_upload=not source.is_file() or source.suffix.lower()!='.dxf'
                length=int(self.headers.get('Content-Length','0'))
                if length<=0 or length>2048:raise ValueError('도곽 선택 요청이 올바르지 않습니다.')
                req=json.loads(self.rfile.read(length).decode('utf-8'))
                floor=str(req.get('floor') or '').strip()
                b=req.get('rect')
                if not floor or not isinstance(b,list) or len(b)!=4:
                    raise ValueError('층과 도곽 좌표가 필요합니다.')
                rect=[float(v) for v in b]
                if not all(map(math.isfinite,rect)) or rect[2]<=rect[0] or rect[3]<=rect[1]:
                    raise ValueError('도곽 범위가 올바르지 않습니다.')
                report=(index.get('stats') or {}).get('frameDetection') or {}
                pool=(report.get('alternatives') or [])+(report.get('added') or [])
                def match(choice):
                    r=choice.get('rect') or []
                    return floor in (choice.get('floors') or []) and len(r)==4 and all(
                        abs(float(a)-float(b))<=max(1,abs(float(a))*.00000001)
                        for a,b in zip(r,rect))
                if not any(match(choice) for choice in pool):
                    raise ValueError('현재 판정 결과에 없는 도곽입니다. 다시 DXF를 읽어주세요.')
                key=str(index.get('signature') or '')
                choices=_read_choices(key)
                choices[floor]=rect
                _save_choices(key,choices)
                if needs_upload:
                    self.respond({'needsUpload':True,'floor':floor},200)
                    return
                job=DXF_JOBS.begin(source,source.name,False,key,force=True)
                self.respond(job,202)
                return
            force=self.headers.get('X-Force-Rebuild','')=='1'
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/octet-stream':
                raise ValueError('DXF 파일 전송 형식이 올바르지 않습니다.')
            length=int(self.headers.get('Content-Length','0'))
            if length <= 0:
                raise ValueError('DXF 업로드 파일이 비어 있습니다.')
            if DXF_JOBS.job and DXF_JOBS.job.get('state')=='running':
                raise ValueError('이미 DXF 분석 중입니다.')
            if parsed.path=='/api/dxf/upload':
                filename=unquote(self.headers.get('X-File-Name','drawing.dxf'))
                filename=Path(filename.replace('\\','/')).name
                if Path(filename).suffix.lower()!='.dxf':raise ValueError('DXF 파일만 지원합니다.')
                folder=Path(tempfile.gettempdir())/'dxf_estimate_web_poc'
                folder.mkdir(parents=True,exist_ok=True)
                target=folder/(uuid.uuid4().hex+'.dxf')
                try:
                    digest=hashlib.blake2b(digest_size=24)
                    with target.open('wb') as f:
                        rest=length
                        while rest:
                            chunk=self.rfile.read(min(rest,1024*1024))
                            if not chunk:raise ValueError('파일 수신이 중단됐습니다.')
                            f.write(chunk);digest.update(chunk);rest-=len(chunk)
                    result=DXF_JOBS.begin(target,filename,True,upload_signature(digest.hexdigest()),force=force)
                except Exception:
                    target.unlink(missing_ok=True);raise
            else:
                raw=self.rfile.read(length).decode('utf-8')
                path=Path(raw.strip().strip('"')).expanduser()
                if path.suffix.lower()!='.dxf' or not path.is_file():
                    raise ValueError('PC에 존재하는 .dxf 파일의 전체 경로를 입력하세요.')
                result=DXF_JOBS.begin(path,path.name,False,signature_for_path(path),force=force)
            self.respond(result,202)
        except Exception as exc:self.respond({'error':f'{type(exc).__name__}: {exc}'},400)

    def do_GET(self):
        p=urlparse(self.path); q=parse_qs(p.query)
        def arg(s,d=''):return q.get(s,[d])[0]
        try:
            if p.path=='/':
                self.send_bytes((STATIC/'index.html').read_bytes(),'text/html; charset=utf-8',compress=False);return
            if p.path=='/app.js':
                self.send_bytes((STATIC/'app.js').read_bytes(),'text/javascript; charset=utf-8',compress=False);return
            if p.path=='/style.css':
                self.send_bytes((STATIC/'style.css').read_bytes(),'text/css; charset=utf-8',compress=False);return
            if p.path=='/api/dxf/log':
                self.send_bytes(DXF_JOBS.log_text().encode('utf-8-sig'),
                                'text/plain; charset=utf-8',compress=False);return
            if p.path=='/api/dxf/frames':
                index=CATALOG.direct_index
                if not index:raise ValueError('DXF 읽기 후 도곽 판정 결과를 확인하세요.')
                self.respond((index.get('stats') or {}).get('frameDetection') or {});return
            if p.path=='/api/dxf/job':
                self.respond(DXF_JOBS.status(arg('id')));return
            if p.path=='/api/config':
                self.respond({'root':CATALOG.root,'sample':True,'direct':CATALOG.direct_title});return
            if p.path=='/api/catalog':
                requested=arg('root') or CATALOG.root or str(APP_ROOT)
                result=CATALOG.scan(requested)
                if arg('root'):
                    try:(ROOT/'root_path.txt').write_text(str(Path(requested).resolve()), encoding='utf-8')
                    except OSError:pass
                self.respond(result);return
            if p.path=='/api/recommended':
                self.respond(CATALOG.recommended());return
            if p.path=='/api/floors':
                self.respond(CATALOG.floors(arg('id')) if arg('id')!='demo' else {'name':'샘플','floors':[{'name':'DEMO','segments':DEMO['totalLines'],'texts':2}]});return
            if p.path in ('/api/scene','/api/geometry','/api/texts','/api/fills','/api/pick'):
                sc=DEMO if arg('id')=='demo' else CATALOG.prepare_scene(arg('id'),arg('floor'))
                if p.path=='/api/scene':self.respond(safe_data(sc));return
                if p.path=='/api/geometry':self.send_bytes(sc['bin'],'application/octet-stream');return
                if p.path=='/api/texts':self.respond(sc['texts']);return
                if p.path=='/api/fills':self.respond(sc['fills']);return
                if p.path=='/api/pick':
                    x=clean_num(arg('x'));y=clean_num(arg('y'))
                    hidden=set(arg('hidden').split('\x1f'));tol=max(5,min(1500,clean_num(arg('tol'),150)))
                    if sc.get('_attrs_path'):
                        found=pick_disk(sc,x,y,hidden,tol)
                    else:
                        found=pick(sc,x,y,{'hidden':hidden,'tol':tol})
                    self.respond({'selected':found});return
            self.respond({'error':'없는 주소'},404)
        except Exception as exc:
            self.respond({'error':f'{type(exc).__name__}: {exc}'},400)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=os.environ.get('DXF_WEB_CACHE_ROOT',''))
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    saved = ROOT/'root_path.txt'
    if args.root:
        root=args.root
    elif saved.is_file():
        root=saved.read_text(encoding='utf-8-sig').strip()
    else:
        existing=Path(r'D:\P\Estimate\DXF_견적프로그램 작성')
        root=str(existing) if existing.is_dir() else str(APP_ROOT)
    # Restore newest completed DXF cache without needing the source DXF again.
    recent=latest_index()
    if recent:
        CATALOG.set_direct_cache(recent)
        print(f'[웹 DXF 캐시] 복원: {recent["title"]} · {len(recent["floors"])}개 층/영역')
    try:
        result=CATALOG.scan(root)
        print(f"[캐시] {root} / {len(result['files'])}개 발견")
    except Exception as e: print('[캐시] 자동검색 실패:',e)
    srv=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    address=f'http://127.0.0.1:{srv.server_port}/'
    print('[실행]', address, '/ 외부 인터넷 연결 없음')
    print('종료: 이 창에서 Ctrl+C')
    if not args.no_browser:
        browser_timer = threading.Timer(0.5, launch_app_window, args=(address,))
        browser_timer.daemon = True
        browser_timer.start()
    try:srv.serve_forever()
    except KeyboardInterrupt:pass
    finally:srv.server_close()

if __name__=='__main__':main()
