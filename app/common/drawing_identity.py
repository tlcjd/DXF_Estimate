from __future__ import annotations

import fnmatch
from collections import Counter
from pathlib import Path


def layer_is_excluded(layer_name: str, business_layers=(), exclude_patterns=()) -> bool:
    layer = str(layer_name or "0").strip().casefold()
    business = {str(name or "").strip().casefold() for name in business_layers if str(name or "").strip()}
    patterns = [str(pattern or "").strip().casefold() for pattern in exclude_patterns if str(pattern or "").strip()]
    return layer in business or any(fnmatch.fnmatchcase(layer, pattern) for pattern in patterns)


def normalize_entity_type(value: str) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("ACDB"):
        text = text[4:]
    aliases = {
        "BLOCKREFERENCE": "INSERT",
        "POLYLINE": "LWPOLYLINE",
        "2DPOLYLINE": "LWPOLYLINE",
        "3DPOLYLINE": "LWPOLYLINE",
        "ROTATEDDIMENSION": "DIMENSION",
        "ALIGNEDDIMENSION": "DIMENSION",
        "RADIALDIMENSION": "DIMENSION",
        "DIAMETRICDIMENSION": "DIMENSION",
        "ANGULARDIMENSION": "DIMENSION",
    }
    if "DIMENSION" in text:
        return "DIMENSION"
    return aliases.get(text, text or "UNKNOWN")


def signature_from_rows(rows, business_layers=(), exclude_patterns=()) -> dict:
    by_type = Counter()
    by_layer = Counter()
    for row in rows:
        if isinstance(row, dict):
            layer = row.get("layer", "0")
            entity_type = row.get("type", row.get("entity_type", ""))
        else:
            layer, entity_type = row
        if layer_is_excluded(layer, business_layers, exclude_patterns):
            continue
        layer_key = str(layer or "0").strip().casefold()
        by_layer[layer_key] += 1
        by_type[normalize_entity_type(entity_type)] += 1
    return {
        "total": int(sum(by_type.values())),
        "by_type": dict(sorted(by_type.items())),
        "by_layer": dict(sorted(by_layer.items())),
    }


def signature_from_dxf(path: str | Path, business_layers=(), exclude_patterns=()) -> dict:
    import ezdxf

    document = ezdxf.readfile(str(path))
    rows = [
        {"layer": str(entity.dxf.get("layer", "0") or "0"), "type": entity.dxftype()}
        for entity in document.modelspace()
    ]
    return signature_from_rows(rows, business_layers, exclude_patterns)


def compare_signatures(reference: dict, current: dict) -> dict:
    reference = dict(reference or {})
    current = dict(current or {})
    same = (
        int(reference.get("total", -1)) == int(current.get("total", -2))
        and dict(reference.get("by_type") or {}) == dict(current.get("by_type") or {})
    )
    return {
        "same": same,
        "reference_total": int(reference.get("total", 0) or 0),
        "current_total": int(current.get("total", 0) or 0),
        "reference_by_type": dict(reference.get("by_type") or {}),
        "current_by_type": dict(current.get("by_type") or {}),
    }
