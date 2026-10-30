from __future__ import annotations

import math
import re
import hashlib
import fnmatch
from collections import defaultdict
from pathlib import Path

import ezdxf
from ezdxf.path import make_path
from ezdxf.colors import aci2rgb

from app.common.models import MISSING_ROOM_NAME, RoomRecord, make_room_program_id
from app.common.layer_defaults import default_layer_name
from app.material_text.core import clean_mtext, get_insert_xy
from app.cad.colors import nearest_aci, normalize_aci
import traceback

ROOM_BOUNDARY_LAYER = default_layer_name("boundary_layer")
ROOM_TAG_LAYER = default_layer_name("tag_layer")
ROOM_BLOCK_NAME = "DXF_ROOM_TAG"
ROOM_FLOOR_TAG = "FLOOR"
ROOM_NAME_TAG = "ROOM_NAME"
ROOM_NUMBER_TAGS = ("NUMBER", "ROOM_NO")
WALL_FINISH_LAYER = default_layer_name("wall_finish_layer")
DRAWING_DIVISION_LAYER = default_layer_name("drawing_division_layer")
ROOM_DEDUCTION_LAYER = default_layer_name("deduction_layer")
STRUCTURE_SLAB_LAYER = default_layer_name("structure_slab_layer")
STRUCTURE_WALL_LAYER = default_layer_name("structure_wall_layer")
EXTERIOR_WALL_LAYER = default_layer_name("exterior_wall_layer")
COORD_TOLERANCE = 0.5  # mm


def _q(value: float, tolerance: float = COORD_TOLERANCE) -> int:
    return round(float(value) / tolerance)


def segment_key(x1, y1, x2, y2, tolerance: float = COORD_TOLERANCE):
    a = (_q(x1, tolerance), _q(y1, tolerance)); b = (_q(x2, tolerance), _q(y2, tolerance))
    return (a, b) if a <= b else (b, a)


def clean_points(pts, tolerance: float = COORD_TOLERANCE):
    cleaned=[]
    for p in pts:
        p=(float(p[0]),float(p[1]))
        if not cleaned or segment_key(*cleaned[-1], *p, tolerance)[0] != segment_key(*cleaned[-1], *p, tolerance)[1]:
            cleaned.append(p)
    if len(cleaned)>1 and _q(cleaned[0][0],tolerance)==_q(cleaned[-1][0],tolerance) and _q(cleaned[0][1],tolerance)==_q(cleaned[-1][1],tolerance):
        cleaned.pop()
    return cleaned


def polygon_key(pts, tolerance: float = COORD_TOLERANCE):
    qpts=[(_q(x,tolerance),_q(y,tolerance)) for x,y in pts]
    if not qpts: return ()
    variants=[]
    for seq in (qpts,list(reversed(qpts))):
        for i in range(len(seq)): variants.append(tuple(seq[i:]+seq[:i]))
    return min(variants)


def duplicate_edge_count(pts, closed=True, tolerance: float = COORD_TOLERANCE):
    seen=set(); dup=0; limit=len(pts) if closed else len(pts)-1
    for i in range(max(0,limit)):
        k=segment_key(*pts[i],*pts[(i+1)%len(pts)],tolerance)
        if k in seen: dup+=1
        else: seen.add(k)
    return dup


def point_in_polygon(x,y,pts):
    inside=False; n=len(pts)
    if n<3:return False
    j=n-1
    for i in range(n):
        xi,yi=pts[i]; xj,yj=pts[j]
        if ((yi>y)!=(yj>y)) and x < (xj-xi)*(y-yi)/((yj-yi) or 1e-30)+xi: inside=not inside
        j=i
    return inside


def polygon_area(pts):
    if len(pts)<3:return 0.0
    return abs(sum(pts[i][0]*pts[(i+1)%len(pts)][1]-pts[(i+1)%len(pts)][0]*pts[i][1] for i in range(len(pts))))/2


def polygon_perimeter(pts, closed=True):
    if len(pts)<2:return 0.0
    limit=len(pts) if closed else len(pts)-1
    return sum(math.hypot(pts[(i+1)%len(pts)][0]-pts[i][0],pts[(i+1)%len(pts)][1]-pts[i][1]) for i in range(limit))


def entity_points(entity):
    try:
        path=make_path(entity); pts=[(float(v.x),float(v.y)) for v in path.flattening(distance=5.0)]
    except Exception:
        if entity.dxftype()=="LWPOLYLINE": pts=[(float(x),float(y)) for x,y,*_ in entity.get_points()]
        elif entity.dxftype()=="POLYLINE": pts=[(float(v.dxf.location.x),float(v.dxf.location.y)) for v in entity.vertices]
        else: pts=[]
    return clean_points(pts)


def _rule(rules, key, default):
    value = (rules or {}).get(key, default)
    return str(value).strip() or default


def room_rules(rules=None):
    number_tags = _rule(rules, "room_number_tags", ",".join(ROOM_NUMBER_TAGS))
    return {
        "boundary_layer": _rule(rules, "boundary_layer", ROOM_BOUNDARY_LAYER),
        "tag_layer": _rule(rules, "tag_layer", ROOM_TAG_LAYER),
        "block_name": _rule(rules, "block_name", ROOM_BLOCK_NAME),
        "floor_tag": _rule(rules, "floor_tag", ROOM_FLOOR_TAG).upper(),
        "room_name_tag": _rule(rules, "room_name_tag", ROOM_NAME_TAG).upper(),
        "room_number_tags": tuple(t.strip().upper() for t in number_tags.split(",") if t.strip()) or ROOM_NUMBER_TAGS,
        "wall_finish_layer": _rule(rules, "wall_finish_layer", WALL_FINISH_LAYER),
        "drawing_division_layer": _rule(rules, "drawing_division_layer", DRAWING_DIVISION_LAYER),
        "deduction_layer": _rule(rules, "deduction_layer", ROOM_DEDUCTION_LAYER),
        "structure_slab_layer": _rule(rules, "structure_slab_layer", STRUCTURE_SLAB_LAYER),
        "structure_wall_layer": _rule(rules, "structure_wall_layer", STRUCTURE_WALL_LAYER),
        "exterior_wall_layer": _rule(rules, "exterior_wall_layer", EXTERIOR_WALL_LAYER),
    }



def _entity_text(entity):
    try:
        if entity.dxftype() == "TEXT":
            return clean_mtext(str(entity.dxf.text or ""))
        if entity.dxftype() == "MTEXT":
            return clean_mtext(str(entity.text or ""))
    except Exception:
        return ""
    return ""


def _text_insert_xy(entity):
    try:
        p = entity.dxf.insert
        return float(p.x), float(p.y)
    except Exception:
        return 0.0, 0.0


def _normalize_floor_token(prefix: str, number: str, suffix: str = "") -> str:
    n = str(int(number))
    p = str(prefix or "").upper()
    if p in ("B", "지하"):
        return f"B{n}F"
    return f"{n}F"


def _expand_floor_range(text: str) -> list[str]:
    """2층~4층, 2~4층, 2F~4F, 2~4F 같은 범위를 층 목록으로 풀어낸다."""
    source = re.sub(r"\s+", "", str(text or "")).upper()
    range_patterns = (
        r"(?<![A-Z0-9])(B|지하)?(\d+)(?:층|F)?[~～\-](B|지하)?(\d+)(?:층|F)(?![A-Z0-9])",
        r"(?<![A-Z0-9])(B|지하)?(\d+)(?:층|F)[~～\-](B|지하)?(\d+)(?:층|F)?(?![A-Z0-9])",
    )
    for pattern in range_patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if not match:
            continue
        p1, n1, p2, n2 = match.groups()
        p1 = (p1 or "").upper()
        p2 = (p2 or p1).upper()
        if p1 != p2:
            return []
        a, b = int(n1), int(n2)
        step = 1 if b >= a else -1
        return [_normalize_floor_token(p1, str(n)) for n in range(a, b + step, step)]
    return []


def _classify_drawing_title(text: str):
    compact = re.sub(r"\s+", "", str(text or "")).upper()
    if not compact:
        return "미구분", []
    drawing_type = "미구분"
    if "재료마감표" in compact or "마감재료표" in compact or "FINISHSCHEDULE" in compact or "MATERIALFINISH" in compact:
        drawing_type = "재료마감표"
    elif "창호평면도" in compact or "창호도" in compact or "WINDOWPLAN" in compact or "WINDOWSCHEDULE" in compact:
        drawing_type = "창호도"
    elif "평면도" in compact or "FLOORPLAN" in compact or "PLAN" in compact:
        drawing_type = "평면도"
    elif any(key in compact for key in ("입면도", "정면도", "좌측면도", "우측면도", "배면도", "ELEVATION")):
        drawing_type = "입면도"
    elif "단면도" in compact or "SECTION" in compact:
        drawing_type = "단면도"
    elif "개요" in compact or "SUMMARY" in compact:
        drawing_type = "개요"

    floors = _expand_floor_range(text)
    if floors:
        return drawing_type, floors

    source = str(text or "").upper()
    single_patterns = (
        (r"(?<![A-Z0-9])B\s*(\d+)\s*F?(?![A-Z0-9])", "B"),
        (r"지하\s*(\d+)\s*층", "B"),
        (r"(?<![A-Z0-9])(\d+)\s*F(?![A-Z0-9])", ""),
        (r"(?<!\d)(\d+)\s*층(?!\d)", ""),
    )
    for pattern, prefix in single_patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            return drawing_type, [_normalize_floor_token(prefix, match.group(1))]
    special = re.search(r"(?<![A-Z0-9])(PITF?|RF|PHR|PH|옥탑층|옥상층|옥상|지붕층)(?![A-Z0-9])", source, re.IGNORECASE)
    if special:
        token = re.sub(r"\s+", "", special.group(1).upper())
        if token.startswith("PIT"):
            token = "PIT"
        elif token in {"PH", "PHR", "옥탑층"}:
            token = "PHR"
        elif token in {"RF", "옥상층", "옥상", "지붕층"}:
            token = "RF"
        return drawing_type, [token]
    return drawing_type, []


def normalize_drawing_floor_input(text: str) -> str:
    """사용자가 입력한 층 표기를 도서명용 표준 토큰으로 바꾼다.

    예: 5층/5F -> 5F, 지하2층 -> B2F, 5~10층 -> 5~10F, 옥상층 -> RF.
    """
    raw = re.sub(r"\s+", "", str(text or "")).upper()
    if not raw:
        return ""
    if raw in {"PIT", "PITF", "PIT층"}:
        return "PIT"
    if raw in {"RF", "옥상", "옥상층", "지붕", "지붕층"}:
        return "RF"
    if raw in {"PH", "PHR", "PH층", "PHR층", "옥탑", "옥탑층"}:
        return "PHR"

    # 범위: 5~10층 / 5F~10F / 지하3~1층 / B3F~B1F
    range_match = re.fullmatch(r"(B|지하)?(\d+)(?:층|F)?[~～\-](B|지하)?(\d+)(?:층|F)?", raw, re.IGNORECASE)
    if range_match:
        p1, n1, p2, n2 = range_match.groups()
        p1 = "B" if (p1 or "").upper() in {"B", "지하"} else ""
        p2 = "B" if (p2 or p1 or "").upper() in {"B", "지하"} else ""
        if p1 != p2:
            return ""
        return f"{p1}{int(n1)}~{int(n2)}F"

    single = re.fullmatch(r"(B|지하)?(\d+)(?:층|F)?", raw, re.IGNORECASE)
    if single:
        prefix, number = single.groups()
        prefix = "B" if (prefix or "").upper() in {"B", "지하"} else ""
        return f"{prefix}{int(number)}F"
    return ""


def drawing_input_from_title(drawing_type: str, title: str, floors=None) -> str:
    """기존 도곽 제목을 목록의 직접 입력값으로 역변환한다."""
    dtype = str(drawing_type or "")
    text = str(title or "")
    compact = re.sub(r"\s+", "", text).upper()
    if dtype in {"평면도", "창호도"}:
        # 범위 표기는 확장된 floors 대신 원문 범위를 우선 살린다.
        match = re.search(r"(B|지하)?(\d+)(?:층|F)?[~～\-](B|지하)?(\d+)(?:층|F)?", compact, re.IGNORECASE)
        if match:
            normalized = normalize_drawing_floor_input(match.group(0))
            if normalized:
                return normalized
        special = re.search(r"(PITF?|PHR|PH|RF|옥탑층|옥상층|옥상|지붕층)", compact, re.IGNORECASE)
        if special:
            normalized = normalize_drawing_floor_input(special.group(0))
            if normalized:
                return normalized
        single = re.search(r"(B|지하)?\s*\d+\s*(?:층|F)", text, re.IGNORECASE)
        if single:
            normalized = normalize_drawing_floor_input(single.group(0))
            if normalized:
                return normalized
        if floors:
            values = [str(x or "") for x in floors if str(x or "")]
            if len(values) == 1:
                return normalize_drawing_floor_input(values[0]) or values[0]
        return ""
    if dtype == "입면도":
        if "정면도" in compact or compact in {"정", "정면"}: return "정"
        if "좌측면도" in compact or compact in {"좌", "좌측"}: return "좌"
        if "우측면도" in compact or compact in {"우", "우측"}: return "우"
        if "배면도" in compact or compact in {"배", "배면"}: return "배"
        return ""
    if dtype == "단면도":
        match = re.search(r"단면도[-_ ]*(\d+)", text, re.IGNORECASE)
        return match.group(1) if match else ""
    return ""


