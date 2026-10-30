from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from app.common.models import room_name_is_missing


ROOM_CATEGORIES = (
    "공용",
    "공용실",
    "기전실",
    "화장실",
    "근생",
    "주거",
    "외부",
    "기타",
)

ROOM_CATEGORY_INI_KEYS = {
    "공용": "Common",
    "공용실": "CommonRoom",
    "기전실": "MechanicalElectrical",
    "화장실": "Restroom",
    "근생": "Commercial",
    "주거": "Residential",
    "외부": "Exterior",
    "기타": "Other",
}

DEFAULT_ROOM_CATEGORY_KEYWORDS = {
    "공용": ["홀", "복도", "계단", "AV", "PD", "EPS", "TPS"],
    "공용실": ["창고"],
    "기전실": ["기계실", "전기실", "발전기실"],
    "화장실": ["화장실"],
    "근생": ["근린", "업무", "판매", "사무"],
    "주거": ["객실", "세대"],
    "외부": ["필로티", "옥상", "정원", "공개", "성큰"],
    "기타": [],
}


def _split_keywords(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,;\r\n]+", value)
    elif isinstance(value, Iterable):
        candidates = list(value)
    else:
        candidates = [value]

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        keyword = str(candidate or "").strip()
        marker = keyword.casefold()
        if keyword and marker not in seen:
            result.append(keyword)
            seen.add(marker)
    return result


def normalize_room_category_rules(values: Mapping | None = None) -> dict[str, list[str]]:
    source = dict(values or {})
    result: dict[str, list[str]] = {}
    for category in ROOM_CATEGORIES:
        value = source.get(category, DEFAULT_ROOM_CATEGORY_KEYWORDS[category])
        result[category] = _split_keywords(value)
    return result


def classify_room_name(room_name: str, rules: Mapping | None = None) -> str:
    name = str(room_name or "").strip()
    if room_name_is_missing(name):
        return ""

    normalized_name = re.sub(r"\s+", "", name).casefold()
    normalized_rules = normalize_room_category_rules(rules)
    for category in ROOM_CATEGORIES:
        if category == "기타":
            continue
        for keyword in normalized_rules[category]:
            normalized_keyword = re.sub(r"\s+", "", keyword).casefold()
            if normalized_keyword and normalized_keyword in normalized_name:
                return category
    # 어느 기준에도 걸리지 않은 실은 사용자가 직접 정할 수 있도록 미지정으로 둔다.
    return ""
