# -*- coding: utf-8 -*-
"""Conservative DXF floor-plan frame discovery (no image AI / no CAD COM).

Never assign a floor to a rectangle based only on its shape/color.
A plan-title and floor token must be present; uncertain rectangles are reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
from statistics import median

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


def _plan_label(text, dtype, floors):
    """Only actual drawing titles; notes, window plans and key plans are excluded."""
    clean=re.sub(r"\s+", "", str(text or "")).upper()
    if any(k in clean for k in ('창호평면도','WINDOWPLAN','KEYPLAN','평면도상세','평면도상세치수','평면도참조')):
        return False, []
    if dtype != '평면도':
        return False, []
    if clean == 'PLAN':  # a separately placed floor label may follow
        return True, list(floors)
    # Floor/roof near the *start* of a true title; not annotations mentioning a plan.
    if not re.match(r'^(?:지상|지하|B\d|\d|기준층|옥탑|지붕|옥상|RF|PH|PIT)',clean):
        return False, []
    if '평면도' not in clean and 'PLAN' not in clean:
        return False, []
    if '옥탑지붕' in clean:
        return True, ['PHR']
    if clean.startswith(('지붕','옥상')) and '평면도' in clean and not floors:
        return True, ['RF']
    return True, list(floors)


def _floor_label(text):
    """Keep only short, standalone floor tokens for split TEXT title matching."""
    clean=re.sub(r'\s+', '', text).upper()
    return bool(re.fullmatch(r'(?:B\d+F?|\d+F|\d+층|지하\d+층|PHR?|RF|PIT)',clean))


def discover_plan_frames(msp, existing=None, preferred=None):
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
                plan_label, plan_floors=_plan_label(label,dtype,floors)
                # Short standalone floor tokens may accompany a separate 'PLAN' text.
                if plan_label or (floors and _floor_label(label)):
                    x,y=_text_insert_xy(e)
                    height=float((e.dxf.get('height',0) if kind=='TEXT' else e.dxf.get('char_height',0)) or 1)
                    titles.append({'text':label,'type':dtype,'floors':floors,'x':x,'y':y,
                                   'height':abs(height),'layer':str(e.dxf.layer),
                                   'isPlanTitle':plan_label,'planFloors':plan_floors})
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
    plan_titles=[t for t in titles if t['isPlanTitle']]
    floor_tokens=[t for t in titles if t['floors'] and not t['isPlanTitle']]
    # Physical title grids: title positions are spaced uniformly across many CAD plans.
    # This permits a *reviewable* inferred region when the actual border is missing.
    rows=[]
    for t in sorted(plan_titles,key=lambda x:(x['y'],x['x'])):
        found=next((r for r in rows if abs(r[0]['y']-t['y'])<=max(t['height']*2.0,180.)),None)
        if found is None:rows.append([t])
        else:found.append(t)
    grid={}
    for r in rows:
        r.sort(key=lambda t:t['x'])
        distances=[r[i+1]['x']-r[i]['x'] for i in range(len(r)-1)]
        spacing=[v for v in distances if v>1000 and v<1_000_000]
        if len(r)>=3 and len(spacing)>=2:
            pitch=median(spacing)
            if sum(abs(v-pitch)<pitch*.08 for v in spacing)>=2:
                for t in r:grid[id(t)]=pitch
    
    # Estimate sheet size from the user's existing real frame family if present.
    # This is used ONLY for a title grid whose nearest outlines are smaller
    # than the pitch (usually floor/building footprints, not sheet borders).
    template=[]
    for fr in existing:
        if fr.get('drawing_type')!='평면도' or not fr.get('points'):continue
        b=_bbox(fr['points']);w=b[2]-b[0];h=b[3]-b[1]
        if min(w,h)<1000:continue
        for t in plan_titles:
            if not set(t['planFloors']) & set(fr.get('floors') or []):continue
            if not _near_rect(t['x'],t['y'],b,.10):continue
            fx=(t['x']-b[0])/w;fy=(t['y']-b[1])/h
            if .55<=fx<=1.15 and -.15<=fy<=.35:
                template.append((w,h,fx,fy));break
    sheet_template=(tuple(median(row[i] for row in template) for i in range(4))
                    if len(template)>=2 else None)
    proposals=[]; uncertain=[]
    for t in plan_titles:
        floor_list=list(t['planFloors'])
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
            # Prioritize sheet-sized/repeated outlines, avoiding a small building
            # footprint rectangle nested inside the floor drawing.
            pitch=grid.get(id(t))
            if pitch and (w < .49*pitch or w > 1.08*pitch):continue
            location_x=(t['x']-box[0])/w
            location_y=(t['y']-box[1])/h
            title_corner= .57 <= location_x <= 1.13 and -.16 <= location_y <= .30
            matches.append((0 if title_corner else 1, 0 if popularity>=2 else 1,
                            -w*h,box,source,layer,popularity))
        if not matches:
            pitch=grid.get(id(t))
            if pitch and t['layer'].casefold() in ('title','a-title','도면문자','sheet'):
                # The title lies near lower-right on the local title grid.
                # No real border -> infer a deliberately *review-required* crop.
                width=pitch*.86;height=width*.78
                box=(t['x']-width*.84,t['y']-height*.075,
                     t['x']+width*.16,t['y']+height*.925)
                proposals.append({'title':t['text'],'floors':floor_list,'box':box,
                                  'source':'반복 제목 배치(가상 도곽)','layer':t['layer'],
                                  'confidence':'추정·확인필요','virtual':True})
            else:
                uncertain.append({'title':t['text'],'floors':floor_list,'reason':'맞는 사각 도곽 없음'})
            continue
        matches.sort(key=lambda x:(x[0],x[1],x[2]))
        outside,_,neg_area,box,source,layer,popularity=matches[0]
        # Typical plan sheet titles sit near the lower-right corner. When the
        # only found rectangle is much smaller than the repeated title pitch,
        # it is likely the *building outline* inside the sheet. Make an outer
        # virtual candidate and flag it for user verification instead of
        # cutting away the edge of the drawing without warning.
        pitch=grid.get(id(t))
        w=box[2]-box[0];h=box[3]-box[1]
        fx=(t['x']-box[0])/w;fy=(t['y']-box[1])/h
        if (pitch and w<pitch*.78 and .65<=fx<=1.16 and -.17<=fy<=.30
                and t['layer'].casefold() in ('title','a-title','sheet','도면문자')):
            if sheet_template:
                tw,th,tx,ty=sheet_template
                if not (.70*pitch<=tw<=1.04*pitch):tw=pitch*.86
                if not (.55<=tx<=1.10 and -.1<=ty<=.3):tx,ty=.84,.068
            else:
                tw=pitch*.86;th=tw*.78;tx,ty=.84,.068
            box=(t['x']-tw*tx,t['y']-th*ty,
                 t['x']+tw*(1-tx),t['y']+th*(1-ty))
            proposals.append({'title':t['text'],'floors':floor_list,'box':box,
                              'source':'반복 제목 배치(외곽 추정)','layer':t['layer'],
                              'confidence':'추정·확인필요','virtual':True})
            continue
        # Nearby title outside the boundary requires a repeated frame family.
        if outside and popularity<2:
            uncertain.append({'title':t['text'],'floors':floor_list,'reason':'도곽 외부 제목/경계 불확실'});continue
        proposals.append({'title':t['text'],'floors':floor_list,'box':box,'source':source,
                          'layer':layer,'confidence':'높음' if not outside and popularity>=2 else '보통'})

    # One title can be duplicated on revisions/alternative plan layouts.
    # Previously every repeated floor was rejected, even when *no* manual frame
    # existed. Keep one previewable default per floor, and surface the other
    # choices with coordinates instead of merging their geometry or hiding them.
    by_floor=defaultdict(list)
    for p in proposals:
        for f in p['floors']:
            by_floor[f].append(p)
    alternatives=[];added=[]
    def choice_rank(p):
        box=p['box'];w=box[2]-box[0];h=box[3]-box[1]
        return (int(p.get('virtual',False)),
                0 if p['confidence']=='높음' else 1,
                0 if p['layer'].casefold() in ('sheet','title','frame','도곽') else 1,
                -(w*h))
    selected={}
    for f,group in by_floor.items():
        unique=[]
        for p in sorted(group,key=choice_rank):
            if not any(_same_rect(p['box'],x['box']) for x in unique):unique.append(p)
        if f in taken:
            for p in unique:
                if not any(_same_rect(p['box'],_bbox(fr['points'])) for fr in existing
                           if fr.get('points') and fr.get('drawing_type')=='평면도'
                           and f in (fr.get('floors') or [])):
                    alternatives.append({'floors':[f],'title':p['title'],'rect':[round(v,2) for v in p['box']],
                                         'source':p['source'],'reason':'기존 지정과 다른 위치의 동일 층 도면'})
            continue
        if unique:
            selected[f]=unique[0]
            for p in unique[1:]:
                alternatives.append({'floors':[f],'title':p['title'],'rect':[round(v,2) for v in p['box']],
                                     'source':p['source'],'reason':'같은 층의 다른 위치 도면(기본 선택 제외)'})
    # Reuse the same candidate rectangle for range floors (4~20F): one cache file.
    chosen={}
    for f,p in selected.items():
        chosen.setdefault(id(p),(p,[]))[1].append(f)
    for p,free in chosen.values():
        if any(_same_rect(p['box'],_bbox(fr.get('points',[]))) for fr in existing
               if fr.get('points') and fr.get('drawing_type')=='평면도' and fr.get('floors')):
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
    # A user-approved alternative is stronger than either automated or legacy
    # assignment. It changes only our generated frame data, never the source DXF.
    manually_applied=[]
    for f,rawbox in (preferred or {}).items():
        try:
            b=tuple(float(v) for v in rawbox)
            if len(b)!=4 or not all(map(math.isfinite,b)) or b[2]<=b[0] or b[3]<=b[1]:continue
            # A manually chosen rectangle must still be one of the discovered
            # drawing candidates. A stale preference is ignored safely.
            pool=[p for p in proposals if f in p['floors'] and _same_rect(p['box'],b)]
            if not pool:continue
            for fr in existing[:]:
                floors=list(fr.get('floors') or [])
                if f not in floors:continue
                floors.remove(f)
                if floors:fr['floors']=floors;fr['floor']=floors[0]
                else:existing.remove(fr)
            src=pool[0]
            existing.append({'points':[(b[0],b[1]),(b[2],b[1]),(b[2],b[3]),(b[0],b[3])],
                'area':(b[2]-b[0])*(b[3]-b[1]),'title':src['title'],
                'drawing_type':'평면도','floors':[f],'floor':f,'handle':'',
                'auto_detected':False,'source':'사용자 선택','confidence':'사용자 확정'})
            manually_applied.append({'floors':[f],'title':src['title'],
                                    'rect':[round(v,2) for v in b], 'source':'사용자 확정'})
        except (ValueError,TypeError,OverflowError):continue
    report={'existing':len(confirmed),'autoAdded':len(added),'rectCandidates':len(candidates),
            'planTitles':len(plan_titles),'added':added,'existingFrames':existing_frames[:100],
            'unresolved':uncertain[:80], 'unresolvedCount':len(uncertain),
            'alternatives':alternatives[:100], 'alternativeCount':len(alternatives),
            'manualSelections':manually_applied,
            'mode':'미지정 포함 제목/도곽 자동인식' if added else '기존 도곽 또는 미판정'}
    return existing,report
