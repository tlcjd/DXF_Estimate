# -*- coding: utf-8 -*-
"""Read-only DXF web cache. Per-floor typed binary, JSON; no pickle in new caches."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
from collections import OrderedDict
from pathlib import Path

CACHE_VERSION = 5
LINE_STRUCT = struct.Struct('<4f4B')  # 20 bytes, compatible with V0.4 WebGL view
ATTR_STRUCT = struct.Struct('<QHH')   # 12 bytes: source handle, block table, entity table
CHUNK_LINES = 2048
CACHE_ROOT = Path(__file__).resolve().parent / 'data' / 'dxf_cache'


def signature_for_path(path: Path) -> str:
    """Fast path-local signature: path+mtime+size; upload uses exact content digest."""
    st = path.stat()
    payload = f'local:{path.resolve()}:{st.st_size}:{st.st_mtime_ns}:v{CACHE_VERSION}'
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def upload_signature(content_hash: str) -> str:
    return hashlib.sha256(f'upload:{content_hash}:v{CACHE_VERSION}'.encode('ascii')).hexdigest()


def floor_order(key):
    s = str(key).strip().upper()
    m = re.match(r'^B(\d+)F?$', s)
    if m: return (0, -int(m.group(1)), s)
    m = re.match(r'^(\d+)F?$', s)
    if m: return (1, int(m.group(1)), s)
    if s in ('RF', 'ROOF'): return (2, 0, s)
    if s.startswith('PH'): return (3, 0, s)
    return (4, 0, s)


def load_index(key: str):
    path = CACHE_ROOT / key / 'index.json'
    if not path.is_file(): return None
    try:
        meta = json.loads(path.read_text(encoding='utf-8'))
        if meta.get('cacheVersion') != CACHE_VERSION or meta.get('signature') != key:
            return None
        if not (path.parent / 'COMPLETED').exists(): return None
        for fl in meta.get('floors', []):
            d = path.parent / fl['directory']
            if not (d/'lines.bin').is_file() or not (d/'info.json').is_file(): return None
        meta['_folder'] = str(path.parent)
        return meta
    except (ValueError, OSError, KeyError, TypeError): return None


def latest_index():
    if not CACHE_ROOT.is_dir(): return None
    last=CACHE_ROOT/'last_opened.txt'
    if last.is_file():
        try:
            index=load_index(last.read_text(encoding='ascii').strip())
            if index:return index
        except (ValueError,OSError):pass
    paths = sorted(CACHE_ROOT.glob('*/index.json'), key=lambda p:p.stat().st_mtime_ns,reverse=True)
    for p in paths:
        index = load_index(p.parent.name)
        if index:return index
    return None


def _as_rgb(items):
    try:
        return tuple(max(0,min(255,int(items[i]))) for i in range(3))
    except (IndexError,ValueError,TypeError):
        return (168,180,192)


def _handle_num(s):
    if len(s)<9:return 0
    raw=str(s[8] or '').split('@')[0]
    try: return int(raw,16) if 0 < len(raw) <= 16 else 0
    except (ValueError,OverflowError): return 0


class LayerWriter:
    __slots__ = ('name','folder','gpath','apath','count','chunks','bounds','minx','miny','maxx','maxy','chunk_count')
    def __init__(self, name, folder, index):
        self.name=name; self.folder=folder
        self.gpath=folder/f'l{index:05d}.geo'
        self.apath=folder/f'l{index:05d}.attr'
        self.count=0; self.chunks=[]; self.bounds=None
        self.minx=self.miny=float('inf');self.maxx=self.maxy=float('-inf');self.chunk_count=0
    def push_bbox(self,x1,y1,x2,y2):
        self.minx=min(self.minx,x1,x2);self.miny=min(self.miny,y1,y2)
        self.maxx=max(self.maxx,x1,x2);self.maxy=max(self.maxy,y1,y2)
        self.chunk_count+=1
        if self.chunk_count==CHUNK_LINES:self.flush_bbox()
    def flush_bbox(self):
        if self.chunk_count:
            self.chunks.append([round(self.minx,3),round(self.miny,3),round(self.maxx,3),round(self.maxy,3)])
        self.minx=self.miny=float('inf');self.maxx=self.maxy=float('-inf');self.chunk_count=0


class FloorWriter:
    def __init__(self, root, name, rect, folder):
        self.name=name; self.rect=tuple(float(v) for v in rect);self.folder=folder
        folder.mkdir(parents=True,exist_ok=True)
        self.layers={};self.fds=OrderedDict();self.block_table=[''];self.entity_table=[''];self.block_ids={'':0};self.entity_ids={'':0}
        self.texts=[];self.fills=[];self.count=0
    def _fd(self,path):
        key=str(path)
        if key in self.fds:
            h=self.fds.pop(key);self.fds[key]=h;return h
        if len(self.fds)>=96:
            _,old=self.fds.popitem(last=False);old.close()
        h=open(path,'ab',buffering=256*1024);self.fds[key]=h;return h
    def _code(self,tab,ids,value):
        value=str(value or '')
        if value in ids:return ids[value]
        if len(tab)>=65535:return 0
        idx=len(tab);ids[value]=idx;tab.append(value);return idx
    def append(self, seg, clipped, flag):
        try:
            meta=seg[9] if len(seg)>9 and isinstance(seg[9],dict) else {}
            layer=str(meta.get('layer') or '레이어 미지정')
            lw=self.layers.get(layer)
            if lw is None:
                lw=LayerWriter(layer,self.folder,len(self.layers));self.layers[layer]=lw
            ox,oy=self.rect[:2]
            x1,y1,x2,y2=(float(clipped[0])-ox,float(clipped[1])-oy,float(clipped[2])-ox,float(clipped[3])-oy)
            if not all(math.isfinite(x) and abs(x)<3.4e38 for x in (x1,y1,x2,y2)):return
            rgb=_as_rgb(seg[4:7]) if len(seg)>=7 else (168,180,192)
            self._fd(lw.gpath).write(LINE_STRUCT.pack(x1,y1,x2,y2,*rgb,flag))
            self._fd(lw.apath).write(ATTR_STRUCT.pack(_handle_num(seg),
                self._code(self.block_table,self.block_ids,meta.get('block_name')),
                self._code(self.entity_table,self.entity_ids,meta.get('entity_type'))))
            lw.count+=1;self.count+=1;lw.push_bbox(x1,y1,x2,y2)
        except (OverflowError,TypeError,ValueError,struct.error):pass
    def finalize(self):
        for h in self.fds.values():h.close()
        self.fds.clear()
        def norm_item(item):
            return item
        offset=0;layers=[]
        with (self.folder/'lines.bin').open('wb') as out, (self.folder/'attrs.bin').open('wb') as ao:
            names=sorted(set(self.layers)|{t.get('layer','레이어 미지정') for t in self.texts}|{f.get('layer','레이어 미지정') for f in self.fills})
            for name in names:
                lw=self.layers.get(name)
                if lw:
                    lw.flush_bbox()
                    with lw.gpath.open('rb') as fh:shutil.copyfileobj(fh,out,1024*1024)
                    with lw.apath.open('rb') as fh:shutil.copyfileobj(fh,ao,1024*1024)
                    lw.gpath.unlink();lw.apath.unlink()
                    count=lw.count;chunks=lw.chunks
                else:count=0;chunks=[]
                layers.append({'name':name,'offset':offset,'count':count,
                               'texts':sum(1 for t in self.texts if t.get('layer')==name), 'chunks':chunks})
                offset+=count
        (self.folder/'texts.json').write_text(json.dumps(self.texts,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        (self.folder/'fills.json').write_text(json.dumps(self.fills,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        info={'name':self.name,'rect':[0,0,self.rect[2]-self.rect[0],self.rect[3]-self.rect[1]],
              'origin':list(self.rect[:2]),'layers':layers,'totalLines':offset,'totalTexts':len(self.texts),
              'totalFills':len(self.fills),'blockTable':self.block_table,'entityTable':self.entity_table,
              'chunkLines':CHUNK_LINES}
        (self.folder/'info.json').write_text(json.dumps(info,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        return {'name':self.name,'directory':self.folder.name,'segments':offset,'texts':len(self.texts),'fills':len(self.fills)}


def write_cache(key, title, path, frames, rects, segments, annotations, texts, fills, extra, progress=None):
    """Crop original geometry into files without duplicating lists per floor."""
    from app.rooms.background_source_cache import _clip_line, _filter_texts, _filter_fills
    tell=progress or (lambda step,msg:None)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    final=CACHE_ROOT/key
    existing=load_index(key)
    if existing:return existing
    working=CACHE_ROOT/(key+'.building.'+uuid.uuid4().hex)
    working.mkdir(parents=True,exist_ok=False)
    try:
        if not rects:
            # There is no trusted floor frame in this DXF. Split by geographic tiles so
            # the UI never automatically requests a multi-million-line '전체' view.
            xmin=ymin=float('inf');xmax=ymax=float('-inf')
            for coll in (segments,annotations):
                for seg in coll:
                    try:
                        ax,ay,bx,by=map(float,seg[:4])
                        if not all(map(math.isfinite,(ax,ay,bx,by))):continue
                        xmin=min(xmin,ax,bx);ymin=min(ymin,ay,by)
                        xmax=max(xmax,ax,bx);ymax=max(ymax,ay,by)
                    except (ValueError,TypeError,OverflowError):pass
            if not math.isfinite(xmin):raise ValueError('표시할 선분이 없습니다.')
            spanx=max(xmax-xmin,1);spany=max(ymax-ymin,1)
            n=max(1,math.ceil(math.sqrt((len(segments)+len(annotations))/160000)))
            rects={}
            if n==1:
                rects['전체']=(xmin-10,ymin-10,xmax+10,ymax+10)
            else:
                for iy in range(n):
                    for ix in range(n):
                        rects[f'영역 {iy+1:02d}-{ix+1:02d}']=(xmin+ix*spanx/n,ymin+iy*spany/n,
                                      xmin+(ix+1)*spanx/n,ymin+(iy+1)*spany/n)
            fallback=True
        else:fallback=False
        # The original floor frame rules are reused, only the persistence path is new.
        writers=[];writer_by_rect={};aliases=[]
        for name,rect in sorted(rects.items(),key=lambda p:floor_order(p[0])):
            rect_id=tuple(round(float(v),4) for v in rect)
            w=writer_by_rect.get(rect_id)
            if w is None:
                w=FloorWriter(working,name,rect,working/f'floor_{len(writers):03d}')
                writers.append(w)
                writer_by_rect[rect_id]=w
            aliases.append((name,w))
        # One geometric crop can represent several floors (e.g. 4~20F).
        # Store its geometry once and let the floor index share the same file.
        count=len(segments)+len(annotations);done=0
        for flag,coll in ((0,segments),(1,annotations)):
            for seg in coll:
                done+=1
                try:
                    x1,y1,x2,y2=map(float,seg[:4]);minx=min(x1,x2);miny=min(y1,y2);maxx=max(x1,x2);maxy=max(y1,y2)
                    for w in writers:
                        r=w.rect
                        if maxx<r[0] or minx>r[2] or maxy<r[1] or miny>r[3]:continue
                        clipped=_clip_line(x1,y1,x2,y2,r)
                        if clipped is not None:w.append(seg,clipped,flag)
                except (ValueError,TypeError,IndexError,OverflowError):pass
                if done%150000==0:
                    tell(81+min(13,int(13*done/max(1,count))),f'층별 디스크 캐시 생성 {done:,}/{count:,} 선분')
        summary_by_writer={}
        for i,w in enumerate(writers):
            # Text/fill clipping reuses original Qt pipeline geometric rules.
            for item in _filter_texts(texts,w.rect):
                try:
                    ox,oy=w.rect[:2]
                    w.texts.append({'x':float(item.get('x',0))-ox,'y':float(item.get('y',0))-oy,
                       'height':max(1,float(item.get('height') or 150)),'rotation':float(item.get('rotation') or 0),
                       'text':str(item.get('text') or '')[:1000], 'rgb':_as_rgb(item.get('rgb')),
                       'layer':str((item.get('meta') or {}).get('layer') or item.get('layer') or '레이어 미지정'),
                       'halign':item.get('halign',0),'valign':item.get('valign',0), 'attachment':item.get('attachment',0)})
                except (ValueError,TypeError):pass
            for item in _filter_fills(fills,w.rect):
                try:
                    ox,oy=w.rect[:2]
                    w.fills.append({'layer':str((item.get('meta') or {}).get('layer') or item.get('layer') or '레이어 미지정'),
                                    'rgb':_as_rgb(item.get('rgb')),
                                    'points':[[float(p[0])-ox,float(p[1])-oy] for p in (item.get('points') or [])[:2000]]})
                except (ValueError,TypeError):pass
            summary=w.finalize()
            if summary['segments'] or summary['texts'] or summary['fills']:
                summary_by_writer[w]=summary
            else:
                shutil.rmtree(w.folder,ignore_errors=True)
            tell(95+int(4*(i+1)/len(writers)),f'층별 저장 {i+1}/{len(writers)} · {w.name}')
        summaries=[]
        for name,w in aliases:
            if w in summary_by_writer:
                summaries.append(dict(summary_by_writer[w],name=name))
        if not summaries:raise ValueError('도곽/영역 내부에 표시할 벡터가 없습니다.')
        diag=extra.get('block_diagnostic') or {}
        stats={'title':title,'floors':[{'name':v['name'],'lines':v['segments'],'texts':v['texts'],'fills':v['fills']} for v in summaries],
               'frames':len(frames),'segments':len(segments),'texts':len(texts),
               'invisibleSkipped':diag.get('invisible_entity_skipped',0),
               'xclips':diag.get('xclip_insert_count',0),'unresolvedXrefs':[], 'emptyBlockRefs':[],
               'spatialFallback':fallback}
        meta={'cacheVersion':CACHE_VERSION,'signature':key,'title':title,'sourcePath':str(path),
              'created':time.time(),'floors':summaries,'stats':stats,'spatialFallback':fallback}
        (working/'index.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        (working/'COMPLETED').write_text('OK\n',encoding='ascii')
        if final.exists():shutil.rmtree(final)  # only a validated own cache folder, never user DXF
        working.rename(final)
        meta['_folder']=str(final)
        return meta
    except Exception:
        shutil.rmtree(working,ignore_errors=True)
        raise


def floor_scene(index, floor):
    item=next((f for f in index.get('floors',[]) if f['name']==floor),None)
    if not item:raise ValueError(f'없는 층/영역입니다: {floor}')
    base=Path(index['_folder'])/item['directory']
    info=json.loads((base/'info.json').read_text(encoding='utf-8'))
    geom=(base/'lines.bin').read_bytes()
    if len(geom)!=info['totalLines']*LINE_STRUCT.size:raise ValueError('층별 캐시 바이너리 크기가 맞지 않습니다.')
    return {'id':'direct','floor':floor,'rect':info['rect'],'origin':info['origin'],
            'layers':info['layers'],'totalLines':info['totalLines'],
            'totalTexts':info['totalTexts'],'totalFills':info['totalFills'],
            'bin':geom,'texts':json.loads((base/'texts.json').read_text(encoding='utf-8')),
            'fills':json.loads((base/'fills.json').read_text(encoding='utf-8')),
            '_attrs_path':base/'attrs.bin','_block_table':info['blockTable'],
            '_entity_table':info['entityTable'],'_chunk_lines':CHUNK_LINES,
            'raw':{}}


def pick_disk(scene,x,y,hidden,tol):
    """Use per-2048 segment bounding boxes before exact segment distance test."""
    geom=memoryview(scene['bin']);r=tol*tol;best=None;best_d2=r
    attrs=scene['_attrs_path'].open('rb')
    try:
        for lyr in scene['layers']:
            if lyr['name'] in hidden or not lyr['count']:continue
            offset=lyr['offset'];count=lyr['count']
            for ci,bb in enumerate(lyr['chunks']):
                if x<bb[0]-tol or x>bb[2]+tol or y<bb[1]-tol or y>bb[3]+tol:continue
                first=offset+ci*CHUNK_LINES
                for i in range(first,min(first+CHUNK_LINES,offset+count)):
                    a,b,c,d=struct.unpack_from('<4f',geom,i*LINE_STRUCT.size)
                    if x<min(a,c)-tol or x>max(a,c)+tol or y<min(b,d)-tol or y>max(b,d)+tol:continue
                    vx=c-a;vy=d-b;den=vx*vx+vy*vy
                    t=max(0,min(1,((x-a)*vx+(y-b)*vy)/den)) if den else 0
                    dx=x-(a+t*vx);dy=y-(b+t*vy);d2=dx*dx+dy*dy
                    if d2>=best_d2:continue
                    best_d2=d2
                    attrs.seek(i*ATTR_STRUCT.size)
                    handle,block,entity=ATTR_STRUCT.unpack(attrs.read(ATTR_STRUCT.size))
                    entityname=scene['_entity_table'][entity] if entity<len(scene['_entity_table']) else 'LINE'
                    best={'kind':entityname or 'LINE','layer':lyr['name'],
                          'handle':format(handle,'X') if handle else '',
                          'block':scene['_block_table'][block] if block<len(scene['_block_table']) else '',
                          'entity':entityname or 'LINE','x1':a,'y1':b,'x2':c,'y2':d,
                          'length':round(math.hypot(vx,vy),2),'distance':round(math.sqrt(d2),2)}
        return best
    finally:attrs.close()
