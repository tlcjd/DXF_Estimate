# -*- coding: utf-8 -*-
"""Conservative DXF floor-plan frame discovery (no image AI / no CAD COM).

Never assign a floor to a rectangle based only on its shape/color.
A plan-title and floor token must be present; uncertain rectangles are reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from app.rooms.core import (_classify_drawing_title, _entity_text, _text_insert_xy,
                            entity_points)


def _roundrect(points):
    if len(points) < 4 or len(points) > 128:
        return None
    try:
        pts=[(float(p[0]),float(p[1])) for p in points]
        xs=[p[0] for p in pts];ys=[p[1] for p in pts]
        box=(min(xs),min(ys),max(xs),max(ys))
        w=box[2]-box[0];h=box[3]-box[1]
        if not all(map(math.isfinite,box)) or min(w,h)<=0 or not .25<w/h<12:
            return None
        # Reject typical floor outline / irregular boundary: every vertex must lie on
        # the bounding box sides; allow extra collinear points on rectangular edges.
        eps=max(w,h)*.001
        if any(min(abs(x-box[0]),abs(x-box[2]),abs(y-box[1]),abs(y-box[3]))>eps for x,y in pts):
            return None
        perimeter=sum(math.hypot(pts[(i+1)%len(pts)][0]-pts[i][0],
                                 pts[(i+1)%len(pts)][1]-pts[i][1]) for i in range(len(pts)))
        if perimeter > 2*(w+h)*1.02:
            return None
        return box
    except (ValueError,TypeError,IndexError,OverflowError):
        return None


def _rect_from_lines(horiz,vert):
    """Recognize rectangles made of four *complete* horizontal/vertical LINEs.

    Hash edges by endpoints to avoid quadratically checking every line pair.
    """
    scale=100.0
    def quant(x):return round(x*scale)
    horizontal=set(); up=defaultdict(set)
    for a,b,y in horiz:
        xa,xb=sorted((quant(a),quant(b)))
        if xa!=xb:horizontal.add((xa,xb,quant(y)))
    for x,a,b in vert:
        ya,yb=sorted((quant(a),quant(b)))
        if ya!=yb:
            up[(quant(x),ya)].add(yb)
            up[(quant(x),yb)].add(ya)
    found=set()
    for xa,xb,y in horizontal:
        left=up.get((xa,y),())
        right=up.get((xb,y),())
        for y2 in left:
            if y2==y or y2 not in right or (xa,xb,y2) not in horizontal:continue
            box=(xa/scale,min(y,y2)/scale,xb/scale,max(y,y2)/scale)
            if .25<(box[2]-box[0])/(box[3]-box[1])<12:
                found.add(box)
    return found


def _near_rect(x,y,box,margin=.07):
    w=box[2]-box[0];h=box[3]-box[1]
    return box[0]-w*margin<=x<=box[2]+w*margin and box[1]-h*margin<=y<=box[3]+h*margin


def _same_rect(a,b):
    w=max(1,a[2]-a[0],b[2]-b[0]);h=max(1,a[3]-a[1],b[3]-b[1])
    return max(abs(a[i]-b[i]) for i in (0,2)) < w*.008 and max(abs(a[i]-b[i]) for i in (1,3))<h*.008


def _bbox(points):
    xs=[x for x,y in points];ys=[y for x,y in points]
    return min(xs),min(ys),max(xs),max(ys)


def discover_plan_frames(msp, existing=None):
    """Return (merged divisions, concise diagnostic); preserve manual frames first.

    Candidate selection uses actual vector geometry and TEXT/MTEXT. Only rectangles
    matched to a floor-specific plan title become active floors; ambiguous floor
    names / title-less boxes are never assigned by guesswork.
    """
    existing=list(existing or [])
    taken=set()
    confirmed=[]
    for frame in existing:
        if frame.get('drawing_type')=='평면도' and frame.get('floors'):
            confirmed.extend(str(x) for x in frame['floors'])
    for f in confirmed:taken.add(f)
    existing_frames=[{'floors':list(frame.get('floors') or []),'title':str(frame.get('title') or ''),
                      'layer':'기존 도곽 지정','source':'기존 설정'}
                     for frame in existing if frame.get('drawing_type')=='평면도' and frame.get('floors')]
    rectangles=[]; titles=[]; horiz=[];vert=[]
    entity_count=0
    for e in msp:
        entity_count+=1
        try:
            if e.dxf.hasattr('invisible') and e.dxf.invisible:continue
            kind=e.dxftype()
            if kind in ('LWPOLYLINE','POLYLINE'):
                if not (getattr(e,'closed',False) or getattr(e,'is_closed',False)):continue
                box=_roundrect(entity_points(e))
                if box:rectangles.append((box,'폴리선',str(e.dxf.layer)))
            elif kind=='LINE':
                a=e.dxf.start;b=e.dxf.end
                ax,ay,bx,by=float(a.x),float(a.y),float(b.x),float(b.y)
                if abs(ay-by)<=.01 and abs(ax-bx)>1:horiz.append((ax,bx,(ay+by)/2))
                elif abs(ax-bx)<=.01 and abs(ay-by)>1:vert.append((ax,ay,by))
            elif kind in ('TEXT','MTEXT'):
                label=_entity_text(e).strip()
                if not label:continue
                dtype,floors=_classify_drawing_title(label)
                if dtype=='평면도' or floors:
                    x,y=_text_insert_xy(e)
                    height=float((e.dxf.get('height',0) if kind=='TEXT' else e.dxf.get('char_height',0)) or 1)
                    titles.append({'text':label,'type':dtype,'floors':floors,'x':x,'y':y,
                                   'height':abs(height),'layer':str(e.dxf.layer)})
        except (ValueError,TypeError,OverflowError,AttributeError):
            continue
    rectangles.extend((box,'LINE 4개','(여러 레이어)') for box in _rect_from_lines(horiz,vert))
    # Deduplicate both polyline and line versions of the same boundary.
    unique={}
    for box,source,layer in rectangles:
        sig=tuple(round(v,1) for v in box)
        if sig not in unique or (unique[sig][1]!='폴리선' and source=='폴리선'):
            unique[sig]=(box,source,layer)
    candidates=list(unique.values())
    dims=Counter((round(math.log(max(box[2]-box[0],1),1.09)),
                  round(math.log(max(box[3]-box[1],1),1.09))) for box,_,_ in candidates)
    plan_titles=[t for t in titles if t['type']=='평면도']
    floor_tokens=[t for t in titles if t['floors']]
    proposals=[]; uncertain=[]
    for t in plan_titles:
        floor_list=list(t['floors'])
        if not floor_list:
            # Adjacent title and floor token (often separate TEXT entities).
            neighbors=[]
            for f in floor_tokens:
                if f is t:continue
                dist=math.hypot(t['x']-f['x'],t['y']-f['y'])
                # Text labels must be near each other relative to text height.
                limit=max(12*max(t['height'],f['height']),200)
                if dist<=limit:neighbors.append((dist,f))
            if neighbors:
                neighbors.sort(key=lambda row:row[0]);floor_list=list(neighbors[0][1]['floors'])
        if not floor_list:
            uncertain.append({'title':t['text'],'reason':'층명 확인 불가'});continue
        matches=[]
        for box,source,layer in candidates:
            w=box[2]-box[0];h=box[3]-box[1]
            if not _near_rect(t['x'],t['y'],box):continue
            # A small box around just the sheet title isn't a floor-plan frame.
            if w*h < max(t['height'],1)**2 * 180:continue
            if min(w,h) < t['height']*9:continue
            inside=box[0]<=t['x']<=box[2] and box[1]<=t['y']<=box[3]
            popularity=dims[(round(math.log(max(w,1),1.09)),round(math.log(max(h,1),1.09)))]
            # Prefer a repeated frame family rather than one huge global outline;
            # within a family prefer the tighter boundary.
            matches.append((0 if inside else 1, 0 if popularity>=2 else 1,
                            w*h,box,source,layer,popularity))
        if not matches:
            uncertain.append({'title':t['text'],'floors':floor_list,'reason':'맞는 사각 도곽 없음'});continue
        matches.sort(key=lambda x:(x[0],x[1],x[2]))
        outside,_,area,box,source,layer,popularity=matches[0]
        # Nearby title outside the boundary requires a repeated frame family.
        if outside and popularity<2:
            uncertain.append({'title':t['text'],'floors':floor_list,'reason':'도곽 외부 제목/경계 불확실'});continue
        proposals.append({'title':t['text'],'floors':floor_list,'box':box,'source':source,
                          'layer':layer,'confidence':'높음' if not outside and popularity>=2 else '보통'})

    # Detect collision: two different rectangles with the same floor designation.
    by_floor=defaultdict(list)
    for p in proposals:
        for f in p['floors']:
            if f not in taken:by_floor[f].append(p)
    rejected=set()
    for floor,group in by_floor.items():
        if len(group)>1 and any(not _same_rect(group[0]['box'],x['box']) for x in group[1:]):
            uncertain.append({'title':' / '.join(x['title'] for x in group[:3]),
                              'floors':[floor],'reason':'동일 층에 서로 다른 도곽이 있어 확인 필요'})
            rejected.add(floor)
    added=[]
    for p in proposals:
        free=[f for f in p['floors'] if f not in taken and f not in rejected]
        if not free:continue
        if any(_same_rect(p['box'],_bbox(fr.get('points',[]))) for fr in existing if fr.get('points') and fr.get('drawing_type')=='평면도' and fr.get('floors')):
            # Existing explicitly labeled frame always takes precedence.
            continue
        b=p['box']
        frame={'points':[(b[0],b[1]),(b[2],b[1]),(b[2],b[3]),(b[0],b[3])],
               'area':(b[2]-b[0])*(b[3]-b[1]),'title':p['title'],'drawing_type':'평면도',
               'floors':free,'floor':free[0],'handle':'','auto_detected':True,
               'source':p['source'],'layer':p['layer'],'confidence':p['confidence']}
        existing.append(frame);taken.update(free)
        added.append({'floors':free,'title':p['title'],'source':p['source'],
                      'layer':p['layer'],'confidence':p['confidence'],
                      'rect':[round(v,2) for v in b]})
    report={'existing':len(confirmed),'autoAdded':len(added),'rectCandidates':len(candidates),
            'planTitles':len(plan_titles),'added':added,'existingFrames':existing_frames[:100],
            'unresolved':uncertain[:30], 'unresolvedCount':len(uncertain),
            'mode':'기존 도곽 + 제목 기반 자동인식' if added else '기존 도곽 또는 미판정'}
    return existing,report