def drawing_title_from_input(drawing_type: str, user_input: str) -> str:
    """도서구분 + 직접입력을 프로그램 표준 도곽명으로 변환한다."""
    dtype = str(drawing_type or "").strip()
    value = str(user_input or "").strip()
    if not dtype:
        return ""
    if dtype == "개요":
        return "개요"
    if dtype in {"평면도", "창호도"}:
        floor = normalize_drawing_floor_input(value)
        if not floor:
            return ""
        suffix = "평면도" if dtype == "평면도" else "창호평면도"
        return f"{floor} {suffix}"
    if dtype == "입면도":
        compact = re.sub(r"\s+", "", value)
        mapping = {"정": "정면도", "정면": "정면도", "정면도": "정면도",
                   "좌": "좌측면도", "좌측": "좌측면도", "좌측면도": "좌측면도",
                   "우": "우측면도", "우측": "우측면도", "우측면도": "우측면도",
                   "배": "배면도", "배면": "배면도", "배면도": "배면도"}
        return mapping.get(compact, "")
    if dtype == "단면도":
        match = re.search(r"\d+", value)
        return f"단면도-{int(match.group(0))}" if match else ""
    return ""


def next_drawing_floor(previous: str, last_numeric: str = "") -> tuple[str, str]:
    """앞의 평면/창호 층 표기에서 다음 공란을 추정하고 마지막 숫자층도 함께 반환한다."""
    prev = normalize_drawing_floor_input(previous) or str(previous or "").strip().upper()
    last = normalize_drawing_floor_input(last_numeric) or str(last_numeric or "").strip().upper()
    if not prev:
        return "", last
    if prev == "PIT":
        if last:
            return next_drawing_floor(last, last)
        return "", last
    if prev == "RF":
        return "PHR", last
    if prev == "PHR":
        return "", last
    range_match = re.fullmatch(r"(B)?(\d+)~(\d+)F", prev)
    if range_match:
        prefix, _a, b = range_match.groups(); n = int(b)
        nxt = f"B{max(1, n-1)}F" if prefix and n > 1 else ("1F" if prefix else f"{n+1}F")
        return nxt, prev
    single = re.fullmatch(r"(B)?(\d+)F", prev)
    if single:
        prefix, number = single.groups(); n = int(number)
        if prefix:
            nxt = f"B{n-1}F" if n > 1 else "1F"
        else:
            nxt = f"{n+1}F"
        return nxt, prev
    return "", last


def read_drawing_divisions(msp, rules=None):
    """도면구분 레이어의 닫힌 틀과 틀 내부 제목문자를 읽는다."""
    r = room_rules(rules)
    layer = r["drawing_division_layer"].casefold()
    frames = []
    texts = []
    for entity in msp:
        kind = entity.dxftype()
        entity_layer = str(getattr(entity.dxf, "layer", "")).casefold()
        # 도곽선과 도곽 제목문자는 모두 도면구분 레이어만 읽는다.
        # 다른 레이어의 실명/치수/주석 문자는 도서구분 판정에 사용하지 않는다.
        if kind in ("LWPOLYLINE", "POLYLINE"):
            if entity_layer != layer:
                continue
            points = entity_points(entity)
            closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
            if closed and len(points) >= 3 and polygon_area(points) > 0:
                frames.append({"points": points, "area": polygon_area(points), "handle": str(entity.dxf.handle or "")})
        elif kind in ("TEXT", "MTEXT"):
            if entity_layer != layer:
                continue
            text = _entity_text(entity)
            x, y = _text_insert_xy(entity)
            if text:
                texts.append({"text": text, "x": x, "y": y, "layer": str(getattr(entity.dxf, "layer", "") or "")})
    for frame in frames:
        inside = [item for item in texts if point_in_polygon(item["x"], item["y"], frame["points"])]
        inside.sort(key=lambda item: (-item["y"], item["x"]))

        # 도곽 안의 모든 실명/치수문자를 제목으로 합치면 B1/B2 같은 층 토큰을
        # 엉뚱한 실명에서 잡을 수 있다. 먼저 '평면도/PLAN/입면도/단면도...' 키워드가
        # 들어간 실제 제목문자를 찾고, 층이 분리 문자라면 그 제목에 가장 가까운 층문자를 붙인다.
        keyword_items = []
        for item in inside:
            item_type, item_floors = _classify_drawing_title(item["text"])
            if item_type != "미구분":
                keyword_items.append((item, item_type, item_floors))

        if keyword_items:
            title_item, drawing_type, floors = keyword_items[0]
            title_parts = [title_item["text"]]
            if not floors:
                candidates = []
                for item in inside:
                    if item is title_item:
                        continue
                    _kind, item_floors = _classify_drawing_title(item["text"])
                    if not item_floors:
                        continue
                    distance = math.hypot(item["x"] - title_item["x"], item["y"] - title_item["y"])
                    candidates.append((distance, item, item_floors))
                if candidates:
                    candidates.sort(key=lambda row: row[0])
                    _distance, floor_item, floors = candidates[0]
                    title_parts.insert(0, floor_item["text"])
            title = " / ".join(title_parts)
        else:
            # 기존 도면처럼 제목이 여러 조각으로 구성되어 키워드 단일문자를 못 찾는 경우의 호환 fallback.
            title = " / ".join(item["text"] for item in inside)
            drawing_type, floors = _classify_drawing_title(title)

        frame.update({"title": title, "drawing_type": drawing_type, "floors": floors, "floor": floors[0] if floors else ""})
    return frames


def classify_room_by_divisions(x: float, y: float, divisions):
    candidates = [frame for frame in divisions if point_in_polygon(x, y, frame["points"])]
    if not candidates:
        return None
    return min(candidates, key=lambda frame: frame["area"])



def read_room_deductions(msp, rules=None):
    """실공제선 레이어의 닫힌 폴리선을 읽는다.

    공제 면적은 실 면적에서 빼고, 공제 둘레는 내부 벽마감 길이에 더한다.
    """
    r = room_rules(rules)
    target = r["deduction_layer"].casefold()
    items = []
    errors = []
    seen = {}
    for entity in msp:
        if str(getattr(entity.dxf, "layer", "")).casefold() != target:
            continue
        handle = str(getattr(entity.dxf, "handle", "") or "")
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            errors.append(f"실공제선 레이어의 지원하지 않는 객체: {entity.dxftype()} / 핸들 {handle}")
            continue
        points = entity_points(entity)
        closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
        if not closed or len(points) < 3:
            errors.append(f"열린 실공제선 폴리선(제외): 핸들 {handle}")
            continue
        area = polygon_area(points)
        if area <= COORD_TOLERANCE * COORD_TOLERANCE:
            errors.append(f"면적 0 또는 지나치게 작은 공제 폴리선(제외): 핸들 {handle}")
            continue
        key = polygon_key(points)
        if key in seen:
            errors.append(f"완전히 중복된 공제 폴리선(후행 객체 제외): 핸들 {handle} / 기준 {seen[key]}")
            continue
        seen[key] = handle
        items.append({
            "points": points, "area": area,
            "perimeter": polygon_perimeter(points, True), "handle": handle,
        })
    return items, errors


def apply_room_deductions(boundaries, deductions, errors):
    """각 실 내부에 완전히 포함된 공제 폴리선을 면적·둘레에 반영한다."""
    for boundary in boundaries:
        matched = []
        for item in deductions:
            # 모든 꼭짓점이 실 내부인 경우만 자동 반영한다. 경계 교차는 검토 대상으로 남긴다.
            inside = [point_in_polygon(x, y, boundary["points"]) for x, y in item["points"]]
            if all(inside):
                matched.append(item)
            elif any(inside):
                errors.append(
                    f"실공제선이 실 경계에 걸쳐 자동 공제하지 않음: 공제 {item['handle']} / 실 {boundary['entity'].dxf.handle}"
                )
        deduction_area = sum(item["area"] for item in matched)
        deduction_perimeter = sum(item["perimeter"] for item in matched)
        boundary["deduction_area"] = deduction_area
        boundary["deduction_perimeter"] = deduction_perimeter
        boundary["deduction_count"] = len(matched)
        boundary["deductions"] = matched
        boundary["net_area"] = max(0.0, boundary["area"] - deduction_area)
        boundary["finish_perimeter"] = boundary["perimeter"] + deduction_perimeter

def read_room_tags(msp, rules=None):
    r = room_rules(rules)
    tags=[]
    for e in msp.query("INSERT"):
        if str(e.dxf.layer).casefold()!=r["tag_layer"].casefold() or str(e.dxf.name).casefold()!=r["block_name"].casefold(): continue
        attrs={str(a.dxf.tag).upper():clean_mtext(a.dxf.text) for a in getattr(e,"attribs",[])}
        number=""; number_tag=r["room_number_tags"][0]
        for tag in r["room_number_tags"]:
            if tag in attrs: number=attrs[tag].strip(); number_tag=tag; break
        x,y=get_insert_xy(e)
        tags.append({"floor":attrs.get(r["floor_tag"],"").strip(),"room_name":attrs.get(r["room_name_tag"],"").strip(),
                     "room_number":number,"number_tag":number_tag,"x":x,"y":y,"handle":str(e.dxf.handle or ""),"layer":str(e.dxf.layer)})
    return tags


def _division_floors(division) -> list[str]:
    if not division or division.get("drawing_type") != "평면도":
        return []
    floors = division.get("floors") or []
    if floors:
        return list(floors)
    floor = str(division.get("floor") or "").strip()
    return [floor] if floor else []



