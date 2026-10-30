from __future__ import annotations

import json
from collections.abc import Iterable, Mapping


DEFAULT_MATERIAL_TRADES = (
    {"code": "01", "name": "건축공사", "parent_code": ""},
    {"code": "0101", "name": "공 통 가 설 공 사", "parent_code": "01"},
    {"code": "0102", "name": "가  설  공  사", "parent_code": "01"},
    {"code": "0103", "name": "철근콘크리트공사", "parent_code": "01"},
    {"code": "0104", "name": "조  적  공  사", "parent_code": "01"},
    {"code": "0105", "name": "미  장  공  사", "parent_code": "01"},
    {"code": "0106", "name": "방  수  공  사", "parent_code": "01"},
    {"code": "0107", "name": "타  일  공  사", "parent_code": "01"},
    {"code": "0108", "name": "석    공    사", "parent_code": "01"},
    {"code": "0109", "name": "금  속  공  사", "parent_code": "01"},
    {"code": "0110", "name": "창  호  공  사", "parent_code": "01"},
    {"code": "0111", "name": "유  리  공  사", "parent_code": "01"},
    {"code": "0112", "name": "수  장  공  사", "parent_code": "01"},
    {"code": "0113", "name": "도  장  공  사", "parent_code": "01"},
    {"code": "0114", "name": "부  대  토  목", "parent_code": "01"},
    {"code": "0115", "name": "조  경  공  사", "parent_code": "01"},
    {"code": "0116", "name": "가  전  공  사", "parent_code": "01"},
    {"code": "0117", "name": "가  구  공  사", "parent_code": "01"},
    {"code": "0118", "name": "승강기 설치공사", "parent_code": "01"},
    {"code": "0119", "name": "작 업 부 산 물 / 골 재 비", "parent_code": "01"},
    {"code": "0120", "name": "건설폐기물처리비", "parent_code": "01"},
    {"code": "02", "name": "토공사", "parent_code": ""},
    {"code": "0201", "name": "토공사", "parent_code": "02"},
    {"code": "0202", "name": "흙막이 공사", "parent_code": "02"},
    {"code": "0203", "name": "SGR 공사", "parent_code": "02"},
    {"code": "0204", "name": "가시설 공사", "parent_code": "02"},
    {"code": "0205", "name": "계측관리", "parent_code": "02"},
    {"code": "0206", "name": "주요자재비", "parent_code": "02"},
    {"code": "0207", "name": "부대공사", "parent_code": "02"},
)


def normalize_material_trades(value=None) -> list[dict[str, str]]:
    if value is None:
        rows = DEFAULT_MATERIAL_TRADES
    elif isinstance(value, str):
        try:
            decoded = json.loads(value.strip()) if value.strip() else DEFAULT_MATERIAL_TRADES
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = DEFAULT_MATERIAL_TRADES
        rows = decoded
    else:
        rows = value
    if isinstance(rows, Mapping) or not isinstance(rows, Iterable):
        rows = DEFAULT_MATERIAL_TRADES

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("code", "") or "").strip()
        name = str(raw.get("name", raw.get("trade", "")) or "").strip()
        parent_code = str(raw.get("parent_code", "") or "").strip()
        marker = code.casefold()
        if not code or not name or marker in seen:
            continue
        seen.add(marker)
        result.append({"code": code, "name": name, "parent_code": parent_code})

    valid_codes = {row["code"] for row in result}
    for row in result:
        if row["parent_code"] not in valid_codes or row["parent_code"] == row["code"]:
            row["parent_code"] = ""
    return result


def serialize_material_trades(value=None) -> str:
    return json.dumps(normalize_material_trades(value), ensure_ascii=False, separators=(",", ":"))


def material_trade_display(code: str, name: str) -> str:
    code = str(code or "").strip()
    name = str(name or "").strip()
    return " ".join(part for part in (code, name) if part)
