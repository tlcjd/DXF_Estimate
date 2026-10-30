from __future__ import annotations

DEFAULT_WIDTH = 1.5
DEFAULT_ACI_WIDTHS = {
    0: 0.03,
    1: 0.0,
    2: 0.13,
    3: 0.13,
    4: 0.09,
    5: 0.03,
    6: 0.03,
    7: 0.09,
    8: 0.0,
    9: 0.0,
    255: 0.03,
}


def parse_width_map(text: str | None) -> dict[int, float]:
    result = dict(DEFAULT_ACI_WIDTHS)
    for token in str(text or '').replace('\n', ';').split(';'):
        token = token.strip()
        if not token or ':' not in token:
            continue
        left, right = token.split(':', 1)
        try:
            aci = int(left.strip())
            width = float(right.strip())
        except (TypeError, ValueError):
            continue
        if 0 <= aci <= 255 and 0.0 <= width <= 20.0:
            result[aci] = width
    return result


def serialize_width_map(values: dict[int, float]) -> str:
    clean = {}
    for aci, width in values.items():
        try:
            aci = int(aci); width = float(width)
        except (TypeError, ValueError):
            continue
        if 0 <= aci <= 255 and 0.0 <= width <= 20.0:
            clean[aci] = width
    return ';'.join(f'{aci}:{width:g}' for aci, width in sorted(clean.items()))


def width_for_aci(aci: int | None, width_map: dict[int, float], default_width: float) -> float:
    """Return final pen width in pixels.

    ACI values are proportional add-on weights referenced to a 1 px base.
    The add-on scales together with the selected base width.
    Example: weight 0.013 -> base 1 px: +0.013 px, base 2 px: +0.026 px.
    Formula: final_width = base * (1.0 + weight)
    Unspecified/invalid ACI uses the base width unchanged.
    """
    base = max(0.0, float(default_width))
    try:
        aci = int(aci)
    except (TypeError, ValueError):
        return base
    weight = max(0.0, float(width_map.get(aci, 0.0)))
    return base * (1.0 + weight)