def _floor_scope_label(floors: list[str]) -> str:
    """대표층 1개만 보관하되 도곽의 실제 적용층 범위를 표시하기 위한 라벨."""
    clean = [str(v).strip() for v in (floors or []) if str(v).strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    # 같은 계열의 연속층이면 4F~20F처럼 간단히 표시한다.
    def parse_floor(value):
        m = re.fullmatch(r"(B)?(\d+)F", value.upper())
        if not m:
            return None
        return (m.group(1) or "", int(m.group(2)))
    parsed = [parse_floor(v) for v in clean]
    if all(v is not None for v in parsed):
        prefixes = {v[0] for v in parsed}
        nums = [v[1] for v in parsed]
        if len(prefixes) == 1 and nums == list(range(nums[0], nums[-1] + (1 if nums[-1] >= nums[0] else -1), 1 if nums[-1] >= nums[0] else -1)):
            return f"{clean[0]}~{clean[-1]}"
    return ",".join(clean)

def _virtual_handle(handle: str, floor: str, base_floor: str) -> str:
    if not handle or floor == base_floor:
        return handle
    return f"{handle}@{floor}"


def read_rooms(doc, rules=None, divisions_override=None):
    r = room_rules(rules)
    msp=doc.modelspace(); boundaries=[]; errors=[]; polygon_handles={}
    for e in msp:
        if str(getattr(e.dxf,"layer","")).casefold()!=r["boundary_layer"].casefold(): continue
        handle=str(e.dxf.handle or "")
        if e.dxftype() not in ("LWPOLYLINE","POLYLINE"):
            errors.append(f"실구분선 레이어의 지원하지 않는 객체: {e.dxftype()} / 핸들 {handle}"); continue
        pts=entity_points(e); closed=bool(getattr(e,"closed",False) or getattr(e,"is_closed",False))
        if len(pts)<3: errors.append(f"꼭짓점 부족: 핸들 {handle}"); continue
        if not closed:
            errors.append(f"열린 폴리선(실 산출 제외): 핸들 {handle}"); continue
        area=polygon_area(pts)
        if area<=COORD_TOLERANCE*COORD_TOLERANCE:
            errors.append(f"면적 0 또는 지나치게 작은 실구분선(제외): 핸들 {handle}"); continue
        dup_edges=duplicate_edge_count(pts,True)
        if dup_edges:
            errors.append(f"동일 선분이 {dup_edges}회 중복된 실구분선(제외): 핸들 {handle}"); continue
        pkey=polygon_key(pts)
        if pkey in polygon_handles:
            errors.append(f"완전히 중복된 실구분선(후행 객체 제외): 핸들 {handle} / 기준 {polygon_handles[pkey]}"); continue
        polygon_handles[pkey]=handle
        boundaries.append({"entity":e,"points":pts,"area":area,"perimeter":polygon_perimeter(pts,True),"closed":True})

    deductions, deduction_errors = read_room_deductions(msp, rules)
    errors.extend(deduction_errors)
    apply_room_deductions(boundaries, deductions, errors)
    # CAD 직접 연동에서는 원본 CAD에서 이미 판정한 도곽 정보를 우선 사용한다.
    # 임시 ezdxf 문서로 옮긴 뒤 제목문자 레이어/복사순서 차이로 B1/B2 판정이 달라지는 것을 막는다.
    divisions = list(divisions_override or []) if divisions_override is not None else read_drawing_divisions(msp, rules)
    tags=read_room_tags(msp,rules); rooms=[]; assigned={}
    for tag in tags:
        candidates=[]
        for idx,b in enumerate(boundaries):
            if point_in_polygon(tag["x"],tag["y"],b["points"]): candidates.append((b["area"],idx,b))
        if not candidates:
            errors.append(f"실명 블록이 어느 실구분선에도 포함되지 않음: {tag['floor']} / {tag['room_name']} / {tag['room_number']} / 핸들 {tag['handle']}"); continue
        _,idx,b=min(candidates,key=lambda x:x[0])
        if idx in assigned:
            errors.append(f"하나의 실구분선에 실명 블록이 2개 이상 있음(후행 블록 제외): 경계 {b['entity'].dxf.handle} / 블록 {assigned[idx]}, {tag['handle']}"); continue
        assigned[idx]=tag['handle']; e=b['entity']
        division = classify_room_by_divisions(tag['x'], tag['y'], divisions) if divisions else None
        if divisions:
            floors = _division_floors(division) or ['(미구분)']
        else:
            floors = [tag['floor'] or '(층 미입력)']
        base_floor = floors[0]
        # V18.0: 4~20F 같은 반복층 도곽은 동일 geometry/실명블록을 층수만큼 복제하지 않는다.
        # 대표층 1개 RoomRecord + 적용층 범위/개소 정보만 보관하고, 산출 합계에서만 repeat_count를 곱한다.
        rooms.append(RoomRecord(
            base_floor, tag['room_name'] or MISSING_ROOM_NAME, tag['room_number'],
            list(b['points']), b.get('finish_perimeter', b['perimeter']), b.get('net_area', b['area']),
            str(e.dxf.handle or ''), tag['handle'],
            str(e.dxf.layer), tag['layer'], tag['x'], tag['y'], True, False,
            division.get('drawing_type','미구분') if division else '미구분',
            division.get('title','') if division else '',
            b.get('deduction_area', 0.0), b.get('deduction_perimeter', 0.0), b.get('deduction_count', 0),
            list(floors), _floor_scope_label(floors), max(1, len(floors)),
            [list(item['points']) for item in b.get('deductions', [])]
        ))
    for idx,b in enumerate(boundaries):
        if idx not in assigned:
            e=b['entity']; cx,cy=interior_point(b['points'])
            division = classify_room_by_divisions(cx, cy, divisions) if divisions else None
            if divisions:
                floors = _division_floors(division) or ['(미구분)']
            else:
                floors = ['(층 미입력)']
            base_floor = floors[0]
            rooms.append(RoomRecord(
                base_floor, MISSING_ROOM_NAME, '', list(b['points']), b.get('finish_perimeter', b['perimeter']), b.get('net_area', b['area']),
                str(e.dxf.handle or ''), '',
                str(e.dxf.layer), '', cx, cy, True, False,
                division.get('drawing_type','미구분') if division else '미구분',
                division.get('title','') if division else '',
                b.get('deduction_area', 0.0), b.get('deduction_perimeter', 0.0), b.get('deduction_count', 0),
                list(floors), _floor_scope_label(floors), max(1, len(floors)),
                [list(item['points']) for item in b.get('deductions', [])]
            ))
            errors.append(f"실명 블록이 없는 실구분선: 핸들 {b['entity'].dxf.handle}")

    # 선택된 평면도 도곽별 실구분선 인식 결과를 검사한다. 도곽은 잡혔는데 실이 0개면
    # 해당 층이 목록에서 통째로 사라지는 원인을 바로 확인할 수 있게 오류에 남긴다.
    if divisions:
        for division in divisions:
            floors = _division_floors(division)
            if not floors:
                continue
            count = 0
            for b in boundaries:
                cx, cy = interior_point(b['points'])
                if point_in_polygon(cx, cy, division.get('points') or []):
                    count += 1
            if count == 0:
                errors.append(
                    f"평면도 도곽 안에서 실구분선을 찾지 못함: {','.join(floors)} / {division.get('title','')}"
                )
    rooms.sort(key=lambda r:(r.floor.casefold(),r.room_number.casefold(),r.room_name.casefold()))
    return rooms,errors


def build_room_import_diagnostics(msp, rules=None, divisions=None, rooms=None):
    """CAD 도서읽기 단계별 층 인식 결과를 사람이 확인하기 쉬운 구조로 반환한다."""
    r = room_rules(rules)
    boundary_layer = r["boundary_layer"].casefold()
    tag_layer = r["tag_layer"].casefold()
    boundaries = []
    tags = []
    for entity in msp:
        try:
            kind = entity.dxftype()
            layer = str(getattr(entity.dxf, "layer", "") or "").casefold()
            if kind in ("LWPOLYLINE", "POLYLINE") and layer == boundary_layer:
                pts = entity_points(entity)
                closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
                if closed and len(pts) >= 3 and polygon_area(pts) > COORD_TOLERANCE * COORD_TOLERANCE:
                    cx, cy = interior_point(pts)
                    boundaries.append({
                        "handle": str(getattr(entity.dxf, "handle", "") or ""),
                        "x": cx, "y": cy, "points": pts,
                    })
            elif kind == "INSERT" and layer == tag_layer:
                try:
                    x, y = get_insert_xy(entity)
                except Exception:
                    continue
                tags.append({"handle": str(getattr(entity.dxf, "handle", "") or ""), "x": x, "y": y})
        except Exception:
            continue

    divs = list(divisions or [])
    frame_rows = []
    for frame in divs:
        pts = frame.get("points") or []
        boundary_count = sum(1 for b in boundaries if pts and point_in_polygon(b["x"], b["y"], pts))
        tag_count = sum(1 for t in tags if pts and point_in_polygon(t["x"], t["y"], pts))
        frame_rows.append({
            "handle": str(frame.get("handle", "") or ""),
            "title": str(frame.get("title", "") or ""),
            "drawing_type": str(frame.get("drawing_type", "") or ""),
            "floors": list(frame.get("floors") or []),
            "boundary_count": boundary_count,
            "tag_count": tag_count,
        })

    room_floor_counts = defaultdict(int)
    for room in rooms or []:
        room_floor_counts[str(room.floor or "(미구분)")] += 1
    return {
        "boundary_total": len(boundaries),
        "tag_total": len(tags),
        "frames": frame_rows,
        "room_floor_counts": dict(room_floor_counts),
        "room_total": sum(room_floor_counts.values()),
    }


def validate_room_numbers(rooms):
    errors=[]; floors=defaultdict(list)
    for r in rooms:floors[r.floor].append(r)
    for floor,items in floors.items():
        seen=defaultdict(list)
        for r in items:
            n=r.room_number.strip()
            if not n: errors.append(f"{floor} / {r.room_name}: NUMBER 미입력")
            else: seen[n].append(r)
        for n,matches in seen.items():
            if len(matches)>1: errors.append(f"{floor}: NUMBER {n} 중복 ({', '.join(r.room_name for r in matches)})")
    return errors


def update_insert_attributes(insert,values):
    attrs={str(a.dxf.tag).upper():a for a in getattr(insert,'attribs',[])}; updated=[]; missing=[]
    for tag,value in values.items():
        if tag.upper() in attrs: attrs[tag.upper()].dxf.text=str(value); updated.append(tag.upper())
        else: missing.append(tag.upper())
    return updated,missing


def _save_room_boundary_geometry(doc, room, tolerance: float = 0.01) -> str:
    """프로그램에서 절점이 추가된 실구분선을 DXF 복사본에 반영한다.

    현재 UI 기능은 기존 직선 구간 위에 절점을 추가하는 동작만 허용한다.
    LWPOLYLINE은 최종 절점 전체를 기록하고, 구형 POLYLINE은 기존 절점 순서를
    보존한 채 추가된 절점만 insert_vertices()로 삽입한다.
    """
    if not bool(getattr(room, "geometry_changed", False)):
        return "unchanged"
    boundary = doc.entitydb.get(str(getattr(room, "boundary_handle", "") or ""))
    if boundary is None:
        return "missing"
    points = [(float(x), float(y)) for x, y in (getattr(room, "points", []) or [])]
    if len(points) < 3:
        return "invalid"
    kind = boundary.dxftype()
    try:
        if kind == "LWPOLYLINE":
            boundary.set_points(points, format="xy")
            boundary.closed = True
            return "updated"
        if kind == "POLYLINE":
            original = entity_points(boundary)
            if not original:
                return "invalid"
            # 기존 절점은 같은 순서로 남고 그 사이에 새 절점만 들어왔는지 확인한다.
            oi = 0
            fi = 0
            while fi < len(points):
                if oi < len(original) and math.hypot(points[fi][0]-original[oi][0], points[fi][1]-original[oi][1]) <= tolerance:
                    oi += 1; fi += 1
                    continue
                boundary.insert_vertices(fi, [points[fi]])
                fi += 1
            if oi != len(original):
                return "unsupported"
            boundary.close(True)
            return "updated"
    except Exception:
        return "failed"
    return "unsupported"


def save_rooms_to_dxf(source_path,output_path,rooms,rules=None):
    r=room_rules(rules); doc=ezdxf.readfile(source_path); report={
        "rooms":len(rooms),"updated":0,"missing_insert":0,"missing_number_attr":0,"missing_basic_attr":0,
        "boundary_updated":0,"boundary_missing":0,"boundary_failed":0,
    }
    for room in rooms:
        geometry_result = _save_room_boundary_geometry(doc, room)
        if geometry_result == "updated": report["boundary_updated"] += 1
        elif geometry_result == "missing": report["boundary_missing"] += 1
        elif geometry_result in ("invalid", "unsupported", "failed"): report["boundary_failed"] += 1

        insert=doc.entitydb.get(room.tag_handle)
        if insert is None or insert.dxftype()!="INSERT": report["missing_insert"]+=1; continue
        _,missing=update_insert_attributes(insert,{r["floor_tag"]:room.floor,r["room_name_tag"]:room.room_name})
        report["missing_basic_attr"]+=len(missing)
        attrs={str(a.dxf.tag).upper():a for a in getattr(insert,'attribs',[])}
        number_attr=next((attrs[t] for t in r["room_number_tags"] if t in attrs),None)
        if number_attr is None: report["missing_number_attr"]+=1
        else: number_attr.dxf.text=room.room_number
        report["updated"]+=1
    doc.saveas(output_path); return report



def interior_point(pts):
    if not pts:
        return 0.0, 0.0
    area2 = 0.0; cx = 0.0; cy = 0.0
    for i,(x0,y0) in enumerate(pts):
        x1,y1=pts[(i+1)%len(pts)]; cross=x0*y1-x1*y0
        area2 += cross; cx += (x0+x1)*cross; cy += (y0+y1)*cross
    if abs(area2)>1e-9:
        candidate=(cx/(3*area2),cy/(3*area2))
        if point_in_polygon(candidate[0],candidate[1],pts): return candidate
    minx=min(x for x,_ in pts); maxx=max(x for x,_ in pts); miny=min(y for _,y in pts); maxy=max(y for _,y in pts)
    for gy in range(1,20):
        y=miny+(maxy-miny)*gy/20
        for gx in range(1,20):
            x=minx+(maxx-minx)*gx/20
            if point_in_polygon(x,y,pts): return x,y
    return pts[0]

def _room_attr_values(room, rules):
    r=room_rules(rules); values={r['floor_tag']:room.floor, r['room_name_tag']:room.room_name}
    values[r['room_number_tags'][0]]=room.room_number
    return values

def _add_room_block(msp, block_name, insert_point, layer, values, rotation=0.0, xscale=1.0, yscale=1.0, zscale=1.0):
    ref=msp.add_blockref(block_name, insert_point, dxfattribs={'layer':layer,'rotation':rotation,'xscale':xscale,'yscale':yscale,'zscale':zscale})
    ref.add_auto_attribs(values)
    return ref

def create_room_blocks(source_path, output_path, rooms, rules=None):
    r=room_rules(rules); doc=ezdxf.readfile(source_path); msp=doc.modelspace()
    if r['block_name'] not in doc.blocks:
        raise ValueError(f"DXF에 블록 정의가 없습니다: {r['block_name']}")
    report={'requested':len(rooms),'created':0,'already_exists':0,'boundary_missing':0}
    for room in rooms:
        if room.tag_handle:
            report['already_exists']+=1; continue
        boundary=doc.entitydb.get(room.boundary_handle)
        if boundary is None:
            report['boundary_missing']+=1; continue
        x,y=interior_point(room.points)
        ref=_add_room_block(msp,r['block_name'],(x,y),r['tag_layer'],_room_attr_values(room,rules))
        report['created']+=1
    doc.saveas(output_path); return report

def refresh_room_blocks(source_path, output_path, rooms, rules=None):
    r=room_rules(rules); doc=ezdxf.readfile(source_path); msp=doc.modelspace()
    if r['block_name'] not in doc.blocks:
        raise ValueError(f"DXF에 블록 정의가 없습니다: {r['block_name']}")
    report={'requested':len(rooms),'refreshed':0,'missing_insert':0}
    for room in rooms:
        old=doc.entitydb.get(room.tag_handle) if room.tag_handle else None
        if old is None or old.dxftype()!='INSERT':
            report['missing_insert']+=1; continue
        loc=old.dxf.insert; rotation=float(getattr(old.dxf,'rotation',0) or 0); xs=float(getattr(old.dxf,'xscale',1) or 1); ys=float(getattr(old.dxf,'yscale',1) or 1); zs=float(getattr(old.dxf,'zscale',1) or 1)
        try: msp.delete_entity(old)
        except Exception: doc.entitydb.delete_entity(old)
        ref=_add_room_block(msp,r['block_name'],(loc.x,loc.y,loc.z),r['tag_layer'],_room_attr_values(room,rules),rotation,xs,ys,zs)
        report['refreshed']+=1
    doc.saveas(output_path); return report


def create_wall_finish_lines(source_path, output_path, rooms, rules=None):
    """실구분선 절점 사이를 독립 LINE으로 작성한다.

    동일 좌표의 공용벽은 한 번만 만들고, 대상 레이어에 이미 존재하는
    같은 선분은 다시 만들지 않는다.
    """
    r = room_rules(rules)
    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()
    layer_name = r["wall_finish_layer"]
    if layer_name not in doc.layers:
        doc.layers.add(layer_name, color=3)

    existing = set()
    for entity in msp:
        try:
            if str(getattr(entity.dxf, "layer", "")).casefold() != layer_name.casefold():
                continue
            if entity.dxftype() == "LINE":
                existing.add(segment_key(entity.dxf.start.x, entity.dxf.start.y, entity.dxf.end.x, entity.dxf.end.y))
            elif entity.dxftype() in ("LWPOLYLINE", "POLYLINE"):
                pts = entity_points(entity)
                closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
                limit = len(pts) if closed else max(0, len(pts)-1)
                for i in range(limit):
                    existing.add(segment_key(*pts[i], *pts[(i+1) % len(pts)]))
        except Exception:
            continue

    requested = created = skipped_existing = skipped_degenerate = 0
    seen = set(existing)
    for room in rooms:
        pts = clean_points(room.points)
        if len(pts) < 2:
            continue
        for i in range(len(pts)):
            requested += 1
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            if math.hypot(x2-x1, y2-y1) <= COORD_TOLERANCE:
                skipped_degenerate += 1
                continue
            key = segment_key(x1, y1, x2, y2)
            if key in seen:
                skipped_existing += 1
                continue
            msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer_name})
            seen.add(key)
            created += 1

    doc.saveas(output_path)
    return {
        "requested": requested,
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_degenerate": skipped_degenerate,
        "layer": layer_name,
    }

