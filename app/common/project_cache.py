from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.common.models import ResultRow, RoomRecord, normalize_room_name
from app.common.room_categories import ROOM_CATEGORIES
from app.core.paths import TEMP_PROJECT_CACHE_DIR

CACHE_VERSION = 5  # V23.45: 기존 캐시 호환 유지, room_id 누락 시 RoomRecord가 자동 생성
PROJECT_DATA_ROOT = TEMP_PROJECT_CACHE_DIR / "연동데이터"


def _cache_dir(source_path: str) -> Path:
    text = str(source_path or "").strip()
    key = str(Path(text).resolve()) if text else "unsaved"
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:12]
    stem = Path(text).stem if text else "현재도면"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem) or "현재도면"
    return PROJECT_DATA_ROOT / f"{safe}_{digest}"


def cache_path(source_path: str) -> Path:
    return _cache_dir(source_path) / "project_cache.json"


def cache_exists(source_path: str) -> bool:
    path = cache_path(source_path)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) != CACHE_VERSION:
            return False
        try:
            stat = Path(source_path).stat()
            return (
                int(payload.get("source_size", -1)) == int(stat.st_size)
                and int(payload.get("source_mtime_ns", -1)) == int(stat.st_mtime_ns)
            )
        except Exception:
            return True
    except Exception:
        return False


