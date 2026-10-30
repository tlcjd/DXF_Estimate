from __future__ import annotations

import hashlib
import math
import pickle
import re
from functools import lru_cache
from pathlib import Path

SOURCE_CACHE_VERSION = 10


@lru_cache(maxsize=512)
def _nearest_aci_for_truecolor(value: int) -> int:
    try:
        value = int(value) & 0xFFFFFF
        from app.cad.colors import nearest_aci
        return int(nearest_aci(f"#{(value >> 16) & 255:02X}{(value >> 8) & 255:02X}{value & 255:02X}"))
    except Exception:
        return 7


def _cache_file(cache_root: Path, source_path: str) -> Path:
    resolved = str(Path(source_path).resolve())
    digest = hashlib.sha1(resolved.encode('utf-8')).hexdigest()[:12]
    stem = Path(source_path).stem or 'cad'
    root = Path(cache_root) / '_source_geometry'
    return root / f'{stem}_{digest}.pkl'


def cache_file_for_source(cache_root: Path, source_path: str) -> Path:
    """프로젝트 수동저장 등에서 현재 도면의 원본도형 캐시 위치를 조회한다."""
    return _cache_file(Path(cache_root), source_path)


def source_path_from_cache_file(cache_path: str | Path) -> str:
    """프로젝트에 백업된 원본도형 캐시가 어떤 원본 경로 키로 생성됐는지 반환한다.

    프로젝트를 다시 열 때 적용 DXF 경로와 CAD 배경 캐시 키(DWG)가 서로 달라도
    저장된 캐시 자체의 source_path를 우선 복원하기 위한 경량 조회다.
    """
    path = Path(cache_path)
    if not path.is_file():
        return ""
    try:
        with path.open('rb') as fp:
            payload = pickle.load(fp)
        if int(payload.get('version', 0)) != SOURCE_CACHE_VERSION:
            return ""
        return str(payload.get('source_path', '') or '').strip()
    except Exception:
        return ""


def _source_signature(path: str | Path) -> dict:
    try:
        p = Path(path)
        stat = p.stat()
        return {
            'path': str(p.resolve()),
            'size': int(stat.st_size),
            'mtime_ns': int(stat.st_mtime_ns),
        }
    except Exception:
        return {'path': str(path or ''), 'size': -1, 'mtime_ns': -1}


def _bbox(points):
    pts = [(float(x), float(y)) for x, y in (points or [])]
    if not pts:
        return None
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _rooms_bounds(rooms):
    pts = []
    for room in rooms or []:
        pts.extend([(float(x), float(y)) for x, y in (getattr(room, 'points', []) or [])])
    return _bbox(pts)


def _bbox_overlap(a, b) -> float:
    if not a or not b:
        return 0.0
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def floor_frame_rects(divisions, rooms=None) -> dict[str, tuple[float, float, float, float]]:
    """평면도 도곽을 층별 사각범위로 정리한다.

    같은 층 도곽이 여러 개면 해당 층 실 bbox와 가장 많이 겹치는 도곽을 우선한다.
    캐시는 도곽 외부 도형을 보관하지 않기 위한 범위로 사용한다.
    """
    floor_rooms = {}
    for room in rooms or []:
        floor = str(getattr(room, 'floor', '') or '').strip()
        if floor:
            floor_rooms.setdefault(floor, []).append(room)

    candidates: dict[str, list[tuple]] = {}
    all_frames = []
    for frame in divisions or []:
        if str(frame.get('drawing_type', '') or '').strip() != '평면도':
            continue
        rect = _bbox(frame.get('points') or [])
        if not rect:
            continue
        floors = [str(v or '').strip() for v in (frame.get('floors') or []) if str(v or '').strip()]
        one = str(frame.get('floor', '') or '').strip()
        if one and one not in floors:
            floors.append(one)
        all_frames.append((rect, frame))
        for floor in floors:
            candidates.setdefault(floor, []).append((rect, frame))

    result = {}
    known_floors = set(candidates) | set(floor_rooms)
    for floor in known_floors:
        pool = list(candidates.get(floor, []))
        room_box = _rooms_bounds(floor_rooms.get(floor, []))
        if not pool and room_box:
            # 층문자 판정이 빠진 도곽은 실 위치와 가장 많이 겹치는 평면도 도곽으로 보완한다.
            pool = list(all_frames)
        if not pool:
            continue
        if room_box:
            def score(item):
                rect, _frame = item
                overlap = _bbox_overlap(rect, room_box)
                cx = (room_box[0] + room_box[2]) / 2.0; cy = (room_box[1] + room_box[3]) / 2.0
                fx = (rect[0] + rect[2]) / 2.0; fy = (rect[1] + rect[3]) / 2.0
                return (-overlap, math.hypot(fx-cx, fy-cy), (rect[2]-rect[0])*(rect[3]-rect[1]))
            result[floor] = min(pool, key=score)[0]
        else:
            result[floor] = min(pool, key=lambda item: (item[0][2]-item[0][0])*(item[0][3]-item[0][1]))[0]
    return result




