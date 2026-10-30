from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from app.common.models import ResultRow, RoomRecord, normalize_room_name


SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DIR_NAME = "수동저장"
AUTOSAVE_DIR_NAME = "자동저장"
SECTION_LABELS = {
    "material": "재료",
    "rooms": "내부",
    "background": "캐드배경",
}


def snapshot_root(project_path: Path, directory_name: str = SNAPSHOT_DIR_NAME) -> Path:
    return Path(project_path) / "캐쉬" / str(directory_name or SNAPSHOT_DIR_NAME)


def _section_path(project_path: Path, section: str, directory_name: str = SNAPSHOT_DIR_NAME) -> Path:
    root = snapshot_root(project_path, directory_name)
    return {
        "material": root / "재료_캐시.json",
        "rooms": root / "내부_캐시.json",
        "background": root / "CAD배경",
    }[section]


def existing_sections(project_path: Path, sections) -> list[str]:
    result = []
    for section in sections:
        path = _section_path(project_path, section, SNAPSHOT_DIR_NAME)
        if path.is_file() or (path.is_dir() and any(path.iterdir())):
            result.append(section)
    return result


def _json_value(value):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _replace_background_cache(target: Path, rendered_cache: Path, source_geometry: Path | None) -> None:
    if not rendered_cache.is_dir():
        raise FileNotFoundError("저장할 CAD 배경 캐시가 없습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}_작성중_{uuid.uuid4().hex[:8]}"
    backup = target.parent / f".{target.name}_이전_{uuid.uuid4().hex[:8]}"
    try:
        (staging / "렌더캐시").mkdir(parents=True, exist_ok=True)
        for item in rendered_cache.iterdir():
            destination = staging / "렌더캐시" / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            elif item.is_file():
                shutil.copy2(item, destination)
        if source_geometry is not None and source_geometry.is_file():
            shutil.copy2(source_geometry, staging / "원본도형_캐시.pkl")
        if target.exists():
            target.replace(backup)
        staging.replace(target)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if target.exists() and backup.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def save_project_snapshot(
    project_path: Path,
    sections,
    *,
    source_path: str,
    material_payload: dict | None = None,
    room_payload: dict | None = None,
    rendered_background_cache: Path | None = None,
    source_geometry_cache: Path | None = None,
    directory_name: str = SNAPSHOT_DIR_NAME,
) -> dict:
    selected = [section for section in sections if section in SECTION_LABELS]
    if not selected:
        raise ValueError("저장할 캐시 항목이 없습니다.")

    root = snapshot_root(project_path, directory_name)
    root.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.now().isoformat(timespec="seconds")
    saved = []
    if "material" in selected:
        _write_json(
            _section_path(project_path, "material", directory_name),
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "section": "material",
                "source_path": str(source_path or ""),
                "saved_at": saved_at,
                **dict(material_payload or {}),
            },
        )
        saved.append("material")
    if "rooms" in selected:
        _write_json(
            _section_path(project_path, "rooms", directory_name),
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "section": "rooms",
                "source_path": str(source_path or ""),
                "saved_at": saved_at,
                **dict(room_payload or {}),
            },
        )
        saved.append("rooms")
    if "background" in selected:
        if rendered_background_cache is None:
            raise FileNotFoundError("저장할 CAD 배경 캐시가 없습니다.")
        _replace_background_cache(
            _section_path(project_path, "background", directory_name),
            Path(rendered_background_cache),
            Path(source_geometry_cache) if source_geometry_cache else None,
        )
        saved.append("background")

    manifest_path = root / "저장정보.json"
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    section_info = dict(manifest.get("sections") or {})
    for section in saved:
        section_info[section] = {"saved_at": saved_at, "source_path": str(source_path or "")}
    _write_json(
        manifest_path,
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "saved_at": saved_at,
            "sections": section_info,
        },
    )
    return {"root": root, "saved": saved, "saved_at": saved_at}


