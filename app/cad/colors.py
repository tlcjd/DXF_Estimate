from __future__ import annotations

from ezdxf.colors import aci2rgb


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return 255, 255, 255
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return 255, 255, 255


def nearest_aci(value: str) -> int:
    """QColor 형식의 #RRGGBB를 가장 가까운 AutoCAD ACI 색상으로 바꾼다."""
    target = hex_to_rgb(value)
    best_index = 7
    best_distance = float("inf")
    for index in range(1, 256):
        try:
            rgb = aci2rgb(index)
        except Exception:
            continue
        distance = sum((int(rgb[i]) - target[i]) ** 2 for i in range(3))
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def normalize_aci(value, fallback: int = 7) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = int(fallback)
    return max(1, min(255, index))


def aci_to_hex(index: int) -> str:
    """AutoCAD ACI 번호를 프로그램 표시용 #RRGGBB로 변환한다."""
    index = normalize_aci(index)
    try:
        rgb = aci2rgb(index)
        return f"#{int(rgb.r):02X}{int(rgb.g):02X}{int(rgb.b):02X}"
    except Exception:
        return "#FFFFFF"


def color_to_aci(value: str = "", explicit_aci=None, fallback: int = 7) -> int:
    """명시 ACI가 있으면 그대로 쓰고, 구형 RGB 데이터만 최근접 ACI로 이관한다."""
    try:
        if explicit_aci is not None and str(explicit_aci).strip() != "":
            return normalize_aci(explicit_aci, fallback)
    except Exception:
        pass
    text = str(value or "").strip()
    return nearest_aci(text) if text else normalize_aci(fallback)