def plan_frame_rects(divisions) -> list[tuple[float, float, float, float]]:
    """층명/실 존재 여부와 무관하게 모든 평면도 도곽 범위를 반환한다.

    배경 원본 수집 범위는 실이 있는 층 목록이 아니라 실제 평면도 도곽 자체가 기준이다.
    동일 사각형은 1회만 반환한다.
    """
    result = []
    seen = set()
    for frame in divisions or []:
        if str(frame.get('drawing_type', '') or '').strip() != '평면도':
            continue
        rect = _bbox(frame.get('points') or [])
        if not rect:
            continue
        key = tuple(round(float(v), 4) for v in rect)
        if key in seen:
            continue
        seen.add(key)
        result.append(tuple(float(v) for v in rect))
    return result

def _clip_line(x1, y1, x2, y2, rect):
    xmin, ymin, xmax, ymax = [float(v) for v in rect]
    left, right, bottom, top = 1, 2, 4, 8
    def code(x, y):
        c = 0
        if x < xmin: c |= left
        elif x > xmax: c |= right
        if y < ymin: c |= bottom
        elif y > ymax: c |= top
        return c
    c1 = code(x1, y1); c2 = code(x2, y2)
    while True:
        if not (c1 | c2):
            return float(x1), float(y1), float(x2), float(y2)
        if c1 & c2:
            return None
        out = c1 or c2
        if out & top:
            if abs(y2-y1) < 1e-12: return None
            x = x1 + (x2-x1)*(ymax-y1)/(y2-y1); y = ymax
        elif out & bottom:
            if abs(y2-y1) < 1e-12: return None
            x = x1 + (x2-x1)*(ymin-y1)/(y2-y1); y = ymin
        elif out & right:
            if abs(x2-x1) < 1e-12: return None
            y = y1 + (y2-y1)*(xmax-x1)/(x2-x1); x = xmax
        else:
            if abs(x2-x1) < 1e-12: return None
            y = y1 + (y2-y1)*(xmin-x1)/(x2-x1); x = xmin
        if out == c1:
            x1, y1 = x, y; c1 = code(x1, y1)
        else:
            x2, y2 = x, y; c2 = code(x2, y2)


def _filter_segments(segments, rect):
    out = []
    for seg in segments or []:
        try:
            clipped = _clip_line(float(seg[0]), float(seg[1]), float(seg[2]), float(seg[3]), rect)
            if clipped is None:
                continue
            out.append((*clipped, *tuple(seg[4:])))
        except Exception:
            continue
    return out


def _filter_texts(texts, rect):
    xmin, ymin, xmax, ymax = rect
    out = []
    for text in texts or []:
        try:
            x = float(text.get('x', 0)); y = float(text.get('y', 0))
            if xmin <= x <= xmax and ymin <= y <= ymax:
                out.append(dict(text))
        except Exception:
            continue
    return out



