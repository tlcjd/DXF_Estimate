from __future__ import annotations

import json
import re


DEFAULT_MATERIAL_UNITS = [
    "식", "EA", "SET", "개소", "KG", "M", "M2", "M3", "TON",
    "개", "본", "매", "천매", "장", "대", "조", "쌍", "벌", "롤",
    "통", "포", "포대", "BOX", "PACK", "BAG", "CAN", "L", "KL",
    "회", "일", "월", "시간", "HR", "인", "인일", "인월", "공수",
    "대일", "대월", "천개", "천본",
]


def normalize_material_unit(value: str) -> str:
    """재료/내역 단위를 공통 표기로 정규화한다. 영문은 대문자로 통일한다."""
    text = str(value or "").strip()
    if not text:
        return ""

    # 제곱/세제곱 미터의 흔한 입력형식을 먼저 표준화한다.
    compact = re.sub(r"\s+", "", text)
    upper = compact.upper()
    aliases = {
        "㎡": "M2", "M²": "M2", "M^2": "M2", "SQM": "M2",
        "㎥": "M3", "M³": "M3", "M^3": "M3", "CBM": "M3",
        "T": "TON",
    }
    if upper in aliases:
        return aliases[upper]
    return upper


def normalize_material_units(value=None) -> list[str]:
    """설정값을 중복 없는 단위 목록으로 정리한다."""
    if value is None:
        source = list(DEFAULT_MATERIAL_UNITS)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            source = []
        else:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = re.split(r"[\r\n,;]+", text)
            source = parsed if isinstance(parsed, (list, tuple)) else []
    elif isinstance(value, (list, tuple, set)):
        source = list(value)
    else:
        source = []

    result: list[str] = []
    seen: set[str] = set()
    for raw in source:
        unit = normalize_material_unit(raw)
        marker = unit.casefold()
        if unit and marker not in seen:
            seen.add(marker)
            result.append(unit)
    return result


def serialize_material_units(value=None) -> str:
    return json.dumps(normalize_material_units(value), ensure_ascii=False, separators=(",", ":"))