def _layer_display_color_info(doc, layer_name: str | None):
    """임시 DXF 레이어의 실제 표시 RGB/ACI를 안전하게 읽는다."""
    try:
        layer = doc.layers.get(str(layer_name or "0"))
    except Exception:
        return None, None

    aci = None
    try:
        raw = int(getattr(layer, "color", 7) or 7)
        raw = abs(raw)
        if 1 <= raw <= 255:
            aci = raw
    except Exception:
        pass

    true_color = 0
    try:
        true_color = int(getattr(layer.dxf, "true_color", 0) or 0)
    except Exception:
        true_color = 0
    if true_color:
        return (
            (true_color >> 16) & 255,
            (true_color >> 8) & 255,
            true_color & 255,
        ), aci

    try:
        layer_rgb = getattr(layer, "rgb", None)
    except Exception:
        layer_rgb = None
    if layer_rgb is not None:
        try:
            return (int(layer_rgb[0]), int(layer_rgb[1]), int(layer_rgb[2])), aci
        except Exception:
            pass

    if aci is not None:
        try:
            rgb = aci2rgb(aci)
            return (int(rgb.r), int(rgb.g), int(rgb.b)), aci
        except Exception:
            pass
    return None, aci


def _entity_color_info_detailed(entity, doc, inherited_rgb=None, inherited_aci=None, effective_layer=None):
    """기존 색상 판정 결과와 선택 경로를 함께 반환한다.

    V18.4의 판정 순서/결과는 유지하고 진단용 reason만 추가한다.
    """
    try:
        own_layer = str(getattr(entity.dxf, "layer", "0") or "0")
        resolved_layer = str(effective_layer or own_layer or "0")
        layer_rgb, layer_aci = _layer_display_color_info(doc, resolved_layer)

        true_color = int(getattr(entity.dxf, "true_color", 0) or 0)
        raw_aci = int(getattr(entity.dxf, "color", 256) or 256)
        if true_color:
            rgb = ((true_color >> 16) & 255, (true_color >> 8) & 255, true_color & 255)
            width_aci = None
            if 1 <= abs(raw_aci) <= 255:
                width_aci = abs(raw_aci)
            elif raw_aci == 256:
                width_aci = layer_aci
            elif raw_aci == 0:
                width_aci = inherited_aci if inherited_aci is not None else layer_aci
            return rgb, width_aci, "ENTITY_TRUE_COLOR"
        if raw_aci == 0:
            if inherited_rgb is not None:
                return inherited_rgb, inherited_aci, "BYBLOCK_INHERITED"
            if layer_rgb is not None:
                return layer_rgb, layer_aci, "BYBLOCK_LAYER"
            return (190, 190, 190), 0, "FALLBACK_190_BYBLOCK_NO_COLOR"
        if raw_aci == 256:
            if layer_rgb is not None:
                return layer_rgb, layer_aci, "BYLAYER_LAYER"
            if inherited_rgb is not None:
                return inherited_rgb, inherited_aci, "BYLAYER_INHERITED"
            return (190, 190, 190), layer_aci, "FALLBACK_190_BYLAYER_NO_COLOR"

        aci = abs(raw_aci)
        rgb = aci2rgb(aci)
        return (int(rgb.r), int(rgb.g), int(rgb.b)), aci, "ENTITY_ACI"
    except Exception as exc:
        if inherited_rgb is not None:
            return inherited_rgb, inherited_aci, f"EXCEPTION_INHERITED:{type(exc).__name__}"
        return (190, 190, 190), inherited_aci, f"FALLBACK_190_EXCEPTION:{type(exc).__name__}"


def _entity_color_info(entity, doc, inherited_rgb=None, inherited_aci=None, effective_layer=None):
    rgb, aci, _reason = _entity_color_info_detailed(
        entity, doc, inherited_rgb, inherited_aci, effective_layer
    )
    return rgb, aci

def _entity_rgb(entity, doc, inherited_rgb=None):
    return _entity_color_info(entity, doc, inherited_rgb, None, None)[0]