def _filter_fills(fills, rect):
    """SOLID/HATCH 채움도 층 도곽에 실제로 잘라 저장한다.

    이전에는 bbox가 조금만 겹쳐도 원본 폴리곤 전체를 층 캐시에 넣어 XREF/XCLIP
    밖의 거대한 회색 면이 화면을 덮을 수 있었다.
    """
    xmin, ymin, xmax, ymax = [float(v) for v in rect]
    out = []
    try:
        from ezdxf.math import Vec2
        from ezdxf.math.clipping import ClippingRect2d
        clipper = ClippingRect2d(Vec2(xmin, ymin), Vec2(xmax, ymax))
    except Exception:
        clipper = None
    for raw in fills or []:
        try:
            pts = [(float(x), float(y)) for x, y in (raw.get('points') or [])]
            if len(pts) < 3:
                continue
            xs = [x for x, _ in pts]; ys = [y for _, y in pts]
            if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
                continue
            polygons = []
            if clipper is not None:
                try:
                    polygons = [
                        [(float(v.x), float(v.y)) for v in poly]
                        for poly in clipper.clip_polygon([Vec2(x, y) for x, y in pts])
                        if len(poly) >= 3
                    ]
                except Exception:
                    polygons = []
            if not polygons:
                # 클리핑 라이브러리가 실패했을 때도 도곽을 크게 벗어나는 채움 전체를
                # 그대로 표시하지 않는다. 완전히 내부인 폴리곤만 안전하게 유지한다.
                if all(xmin <= x <= xmax and ymin <= y <= ymax for x, y in pts):
                    polygons = [pts]
            for poly in polygons:
                item = dict(raw)
                item['points'] = poly
                out.append(item)
        except Exception:
            continue
    return out

def build_floor_vectors(segments, divisions=None, texts=None, annotation_segments=None, rooms=None, opening_candidates=None, fills=None):
    rects = floor_frame_rects(divisions, rooms)
    floors = {}
    geometry_by_rect = {}
    for floor, rect in rects.items():
        rect_key = tuple(round(float(v), 4) for v in rect)
        base = geometry_by_rect.get(rect_key)
        if base is None:
            base = {
                'crop_rect': tuple(float(v) for v in rect),
                'crop_source': 'drawing_frame',
                'segments': _filter_segments(segments, rect),
                'texts': _filter_texts(texts, rect),
                'annotation_segments': _filter_segments(annotation_segments, rect),
                'fills': _filter_fills(fills, rect),
            }
            geometry_by_rect[rect_key] = base
        # 4~20F처럼 같은 도곽을 공유하는 층은 geometry 리스트 객체를 공유해 캐시 중복을 줄인다.
        floors[str(floor)] = {
            **base,
            'opening_candidates': [dict(v) for v in (opening_candidates or {}).get(str(floor), [])],
        }
    return floors