def save_project_cache(source_path: str, result: dict, rooms: list[RoomRecord], errors: list[str], divisions=None) -> Path:
    path = cache_path(source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    source_size = -1
    source_mtime_ns = -1
    try:
        stat = Path(source_path).stat()
        source_size = int(stat.st_size)
        source_mtime_ns = int(stat.st_mtime_ns)
    except Exception:
        pass
    payload = {
        "version": CACHE_VERSION,
        "source_path": str(source_path or ""),
        "source_size": source_size,
        "source_mtime_ns": source_mtime_ns,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "result": {key: [asdict(row) for row in rows] for key, rows in (result or {}).items()},
        "rooms": [asdict(room) for room in (rooms or [])],
        "errors": [str(item) for item in (errors or [])],
        "divisions": list(divisions or []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_project_cache(source_path: str):
    path = cache_path(source_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) != CACHE_VERSION:
            return None
        # CAD가 캐시 저장 뒤 변경되었다면 오래된 실 목록/도곽 판정을 재사용하지 않는다.
        try:
            stat = Path(source_path).stat()
            cached_size = int(payload.get("source_size", -1))
            cached_mtime = int(payload.get("source_mtime_ns", -1))
            if cached_size != int(stat.st_size) or cached_mtime != int(stat.st_mtime_ns):
                return None
        except Exception:
            # 저장 전 도면 등 실제 파일이 없는 경우에만 기존 방식으로 허용한다.
            pass
        result = {}
        for key, rows in (payload.get("result") or {}).items():
            result[key] = [ResultRow(**row) for row in rows]
        for key in ("재료 후보", "보류", "제외"):
            result.setdefault(key, [])
        rooms = []
        for row in payload.get("rooms") or []:
            row = dict(row)
            row["room_name"] = normalize_room_name(row.get("room_name"))
            row["points"] = [tuple(point) for point in row.get("points", [])]
            row["deduction_points"] = [
                [tuple(point) for point in polygon]
                for polygon in row.get("deduction_points", [])
            ]
            rooms.append(RoomRecord(**row))
        return {
            "result": result,
            "rooms": rooms,
            "errors": [str(item) for item in payload.get("errors") or []],
            "divisions": list(payload.get("divisions") or []),
            "saved_at": str(payload.get("saved_at", "")),
            "path": str(path),
        }
    except Exception:
        return None


def load_room_category_snapshots(source_path: str) -> list[dict]:
    """도면 변경 여부와 관계없이 같은 도면 캐시의 수동/기존 실 구분만 읽는다.

    전체 실 geometry 캐시는 원본 크기·수정시각이 달라지면 폐기하지만, 사용자가 목록에서
    정한 구분은 CAD를 새로 읽을 때도 유지해야 하므로 별도 경로로 복원한다.
    """
    path = cache_path(source_path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) < 5:
            return []
        snapshots = []
        for raw in payload.get("rooms") or []:
            row = dict(raw or {})
            category = str(row.get("room_category", "") or "").strip()
            if category not in ROOM_CATEGORIES:
                continue
            snapshots.append({
                "room_category": category,
                "room_id": str(row.get("room_id", "") or ""),
                "boundary_handle": str(row.get("boundary_handle", "") or ""),
                "tag_handle": str(row.get("tag_handle", "") or ""),
                "floor": str(row.get("floor", "") or ""),
                "room_number": str(row.get("room_number", "") or ""),
                "room_name": normalize_room_name(row.get("room_name")),
                "area_raw": float(row.get("area_raw", 0.0) or 0.0),
                "points": list(row.get("points") or []),
            })
        return snapshots
    except Exception:
        return []


def restore_room_categories(rooms: list[RoomRecord], snapshots: list[dict]) -> int:
    """새로 읽은 실에 이전 목록의 구분을 안정적인 식별자 순서로 복원한다."""
    valid_snapshots = []
    for row in snapshots or []:
        if not isinstance(row, dict):
            continue
        data = dict(row)
        if str(data.get("room_category", "") or "") in ROOM_CATEGORIES:
            valid_snapshots.append(data)
    if not rooms or not valid_snapshots:
        return 0

    def normalized(value) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    def geometry_key(value):
        points = value.get("points", []) if isinstance(value, dict) else getattr(value, "points", [])
        points = list(points or [])
        try:
            cx = sum(float(point[0]) for point in points) / len(points)
            cy = sum(float(point[1]) for point in points) / len(points)
        except Exception:
            cx = cy = 0.0
        floor = value.get("floor", "") if isinstance(value, dict) else getattr(value, "floor", "")
        name = value.get("room_name", "") if isinstance(value, dict) else getattr(value, "room_name", "")
        area = value.get("area_raw", 0.0) if isinstance(value, dict) else getattr(value, "area_raw", 0.0)
        return (normalized(floor), normalized(name), round(float(area or 0.0), 1), round(cx, 1), round(cy, 1))

    def unique_map(key_function):
        result = {}
        duplicates = set()
        for row in valid_snapshots:
            key = key_function(row)
            if not key or key in duplicates:
                continue
            if key in result:
                result.pop(key, None)
                duplicates.add(key)
                continue
            result[key] = row["room_category"]
        return result

    by_room_id = unique_map(lambda row: normalized(row.get("room_id")))
    by_boundary = unique_map(lambda row: normalized(row.get("boundary_handle")))
    by_tag = unique_map(lambda row: normalized(row.get("tag_handle")))
    by_floor_number = unique_map(
        lambda row: (normalized(row.get("floor")), normalized(row.get("room_number")))
        if normalized(row.get("room_number")) else None
    )
    by_geometry = unique_map(geometry_key)

    restored = 0
    for room in rooms:
        if str(getattr(room, "room_category", "") or "").strip() in ROOM_CATEGORIES:
            continue
        category = None
        room_id = normalized(getattr(room, "room_id", ""))
        boundary = normalized(getattr(room, "boundary_handle", ""))
        tag = normalized(getattr(room, "tag_handle", ""))
        number = normalized(getattr(room, "room_number", ""))
        if room_id:
            category = by_room_id.get(room_id)
        if category is None and boundary:
            category = by_boundary.get(boundary)
        if category is None and tag:
            category = by_tag.get(tag)
        if category is None and number:
            category = by_floor_number.get((normalized(getattr(room, "floor", "")), number))
        if category is None:
            category = by_geometry.get(geometry_key(room))
        if category in ROOM_CATEGORIES:
            room.room_category = category
            restored += 1
    return restored