def read_cad_background(doc, exclude_patterns=None, business_layers=None, color_overrides=None, color_diagnostic=None, crop_rects=None, xclip_by_handle=None, block_name_aliases=None, xref_block_names=None):
    segments=[]; annotation_segments=[]; texts=[]; fills=[]; keys=set(); annotation_keys=set(); duplicate_count=0; skipped_count=0; max_depth=8
    # 블록 전개 문제를 추측으로 고치지 않도록 INSERT별 전개 결과/실패 원인을 수집한다.
    block_diag = {
        "insert_count": 0, "recursive_ok": 0, "recursive_failed": 0,
        "virtual_fallback_ok": 0, "virtual_fallback_failed": 0,
        "attrib_count": 0, "child_type_counts": {}, "details": [],
        "xclip_insert_count": 0, "xclip_forced_count": 0, "xclip_errors": [],
        "xclip_segment_input": 0, "xclip_segment_parts": 0, "xclip_segment_rejected": 0,
        "crop_segment_input": 0, "crop_segment_output": 0, "crop_segment_rejected": 0,
        "fill_clip_input": 0, "fill_clip_output": 0, "fill_clip_errors": 0,
        # V0.24.12: CAD/DXF에서 표시 꺼진 객체(group code 60 / ATTRIB invisible flag)를
        # 블록 전개 전에 제외한다. 동적블록의 비표시 상태가 전부 겹쳐 보이는 문제 진단용.
        "invisible_entity_skipped": 0, "invisible_entity_examples": [],
    }
    color_overrides = dict(color_overrides or {})
    forced_xclips = {
        str(k or "").strip().upper(): dict(v or {})
        for k, v in dict(xclip_by_handle or {}).items()
        if str(k or "").strip()
    }
    block_aliases = {
        str(k or "").strip().casefold(): str(v or "").strip()
        for k, v in dict(block_name_aliases or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }
    xref_roots = {
        str(v or "").strip().casefold()
        for v in (xref_block_names or [])
        if str(v or "").strip()
    }
    patterns=[str(x).strip().casefold() for x in (exclude_patterns or []) if str(x).strip()]
    business={str(x).strip().casefold() for x in (business_layers or []) if str(x).strip()}
    normalized_crop_rects = []
    for raw_rect in crop_rects or []:
        try:
            ax, ay, bx, by = [float(v) for v in raw_rect]
            normalized_crop_rects.append((min(ax,bx), min(ay,by), max(ax,bx), max(ay,by)))
        except Exception:
            continue

    def clip_segment_rect(x1, y1, x2, y2, rect):
        xmin, ymin, xmax, ymax = rect
        left, right, bottom, top = 1, 2, 4, 8
        def code(x, y):
            value = 0
            if x < xmin: value |= left
            elif x > xmax: value |= right
            if y < ymin: value |= bottom
            elif y > ymax: value |= top
            return value
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

    def clipped_segments_in_crop(x1, y1, x2, y2):
        if not normalized_crop_rects:
            return [(float(x1), float(y1), float(x2), float(y2))]
        result = []
        for rect in normalized_crop_rects:
            clipped = clip_segment_rect(float(x1), float(y1), float(x2), float(y2), rect)
            if clipped is not None:
                result.append(clipped)
        return result

    def point_in_crop(x, y):
        if not normalized_crop_rects:
            return True
        return any(xmin <= x <= xmax and ymin <= y <= ymax for xmin,ymin,xmax,ymax in normalized_crop_rects)

    # V24.05.2: XREF XCLIP용 임의 다각형 선분 클리핑.
    # XCLIP 경계는 WCS 좌표로 전달되며, recursive_decompose() 결과도 WCS이므로
    # 별도 블록 역변환 없이 그대로 판정할 수 있다.
    def point_in_polygon(x, y, polygon):
        pts = list(polygon or [])
        if len(pts) < 3:
            return True
        inside = False
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = float(pts[i][0]), float(pts[i][1])
            xj, yj = float(pts[j][0]), float(pts[j][1])
            # 경계 위 점은 내부로 취급한다.
            dx, dy = xj - xi, yj - yi
            cross = (x - xi) * dy - (y - yi) * dx
            if abs(cross) <= 1e-7:
                dot = (x - xi) * (xj - xi) + (y - yi) * (yj - yi)
                if -1e-7 <= dot <= dx * dx + dy * dy + 1e-7:
                    return True
            if ((yi > y) != (yj > y)):
                xcross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi
                if x < xcross:
                    inside = not inside
            j = i
        return inside

    def segment_intersection_t(a, b, c, d):
        ax, ay = a; bx, by = b; cx, cy = c; dx, dy = d
        rx, ry = bx - ax, by - ay
        sx, sy = dx - cx, dy - cy
        den = rx * sy - ry * sx
        if abs(den) <= 1e-12:
            return None
        qx, qy = cx - ax, cy - ay
        t = (qx * sy - qy * sx) / den
        u = (qx * ry - qy * rx) / den
        if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
            return max(0.0, min(1.0, float(t)))
        return None

    def clip_segment_polygon(x1, y1, x2, y2, polygon, inverted=False):
        pts = [(float(x), float(y)) for x, y in (polygon or [])]
        if len(pts) < 3:
            return [(float(x1), float(y1), float(x2), float(y2))]
        a = (float(x1), float(y1)); b = (float(x2), float(y2))
        ts = [0.0, 1.0]
        for i in range(len(pts)):
            t = segment_intersection_t(a, b, pts[i], pts[(i + 1) % len(pts)])
            if t is not None:
                ts.append(t)
        ts = sorted(ts)
        uniq = []
        for t in ts:
            if not uniq or abs(t - uniq[-1]) > 1e-8:
                uniq.append(t)
        out = []
        dx, dy = b[0] - a[0], b[1] - a[1]
        for ta, tb in zip(uniq, uniq[1:]):
            if tb - ta <= 1e-9:
                continue
            tm = (ta + tb) * 0.5
            mx, my = a[0] + dx * tm, a[1] + dy * tm
            keep = point_in_polygon(mx, my, pts)
            if inverted:
                keep = not keep
            if keep:
                out.append((a[0] + dx * ta, a[1] + dy * ta, a[0] + dx * tb, a[1] + dy * tb))
        return out

    def clip_fill_polygons(points, clip_polygon=None, clip_inverted=False):
        """채움 폴리곤을 XCLIP과 평면도 도곽에 실제로 자른다.

        선분은 기존에도 XCLIP 적용이 됐지만 SOLID/HATCH 채움은 bbox만 확인하고
        원본 면 전체를 저장해 클립 밖의 큰 회색 면이 보일 수 있었다.
        """
        pts = [(float(x), float(y)) for x, y in (points or [])]
        if len(pts) < 3:
            return []
        block_diag["fill_clip_input"] = block_diag.get("fill_clip_input", 0) + 1
        try:
            from ezdxf.math import Vec2, BoundingBox2d, is_convex_polygon_2d
            from ezdxf.math.clipping import (
                ConvexClippingPolygon2d, ConcaveClippingPolygon2d, ClippingRect2d, InvertedClippingPolygon2d
            )
            polys = [[Vec2(x, y) for x, y in pts]]
            if clip_polygon and len(clip_polygon) >= 3:
                clip_pts = [Vec2(float(x), float(y)) for x, y in clip_polygon]
                if clip_inverted:
                    if normalized_crop_rects:
                        bx0 = min(r[0] for r in normalized_crop_rects); by0 = min(r[1] for r in normalized_crop_rects)
                        bx1 = max(r[2] for r in normalized_crop_rects); by1 = max(r[3] for r in normalized_crop_rects)
                        outer = BoundingBox2d([Vec2(bx0, by0), Vec2(bx1, by1)])
                    else:
                        all_pts = pts + [(float(v.x), float(v.y)) for v in clip_pts]
                        xs = [v[0] for v in all_pts]; ys = [v[1] for v in all_pts]
                        pad = max(max(xs)-min(xs), max(ys)-min(ys), 1.0) * 0.05 + 1.0
                        outer = BoundingBox2d([Vec2(min(xs)-pad, min(ys)-pad), Vec2(max(xs)+pad, max(ys)+pad)])
                    clipper = InvertedClippingPolygon2d(clip_pts, outer)
                else:
                    # 현장 XCLIP은 대부분 사각/볼록 다각형이다. ezdxf의 concave
                    # 클리퍼는 한 폴리곤이 다른 폴리곤을 완전히 포함하는 경우 빈 결과를
                    # 반환하는 버전이 있어, 볼록 경계는 전용 클리퍼를 사용한다.
                    clipper = ConvexClippingPolygon2d(clip_pts) if is_convex_polygon_2d(clip_pts) else ConcaveClippingPolygon2d(clip_pts)
                clipped = []
                for poly in polys:
                    clipped.extend([list(v) for v in clipper.clip_polygon(poly) if len(v) >= 3])
                polys = clipped

            if normalized_crop_rects and polys:
                cropped = []
                for rect in normalized_crop_rects:
                    xmin, ymin, xmax, ymax = rect
                    rect_clipper = ClippingRect2d(Vec2(xmin, ymin), Vec2(xmax, ymax))
                    for poly in polys:
                        cropped.extend([list(v) for v in rect_clipper.clip_polygon(poly) if len(v) >= 3])
                polys = cropped

            result = []
            for poly in polys:
                clean = [(float(v.x), float(v.y)) for v in poly]
                if len(clean) >= 3:
                    result.append(clean)
            block_diag["fill_clip_output"] = block_diag.get("fill_clip_output", 0) + len(result)
            return result
        except Exception:
            block_diag["fill_clip_errors"] = block_diag.get("fill_clip_errors", 0) + 1
            # XCLIP 또는 도곽 클립이 필요한 상황에서 실패한 채움은 통째로 내보내지 않는다.
            # 잘못된 대형 면으로 화면을 덮는 것보다 경계선만 남기는 것이 안전하다.
            if clip_polygon or normalized_crop_rects:
                return []
            return [pts]

    def entity_intersects_crop(entity):
        if not normalized_crop_rects:
            return True
        try:
            from ezdxf import bbox as _ezbbox
            ext = _ezbbox.extents([entity], fast=True)
            if not ext.has_data:
                return True
            ex1, ey1 = float(ext.extmin.x), float(ext.extmin.y)
            ex2, ey2 = float(ext.extmax.x), float(ext.extmax.y)
            return any(not (ex2 < xmin or ex1 > xmax or ey2 < ymin or ey1 > ymax)
                       for xmin,ymin,xmax,ymax in normalized_crop_rects)
        except Exception:
            # bbox 판정 실패 객체는 누락보다 표시를 우선한다.
            return True

    def layer_is_visible(layer_name):
        try:
            layer=doc.layers.get(layer_name)
            return not bool(layer.is_off()) and not bool(layer.is_frozen())
        except Exception:
            return True

    def layer_is_excluded(layer_name):
        name=str(layer_name or "0").casefold()
        return name in business or any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    def entity_is_invisible(entity):
        """DXF 자체에서 현재 표시 꺼진 객체를 판정한다.

        일반 그래픽 객체의 group code 60(invisible)과 ATTRIB/ATTDEF의
        invisible flag(bit 1)를 모두 본다. ezdxf virtual_entities()는 동적/익명
        블록 정의 안의 비표시 객체도 돌려줄 수 있으므로 전개 직전에 걸러야
        CAD 화면에서 보이지 않는 상태가 프로그램에 겹쳐 나오지 않는다.
        """
        try:
            if int(getattr(entity.dxf, "invisible", 0) or 0) != 0:
                return True, "DXF60"
        except Exception:
            pass
        try:
            et = str(entity.dxftype() or "").upper()
        except Exception:
            et = ""
        if et in ("ATTRIB", "ATTDEF"):
            try:
                if int(getattr(entity.dxf, "flags", 0) or 0) & 1:
                    return True, "ATTRIB_FLAG"
            except Exception:
                pass
        return False, ""

    def make_meta(layer_name, entity_type, block_stack=()):
        stack = tuple(str(v or "").strip() for v in (block_stack or ()) if str(v or "").strip())
        # XREF는 2단 날것 캐시에서 외부참조 껍데기로 취급하지 않는다.
        # 최상위 XREF명은 출처(xref_name)로만 남기고, XREF 내부의 일반 BLOCK만
        # block_name/block_path에 유지해 사용자가 장애인주차 같은 블록을 통째로 끌 수 있게 한다.
        xref_name = ""
        block_stack_only = stack
        if stack and stack[0].casefold() in xref_roots:
            xref_name = stack[0]
            block_stack_only = stack[1:]
        return {
            "layer": str(layer_name or "0"),
            "entity_type": str(entity_type or "기타"),
            "source_kind": "BLOCK" if block_stack_only else "DIRECT",
            "block_name": block_stack_only[-1] if block_stack_only else "",
            "block_path": " > ".join(block_stack_only),
            "xref_name": xref_name,
        }

    def add_segment(x1,y1,x2,y2,rgb,aci=None,trace_handle="",annotation=False,clip_polygon=None,clip_inverted=False,meta=None):
        nonlocal duplicate_count,skipped_count
        try:
            vals=tuple(map(float,(x1,y1,x2,y2)))
            if not all(math.isfinite(v) for v in vals) or math.hypot(vals[2]-vals[0],vals[3]-vals[1])<=COORD_TOLERANCE:
                skipped_count+=1; return
            # V24.39: XREF가 어느 단계에서 사라지는지 진단하기 위해
            # XCLIP 전/후와 도곽 crop 전/후 선분 수를 각각 집계한다.
            if clip_polygon:
                block_diag["xclip_segment_input"] = block_diag.get("xclip_segment_input", 0) + 1
            xclip_parts = clip_segment_polygon(*vals, clip_polygon, clip_inverted) if clip_polygon else [vals]
            if clip_polygon:
                block_diag["xclip_segment_parts"] = block_diag.get("xclip_segment_parts", 0) + len(xclip_parts)
                if not xclip_parts:
                    block_diag["xclip_segment_rejected"] = block_diag.get("xclip_segment_rejected", 0) + 1
            clipped_list = []
            for part in xclip_parts:
                block_diag["crop_segment_input"] = block_diag.get("crop_segment_input", 0) + 1
                cropped_parts = clipped_segments_in_crop(*part)
                block_diag["crop_segment_output"] = block_diag.get("crop_segment_output", 0) + len(cropped_parts)
                if not cropped_parts:
                    block_diag["crop_segment_rejected"] = block_diag.get("crop_segment_rejected", 0) + 1
                clipped_list.extend(cropped_parts)
            if not clipped_list:
                return
            target_keys = annotation_keys if annotation else keys
            target_segments = annotation_segments if annotation else segments
            for clipped_vals in clipped_list:
                if math.hypot(clipped_vals[2]-clipped_vals[0],clipped_vals[3]-clipped_vals[1]) <= COORD_TOLERANCE:
                    continue
                base_key=segment_key(*clipped_vals)
                meta_key = (
                    str((meta or {}).get("layer", "") or "").casefold(),
                    str((meta or {}).get("entity_type", "") or ""),
                    str((meta or {}).get("block_path", "") or "").casefold(),
                )
                key=(base_key, tuple(int(v) for v in rgb[:3]), int(aci if aci is not None else -1), meta_key)
                if key in target_keys:
                    duplicate_count+=1
                    continue
                target_keys.add(key)
                target_segments.append((*clipped_vals,*rgb, aci if aci is not None else -1, str(trace_handle or "").strip().upper(), dict(meta or {})))
        except (TypeError,ValueError): skipped_count+=1
    def add_path(entity,rgb,aci=None,trace_handle="",annotation=False,clip_polygon=None,clip_inverted=False,meta=None):
        try:
            pts=[(float(v.x),float(v.y)) for v in make_path(entity).flattening(distance=10.0)]
            for i in range(len(pts)-1):
                add_segment(*pts[i],*pts[i+1],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,meta)
        except Exception: pass
    def process(entity,depth=0,inherited_rgb=None,inherited_layer=None,inherited_aci=None,parent_handle="",annotation=False,clip_polygon=None,clip_inverted=False,block_stack=(),semantic_type=""):
        if depth>max_depth:return
        try:
            # 최상위 ModelSpace 객체는 평면 도곽과 겹치지 않으면 블록 분해/경로생성을 아예 하지 않는다.
            if depth == 0 and not entity_intersects_crop(entity):
                return
            own_layer=str(getattr(entity.dxf,"layer","0") or "0")
            effective_layer=inherited_layer if own_layer=="0" and inherited_layer else own_layer
            if layer_is_excluded(effective_layer) or not layer_is_visible(effective_layer):
                return
            invisible, invisible_reason = entity_is_invisible(entity)
            if invisible:
                block_diag["invisible_entity_skipped"] = block_diag.get("invisible_entity_skipped", 0) + 1
                examples = block_diag.setdefault("invisible_entity_examples", [])
                if len(examples) < 40:
                    try:
                        etype = str(entity.dxftype() or "")
                    except Exception:
                        etype = ""
                    examples.append({
                        "type": etype,
                        "layer": effective_layer,
                        "handle": str(getattr(entity.dxf, "handle", "") or "").strip().upper(),
                        "reason": invisible_reason,
                        "block_path": " > ".join(str(v or "").strip() for v in (block_stack or ()) if str(v or "").strip()),
                    })
                return
            # V18.4: CAD COM에서 이미 계산해 둔 실제 표시색이 있으면 최우선 사용한다.
            # 없을 때만 임시 ezdxf 문서의 ACI/BYLAYER/TrueColor 해석으로 fallback한다.
            handle_key = str(getattr(entity.dxf, "handle", "") or "").strip().upper()
            trace_handle = handle_key or str(parent_handle or "").strip().upper()
            override = color_overrides.get(handle_key) if handle_key else None
            if override is not None:
                rgb, aci = override
                color_reason = "CAD_OVERRIDE_BY_HANDLE"
            else:
                rgb, aci, color_reason = _entity_color_info_detailed(
                    entity, doc, inherited_rgb, inherited_aci, effective_layer
                )
            et=entity.dxftype()
            current_type = str(semantic_type or et)
            current_meta = make_meta(effective_layer, current_type, block_stack)
            if color_diagnostic is not None:
                try:
                    temp_color = int(getattr(entity.dxf, "color", 256) or 256)
                    temp_tc = int(getattr(entity.dxf, "true_color", 0) or 0)
                    color_diagnostic.log(
                        "CORE_COLOR",
                        handle=trace_handle,
                        entity_handle=handle_key,
                        parent_handle=str(parent_handle or "").strip().upper(),
                        type=et,
                        depth=depth,
                        own_layer=own_layer,
                        effective_layer=effective_layer,
                        temp_color=temp_color,
                        temp_true_color=temp_tc,
                        override_found=(override is not None),
                        override_rgb=(override[0] if override is not None else None),
                        override_aci=(override[1] if override is not None else None),
                        inherited_rgb=inherited_rgb,
                        inherited_aci=inherited_aci,
                        final_rgb=rgb,
                        final_aci=aci,
                        reason=color_reason,
                    )
                    if str(color_reason).startswith("FALLBACK_190"):
                        color_diagnostic.mark_fallback("CORE_COLOR", trace_handle, color_reason)
                except Exception:
                    pass
            if et in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                text = _entity_text(entity)
                if text:
                    try:
                        insert = entity.dxf.insert
                        x, y = float(insert.x), float(insert.y)
                    except Exception:
                        x, y = 0.0, 0.0
                    try:
                        height = float(getattr(entity.dxf, "height", 0.0) or getattr(entity.dxf, "char_height", 0.0) or 200.0)
                    except Exception:
                        height = 200.0
                    try:
                        rotation = float(getattr(entity.dxf, "rotation", 0.0) or 0.0)
                    except Exception:
                        rotation = 0.0
                    halign = 0; valign = 0; attachment = 0
                    # TEXT의 중앙/우측 정렬점, MTEXT의 text_direction을 보존해
                    # 프로그램 재생성 시 치수문자/Y4 같은 중앙문자의 위치와 회전을 복원한다.
                    if et in ("TEXT", "ATTRIB", "ATTDEF"):
                        try:
                            halign = int(getattr(entity.dxf, "halign", 0) or 0)
                            valign = int(getattr(entity.dxf, "valign", 0) or 0)
                            if halign or valign:
                                align = entity.dxf.align_point
                                if align is not None:
                                    x, y = float(align.x), float(align.y)
                        except Exception:
                            pass
                    else:
                        try:
                            attachment = int(getattr(entity.dxf, "attachment_point", 0) or 0)
                        except Exception:
                            attachment = 0
                        try:
                            if entity.dxf.hasattr("text_direction"):
                                direction = entity.dxf.text_direction
                                if direction is not None and (abs(float(direction.x)) > 1e-12 or abs(float(direction.y)) > 1e-12):
                                    rotation = math.degrees(math.atan2(float(direction.y), float(direction.x)))
                        except Exception:
                            pass
                    in_xclip = True if not clip_polygon else point_in_polygon(x, y, clip_polygon)
                    if clip_inverted and clip_polygon:
                        in_xclip = not in_xclip
                    if in_xclip and point_in_crop(x, y):
                        texts.append({
                            "x": x, "y": y, "text": text, "height": height, "rotation": rotation,
                            "halign": halign, "valign": valign, "attachment": attachment,
                            "rgb": list(rgb), "aci": aci if aci is not None else -1,
                            "handle": trace_handle, "meta": dict(current_meta),
                        })
            elif et=="LINE":
                add_segment(entity.dxf.start.x,entity.dxf.start.y,entity.dxf.end.x,entity.dxf.end.y,rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
            elif et in ("LWPOLYLINE","POLYLINE","CIRCLE","ARC","ELLIPSE","SPLINE"):
                add_path(entity,rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
            elif et in ("SOLID","TRACE","3DFACE"):
                pts=[]
                for attr in ("vtx0","vtx1","vtx2","vtx3"):
                    try:
                        v=getattr(entity.dxf,attr)
                        pts.append((float(v.x),float(v.y)))
                    except Exception:
                        pass
                clean=[]
                for pt in pts:
                    if not clean or math.hypot(pt[0]-clean[-1][0],pt[1]-clean[-1][1])>COORD_TOLERANCE:
                        clean.append(pt)
                if len(clean)>=2:
                    for i in range(len(clean)-1): add_segment(*clean[i],*clean[i+1],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                    if len(clean)>=3: add_segment(*clean[-1],*clean[0],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
            elif et == "INSERT":
                # V24.03.2: 블록 전개 진단을 남긴다. 전개 실패/0개 결과를 로그로 확인할 수 있게 한다.
                block_diag["insert_count"] += 1
                raw_name = str(getattr(entity.dxf, "name", "") or "")
                name = block_aliases.get(raw_name.strip().casefold(), raw_name)
                try:
                    ins = entity.dxf.insert
                    insert_xy = (float(ins.x), float(ins.y))
                except Exception:
                    insert_xy = None
                try:
                    block_def = doc.blocks.get(raw_name)
                    def_types = {}
                    def_count = 0
                    for child in block_def:
                        ctype = str(child.dxftype())
                        def_types[ctype] = def_types.get(ctype, 0) + 1
                        def_count += 1
                except Exception as exc:
                    def_count = -1
                    def_types = {}
                    block_def_error = f"{type(exc).__name__}: {exc}"
                else:
                    block_def_error = ""

                before_segments = len(segments) + len(annotation_segments)
                before_texts = len(texts)
                before_fills = len(fills)
                diag_xclip_in = block_diag.get("xclip_segment_input", 0)
                diag_xclip_parts = block_diag.get("xclip_segment_parts", 0)
                diag_xclip_rejected = block_diag.get("xclip_segment_rejected", 0)
                diag_crop_in = block_diag.get("crop_segment_input", 0)
                diag_crop_out = block_diag.get("crop_segment_output", 0)
                diag_crop_rejected = block_diag.get("crop_segment_rejected", 0)
                recursive_error = ""
                fallback_error = ""
                # V24.08: 한 단계씩 virtual_entities()를 전개해 중첩 BLOCK 이름과
                # 변환행렬을 보존한다. 장애인주차 같은 개별 블록을 목록에서 제외할 수 있고,
                # 부모/자식 INSERT 축척·회전도 단계별로 누적된다.
                method = "virtual_entities_recursive"
                virtual=[]
                try:
                    virtual=list(entity.virtual_entities())
                    block_diag["virtual_fallback_ok"] += 1
                except Exception as exc:
                    block_diag["virtual_fallback_failed"] += 1
                    fallback_error = f"{type(exc).__name__}: {exc}"
                    method = "recursive_decompose_fallback"
                    try:
                        from ezdxf.disassemble import recursive_decompose
                        virtual=list(recursive_decompose([entity]))
                        block_diag["recursive_ok"] += 1
                    except Exception as exc2:
                        block_diag["recursive_failed"] += 1
                        recursive_error = f"{type(exc2).__name__}: {exc2}"
                        virtual=[]

                child_types = {}
                for ve in virtual:
                    vt = str(ve.dxftype())
                    child_types[vt] = child_types.get(vt, 0) + 1
                    block_diag["child_type_counts"][vt] = block_diag["child_type_counts"].get(vt, 0) + 1

                # V24.05.2: 이 INSERT 자체에 XCLIP이 있으면 해당 경계를 자식 전체에 적용한다.
                # 상위 XCLIP이 이미 있으면 둘 다 만족해야 하지만, 현재 대상은 최상위 XREF가
                # 대부분이므로 우선 최상위/현재 INSERT 경계를 보존한다.
                child_clip_polygon = clip_polygon
                child_clip_inverted = clip_inverted
                # V24.40: 현재 INSERT 자체의 XCLIP과 상위에서 상속된 XCLIP을 구분해
                # 일반 블록 XCLIP 진단에서 실제 클립 소유 블록만 정확히 찾는다.
                own_xclip = False
                xclip_source = "inherited" if clip_polygon else "none"
                forced_clip = forced_xclips.get(str(handle_key or trace_handle or "").strip().upper())
                if forced_clip:
                    try:
                        verts = [(float(x), float(y)) for x, y in (forced_clip.get("vertices") or [])]
                        if len(verts) >= 3:
                            # V24.07.2: XREF 블록명을 로컬블록으로 치환하기 전에 적용 DXF에서
                            # 확보한 원래 WCS XCLIP을 핸들 기준으로 강제 사용한다. 블록 치환 후
                            # XClip() 재해석에 의존하지 않아 원본 28개 경계를 그대로 유지한다.
                            child_clip_polygon = verts
                            child_clip_inverted = bool(forced_clip.get("inverted", False))
                            own_xclip = True
                            xclip_source = "forced_wcs"
                            block_diag["xclip_insert_count"] = block_diag.get("xclip_insert_count", 0) + 1
                            block_diag["xclip_forced_count"] = block_diag.get("xclip_forced_count", 0) + 1
                    except Exception as exc:
                        block_diag.setdefault("xclip_errors", []).append(f"{trace_handle} forced: {type(exc).__name__}: {exc}")
                else:
                    try:
                        from ezdxf.xclip import XClip
                        xclip = XClip(entity)
                        if xclip.has_clipping_path and xclip.is_clipping_enabled:
                            cp = xclip.get_wcs_clipping_path()
                            verts = [(float(v.x), float(v.y)) for v in (cp.vertices or [])]
                            if len(verts) >= 3:
                                child_clip_polygon = verts
                                child_clip_inverted = bool(cp.is_inverted_clip)
                                own_xclip = True
                                xclip_source = "entity_wcs"
                                block_diag["xclip_insert_count"] = block_diag.get("xclip_insert_count", 0) + 1
                    except Exception as exc:
                        block_diag.setdefault("xclip_errors", []).append(f"{trace_handle}: {type(exc).__name__}: {exc}")

                # INSERT의 ATTRIB은 블록 정의 밖에 붙어 있으므로 별도로 챙긴다.
                attribs = []
                try:
                    attribs = list(getattr(entity, "attribs", []) or [])
                    block_diag["attrib_count"] += len(attribs)
                    for attrib in attribs:
                        process(attrib, depth+1, rgb, effective_layer, aci, trace_handle, annotation, child_clip_polygon, child_clip_inverted, tuple(block_stack) + (name,), semantic_type)
                except Exception:
                    pass
                for ve in virtual:
                    process(ve,depth+1,rgb,effective_layer,aci,trace_handle,annotation,child_clip_polygon,child_clip_inverted,tuple(block_stack) + (name,),semantic_type)

                after_segments = len(segments) + len(annotation_segments)
                if len(block_diag["details"]) < 1000:
                    block_diag["details"].append({
                        "handle": trace_handle, "name": name, "depth": depth,
                        "layer": effective_layer, "insert": insert_xy,
                        "rotation": float(getattr(entity.dxf, "rotation", 0.0) or 0.0),
                        "xscale": float(getattr(entity.dxf, "xscale", 1.0) or 1.0),
                        "yscale": float(getattr(entity.dxf, "yscale", 1.0) or 1.0),
                        "attribs": len(attribs), "block_def_count": def_count,
                        "block_def_types": def_types, "block_def_error": block_def_error,
                        "method": method, "virtual_count": len(virtual),
                        "virtual_types": child_types,
                        "recursive_error": recursive_error, "fallback_error": fallback_error,
                        "segments_added": max(0, after_segments-before_segments),
                        "texts_added": max(0, len(texts)-before_texts),
                        "fills_added": max(0, len(fills)-before_fills),
                        "xclip_applied": bool(child_clip_polygon),
                        "xclip_own": bool(own_xclip),
                        "xclip_source": xclip_source,
                        "xclip_vertices": len(child_clip_polygon or []),
                        "xclip_inverted": bool(child_clip_inverted),
                        "xclip_bounds": (
                            (min(v[0] for v in child_clip_polygon), min(v[1] for v in child_clip_polygon),
                             max(v[0] for v in child_clip_polygon), max(v[1] for v in child_clip_polygon))
                            if child_clip_polygon else None
                        ),
                        "xclip_segment_input": max(0, block_diag.get("xclip_segment_input", 0)-diag_xclip_in),
                        "xclip_segment_parts": max(0, block_diag.get("xclip_segment_parts", 0)-diag_xclip_parts),
                        "xclip_segment_rejected": max(0, block_diag.get("xclip_segment_rejected", 0)-diag_xclip_rejected),
                        "crop_segment_input": max(0, block_diag.get("crop_segment_input", 0)-diag_crop_in),
                        "crop_segment_output": max(0, block_diag.get("crop_segment_output", 0)-diag_crop_out),
                        "crop_segment_rejected": max(0, block_diag.get("crop_segment_rejected", 0)-diag_crop_rejected),
                    })
            elif et in ("MLINE","DIMENSION","LEADER","MLEADER"):
                try:
                    virtual=list(entity.virtual_entities())
                except Exception:
                    virtual=[]
                if virtual:
                    child_annotation = bool(annotation or et == "DIMENSION")
                    for ve in virtual: process(ve,depth+1,rgb,effective_layer,aci,trace_handle,child_annotation,clip_polygon,clip_inverted,block_stack,("DIMENSION" if et == "DIMENSION" else semantic_type))
                elif et=="MLINE":
                    # ezdxf 버전에 따라 MLINE 가상도형 생성이 실패할 수 있어 중심선이라도 표시한다.
                    try:
                        verts=list(entity.vertices)
                        pts=[]
                        for vertex in verts:
                            loc=getattr(vertex,"location",None)
                            if loc is not None: pts.append((float(loc.x),float(loc.y)))
                        for i in range(len(pts)-1): add_segment(*pts[i],*pts[i+1],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                    except Exception: pass
            elif et=="HATCH":
                # 해치는 우선 경계영역을 항상 보존한다. 패턴선 재현은 하지 않고,
                # SOLID 해치만 화면에서 채울 수 있도록 대표 외곽 폴리곤도 별도 저장한다.
                hatch_polygons = []
                try:
                    from ezdxf.path import from_hatch
                    for hp in from_hatch(entity):
                        pts=[(float(v.x),float(v.y)) for v in hp.flattening(distance=10.0)]
                        if len(pts)>=2:
                            for i in range(len(pts)-1): add_segment(*pts[i],*pts[i+1],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                            if len(pts)>=3 and math.hypot(pts[-1][0]-pts[0][0], pts[-1][1]-pts[0][1])>COORD_TOLERANCE:
                                add_segment(*pts[-1],*pts[0],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                            hatch_polygons.append(pts)
                except Exception:
                    for boundary in entity.paths:
                        if hasattr(boundary,'vertices'):
                            pts=[(float(x),float(y)) for x,y,*_ in boundary.vertices]
                            for i in range(len(pts)-1): add_segment(*pts[i],*pts[i+1],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                            if getattr(boundary,'is_closed',False) and len(pts)>2:add_segment(*pts[-1],*pts[0],rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                            if len(pts)>=3: hatch_polygons.append(pts)
                        elif hasattr(boundary,'edges'):
                            for edge in boundary.edges:
                                try:
                                    add_path(edge,rgb,aci,trace_handle,annotation,clip_polygon,clip_inverted,current_meta)
                                except Exception:
                                    pass
                try:
                    pattern_name = str(getattr(entity.dxf, 'pattern_name', '') or '').upper()
                    is_solid = bool(getattr(entity, 'solid_fill', False) or pattern_name == 'SOLID')
                except Exception:
                    is_solid = False
                if is_solid:
                    for pts in hatch_polygons:
                        if len(pts) < 3:
                            continue
                        for clipped_pts in clip_fill_polygons(pts, clip_polygon, clip_inverted):
                            fills.append({
                                'points': clipped_pts, 'rgb': list(rgb),
                                'aci': aci if aci is not None else -1,
                                'handle': str(trace_handle or ''), 'solid': True,
                                'meta': dict(current_meta),
                            })
        except Exception: pass
    modelspace_type_counts = {}
    try:
        for entity in doc.modelspace():
            et = str(entity.dxftype())
            modelspace_type_counts[et] = modelspace_type_counts.get(et, 0) + 1
            process(entity)
    except Exception:
        # 기존 동작처럼 개별 도형 오류로 전체 배경을 중단하지 않되 진단에 남긴다.
        block_diag["modelspace_error"] = traceback.format_exc()
    return segments,{
        "duplicates_removed":duplicate_count,"degenerate_skipped":skipped_count,
        "texts":texts,"annotation_segments":annotation_segments,"fills":fills,
        "modelspace_type_counts": modelspace_type_counts, "block_diagnostic": block_diag,
    }

# V23.34: 실/벽 연동 레이어도 CAD 설정의 레이어 리스트에서 관리한다.
# 내부 부위키는 고정하고 실제 CAD 레이어명만 설정값을 읽어 사용한다.
ROOM_WALL_LINK_LAYER_KEYS = {
    "벽": "room_wall_link_wall_layer",
    "바닥": "room_wall_link_floor_layer",
    "천정": "room_wall_link_ceiling_layer",
    "기타": "room_wall_link_other_layer",
}
ROOM_WALL_LINK_LAYERS = {
    section: default_layer_name(key) for section, key in ROOM_WALL_LINK_LAYER_KEYS.items()
}

def room_wall_link_layers(rules=None) -> dict[str, str]:
    rules = dict(rules or {})
    result = {}
    for section, key in ROOM_WALL_LINK_LAYER_KEYS.items():
        fallback = default_layer_name(key)
        result[section] = str(rules.get(key, fallback) or fallback).strip() or fallback
    return result

ROOM_WALL_LINK_APPID = "DXF_ESTIMATE_ROOM_WALL_LINK"
ROOM_WALL_LINK_XDATA_VERSION = "V2"


def room_program_id(room) -> str:
    value = str(getattr(room, "room_id", "") or "").strip()
    if value:
        return value
    return make_room_program_id(getattr(room, "floor", ""), getattr(room, "points", []) or [])


def wall_program_id(room_id: str, p1, p2, role: str = "OUTER") -> str:
    """HANDLE/절점 순서와 무관한 실 벽구간 고유 ID."""
    key = segment_key(float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1]), 0.1)
    payload = f"{str(room_id or '').strip()}|{str(role or 'OUTER').upper()}|{key!r}"
    digest = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:20].upper()
    return f"W-{digest}"


def link_object_id(section: str, group_id: str, room_id: str = "", wall_id: str = "") -> str:
    """CAD에 생성되는 프로그램 관리 객체의 고유 ID.

    CAD HANDLE이 달라져도 같은 그룹/실(또는 벽구간)이면 같은 ID를 사용한다.
    """
    payload = "|".join((str(section or ""), str(group_id or ""), str(room_id or ""), str(wall_id or "")))
    digest = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:24].upper()
    return f"O-{digest}"


def room_wall_link_xdata_values(item: dict) -> list[str]:
    return [
        ROOM_WALL_LINK_XDATA_VERSION,
        str(item.get("object_id", "") or ""),
        str(item.get("room_id", "") or ""),
        str(item.get("wall_id", "") or ""),
        str(item.get("group_id", "") or ""),
        str(item.get("group_type", "") or ""),
        str(item.get("group_division", "") or ""),
        str(item.get("group_name", "") or ""),
        str(item.get("section", "") or ""),
        # 아래 2개는 V23.44 이하 프로젝트/진단 호환용 보조값이다. 주 식별자로 사용하지 않는다.
        str(item.get("wall_key", "") or ""),
        str(item.get("room_handle", "") or ""),
    ]


def parse_room_wall_link_xdata_values(values) -> dict:
    strings = [str(value) for value in (values or [])]
    if strings and strings[0] == ROOM_WALL_LINK_XDATA_VERSION:
        keys = ("object_id", "room_id", "wall_id", "group_id", "group_type", "group_division", "group_name", "section", "wall_key", "room_handle")
        payload = strings[1:]
        return {key: (payload[i] if i < len(payload) else "") for i, key in enumerate(keys)}
    # V1: group_id/group_type/group_division/group_name/section/wall_key/room_handle
    keys = ("group_id", "group_type", "group_division", "group_name", "section", "wall_key", "room_handle")
    result = {key: (strings[i] if i < len(strings) else "") for i, key in enumerate(keys)}
    result.update({"object_id": "", "room_id": "", "wall_id": ""})
    return result


def _hex_to_rgb(value: str):
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return (255, 255, 255)
    try:
        return tuple(int(text[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 255, 255)


def _rgb_to_hex(rgb) -> str:
    try:
        r, g, b = [max(0, min(255, int(v))) for v in rgb[:3]]
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return "#FFFFFF"


def _entity_rgb_hex(doc, entity) -> str:
    try:
        true_color = int(getattr(entity.dxf, "true_color", 0) or 0)
    except Exception:
        true_color = 0
    if true_color:
        return _rgb_to_hex(((true_color >> 16) & 255, (true_color >> 8) & 255, true_color & 255))
    try:
        aci = abs(int(getattr(entity.dxf, "color", 256) or 256))
    except Exception:
        aci = 256
    if aci in (0, 256):
        try:
            layer = doc.layers.get(str(getattr(entity.dxf, "layer", "0") or "0"))
            aci = abs(int(getattr(layer, "color", 7) or 7))
        except Exception:
            aci = 7
    try:
        rgb = aci2rgb(max(1, min(255, aci)))
        return _rgb_to_hex((rgb.r, rgb.g, rgb.b))
    except Exception:
        return "#FFFFFF"


def _entity_aci_index(doc, entity) -> int:
    """엔티티의 실제 CAD ACI 번호를 읽는다. TrueColor는 최근접 ACI로 변환한다."""
    try:
        true_color = int(getattr(entity.dxf, "true_color", 0) or 0)
    except Exception:
        true_color = 0
    if true_color:
        rgb_hex = _rgb_to_hex(((true_color >> 16) & 255, (true_color >> 8) & 255, true_color & 255))
        return nearest_aci(rgb_hex)
    try:
        aci = abs(int(getattr(entity.dxf, "color", 256) or 256))
    except Exception:
        aci = 256
    if aci in (0, 256):
        try:
            layer = doc.layers.get(str(getattr(entity.dxf, "layer", "0") or "0"))
            aci = abs(int(getattr(layer, "color", 7) or 7))
        except Exception:
            aci = 7
    return normalize_aci(aci)


def _link_xdata_payload(item: dict):
    return [(1000, value) for value in room_wall_link_xdata_values(item)]


def _parse_link_xdata(entity) -> dict:
    try:
        raw = entity.get_xdata(ROOM_WALL_LINK_APPID)
    except Exception:
        raw = None
    if not raw:
        return {}
    strings = [str(value) for code, value in raw if int(code) == 1000]
    return parse_room_wall_link_xdata_values(strings)


def write_room_wall_link_dxf(source_path, output_path, items, layers=None) -> dict:
    """프로그램의 그룹 형상을 설정된 실/벽 연동 레이어에 작성한다.

    그룹 표시색은 ACI로 보존하고, XData V2의 object_id/room_id/wall_id를 주 식별자로 기록한다.
    HANDLE/구버전 벽키는 진단·호환용 보조값만 유지한다.
    """
    link_layers = dict(layers or ROOM_WALL_LINK_LAYERS)
    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()
    try:
        if ROOM_WALL_LINK_APPID not in doc.appids:
            doc.appids.add(ROOM_WALL_LINK_APPID)
    except Exception:
        pass
    for section, layer_name in link_layers.items():
        if layer_name not in doc.layers:
            doc.layers.add(layer_name, color=7)

    layer_to_section = {name.casefold(): section for section, name in link_layers.items()}
    existing = set()
    existing_by_object_id = {}
    for entity in msp:
        try:
            layer = str(getattr(entity.dxf, "layer", "") or "")
            section = layer_to_section.get(layer.casefold())
            if not section:
                continue
            metadata = _parse_link_xdata(entity)
            object_id = str(metadata.get("object_id", "") or "").strip()
            if object_id:
                existing_by_object_id[object_id] = entity
            aci = _entity_aci_index(doc, entity)
            if entity.dxftype() == "LINE":
                a = (float(entity.dxf.start.x), float(entity.dxf.start.y)); b = (float(entity.dxf.end.x), float(entity.dxf.end.y))
                existing.add((section, aci, "L", segment_key(*a, *b, COORD_TOLERANCE)))
            elif entity.dxftype() in ("LWPOLYLINE", "POLYLINE"):
                pts = entity_points(entity)
                if pts:
                    if section == "벽":
                        closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
                        limit = len(pts) if closed else max(0, len(pts)-1)
                        for i in range(limit):
                            existing.add((section, aci, "L", segment_key(*pts[i], *pts[(i+1) % len(pts)], COORD_TOLERANCE)))
                    else:
                        existing.add((section, aci, "P", polygon_key(pts, COORD_TOLERANCE)))
        except Exception:
            continue

    report = {"requested": 0, "created": 0, "updated": 0, "skipped_existing": 0, "failed": 0, "by_section": defaultdict(int), "layers": dict(link_layers), "details": []}
    for raw in items or []:
        item = dict(raw or {})
        section = str(item.get("section", "") or "")
        layer_name = link_layers.get(section)
        points = [(float(x), float(y)) for x, y in (item.get("points") or [])]
        if not layer_name or len(points) < 2:
            continue
        report["requested"] += 1
        aci = normalize_aci(item.get("aci", nearest_aci(item.get("color", ""))))
        object_id = str(item.get("object_id", "") or "").strip()
        geom_key = (section, aci, "L", segment_key(*points[0], *points[1], COORD_TOLERANCE)) if section == "벽" and len(points) == 2 and not item.get("closed") else (section, aci, "P", polygon_key(points, COORD_TOLERANCE))
        existing_entity = existing_by_object_id.get(object_id) if object_id else None
        if existing_entity is not None:
            try:
                existing_entity.dxf.layer = layer_name
                existing_entity.dxf.color = int(aci)
                try:
                    existing_entity.dxf.discard("true_color")
                except Exception:
                    pass
                if existing_entity.dxftype() == "LINE" and section == "벽" and len(points) == 2 and not item.get("closed"):
                    existing_entity.dxf.start = (points[0][0], points[0][1], 0.0)
                    existing_entity.dxf.end = (points[1][0], points[1][1], 0.0)
                elif existing_entity.dxftype() == "LWPOLYLINE":
                    existing_entity.set_points(points, format="xy")
                    existing_entity.closed = bool(item.get("closed", True))
                try:
                    existing_entity.set_xdata(ROOM_WALL_LINK_APPID, _link_xdata_payload(item))
                except Exception:
                    pass
                report["updated"] += 1
                report["by_section"][section] += 1
                continue
            except Exception as exc:
                if len(report["details"]) < 30:
                    report["details"].append(f"ID 업데이트 실패 {object_id}: {type(exc).__name__}: {exc}")
        if geom_key in existing:
            report["skipped_existing"] += 1
            continue
        try:
            if section == "벽" and len(points) == 2 and not item.get("closed"):
                entity = msp.add_line(points[0], points[1], dxfattribs={"layer": layer_name})
            else:
                entity = msp.add_lwpolyline(points, close=bool(item.get("closed", True)), dxfattribs={"layer": layer_name})
            # 그룹색은 CAD와 1:1 대응하도록 TrueColor가 아니라 ACI 번호로 기록한다.
            entity.dxf.color = int(aci)
            try:
                entity.dxf.discard("true_color")
            except Exception:
                pass
            try:
                entity.set_xdata(ROOM_WALL_LINK_APPID, _link_xdata_payload(item))
            except Exception:
                pass
            existing.add(geom_key)
            if object_id:
                existing_by_object_id[object_id] = entity
            report["created"] += 1
            report["by_section"][section] += 1
        except Exception as exc:
            report["failed"] += 1
            if len(report["details"]) < 30:
                report["details"].append(f"{section}/{item.get('group_name', '')}: {type(exc).__name__}: {exc}")
    doc.saveas(output_path)
    report["by_section"] = dict(report["by_section"])
    return report


def read_room_wall_link_dxf(path, layers=None) -> dict:
    """설정된 실/벽 연동 레이어의 선/폐합선을 읽어 색상/메타데이터와 함께 반환한다."""
    link_layers = dict(layers or ROOM_WALL_LINK_LAYERS)
    doc = ezdxf.readfile(path)
    layer_to_section = {name.casefold(): section for section, name in link_layers.items()}
    items = []
    unsupported = 0
    for entity in doc.modelspace():
        layer = str(getattr(entity.dxf, "layer", "") or "")
        section = layer_to_section.get(layer.casefold())
        if not section:
            continue
        kind = entity.dxftype()
        metadata = _parse_link_xdata(entity)
        color = _entity_rgb_hex(doc, entity)
        aci = _entity_aci_index(doc, entity)
        if kind == "LINE":
            points = [(float(entity.dxf.start.x), float(entity.dxf.start.y)), (float(entity.dxf.end.x), float(entity.dxf.end.y))]
            items.append({"section": section, "points": points, "closed": False, "color": color, "aci": aci, "handle": str(entity.dxf.handle or ""), **metadata})
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            pts = entity_points(entity)
            closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
            if len(pts) >= 2:
                items.append({"section": section, "points": [(float(x), float(y)) for x, y in pts], "closed": closed, "color": color, "aci": aci, "handle": str(entity.dxf.handle or ""), **metadata})
        else:
            unsupported += 1
    return {"items": items, "count": len(items), "unsupported": unsupported, "layers": dict(link_layers)}


def _room_wall_segments(rooms):
    """현재 실구획선의 1:1 벽구간 목록.

    key/room_handle은 구버전 호환용이고, 실제 연동 식별은 room_id/wall_id를 우선한다.
    """
    result = []
    for room in rooms or []:
        handle = str(getattr(room, "boundary_handle", "") or "")
        rid = room_program_id(room)
        pts = clean_points(getattr(room, "points", []) or [])
        for i in range(len(pts)):
            if len(pts) < 2:
                break
            p1, p2 = pts[i], pts[(i + 1) % len(pts)]
            if math.hypot(p2[0]-p1[0], p2[1]-p1[1]) <= 1e-9:
                continue
            result.append({
                "key": f"{handle}:{i}", "room_handle": handle, "room_id": rid,
                "wall_id": wall_program_id(rid, p1, p2, "OUTER"),
                "room": room, "points": (p1, p2), "role": "OUTER",
            })
        for di, raw in enumerate(getattr(room, "deduction_points", []) or []):
            dpts = clean_points(raw or [])
            for i in range(len(dpts)):
                if len(dpts) < 2:
                    break
                p1, p2 = dpts[i], dpts[(i + 1) % len(dpts)]
                if math.hypot(p2[0]-p1[0], p2[1]-p1[1]) <= 1e-9:
                    continue
                result.append({
                    "key": f"{handle}:d{di}:{i}", "room_handle": handle, "room_id": rid,
                    "wall_id": wall_program_id(rid, p1, p2, "DEDUCTION"),
                    "room": room, "points": (p1, p2), "role": "DEDUCTION",
                })
    return result

def _point_distance(a, b):
    return math.hypot(float(a[0])-float(b[0]), float(a[1])-float(b[1]))


def _segments_equal(a1, a2, b1, b2, tolerance: float) -> bool:
    return (
        _point_distance(a1, b1) <= tolerance and _point_distance(a2, b2) <= tolerance
    ) or (
        _point_distance(a1, b2) <= tolerance and _point_distance(a2, b1) <= tolerance
    )


def _segment_overlap_length(a1, a2, b1, b2, tolerance: float) -> float:
    """두 선이 거의 같은 직선상일 때 겹치는 길이(mm). 아니면 0."""
    ax, ay = float(a1[0]), float(a1[1]); bx, by = float(a2[0]), float(a2[1])
    cx, cy = float(b1[0]), float(b1[1]); dx, dy = float(b2[0]), float(b2[1])
    vx, vy = bx-ax, by-ay
    length = math.hypot(vx, vy)
    if length <= tolerance:
        return 0.0
    # 두 점의 기준직선 수직거리
    def line_dist(px, py):
        return abs(vx*(ay-py) - (ax-px)*vy) / length
    if line_dist(cx, cy) > tolerance or line_dist(dx, dy) > tolerance:
        return 0.0
    ux, uy = vx/length, vy/length
    c = (cx-ax)*ux + (cy-ay)*uy
    d = (dx-ax)*ux + (dy-ay)*uy
    low, high = sorted((c, d))
    return max(0.0, min(length, high) - max(0.0, low))


def match_room_wall_link_items(items, rooms, tolerance: float = 2.0) -> dict:
    """연동 객체를 현재 실구획에 매칭한다.

    우선순위: 프로그램 room_id/wall_id -> 구버전 HANDLE/벽키 -> 절점/길이 형상검증.
    어떤 경우에도 전체 CAD를 다시 훑지 않고 전달된 연동 레이어 객체와 현재 도곽 실목록만 사용한다.
    """
    canonical_walls = _room_wall_segments(rooms)
    room_by_id = {room_program_id(r): r for r in rooms or [] if room_program_id(r)}
    room_by_handle = {str(getattr(r, "boundary_handle", "") or ""): r for r in rooms or []}
    wall_by_id = {str(entry.get("wall_id", "") or ""): entry for entry in canonical_walls if entry.get("wall_id")}
    wall_by_legacy_key = {str(entry.get("key", "") or ""): entry for entry in canonical_walls if entry.get("key")}
    wall_index = defaultdict(list)
    for entry in canonical_walls:
        p1, p2 = entry["points"]
        wall_index[segment_key(*p1, *p2, tolerance)].append(entry)

    polygon_index = defaultdict(list)
    for room in rooms or []:
        pts = clean_points(getattr(room, "points", []) or [], tolerance)
        if pts:
            polygon_index[polygon_key(pts, tolerance)].append(room)

    matched = []
    summary = {"id": 0, "legacy_metadata": 0, "exact": 0, "metadata": 0, "shared_wall": 0, "unmatched": 0, "polygon_unmatched": 0}
    for raw in items or []:
        item = dict(raw or {})
        section = str(item.get("section", "") or "")
        points = [(float(x), float(y)) for x, y in (item.get("points") or [])]
        item["matched_wall_keys"] = []
        item["matched_wall_ids"] = []
        item["matched_room_handles"] = []
        item["matched_room_ids"] = []
        item["match_issue"] = ""
        if section == "벽":
            segments = []
            if len(points) == 2 and not item.get("closed"):
                segments = [(points[0], points[1])]
            elif len(points) >= 2:
                limit = len(points) if item.get("closed") else len(points)-1
                segments = [(points[i], points[(i+1) % len(points)]) for i in range(max(0, limit))]
            all_entries = []
            issue = ""
            for p1, p2 in segments:
                # 1) 프로그램 벽 ID 우선
                wall_ids = [value for value in str(item.get("wall_id", "") or "").split("|") if value]
                id_entries = []
                id_shape_mismatch = False
                for wid in wall_ids:
                    entry = wall_by_id.get(wid)
                    if entry and _segments_equal(p1, p2, entry["points"][0], entry["points"][1], tolerance):
                        id_entries.append(entry)
                    elif entry:
                        id_shape_mismatch = True
                if id_entries:
                    all_entries.extend(id_entries)
                    summary["id"] += len(id_entries)
                    summary["metadata"] += len(id_entries)
                    if len(id_entries) > 1:
                        summary["shared_wall"] += 1
                    continue
                if id_shape_mismatch:
                    issue = "프로그램 벽 ID는 일치하지만 절점/길이가 변경됨"
                # 2) V23.44 이하 벽키 보조 호환. V23.45 복수면 | 구분.
                meta_keys = [value for value in str(item.get("wall_key", "") or "").split("|") if value]
                legacy_entries = []
                for meta_key in meta_keys:
                    entry = wall_by_legacy_key.get(meta_key)
                    if entry and _segments_equal(p1, p2, entry["points"][0], entry["points"][1], tolerance):
                        legacy_entries.append(entry)
                if legacy_entries:
                    all_entries.extend(legacy_entries)
                    summary["legacy_metadata"] += len(legacy_entries)
                    summary["metadata"] += len(legacy_entries)
                    if len(legacy_entries) > 1:
                        summary["shared_wall"] += 1
                    continue
                # 3) 최종 검증/복구는 형상 1:1
                candidates = wall_index.get(segment_key(*p1, *p2, tolerance), [])
                candidates = [e for e in candidates if _segments_equal(p1, p2, e["points"][0], e["points"][1], tolerance)]
                if candidates:
                    all_entries.extend(candidates)
                    summary["exact"] += 1
                    if len(candidates) > 1:
                        summary["shared_wall"] += 1
                else:
                    issue = issue or "실구획선과 절점/길이 1:1 불일치"
            # 동일 벽을 여러 경로로 찾았을 수 있으므로 key 기준 중복 제거
            unique = {str(e.get("key", "")): e for e in all_entries if e.get("key")}
            item["matched_wall_keys"] = sorted(unique)
            item["matched_wall_ids"] = sorted({str(e.get("wall_id", "")) for e in unique.values() if e.get("wall_id")})
            item["matched_room_ids"] = sorted({str(e.get("room_id", "")) for e in unique.values() if e.get("room_id")})
            if not item["matched_wall_keys"]:
                item["match_issue"] = issue or "실구획선 미대응"
                summary["unmatched"] += 1
        else:
            # 1) 프로그램 실 ID 우선
            rid = str(item.get("room_id", "") or "").strip()
            room = room_by_id.get(rid) if rid else None
            if room is not None:
                target_key = polygon_key(clean_points(getattr(room, "points", []) or [], tolerance), tolerance)
                if points and polygon_key(points, tolerance) == target_key:
                    item["matched_room_ids"] = [rid]
                    handle = str(getattr(room, "boundary_handle", "") or "")
                    item["matched_room_handles"] = [handle] if handle else []
                    summary["id"] += 1
                    summary["metadata"] += 1
                else:
                    item["match_issue"] = "프로그램 실 ID는 일치하지만 폐합형상이 변경됨"
            # 2) V23.44 이하 실 HANDLE 보조 호환
            if not item["matched_room_handles"]:
                meta_handle = str(item.get("room_handle", "") or "")
                room = room_by_handle.get(meta_handle) if meta_handle else None
                if room is not None:
                    if polygon_key(points, tolerance) == polygon_key(clean_points(getattr(room, "points", []) or [], tolerance), tolerance):
                        item["matched_room_handles"] = [meta_handle]
                        item["matched_room_ids"] = [room_program_id(room)]
                        summary["legacy_metadata"] += 1
                        summary["metadata"] += 1
            # 3) 형상 1:1
            if not item["matched_room_handles"] and points:
                candidates = polygon_index.get(polygon_key(points, tolerance), [])
                item["matched_room_handles"] = sorted({str(getattr(r, "boundary_handle", "") or "") for r in candidates if getattr(r, "boundary_handle", "")})
                item["matched_room_ids"] = sorted({room_program_id(r) for r in candidates if room_program_id(r)})
                if candidates:
                    summary["exact"] += 1
                    item["match_issue"] = ""
            if not item["matched_room_handles"]:
                item["match_issue"] = item["match_issue"] or "실구획선 폐합형상과 1:1 불일치"
                summary["polygon_unmatched"] += 1
        matched.append(item)
    return {"items": matched, "summary": summary}

def validate_room_wall_link_items(items, rooms, tolerance: float = 2.0) -> dict:
    """실/벽 연동 객체의 1:1 매칭, 완전중복, 부분중복을 검사한다."""
    matched = match_room_wall_link_items(items, rooms, tolerance)
    wall_segments = []
    for item_index, item in enumerate(matched["items"]):
        if str(item.get("section", "")) != "벽":
            continue
        pts = item.get("points") or []
        if len(pts) == 2 and not item.get("closed"):
            wall_segments.append((item_index, pts[0], pts[1], str(item.get("handle", "") or "")))
        elif len(pts) >= 2:
            limit = len(pts) if item.get("closed") else len(pts)-1
            for i in range(max(0, limit)):
                wall_segments.append((item_index, pts[i], pts[(i+1) % len(pts)], str(item.get("handle", "") or "")))

    issues = []
    exact_duplicate_pairs = 0
    partial_overlap_pairs = 0
    seen_pairs = set()
    buckets = defaultdict(list)
    for idx, p1, p2, handle in wall_segments:
        buckets[segment_key(*p1, *p2, tolerance)].append((idx, p1, p2, handle))
    for values in buckets.values():
        for i in range(len(values)):
            for j in range(i+1, len(values)):
                a, b = values[i], values[j]
                if _segments_equal(a[1], a[2], b[1], b[2], tolerance):
                    pair = tuple(sorted((a[0], b[0])))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        exact_duplicate_pairs += 1
                        issues.append(f"벽 연동선 완전중복: {a[3] or a[0]} ↔ {b[3] or b[0]}")

    # 완전중복이 아닌 동일직선 부분겹침도 찾는다.
    for i in range(len(wall_segments)):
        ai, a1, a2, ah = wall_segments[i]
        alen = _point_distance(a1, a2)
        if alen <= tolerance:
            issues.append(f"벽 연동선 길이 0/극소: {ah or ai}")
            continue
        for j in range(i+1, len(wall_segments)):
            bi, b1, b2, bh = wall_segments[j]
            pair = tuple(sorted((ai, bi)))
            if pair in seen_pairs:
                continue
            overlap = _segment_overlap_length(a1, a2, b1, b2, tolerance)
            if overlap > tolerance:
                blen = _point_distance(b1, b2)
                if overlap < max(alen, blen) - tolerance:
                    partial_overlap_pairs += 1
                    seen_pairs.add(pair)
                    issues.append(f"벽 연동선 부분중복: {ah or ai} ↔ {bh or bi} / 겹침 {overlap:.1f}mm")

    unmatched = 0
    for item in matched["items"]:
        if item.get("match_issue"):
            unmatched += 1
            issues.append(f"{item.get('section', '')} {item.get('handle', '') or '?'}: {item.get('match_issue')}")

    return {
        "items": matched["items"],
        "summary": matched["summary"],
        "issue_count": len(issues),
        "issues": issues,
        "exact_duplicate_pairs": exact_duplicate_pairs,
        "partial_overlap_pairs": partial_overlap_pairs,
        "unmatched": unmatched,
        "tolerance": float(tolerance),
    }