def save_background_source_cache(cache_root: Path, source_path: str, segments, divisions=None, texts=None,
                                 annotation_segments=None, rooms=None, opening_candidates=None,
                                 signature_source_path: str | None = None, fills=None) -> Path:
    path = _cache_file(Path(cache_root), source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    signature_path = signature_source_path or source_path
    payload = {
        'version': SOURCE_CACHE_VERSION,
        'cache_stage': 2,
        'source_path': str(Path(source_path).resolve()),
        'signature': _source_signature(signature_path),
        # 호환/진단용 전체 목록도 이미 평면 도곽 합집합으로 잘린 데이터만 저장한다.
        'segments': list(segments or []),
        'divisions': list(divisions or []),
        'texts': list(texts or []),
        'annotation_segments': list(annotation_segments or []),
        'fills': list(fills or []),
        'floors': build_floor_vectors(
            segments, divisions, texts, annotation_segments, rooms, opening_candidates, fills
        ),
    }
    tmp = path.with_suffix('.tmp')
    with tmp.open('wb') as fp:
        pickle.dump(payload, fp, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def load_background_source_cache(cache_root: Path, source_path: str, signature_source_path: str | None = None):
    path = _cache_file(Path(cache_root), source_path)
    if not path.exists():
        return None
    try:
        with path.open('rb') as fp:
            payload = pickle.load(fp)
        if int(payload.get('version', 0)) != SOURCE_CACHE_VERSION:
            return None
        if str(payload.get('source_path', '')) != str(Path(source_path).resolve()):
            return None
        if signature_source_path:
            old = dict(payload.get('signature') or {})
            new = _source_signature(signature_source_path)
            if (
                str(old.get('path', '')).casefold() != str(new.get('path', '')).casefold()
                or int(old.get('size', -1)) != int(new.get('size', -2))
                or int(old.get('mtime_ns', -1)) != int(new.get('mtime_ns', -2))
            ):
                return None
        return {
            'cache_stage': int(payload.get('cache_stage', 2) or 2),
            'segments': list(payload.get('segments') or []),
            'divisions': list(payload.get('divisions') or []),
            'texts': list(payload.get('texts') or []),
            'annotation_segments': list(payload.get('annotation_segments') or []),
            'fills': list(payload.get('fills') or []),
            'floors': dict(payload.get('floors') or {}),
            'signature': dict(payload.get('signature') or {}),
        }
    except Exception:
        return None


def clear_background_source_cache(cache_root: Path, source_path: str) -> None:
    path = _cache_file(Path(cache_root), source_path)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _entity_aci(doc, entity) -> int:
    try:
        true_color = int(getattr(entity.dxf, 'true_color', 0) or 0)
    except Exception:
        true_color = 0
    if true_color:
        # TrueColor 도면도 CAD 연동 판정과 비슷하게 최근접 ACI로 보조 판정한다.
        return _nearest_aci_for_truecolor(true_color)
    try:
        aci = abs(int(getattr(entity.dxf, 'color', 256) or 256))
    except Exception:
        aci = 256
    if aci in (0, 256):
        try:
            layer = doc.layers.get(str(getattr(entity.dxf, 'layer', '0') or '0'))
            aci = abs(int(getattr(layer.dxf, 'color', getattr(layer, 'color', 7)) or 7))
        except Exception:
            aci = 7
    return max(1, min(255, aci if aci not in (0, 256) else 7))


def _entity_bounds(entity):
    try:
        from ezdxf import bbox
        ext = bbox.extents([entity], fast=True)
        if ext.has_data:
            return float(ext.extmin.x), float(ext.extmin.y), float(ext.extmax.x), float(ext.extmax.y)
    except Exception:
        pass
    try:
        kind = entity.dxftype()
        if kind == 'LINE':
            a = entity.dxf.start; b = entity.dxf.end
            return min(a.x,b.x), min(a.y,b.y), max(a.x,b.x), max(a.y,b.y)
        if kind == 'ARC':
            c = entity.dxf.center; r = float(entity.dxf.radius)
            return c.x-r, c.y-r, c.x+r, c.y+r
        if kind in ('LWPOLYLINE','POLYLINE'):
            pts = []
            if kind == 'LWPOLYLINE':
                pts = [(float(p[0]), float(p[1])) for p in entity.get_points('xy')]
            else:
                pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
            return _bbox(pts)
    except Exception:
        pass
    return None


def _rect_intersects(a, b) -> bool:
    return bool(a and b and not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3]))