def _snapshot_time_key(payload: dict | None, path: Path, directory_name: str) -> tuple[float, int, int]:
    """저장본의 실제 최신 순서를 계산한다.

    V22.5까지는 자동저장을 무조건 수동저장보다 먼저 읽어서, 사용자가 방금
    [저장]으로 최신 캐시를 만든 뒤 재실행해도 더 오래된 자동저장이 복원되는
    문제가 있었다. saved_at을 우선하고 파일 mtime을 보조값으로 사용한다.
    """
    saved_ts = 0.0
    try:
        raw = str((payload or {}).get("saved_at", "") or "").strip()
        if raw:
            saved_ts = datetime.fromisoformat(raw).timestamp()
    except Exception:
        saved_ts = 0.0
    try:
        mtime_ns = int(path.stat().st_mtime_ns)
    except Exception:
        mtime_ns = 0
    # 시간이 완전히 같으면 명시적으로 누른 수동저장을 우선한다.
    manual_priority = 1 if directory_name == SNAPSHOT_DIR_NAME else 0
    return (saved_ts, mtime_ns, manual_priority)


def _read_snapshot_json(project_path: Path, section: str):
    """자동/수동 저장 중 해당 항목의 가장 최신 저장본을 읽는다."""
    candidates = []
    for directory_name in (AUTOSAVE_DIR_NAME, SNAPSHOT_DIR_NAME):
        path = _section_path(project_path, section, directory_name)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                candidates.append((_snapshot_time_key(payload, path, directory_name), payload, directory_name, path))
        except Exception:
            continue
    if not candidates:
        return None, "", None
    _key, payload, directory_name, path = max(candidates, key=lambda item: item[0])
    return payload, directory_name, path


def _result_rows(payload) -> dict[str, list[ResultRow]]:
    result = {}
    for category, rows in dict(payload or {}).items():
        converted = []
        for raw in rows or []:
            if isinstance(raw, ResultRow):
                converted.append(raw)
                continue
            try:
                converted.append(ResultRow(**dict(raw or {})))
            except Exception:
                continue
        result[str(category)] = converted
    for category in ("재료 후보", "보류", "제외"):
        result.setdefault(category, [])
    return result


def _room_rows(payload) -> list[RoomRecord]:
    rooms = []
    for raw in payload or []:
        if isinstance(raw, RoomRecord):
            rooms.append(raw)
            continue
        try:
            row = dict(raw or {})
            row["room_name"] = normalize_room_name(row.get("room_name"))
            row["points"] = [tuple(point) for point in row.get("points", [])]
            row["deduction_points"] = [
                [tuple(point) for point in polygon]
                for polygon in row.get("deduction_points", [])
            ]
            rooms.append(RoomRecord(**row))
        except Exception:
            continue
    return rooms


def _manifest_section_time(project_path: Path, directory_name: str, section: str, fallback_path: Path) -> tuple[float, int, int]:
    payload = {}
    try:
        manifest_path = snapshot_root(project_path, directory_name) / "저장정보.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        section_info = dict((manifest.get("sections") or {}).get(section) or {})
        payload = {"saved_at": section_info.get("saved_at") or manifest.get("saved_at") or ""}
    except Exception:
        payload = {}
    return _snapshot_time_key(payload, fallback_path, directory_name)


