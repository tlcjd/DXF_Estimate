from __future__ import annotations

import hashlib
import math
import re
import pickle
from pathlib import Path

from app.rooms.background_source_cache import build_floor_vectors, cache_file_for_source

PROGRAM_CACHE_VERSION = 7
MERGE_TOLERANCE = 0.5  # CAD mm 기준. 중복/연속선 정리 허용오차.
GROUP_KEYS = ("LINE", "WINDOW", "BLOCK", "DIMENSION", "TEXT", "HATCH")
MISSING_LAYER = "(레이어 정보 없음)"


def _program_cache_file(cache_root: Path, source_path: str) -> Path:
    resolved = str(Path(source_path).resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    stem = Path(source_path).stem or "cad"
    return Path(cache_root) / f"{stem}_{digest}" / "program_vector.pkl"


def program_cache_file(cache_root: Path, source_path: str) -> Path:
    return _program_cache_file(Path(cache_root), source_path)


def _reference_signature(cache_root: Path, source_path: str) -> dict:
    path = cache_file_for_source(Path(cache_root), source_path)
    try:
        stat = path.stat()
        return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
    except Exception:
        return {"size": -1, "mtime_ns": -1}


def clear_background_program_cache(cache_root: Path, source_path: str) -> None:
    try:
        _program_cache_file(Path(cache_root), source_path).unlink(missing_ok=True)
    except Exception:
        pass


def load_background_program_cache(cache_root: Path, source_path: str):
    path = _program_cache_file(Path(cache_root), source_path)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fp:
            payload = pickle.load(fp)
        cache_version = int(payload.get("version", 0) or 0)
        if cache_version not in (2, 3, 4, 5, 6, PROGRAM_CACHE_VERSION):
            return None
        old = dict(payload.get("reference_signature") or {})
        now = _reference_signature(Path(cache_root), source_path)
        payload = dict(payload)
        payload["legacy_program_cache"] = bool(cache_version < PROGRAM_CACHE_VERSION)
        # 3단 최적화 도면은 2단 원본 정리 캐시가 새로 생성돼도 사용자가
        # [최적화 도면 생성]을 누르기 전까지 기존 결과를 백업/표시용으로 유지한다.
        # 서명이 다르면 폐기하지 않고 stale 상태만 표시한다.
        payload["source_outdated"] = bool(
            int(old.get("size", -1)) != int(now.get("size", -2))
            or int(old.get("mtime_ns", -1)) != int(now.get("mtime_ns", -2))
        )
        return payload
    except Exception:
        return None


def save_background_program_cache(cache_root: Path, source_path: str, payload: dict) -> Path:
    path = _program_cache_file(Path(cache_root), source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(payload or {})
    data["version"] = PROGRAM_CACHE_VERSION
    data["reference_signature"] = _reference_signature(Path(cache_root), source_path)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fp:
        pickle.dump(data, fp, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def normalize_group_settings(values: dict | None = None) -> dict:
    source = dict(values or {})
    result = {}
    for key in GROUP_KEYS:
        raw = dict(source.get(key) or {})
        try:
            offset = int(raw.get("opacity_offset", 0) or 0)
        except Exception:
            offset = 0
        result[key] = {
            "visible": bool(raw.get("visible", True)),
            "opacity_offset": max(-100, min(100, offset)),
        }
    return result


def _segment_meta(segment, fallback_type="LINE") -> dict:
    try:
        if len(segment) >= 10 and isinstance(segment[9], dict):
            meta = dict(segment[9])
        else:
            meta = {}
    except Exception:
        meta = {}
    meta.setdefault("layer", MISSING_LAYER)
    if str(meta.get("layer", "") or "").strip() in ("", "(기존 캐시)"):
        meta["layer"] = MISSING_LAYER
    meta.setdefault("entity_type", fallback_type)
    meta.setdefault("source_kind", "DIRECT")
    meta.setdefault("block_name", "")
    meta.setdefault("block_path", "")
    meta.setdefault("xref_name", "")
    return meta


def _item_meta(item: dict, fallback_type: str) -> dict:
    meta = dict(item.get("meta") or {})
    meta.setdefault("layer", str(item.get("layer", "") or MISSING_LAYER))
    if str(meta.get("layer", "") or "").strip() in ("", "(기존 캐시)"):
        meta["layer"] = MISSING_LAYER
    meta.setdefault("entity_type", str(item.get("entity_type", "") or fallback_type))
    meta.setdefault("source_kind", str(item.get("source_kind", "") or "DIRECT"))
    meta.setdefault("block_name", str(item.get("block_name", "") or ""))
    meta.setdefault("block_path", str(item.get("block_path", "") or ""))
    meta.setdefault("xref_name", str(item.get("xref_name", "") or ""))
    return meta


def _block_parts(meta: dict) -> list[str]:
    path = str(meta.get("block_path", "") or "").strip()
    block = str(meta.get("block_name", "") or "").strip()
    parts = [part.strip() for part in path.split(">") if part.strip()]
    if not parts and block:
        parts = [block]
    return list(dict.fromkeys(parts))


_DOOR_BLOCK_RE = re.compile(r'(DOOR|출입문|(?:^|[^A-Z])(SD|FD|DD|PD|D)[-_]?\d)', re.I)
_WINDOW_BLOCK_RE = re.compile(r'(WINDOW|창호|창문|(?:^|[^A-Z])(AW|CW|PW|SW|W)[-_]?\d)', re.I)

def _opening_block_names(source_cache: dict | None) -> set[str]:
    """기존 창호 읽기 결과에서 창/문으로 판정된 실제 블록명을 모은다."""
    names = set()
    for _floor, info in dict((source_cache or {}).get("floors") or {}).items():
        for candidate in list(dict(info or {}).get("opening_candidates") or []):
            name = str(dict(candidate or {}).get("block_name", "") or "").strip()
            if name:
                names.add(name.casefold())
    return names

def _opening_layer_names(source_cache: dict | None, opening_blocks: set[str] | None = None) -> set[str]:
    """창호 후보 블록의 내부 구성 객체 레이어를 최우선으로 모은다.

    창호 검출 단계가 저장한 component_layers를 직접 사용한다. 따라서 INSERT 자체의
    레이어가 제각각이어도 90/180도 ARC 블록 또는 기존 창호 판정 블록 내부에서 실제로
    사용한 선/호/문자/중첩블록 레이어 전체가 창호 구성 레이어가 된다.
    이전 캐시에는 component_layers가 없을 수 있으므로 block_path 기반 수집도 fallback으로 유지한다.
    """
    known = set(opening_blocks or _opening_block_names(source_cache))
    layers: set[str] = set()

    # V24.35: 창호 검출 시 블록 정의 내부를 재귀 순회해 저장한 실제 구성 레이어 사용.
    for _floor, info in dict((source_cache or {}).get("floors") or {}).items():
        for candidate in list(dict(info or {}).get("opening_candidates") or []):
            cand = dict(candidate or {})
            name = str(cand.get("block_name", "") or "").strip().casefold()
            if known and name and name not in known:
                continue
            for layer in cand.get("component_layers") or []:
                text = str(layer or "").strip()
                if text and text != MISSING_LAYER and text != "0":
                    layers.add(text.casefold())

    # 구버전 2단 캐시/후보 데이터 호환 fallback: 펼쳐진 객체의 block_path에서 수집.
    if known:
        def collect(meta: dict):
            parts = {str(v).casefold() for v in _block_parts(meta)}
            if parts.intersection(known):
                layer = str(meta.get("layer", "") or "").strip()
                if layer and layer != MISSING_LAYER and layer != "0":
                    layers.add(layer.casefold())

        for seg in (source_cache or {}).get("segments") or []:
            collect(_segment_meta(seg, "LINE"))
        for seg in (source_cache or {}).get("annotation_segments") or []:
            collect(_segment_meta(seg, "DIMENSION"))
        for item in (source_cache or {}).get("texts") or []:
            collect(_item_meta(dict(item or {}), "TEXT"))
        for item in (source_cache or {}).get("fills") or []:
            collect(_item_meta(dict(item or {}), "HATCH"))
    return layers


def _is_window_meta(meta: dict, opening_blocks: set[str] | None = None) -> bool:
    parts = _block_parts(meta)
    known = set(opening_blocks or set())
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        if text.casefold() in known:
            return True
        # 블록명이 명시적으로 창/문 규칙에 맞는 경우에는 캐시 재생성 전에도 창호로 분류한다.
        if _DOOR_BLOCK_RE.search(text) or _WINDOW_BLOCK_RE.search(text):
            return True
    return False

def _group_for(meta: dict, item_kind: str, opening_blocks: set[str] | None = None, opening_layers: set[str] | None = None) -> str:
    layer_name = str(meta.get("layer", "") or "").strip().upper()
    # 창호 판정이 최우선이다. 기존 창호 읽기에서 잡힌 블록(ARC 90/180 포함)과
    # 그 블록이 사용하는 레이어는 객체 형식/TEXT 레이어 여부와 무관하게 전부 창호 그룹으로 묶는다.
    if _is_window_meta(meta, opening_blocks):
        return "WINDOW"
    if layer_name.casefold() in set(opening_layers or set()):
        return "WINDOW"
    # 창호 구성레이어가 아닌 TEXT 계열 레이어는 실제 객체 형식이 선이어도 문자 그룹으로 분류한다.
    if "TEXT" in layer_name:
        return "TEXT"
    if _block_parts(meta) or str(meta.get("source_kind", "") or "").upper() == "BLOCK":
        return "BLOCK"
    etype = str(meta.get("entity_type", "") or "").upper()
    if etype == "DIMENSION":
        return "DIMENSION"
    if item_kind == "text":
        return "TEXT"
    if item_kind == "fill" or etype == "HATCH":
        return "HATCH"
    return "LINE"


def _color_signature(rgb=None, aci=-1):
    try:
        rgb_values = tuple(max(0, min(255, int(v))) for v in (rgb or ())[:3])
    except Exception:
        rgb_values = ()
    try:
        aci_value = int(aci)
    except Exception:
        aci_value = -1
    if len(rgb_values) != 3:
        rgb_values = None
    return rgb_values, aci_value


def _segment_color(segment):
    try:
        rgb = tuple(int(v) for v in segment[4:7]) if len(segment) >= 7 else None
    except Exception:
        rgb = None
    try:
        aci = int(segment[7]) if len(segment) >= 8 else -1
    except Exception:
        aci = -1
    return _color_signature(rgb, aci)


def _item_color(item: dict):
    return _color_signature(item.get("rgb"), item.get("aci", -1))


def _finalize_catalog_detail(bucket: dict) -> dict:
    colors = dict(bucket.get("colors") or {})
    if not colors:
        return {"count": int(bucket.get("count", 0) or 0), "rgb": None, "aci": -1, "mixed": False}
    dominant, _dominant_count = max(colors.items(), key=lambda kv: kv[1])
    rgb, aci = dominant
    return {
        "count": int(bucket.get("count", 0) or 0),
        "rgb": list(rgb) if isinstance(rgb, tuple) and len(rgb) == 3 else None,
        "aci": int(aci),
        "mixed": len(colors) > 1,
    }


def build_filter_catalog(source_cache: dict) -> dict:
    """2단 원본 정리 캐시에서 실제 레이어/내부 블록/색상과 최종 그룹 수를 집계한다."""
    catalog = {"layer": {}, "block": {}, "group": {key: 0 for key in GROUP_KEYS}}
    opening_blocks = _opening_block_names(source_cache)
    opening_layers = _opening_layer_names(source_cache, opening_blocks)
    # V24.37: 창호 관련 레이어는 최적화 전체 객체에 적용하는 전역 집합이다.
    # 한 번 창호 구성 레이어로 판정되면 블록 내부/외부, 객체 종류와 관계없이 모두 WINDOW 그룹으로 보낸다.
    opening_layers = {str(v or '').strip().casefold() for v in opening_layers if str(v or '').strip()}
    detail_buckets = {"layer": {}, "block": {}}
    metadata_ready = False

    def add(meta: dict, item_kind: str, color=None):
        nonlocal metadata_ready
        layer = str(meta.get("layer", "") or MISSING_LAYER)
        parts = _block_parts(meta)
        if layer != MISSING_LAYER or parts or str(meta.get("xref_name", "") or "").strip():
            metadata_ready = True
        catalog["layer"][layer] = catalog["layer"].get(layer, 0) + 1
        layer_bucket = detail_buckets["layer"].setdefault(layer, {"count": 0, "colors": {}})
        layer_bucket["count"] += 1
        if color is not None:
            layer_bucket["colors"][color] = layer_bucket["colors"].get(color, 0) + 1
        for block_part in parts:
            catalog["block"][block_part] = catalog["block"].get(block_part, 0) + 1
            block_bucket = detail_buckets["block"].setdefault(block_part, {"count": 0, "colors": {}})
            block_bucket["count"] += 1
            if color is not None:
                block_bucket["colors"][color] = block_bucket["colors"].get(color, 0) + 1
        group = _group_for(meta, item_kind, opening_blocks, opening_layers)
        catalog["group"][group] = catalog["group"].get(group, 0) + 1

    for seg in source_cache.get("segments") or []:
        add(_segment_meta(seg, "LINE"), "segment", _segment_color(seg))
    for seg in source_cache.get("annotation_segments") or []:
        add(_segment_meta(seg, "DIMENSION"), "annotation", _segment_color(seg))
    for text in source_cache.get("texts") or []:
        add(_item_meta(text, "TEXT"), "text", _item_color(text))
    for fill in source_cache.get("fills") or []:
        add(_item_meta(fill, "HATCH"), "fill", _item_color(fill))

    details = {
        category: {name: _finalize_catalog_detail(bucket) for name, bucket in buckets.items()}
        for category, buckets in detail_buckets.items()
    }
    return {**catalog, "details": details, "metadata_ready": metadata_ready}

def default_selection(catalog: dict) -> dict:
    return {
        "layers": sorted(catalog.get("layer", {}).keys(), key=str.casefold),
        "blocks": sorted(catalog.get("block", {}).keys(), key=str.casefold),
    }


def _selected(meta: dict, selection: dict) -> bool:
    layer = str(meta.get("layer", "") or MISSING_LAYER)
    if "layers" in selection and layer not in set(selection.get("layers") or []):
        return False
    if "types" in selection:
        etype = str(meta.get("entity_type", "") or "기타")
        if etype not in set(selection.get("types") or []):
            return False
    if "blocks" in selection:
        selected_blocks = set(selection.get("blocks") or [])
        # 내부 BLOCK 경로의 어느 단계든 꺼져 있으면 그 하위 geometry 전체를 제외한다.
        for part in _block_parts(meta):
            if part not in selected_blocks:
                return False
    return True


def _style_key(seg):
    rgb = tuple(int(v) for v in (seg[4:7] if len(seg) >= 7 else (170, 170, 170)))
    try:
        aci = int(seg[7]) if len(seg) >= 8 else -1
    except Exception:
        aci = -1
    return rgb, aci


def _quant(value: float, tol: float) -> int:
    return int(round(float(value) / max(tol, 1e-9)))


def _dedupe_segments(segments, tol: float = MERGE_TOLERANCE):
    result = []
    seen = set()
    removed = 0
    for seg in segments or []:
        try:
            x1, y1, x2, y2 = [float(v) for v in seg[:4]]
            a = (_quant(x1, tol), _quant(y1, tol))
            b = (_quant(x2, tol), _quant(y2, tol))
            if b < a:
                a, b = b, a
            key = (a, b, _style_key(seg))
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            result.append(seg)
        except Exception:
            result.append(seg)
    return result, removed


def _semantic_key(seg):
    """최적화 때 서로 의미가 다른 레이어/블록 선을 무리하게 이어붙이지 않기 위한 키."""
    meta = _segment_meta(seg, "LINE")
    return (
        str(meta.get("layer", "") or MISSING_LAYER).casefold(),
        str(meta.get("block_path", "") or "").casefold(),
        str(meta.get("xref_name", "") or "").casefold(),
    )


def _merge_collinear_segments(segments, tol: float = MERGE_TOLERANCE):
    """같은 의미/색/ACI이면서 동일 직선 위에 겹치거나 이어지는 선을 하나로 합친다."""
    groups = {}
    passthrough = []
    angle_tol = 1e-6
    for seg in segments or []:
        try:
            x1, y1, x2, y2 = [float(v) for v in seg[:4]]
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length <= tol * 0.1:
                continue
            ux, uy = dx / length, dy / length
            if ux < -angle_tol or (abs(ux) <= angle_tol and uy < 0):
                ux, uy = -ux, -uy
            dir_key = (round(ux, 6), round(uy, 6))
            nx, ny = -uy, ux
            offset = x1 * nx + y1 * ny
            key = (_semantic_key(seg), _style_key(seg), dir_key, _quant(offset, tol))
            t1 = x1 * ux + y1 * uy
            t2 = x2 * ux + y2 * uy
            if t2 < t1:
                t1, t2 = t2, t1
            groups.setdefault(key, []).append((t1, t2, ux, uy, offset, seg))
        except Exception:
            passthrough.append(seg)

    result = list(passthrough)
    merged_count = 0
    for entries in groups.values():
        entries.sort(key=lambda v: v[0])
        current = None
        for entry in entries:
            t1, t2, ux, uy, offset, seg = entry
            if current is None:
                current = [t1, t2, ux, uy, offset, seg]
                continue
            if t1 <= current[1] + tol:
                current[1] = max(current[1], t2)
                merged_count += 1
            else:
                result.append(_merged_segment_tuple(current))
                current = [t1, t2, ux, uy, offset, seg]
        if current is not None:
            result.append(_merged_segment_tuple(current))
    return result, merged_count


def _merged_segment_tuple(state):
    t1, t2, ux, uy, offset, template = state
    nx, ny = -uy, ux
    x1, y1 = ux * t1 + nx * offset, uy * t1 + ny * offset
    x2, y2 = ux * t2 + nx * offset, uy * t2 + ny * offset
    # meta까지 유지해야 폴리선 재구성 시 레이어/블록 경계를 안전하게 구분할 수 있다.
    tail = tuple(template[4:])
    return (float(x1), float(y1), float(x2), float(y2), *tail)



def _polyline_style(seg):
    rgb, aci = _style_key(seg)
    return tuple(rgb), int(aci)


def _rebuild_polylines(segments, tol: float = MERGE_TOLERANCE):
    """분기점에서는 끊고, degree=2로 이어지는 선 사슬만 프로그램용 POLYLINE으로 재구성한다.

    - 끝점은 tol 격자에서 스냅/평균해 미세한 CAD 오차를 제거한다.
    - 레이어/블록 출처와 표시 스타일이 다른 선은 서로 연결하지 않는다.
    - T/십자 분기(degree != 2)는 경계로 취급해 잘못된 폴리선 연결을 방지한다.
    """
    buckets = {}
    for seg in segments or []:
        try:
            x1, y1, x2, y2 = [float(v) for v in seg[:4]]
            if math.hypot(x2 - x1, y2 - y1) <= max(tol, 1e-9) * 0.05:
                continue
            key = (_semantic_key(seg), _style_key(seg))
            buckets.setdefault(key, []).append(seg)
        except Exception:
            continue

    result = []
    source_count = 0
    for (_semantic, _style), bucket in buckets.items():
        if not bucket:
            continue
        node_points = {}
        node_counts = {}
        edges = []
        adjacency = {}

        def node_key(x, y):
            return (_quant(x, tol), _quant(y, tol))

        def add_node(key, x, y):
            sx, sy = node_points.get(key, (0.0, 0.0))
            count = node_counts.get(key, 0) + 1
            node_points[key] = (sx + x, sy + y)
            node_counts[key] = count

        for seg in bucket:
            try:
                x1, y1, x2, y2 = [float(v) for v in seg[:4]]
            except Exception:
                continue
            a = node_key(x1, y1); b = node_key(x2, y2)
            if a == b:
                continue
            add_node(a, x1, y1); add_node(b, x2, y2)
            edge_id = len(edges)
            edges.append((a, b, seg))
            adjacency.setdefault(a, []).append(edge_id)
            adjacency.setdefault(b, []).append(edge_id)

        if not edges:
            continue
        source_count += len(edges)

        canonical = {
            key: (node_points[key][0] / node_counts[key], node_points[key][1] / node_counts[key])
            for key in node_points
        }
        visited = set()

        def walk(start_node, first_edge):
            points = [canonical[start_node]]
            edge_ids = []
            current_node = start_node
            edge_id = first_edge
            while edge_id not in visited:
                visited.add(edge_id)
                edge_ids.append(edge_id)
                a, b, _seg = edges[edge_id]
                next_node = b if current_node == a else a
                points.append(canonical[next_node])
                if next_node == start_node and len(points) > 2:
                    break
                candidates = [eid for eid in adjacency.get(next_node, []) if eid not in visited]
                if len(adjacency.get(next_node, [])) != 2 or not candidates:
                    break
                current_node = next_node
                edge_id = candidates[0]
            return points, edge_ids

        chains = []
        # 끝점/분기점부터 우선 시작한다.
        for node, edge_ids in adjacency.items():
            if len(edge_ids) == 2:
                continue
            for edge_id in edge_ids:
                if edge_id in visited:
                    continue
                chains.append(walk(node, edge_id))

        # degree=2만 있는 폐합 루프 처리.
        for edge_id, (a, _b, _seg) in enumerate(edges):
            if edge_id in visited:
                continue
            chains.append(walk(a, edge_id))

        for points, edge_ids in chains:
            if len(points) < 2 or not edge_ids:
                continue
            template = edges[edge_ids[0]][2]
            rgb, aci = _polyline_style(template)
            meta = _segment_meta(template, "LINE")
            closed = len(points) >= 4 and math.hypot(points[0][0]-points[-1][0], points[0][1]-points[-1][1]) <= max(tol, 1e-9)
            if closed:
                points[-1] = points[0]
            result.append({
                "points": [(float(x), float(y)) for x, y in points],
                "closed": bool(closed),
                "rgb": tuple(int(v) for v in rgb[:3]),
                "aci": int(aci),
                "source_segments": len(edge_ids),
                "layer": str(meta.get("layer", "") or MISSING_LAYER),
                "block_name": str(meta.get("block_name", "") or ""),
                "block_path": str(meta.get("block_path", "") or ""),
                "xref_name": str(meta.get("xref_name", "") or ""),
                "entity_type": str(meta.get("entity_type", "") or "LINE"),
            })

    return result, source_count


def _polyline_to_segments(polyline: dict):
    pts = [(float(x), float(y)) for x, y in (polyline.get("points") or [])]
    if len(pts) < 2:
        return []
    rgb = tuple(polyline.get("rgb") or (170, 170, 170))
    aci = int(polyline.get("aci", -1) or -1)
    out = []
    for i in range(len(pts) - 1):
        out.append((pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], *rgb[:3], aci, ""))
    return out

def _clean_segment(seg):
    return tuple(seg[:9])


def _clean_item(raw):
    item = dict(raw)
    meta = dict(item.pop("meta", None) or {})
    # 3단 최적화에서도 레이어/블록 표시 필터를 다시 적용할 수 있도록
    # 렌더에 필요한 최소 메타는 최상위 필드로 보존한다.
    item.setdefault("layer", str(meta.get("layer", "") or MISSING_LAYER))
    item.setdefault("block_name", str(meta.get("block_name", "") or ""))
    item.setdefault("block_path", str(meta.get("block_path", "") or ""))
    item.setdefault("xref_name", str(meta.get("xref_name", "") or ""))
    item.setdefault("entity_type", str(meta.get("entity_type", "") or item.get("entity_type", "") or ""))
    return item


def _empty_group_data():
    return {"segments": [], "annotation_segments": [], "polylines": [], "annotation_polylines": [], "texts": [], "fills": []}


def _build_grouped_floors(source_cache: dict, grouped: dict, group_settings: dict) -> dict:
    divisions = list(source_cache.get("divisions") or [])
    opening_candidates = {
        str(floor): [dict(v) for v in (info.get("opening_candidates") or [])]
        for floor, info in dict(source_cache.get("floors") or {}).items()
    }
    base_floors = build_floor_vectors([], divisions, [], [], rooms=[], opening_candidates=opening_candidates, fills=[])
    group_floor_maps = {}
    for key in GROUP_KEYS:
        data = grouped.get(key) or _empty_group_data()
        group_floor_maps[key] = build_floor_vectors(
            data.get("segments") or [], divisions,
            data.get("texts") or [], data.get("annotation_segments") or [],
            rooms=[], opening_candidates={}, fills=data.get("fills") or [],
        )

    floors = {}
    for floor, base in base_floors.items():
        info = {
            "crop_rect": tuple(base.get("crop_rect") or (0.0, 0.0, 1.0, 1.0)),
            "crop_source": base.get("crop_source", "drawing_frame"),
            "opening_candidates": [dict(v) for v in (base.get("opening_candidates") or [])],
            "group_settings": dict(group_settings),
            "groups": {},
        }
        for key in GROUP_KEYS:
            raw = dict(group_floor_maps.get(key, {}).get(str(floor)) or {})
            # 층 도곽에서 한 번 잘린 선을 다시 연결해 실제 표시용 POLYLINE을 만든다.
            # 이렇게 해야 도곽 경계 때문에 잘린 선이 다른 층과 잘못 이어지지 않는다.
            floor_polylines, _ = _rebuild_polylines(raw.get("segments") or [], MERGE_TOLERANCE)
            floor_annotation_polylines, _ = _rebuild_polylines(raw.get("annotation_segments") or [], MERGE_TOLERANCE)
            info["groups"][key] = {
                "segments": [],
                "annotation_segments": [],
                "polylines": floor_polylines,
                "annotation_polylines": floor_annotation_polylines,
                "texts": raw.get("texts") or [],
                "fills": raw.get("fills") or [],
            }
        floors[str(floor)] = info
    return floors


def build_raw_preview_floors(source_cache: dict, selection: dict) -> dict:
    """2단 날것 캐시를 변경하지 않고 현재 레이어/블록 선택만 적용한 미리보기 층 캐시를 만든다.

    중복선 제거/연속선 병합/그룹 정규화는 하지 않는다. 따라서 [원본 도면 적용]은
    2단 캐시 자체를 덮어쓰지 않고, 사용자가 고른 항목만 화면에서 확인하는 용도다.
    """
    segments = []
    annotations = []
    texts = []
    fills = []

    for seg in source_cache.get("segments") or []:
        if _selected(_segment_meta(seg, "LINE"), selection):
            segments.append(seg)
    for seg in source_cache.get("annotation_segments") or []:
        if _selected(_segment_meta(seg, "DIMENSION"), selection):
            annotations.append(seg)
    for raw in source_cache.get("texts") or []:
        if _selected(_item_meta(raw, "TEXT"), selection):
            texts.append(dict(raw))
    for raw in source_cache.get("fills") or []:
        if _selected(_item_meta(raw, "HATCH"), selection):
            fills.append(dict(raw))

    opening_candidates = {
        str(floor): [dict(v) for v in (info.get("opening_candidates") or [])]
        for floor, info in dict(source_cache.get("floors") or {}).items()
    }
    return build_floor_vectors(
        segments, list(source_cache.get("divisions") or []), texts, annotations,
        rooms=[], opening_candidates=opening_candidates, fills=fills,
    )


def _polyline_meta(raw: dict, fallback_type: str = "LINE") -> dict:
    raw = dict(raw or {})
    return {
        "layer": str(raw.get("layer", "") or MISSING_LAYER),
        "entity_type": str(raw.get("entity_type", "") or fallback_type),
        "source_kind": "BLOCK" if str(raw.get("block_name", "") or "").strip() else "DIRECT",
        "block_name": str(raw.get("block_name", "") or ""),
        "block_path": str(raw.get("block_path", "") or raw.get("block_name", "") or ""),
        "xref_name": str(raw.get("xref_name", "") or ""),
    }


def _source_item_meta_lookup(source_cache: dict | None) -> dict[str, dict]:
    lookup = {}
    for collection, fallback in (((source_cache or {}).get("texts") or [], "TEXT"), ((source_cache or {}).get("fills") or [], "HATCH")):
        for item in collection:
            try:
                handle = str(dict(item or {}).get("handle", "") or "").strip().upper()
                if handle:
                    lookup[handle] = _item_meta(dict(item or {}), fallback)
            except Exception:
                continue
    return lookup


def filter_program_floors(program_cache: dict, selection: dict, source_cache: dict | None = None) -> dict:
    """이미 생성된 3단 최적화 캐시를 재최적화하지 않고 레이어/블록 표시만 거른다.

    [레이어 적용]은 POLYLINE 재구성/중복선 제거를 다시 하지 않고, 기존 최적화
    geometry에서 표시 대상만 추려 화면용 층 캐시를 만든다. 원본/최적화 캐시는 보존한다.
    """
    result = {}
    source_meta_by_handle = _source_item_meta_lookup(source_cache)

    def item_meta(item: dict, fallback: str) -> dict:
        meta = _item_meta(item, fallback)
        if str(meta.get("layer", "") or MISSING_LAYER) == MISSING_LAYER:
            handle = str(dict(item or {}).get("handle", "") or "").strip().upper()
            if handle and handle in source_meta_by_handle:
                return dict(source_meta_by_handle[handle])
        return meta

    for floor, raw_floor in dict((program_cache or {}).get("floors") or {}).items():
        src = dict(raw_floor or {})
        dst = {key: value for key, value in src.items() if key != "groups"}
        groups = {}
        for group_key, raw_group in dict(src.get("groups") or {}).items():
            data = dict(raw_group or {})
            filtered = {
                "segments": [],
                "annotation_segments": [],
                "polylines": [],
                "annotation_polylines": [],
                "texts": [],
                "fills": [],
            }
            for seg in data.get("segments") or []:
                if _selected(_segment_meta(seg, "LINE"), selection):
                    filtered["segments"].append(seg)
            for seg in data.get("annotation_segments") or []:
                if _selected(_segment_meta(seg, "DIMENSION"), selection):
                    filtered["annotation_segments"].append(seg)
            for poly in data.get("polylines") or []:
                if _selected(_polyline_meta(poly, "LINE"), selection):
                    filtered["polylines"].append(dict(poly))
            for poly in data.get("annotation_polylines") or []:
                if _selected(_polyline_meta(poly, "DIMENSION"), selection):
                    filtered["annotation_polylines"].append(dict(poly))
            for item in data.get("texts") or []:
                if _selected(item_meta(item, "TEXT"), selection):
                    filtered["texts"].append(dict(item))
            for item in data.get("fills") or []:
                if _selected(item_meta(item, "HATCH"), selection):
                    filtered["fills"].append(dict(item))
            groups[str(group_key)] = filtered
        dst["groups"] = groups
        result[str(floor)] = dst
    return result


def ensure_program_edit_ids(program_cache: dict) -> dict:
    """최적화 도형에 그룹 편집용 안정 ID를 부여한다.

    기존 캐시에도 실행 즉시 ID를 채워 넣을 수 있도록 별도 마이그레이션 함수로 둔다.
    """
    cache = program_cache
    for floor, raw_floor in dict(cache.get("floors") or {}).items():
        groups = dict(dict(raw_floor or {}).get("groups") or {})
        for group_key, raw_group in groups.items():
            data = dict(raw_group or {})
            for collection in ("polylines", "annotation_polylines", "texts", "fills"):
                values = list(data.get(collection) or [])
                for index, raw in enumerate(values):
                    if isinstance(raw, dict):
                        raw.setdefault("_edit_id", f"{floor}|{group_key}|{collection}|{index}")
    return cache


def program_layer_names(program_cache: dict) -> list[str]:
    names = set()
    for key in ("selection", "draft_selection", "display_selection"):
        names.update(str(v) for v in dict(program_cache.get(key) or {}).get("layers") or [] if str(v).strip())
    for _floor, raw_floor in dict(program_cache.get("floors") or {}).items():
        for _group_key, raw_group in dict(dict(raw_floor or {}).get("groups") or {}).items():
            data = dict(raw_group or {})
            for collection in ("polylines", "annotation_polylines", "texts", "fills"):
                for raw in data.get(collection) or []:
                    if isinstance(raw, dict):
                        layer = str(raw.get("layer", "") or "").strip()
                        if layer:
                            names.add(layer)
    return sorted(names, key=str.casefold)


def _color_layer_name(item: dict) -> str:
    try:
        aci = int(item.get("aci", -1))
    except Exception:
        aci = -1
    if 0 <= aci <= 255:
        return str(aci)
    try:
        rgb = tuple(int(v) for v in (item.get("rgb") or ())[:3])
    except Exception:
        rgb = ()
    if len(rgb) == 3:
        return f"{rgb[0]},{rgb[1]},{rgb[2]}"
    return "색상없음"


def _refresh_program_group_counts(program_cache: dict) -> None:
    counts = {key: 0 for key in GROUP_KEYS}
    for _floor, raw_floor in dict(program_cache.get("floors") or {}).items():
        for group_key, raw_group in dict(dict(raw_floor or {}).get("groups") or {}).items():
            data = dict(raw_group or {})
            counts[str(group_key)] = counts.get(str(group_key), 0) + sum(
                len(data.get(collection) or [])
                for collection in ("polylines", "annotation_polylines", "texts", "fills")
            )
    stats = dict(program_cache.get("stats") or {})
    stats["group_counts"] = counts
    program_cache["stats"] = stats


def move_program_edit_items(program_cache: dict, edit_ids, target_group: str,
                            layer_mode: str = "color", target_layer: str = "", source_group: str = "") -> dict:
    """선택한 최적화 객체만 다른 그룹/레이어로 이동한다.

    layer_mode='color'이면 객체 ACI/RGB 문자열을 레이어명으로 사용하고,
    layer_mode='specified'이면 target_layer를 사용한다. 새 레이어는 최적화
    메타데이터의 레이어 목록에 자동 추가된다.
    """
    cache = ensure_program_edit_ids(program_cache)
    wanted = {str(v) for v in (edit_ids or set()) if str(v)}
    target_group = str(target_group or "").upper()
    source_group = str(source_group or "").upper()
    if not wanted or target_group not in GROUP_KEYS:
        return {"moved": 0, "layers": []}
    mode = str(layer_mode or "color").strip().lower()
    if mode == "specified" and not str(target_layer or "").strip():
        return {"moved": 0, "layers": []}

    moved = 0
    existing_layers = set(program_layer_names(cache))
    created_layers = set()
    for _floor, raw_floor in dict(cache.get("floors") or {}).items():
        groups = dict(dict(raw_floor or {}).get("groups") or {})
        for current_group, raw_group in list(groups.items()):
            if source_group and str(current_group).upper() != source_group:
                continue
            data = dict(raw_group or {})
            for collection in ("polylines", "annotation_polylines", "texts", "fills"):
                src_values = list(data.get(collection) or [])
                keep = []
                transfer = []
                for raw in src_values:
                    if not isinstance(raw, dict) or str(raw.get("_edit_id", "") or "") not in wanted:
                        keep.append(raw)
                        continue
                    item = dict(raw)
                    layer_name = _color_layer_name(item) if mode == "color" else str(target_layer).strip()
                    item["layer"] = layer_name
                    created_layers.add(layer_name)
                    transfer.append(item)
                    moved += 1
                if transfer:
                    data[collection] = keep
                    target_data = dict(groups.setdefault(target_group, _empty_group_data()))
                    target_values = list(target_data.get(collection) or [])
                    target_values.extend(transfer)
                    target_data[collection] = target_values
                    groups[target_group] = target_data
            groups[current_group] = data
        raw_floor["groups"] = groups

    if moved:
        for key in ("selection", "draft_selection", "display_selection"):
            payload = dict(cache.get(key) or {})
            layers = set(str(v) for v in payload.get("layers") or [])
            layers.update(created_layers)
            payload["layers"] = sorted(layers, key=str.casefold)
            cache[key] = payload
        manual_layers = set(str(v) for v in cache.get("manual_layers") or [])
        manual_layers.update(layer for layer in created_layers if layer not in existing_layers)
        cache["manual_layers"] = sorted(manual_layers, key=str.casefold)
        _refresh_program_group_counts(cache)
    return {"moved": moved, "layers": sorted(created_layers, key=str.casefold)}


def build_program_cache(source_cache: dict, selection: dict, merge_tolerance: float = MERGE_TOLERANCE,
                        group_settings: dict | None = None) -> dict:
    raw_segments = list(source_cache.get("segments") or [])
    raw_annotations = list(source_cache.get("annotation_segments") or [])
    raw_texts = list(source_cache.get("texts") or [])
    raw_fills = list(source_cache.get("fills") or [])
    opening_blocks = _opening_block_names(source_cache)
    opening_layers = _opening_layer_names(source_cache, opening_blocks)
    # V24.37: 창호 관련 레이어는 최적화 전체 객체에 적용하는 전역 집합이다.
    # 한 번 창호 구성 레이어로 판정되면 블록 내부/외부, 객체 종류와 관계없이 모두 WINDOW 그룹으로 보낸다.
    opening_layers = {str(v or '').strip().casefold() for v in opening_layers if str(v or '').strip()}

    grouped = {key: _empty_group_data() for key in GROUP_KEYS}
    selected_before = 0
    optimized_layer_counts = {}
    optimized_block_counts = {}

    def mark_used(meta: dict):
        layer = str(meta.get("layer", "") or MISSING_LAYER)
        optimized_layer_counts[layer] = optimized_layer_counts.get(layer, 0) + 1
        for part in _block_parts(meta):
            optimized_block_counts[part] = optimized_block_counts.get(part, 0) + 1

    for seg in raw_segments:
        meta = _segment_meta(seg, "LINE")
        if not _selected(meta, selection):
            continue
        key = _group_for(meta, "segment", opening_blocks, opening_layers)
        grouped[key]["segments"].append(seg)
        mark_used(meta)
        selected_before += 1
    for seg in raw_annotations:
        meta = _segment_meta(seg, "DIMENSION")
        if not _selected(meta, selection):
            continue
        key = _group_for(meta, "annotation", opening_blocks, opening_layers)
        grouped[key]["annotation_segments"].append(seg)
        mark_used(meta)
        selected_before += 1
    for raw in raw_texts:
        meta = _item_meta(raw, "TEXT")
        if not _selected(meta, selection):
            continue
        key = _group_for(meta, "text", opening_blocks, opening_layers)
        grouped[key]["texts"].append(dict(raw))
        mark_used(meta)
        selected_before += 1
    for raw in raw_fills:
        meta = _item_meta(raw, "HATCH")
        if not _selected(meta, selection):
            continue
        key = _group_for(meta, "fill", opening_blocks, opening_layers)
        grouped[key]["fills"].append(dict(raw))
        mark_used(meta)
        selected_before += 1

    deduped = merged = 0
    polyline_count = polyline_source_segments = 0
    for key in GROUP_KEYS:
        data = grouped[key]
        data["segments"], count = _dedupe_segments(data["segments"], merge_tolerance)
        deduped += count
        data["segments"], count = _merge_collinear_segments(data["segments"], merge_tolerance)
        merged += count
        data["annotation_segments"], count = _dedupe_segments(data["annotation_segments"], merge_tolerance)
        deduped += count
        data["annotation_segments"], count = _merge_collinear_segments(data["annotation_segments"], merge_tolerance)
        merged += count

        data["polylines"], src_count = _rebuild_polylines(data["segments"], merge_tolerance)
        polyline_source_segments += src_count
        polyline_count += len(data["polylines"])
        data["annotation_polylines"], src_count = _rebuild_polylines(data["annotation_segments"], merge_tolerance)
        polyline_source_segments += src_count
        polyline_count += len(data["annotation_polylines"])

        # floor별 클리핑은 기존 segment를 기준으로 한 뒤 다시 폴리선화하므로 여기서는 meta를 유지한다.
        data["texts"] = [_clean_item(v) for v in data["texts"]]
        data["fills"] = [_clean_item(v) for v in data["fills"]]

    settings = normalize_group_settings(group_settings)
    floors = _build_grouped_floors(source_cache, grouped, settings)
    counts = {
        key: (
            len(grouped[key]["polylines"]) + len(grouped[key]["annotation_polylines"]) +
            len(grouped[key]["texts"]) + len(grouped[key]["fills"])
        ) for key in GROUP_KEYS
    }

    return {
        "selection": {
            "layers": sorted(set(selection.get("layers") or []), key=str.casefold),
            "blocks": sorted(set(selection.get("blocks") or []), key=str.casefold),
        },
        # 도면정리 창을 닫았다 다시 열어도 2단 필터 체크 상태를 복원한다.
        # selection은 실제 3단 생성에 사용한 값, draft_selection은 다음 작업용 체크 상태다.
        "draft_selection": {
            "layers": sorted(set(selection.get("layers") or []), key=str.casefold),
            "blocks": sorted(set(selection.get("blocks") or []), key=str.casefold),
        },
        "display_selection": {
            "layers": sorted(set(selection.get("layers") or []), key=str.casefold),
            "blocks": sorted(set(selection.get("blocks") or []), key=str.casefold),
        },
        "group_settings": settings,
        "divisions": list(source_cache.get("divisions") or []),
        "floors": floors,
        "stats": {
            "source_items": len(raw_segments) + len(raw_annotations) + len(raw_texts) + len(raw_fills),
            "selected_items": selected_before,
            "duplicates_removed": deduped,
            "collinear_merged": merged,
            "polyline_source_segments": polyline_source_segments,
            "polylines_created": polyline_count,
            "polyline_reduction": max(0, polyline_source_segments - polyline_count),
            "group_counts": counts,
            "optimized_layer_counts": dict(optimized_layer_counts),
            "optimized_block_counts": dict(optimized_block_counts),
            "opening_layer_count": len(opening_layers),
            "opening_layers": sorted(opening_layers),
        },
    }
