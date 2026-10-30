from __future__ import annotations

import json
from collections.abc import Iterable, Mapping


DEFAULT_FORMULA_CODES = (
    {"part": "바닥", "code": "A1", "formula": "A - A0", "description": "바닥면적"},
    {"part": "벽", "code": "W1", "formula": "(L * H) - W0", "description": "벽면적"},
)

DEFAULT_FINISH_CATEGORIES = (
    {"category": "바닥", "formula_code": "A1", "formula": ""},
    {"category": "벽", "formula_code": "", "formula": ""},
    {"category": "천정", "formula_code": "A1", "formula": ""},
    {"category": "기타", "formula_code": "", "formula": ""},
)

def normalize_formula_text(value) -> str:
    """산식은 대소문자를 구분하지 않고 저장/표시는 대문자로 통일한다."""
    return str(value or "").strip().upper()



def _decode_rows(value, default_rows) -> list:
    if value is None:
        return [dict(row) for row in default_rows]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [dict(row) for row in default_rows]
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [dict(row) for row in default_rows]
    if isinstance(value, Mapping) or not isinstance(value, Iterable):
        return [dict(row) for row in default_rows]
    return list(value)


def normalize_formula_codes(value=None) -> list[dict[str, str]]:
    rows = _decode_rows(value, DEFAULT_FORMULA_CODES)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = {
            "part": str(raw.get("part", "") or "").strip(),
            "code": str(raw.get("code", "") or "").strip().upper(),
            "formula": normalize_formula_text(raw.get("formula", "")),
            "description": str(raw.get("description", "") or "").strip(),
        }
        marker = row["code"].casefold()
        if not row["code"] or marker in seen:
            continue
        seen.add(marker)
        result.append(row)
    return result


def normalize_finish_categories(value=None) -> list[dict[str, str]]:
    rows = _decode_rows(value, DEFAULT_FINISH_CATEGORIES)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        category = str(raw.get("category", "") or "").strip()
        marker = category.casefold()
        if not category or marker in seen:
            continue
        seen.add(marker)
        result.append({
            "category": category,
            "formula_code": str(raw.get("formula_code", "") or "").strip().upper(),
            "formula": normalize_formula_text(raw.get("formula", "")),
        })
    return result


def serialize_formula_codes(value=None) -> str:
    return json.dumps(normalize_formula_codes(value), ensure_ascii=False, separators=(",", ":"))


def serialize_finish_categories(value=None) -> str:
    return json.dumps(normalize_finish_categories(value), ensure_ascii=False, separators=(",", ":"))


def canonical_finish_category(category: str) -> str:
    """표시명 `천정`과 기존 저장명 `천장`을 같은 산출 대상으로 취급한다."""
    value = str(category or "").strip()
    return "천장" if value in ("천정", "천장") else value


def formula_for_category(category: str, formula_codes, categories) -> tuple[str, str]:
    canonical = canonical_finish_category(category)
    code = ""
    direct_formula = ""
    for row in normalize_finish_categories(categories):
        if canonical_finish_category(row["category"]) == canonical:
            code = row["formula_code"]
            direct_formula = row.get("formula", "")
            break
    if direct_formula:
        return code, direct_formula
    if not code:
        return "", ""
    for row in normalize_formula_codes(formula_codes):
        if row["code"].casefold() == code.casefold():
            return row["code"], row["formula"]
    return code, ""