def detect_opening_candidates_dxf(doc, rect, window_acis=(4, 5), entities_override=None) -> dict:
    """베이스 DXF에서 평면 도곽 안 창호 후보를 검출한다.

    연결 CAD의 후보판정과 같은 목적의 캐시용 경량 판정이다. 도곽 내부 INSERT/ARC 및
    ACI 4/5 선만 분석하고 결과 후보 자체를 벡터 캐시에 저장한다.
    """
    scan_rect = tuple(float(v) for v in rect)
    wanted = {max(1, min(255, int(v))) for v in (window_acis or (4,5))}
    entities = []
    source_entities = entities_override if entities_override is not None else doc.modelspace()
    for entry in source_entities:
        try:
            if entities_override is not None and isinstance(entry, tuple) and len(entry) == 2:
                entity, bounds = entry
            else:
                entity = entry
                bounds = _entity_bounds(entity)
            kind = entity.dxftype()
            if kind not in {'INSERT','ARC','LINE','LWPOLYLINE','POLYLINE'}:
                continue
            if not _rect_intersects(bounds, scan_rect):
                continue
            if kind in {'LINE','LWPOLYLINE','POLYLINE'} and _entity_aci(doc, entity) not in wanted:
                continue
            entities.append((entity, bounds))
        except Exception:
            continue

    def center(bounds):
        return ((bounds[0]+bounds[2])/2.0, (bounds[1]+bounds[3])/2.0) if bounds else None
    def size(bounds):
        return (abs(bounds[2]-bounds[0]), abs(bounds[3]-bounds[1])) if bounds else (0.0,0.0)
    def record(kind, xy, confidence, source, entity=None, bounds=None, aci=None, block_name='', length=0.0, component_layers=None):
        if not xy: return None
        x,y = float(xy[0]),float(xy[1])
        if not (scan_rect[0] <= x <= scan_rect[2] and scan_rect[1] <= y <= scan_rect[3]): return None
        if not length and bounds:
            w,h=size(bounds); length=max(w,h)
        return {'kind':kind,'x':x,'y':y,'confidence':int(confidence),'source':source,
                'aci':aci,'block_name':str(block_name or ''),'handle':str(getattr(getattr(entity,'dxf',None),'handle','') or ''),
                'bounds':tuple(float(v) for v in bounds) if bounds else None,'detected_length_mm':float(length or 0.0),
                'component_layers':sorted({str(v) for v in (component_layers or []) if str(v).strip()}, key=str.casefold)}

    def block_component_layers(block_name, insert_layer="", _stack=None):
        """블록 정의 내부 엔티티의 실제 구성 레이어를 재귀적으로 모은다.

        layer 0은 해당 INSERT의 레이어를 상속한 것으로 처리한다.
        중첩 INSERT는 자식 INSERT의 레이어를 상속 기준으로 다시 내려간다.
        """
        result=set()
        name=str(block_name or '').strip()
        if not name:
            return result
        stack=set(_stack or set())
        key=name.casefold()
        if key in stack:
            return result
        stack.add(key)
        try:
            block=doc.blocks.get(name)
        except Exception:
            return result
        try:
            children=list(block)
        except Exception:
            children=[]
        for child in children:
            try:
                own_layer=str(getattr(child.dxf, 'layer', '') or '0').strip() or '0'
            except Exception:
                own_layer='0'
            effective_layer=str(insert_layer or '').strip() if own_layer == '0' and str(insert_layer or '').strip() else own_layer
            if effective_layer and effective_layer != '0':
                result.add(effective_layer)
            try:
                if child.dxftype() == 'INSERT':
                    child_name=str(getattr(child.dxf, 'name', '') or '').strip()
                    child_insert_layer=effective_layer if effective_layer and effective_layer != '0' else str(insert_layer or '').strip()
                    result.update(block_component_layers(child_name, child_insert_layer, stack))
            except Exception:
                pass
        return result

    candidates=[]; color_entities=[]; free_arcs=[]
    door_pat = re.compile(r'(DOOR|출입문|(?:^|[^A-Z])(SD|FD|DD|PD|D)[-_]?\d)', re.I)
    win_pat = re.compile(r'(WINDOW|창호|창문|(?:^|[^A-Z])(AW|CW|PW|SW|W)[-_]?\d)', re.I)

    # V24.37 창호 강제판정:
    # 1) 블록 정의 내부(중첩 블록 포함)에 약 90도/180도 ARC가 하나라도 있으면 그 블록은 창호다.
    # 2) 그 창호 블록 내부에서 실제로 사용되는 구성 레이어를 전부 수집한다.
    # 3) 수집된 창호 레이어를 하나라도 사용하는 다른 블록도 창호로 편입하고,
    #    그 블록의 구성 레이어도 다시 창호 레이어에 추가한다. 이 과정을 변화가 없을 때까지 반복한다.
    # 결과적으로 "창호 블록 내부에서 사용된 레이어"는 최적화 전체에서 창호 그룹으로 강제 분류된다.
    arc_opening_blocks=set()
    arc_opening_layers=set()
    block_layers={}
    block_has_opening_arc={}

    def scan_block_definition(block_name, inherited_layer='', stack=None):
        name=str(block_name or '').strip()
        if not name:
            return set(), False
        seen=set(stack or set())
        key=name.casefold()
        if key in seen:
            return set(), False
        seen.add(key)
        try:
            block=doc.blocks.get(name)
            children=list(block)
        except Exception:
            return set(), False
        layers=set()
        has_arc=False
        for child in children:
            try:
                own_layer=str(getattr(child.dxf, 'layer', '') or '0').strip() or '0'
            except Exception:
                own_layer='0'
            effective_layer=(str(inherited_layer or '').strip() if own_layer=='0' and str(inherited_layer or '').strip() else own_layer)
            if effective_layer and effective_layer!='0':
                layers.add(effective_layer.casefold())
            try:
                ctype=child.dxftype()
            except Exception:
                ctype=''
            if ctype=='ARC':
                try:
                    sweep=(float(child.dxf.end_angle)-float(child.dxf.start_angle))%360.0
                    if abs(sweep-90.0)<=18.0 or abs(sweep-180.0)<=18.0:
                        has_arc=True
                except Exception:
                    pass
            elif ctype=='INSERT':
                try:
                    child_name=str(getattr(child.dxf, 'name', '') or '').strip()
                except Exception:
                    child_name=''
                child_layers, child_has_arc = scan_block_definition(child_name, effective_layer if effective_layer!='0' else inherited_layer, seen)
                layers.update(child_layers)
                has_arc = has_arc or child_has_arc
        return layers, has_arc

    try:
        for block in doc.blocks:
            try:
                block_name=str(getattr(block, 'name', '') or '')
            except Exception:
                block_name=''
            if not block_name or block_name.startswith('*'):
                continue
            layers, has_arc = scan_block_definition(block_name)
            key=block_name.casefold()
            block_layers[key]=set(layers)
            block_has_opening_arc[key]=bool(has_arc)
            if has_arc:
                arc_opening_blocks.add(key)
                arc_opening_layers.update(layers)

        # 창호 레이어를 공유하는 블록까지 재귀적으로 확장한다.
        changed=True
        while changed:
            changed=False
            for block_name, layers in block_layers.items():
                if block_name in arc_opening_blocks:
                    continue
                if layers.intersection(arc_opening_layers):
                    arc_opening_blocks.add(block_name)
                    before=len(arc_opening_layers)
                    arc_opening_layers.update(layers)
                    changed = True or (len(arc_opening_layers)!=before)
    except Exception:
        pass
    for entity,bounds in entities:
        kind=entity.dxftype(); aci=_entity_aci(doc,entity)
        if kind=='INSERT':
            try: name=str(getattr(entity.dxf,'name','') or '')
            except Exception: name=''
            w,h=size(bounds); longest=max(w,h); shortest=min(w,h)
            if not (80 <= longest <= 30000): continue
            # ARC 90/180 직접포함 또는 해당 ARC 레이어 공유 블록은 이름/색상과 무관하게 창호로 확정한다.
            forced_opening = bool(name and name.casefold() in arc_opening_blocks)
            dscore=10 if forced_opening else (5 if door_pat.search(name.upper()) else 0)
            wscore=5 if win_pat.search(name.upper()) else 0
            if aci in wanted: wscore += 3
            if longest >= 120 and shortest > 0 and longest/shortest >= 1.8 and shortest <= 1500: wscore += 2
            # 블록 정의 내부 90도/180도 ARC와 창호색 도형을 보조판정한다.
            try:
                block=doc.blocks.get(name); door_arcs=0; colored=0
                for child in block:
                    if _entity_aci(doc, child) in wanted: colored += 1
                    if child.dxftype()=='ARC':
                        sweep=(float(child.dxf.end_angle)-float(child.dxf.start_angle))%360.0
                        if abs(sweep-90.0)<=18.0 or abs(sweep-180.0)<=18.0:
                            door_arcs += 1
                if door_arcs: dscore += 5
                if colored >= 2: wscore += 5
                elif colored == 1: wscore += 3
            except Exception: pass
            chosen=None
            component_layers=block_component_layers(name, str(getattr(entity.dxf, 'layer', '') or '').strip())
            if dscore>=5 and dscore>=wscore: chosen=record('door',center(bounds),dscore,'dxf-block',entity,bounds,aci,name,component_layers=component_layers)
            elif wscore>=4: chosen=record('window',center(bounds),wscore,'dxf-block',entity,bounds,aci,name,component_layers=component_layers)
            if chosen: candidates.append(chosen)
        elif kind=='ARC':
            try:
                sweep=(float(entity.dxf.end_angle)-float(entity.dxf.start_angle))%360.0; radius=float(entity.dxf.radius)
                if (abs(sweep-90.0)<=15.0 or abs(sweep-180.0)<=15.0) and 300.0<=radius<=2500.0:
                    c=entity.dxf.center
                    arc_source = 'dxf-arc180' if abs(sweep-180.0)<=15.0 else 'dxf-arc90'
                    arc_layer=str(getattr(entity.dxf, 'layer', '') or '').strip()
                    free_arcs.append(record('door',(c.x,c.y),4,arc_source,entity,bounds,aci,length=radius,
                                            component_layers=[arc_layer] if arc_layer else []))
            except Exception: pass
            if aci in wanted and bounds: color_entities.append((entity,bounds,aci))
        elif aci in wanted and bounds:
            color_entities.append((entity,bounds,aci))

    def gap(a,b):
        dx=max(a[0]-b[2],b[0]-a[2],0.0); dy=max(a[1]-b[3],b[1]-a[3],0.0)
        return math.hypot(dx,dy)
    remaining=set(range(len(color_entities)))
    while remaining:
        seed=remaining.pop(); group={seed}; queue=[seed]
        while queue:
            idx=queue.pop(); b=color_entities[idx][1]
            near=[j for j in list(remaining) if gap(b,color_entities[j][1])<=250.0]
            for j in near: remaining.remove(j); group.add(j); queue.append(j)
        if len(group)<2: continue
        boxes=[color_entities[j][1] for j in group]
        cb=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
        w,h=size(cb); longest=max(w,h)
        if 80<=longest<=20000:
            candidates.append(record('window',center(cb),3,'dxf-color-cluster',None,cb,color_entities[next(iter(group))][2]))

    for cand in [v for v in free_arcs if v]:
        if not any(v.get('kind')=='door' and math.hypot(cand['x']-v['x'],cand['y']-v['y'])<=1800 for v in candidates):
            candidates.append(cand)

    ordered=sorted([v for v in candidates if v],key=lambda v:-int(v.get('confidence',0)))
    final=[]
    for cand in ordered:
        if any(cand['kind']==v['kind'] and math.hypot(cand['x']-v['x'],cand['y']-v['y'])<=300 for v in final): continue
        final.append(cand)
    final.sort(key=lambda v:(-float(v.get('y',0)),float(v.get('x',0)),v.get('kind','')))
    dno=wno=0
    known=re.compile(r'(?:AW|CW|PW|SW|W|AD|SD|FD|DD|PD|D)[-_]?\d{1,4}',re.I)
    for cand in final:
        block=str(cand.get('block_name','') or '').strip(); is_known=bool(known.fullmatch(block.replace(' ','')))
        cand['auto_label']=not is_known
        if cand['kind']=='door': dno+=1; cand['label']=block if is_known else f'D?{dno:02d}'
        else: wno+=1; cand['label']=block if is_known else f'W?{wno:02d}'
    return {'rect':scan_rect,'scanned':len(entities),'candidates':final,'door_count':dno,'window_count':wno}