def _background_files(project_path: Path):
    """PNG가 없어도 벡터 원본/프로그램 캐시가 있으면 배경 저장본으로 인정한다.

    반환값에는 배경 캐시를 만들 때 사용한 source_path도 포함한다. 적용 DXF와
    CAD 배경 캐시 키가 서로 다른 프로젝트를 CAD 미연동 상태에서 복원할 때 필요하다.
    """
    candidates = []
    for directory_name in (AUTOSAVE_DIR_NAME, SNAPSHOT_DIR_NAME):
        section_dir = _section_path(project_path, "background", directory_name)
        cache_dir = section_dir / "렌더캐시"
        meta_path = cache_dir / "cache_info.json"
        source_geometry = section_dir / "원본도형_캐시.pkl"
        program_geometry = cache_dir / "program_vector.pkl"
        result = {}
        key_path = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for floor, info in dict(meta.get("floors") or {}).items():
                    image_path = cache_dir / str(info.get("image", ""))
                    if not image_path.is_file():
                        continue
                    text_name = str(info.get("text_image", "") or "")
                    text_path = cache_dir / text_name if text_name else None
                    result[str(floor)] = {
                        "path": str(image_path),
                        "text_path": str(text_path) if text_path and text_path.is_file() else "",
                        "crop_rect": tuple(float(value) for value in info.get("crop_rect", [])),
                        "crop_source": str(info.get("crop_source", "rooms")),
                        "clip_polygon": list(info.get("clip_polygon") or []),
                    }
                key_path = meta_path
            except Exception:
                result = {}
        if key_path is None:
            if source_geometry.is_file():
                key_path = source_geometry
            elif program_geometry.is_file():
                key_path = program_geometry
        if key_path is None:
            continue
        key = _manifest_section_time(project_path, directory_name, "background", key_path)
        source_path = ""
        try:
            manifest_path = snapshot_root(project_path, directory_name) / "저장정보.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
            section_info = dict((manifest.get("sections") or {}).get("background") or {})
            source_path = str(section_info.get("source_path", "") or "").strip()
        except Exception:
            source_path = ""
        candidates.append((key, result, directory_name, cache_dir, source_path))
    if not candidates:
        return {}, "", None, ""
    _key, result, directory_name, cache_dir, source_path = max(candidates, key=lambda item: item[0])
    return result, directory_name, cache_dir, source_path


def load_project_snapshot(project_path: Path) -> dict | None:
    """프로젝트의 자동/수동 저장을 항목별로 합쳐 복원 가능한 구조로 반환한다."""
    material_payload, material_source, _material_path = _read_snapshot_json(project_path, "material")
    room_payload, room_source, _room_path = _read_snapshot_json(project_path, "rooms")
    background_files, background_source, _background_path, background_source_path = _background_files(project_path)
    source_geometry_path = None
    program_geometry_path = None
    if _background_path is not None:
        candidate = _background_path.parent / "원본도형_캐시.pkl"
        if candidate.is_file():
            source_geometry_path = str(candidate)
        program_candidate = _background_path / "program_vector.pkl"
        if program_candidate.is_file():
            program_geometry_path = str(program_candidate)
    if (
        material_payload is None and room_payload is None and not background_files
        and not source_geometry_path and not program_geometry_path
    ):
        return None

    source_path = ""
    for payload in (room_payload, material_payload):
        if payload and str(payload.get("source_path", "") or "").strip():
            source_path = str(payload.get("source_path", "") or "").strip()
            break
    return {
        "source_path": source_path,
        "result": _result_rows((material_payload or {}).get("result")),
        "finish_material_list": (material_payload or {}).get("finish_material_list"),
        "wall_finish_assignments": dict((material_payload or {}).get("wall_finish_assignments") or {}),
        "room_finish_assignments": dict((material_payload or {}).get("room_finish_assignments") or {}),
        "finish_groups": dict((material_payload or {}).get("finish_groups") or {}),
        "summary_markups": (material_payload or {}).get("summary_markups"),
        "opening_read_results": dict((material_payload or {}).get("opening_read_results") or {}),
        "other_items": list((material_payload or {}).get("other_items") or []),
        "window_data": dict((material_payload or {}).get("window_data") or {}),
        "formula_codes": (material_payload or {}).get("formula_codes"),
        "finish_categories": (material_payload or {}).get("finish_categories"),
        "material_trades": (material_payload or {}).get("material_trades"),
        "material_units": (material_payload or {}).get("material_units"),
        "rooms": _room_rows((room_payload or {}).get("rooms")),
        "errors": [str(item) for item in (room_payload or {}).get("errors", [])],
        "divisions": list((room_payload or {}).get("divisions") or []),
        "background_files": background_files,
        "source_geometry_path": source_geometry_path,
        "program_geometry_path": program_geometry_path,
        "background_source_path": background_source_path,
        "sources": {
            "material": material_source,
            "rooms": room_source,
            "background": background_source,
        },
    }