def build_opening_candidates_by_floor(doc, divisions, rooms=None, window_acis=(4,5)) -> dict[str, list[dict]]:
    """ModelSpace는 한 번만 순회하고 각 평면 도곽 후보목록으로 분배한다."""
    rects = floor_frame_rects(divisions, rooms)
    by_floor = {str(floor): [] for floor in rects}
    if not rects:
        return by_floor
    for entity in doc.modelspace():
        try:
            kind = entity.dxftype()
            if kind not in {'INSERT','ARC','LINE','LWPOLYLINE','POLYLINE'}:
                continue
            bounds = _entity_bounds(entity)
            if not bounds:
                continue
            # 일반선은 창호색이 아니면 애초에 후보 캐시에 넣지 않는다.
            if kind in {'LINE','LWPOLYLINE','POLYLINE'} and _entity_aci(doc, entity) not in {int(v) for v in window_acis}:
                continue
            for floor, rect in rects.items():
                if _rect_intersects(bounds, rect):
                    by_floor[str(floor)].append((entity, bounds))
        except Exception:
            continue
    result = {}
    for floor, rect in rects.items():
        try:
            result[str(floor)] = list(detect_opening_candidates_dxf(
                doc, rect, window_acis, entities_override=by_floor.get(str(floor), [])
            ).get('candidates', []) or [])
        except Exception:
            result[str(floor)] = []
    return result
