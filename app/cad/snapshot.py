from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


@dataclass
class CadVec3:
    x: float
    y: float
    z: float = 0.0


def _vec(value) -> CadVec3:
    if isinstance(value, CadVec3):
        return value
    seq = list(value) if isinstance(value, (list, tuple)) else [0.0, 0.0, 0.0]
    return CadVec3(float(seq[0]), float(seq[1]), float(seq[2] if len(seq) > 2 else 0.0))


class CadSnapshotEntity:
    """ezdxf 객체가 아니라 CAD COM에서 읽은 최소 정보만 담는 내부 스냅샷 객체."""

    def __init__(self, kind: str, **dxf_values):
        self._kind = str(kind).upper()
        self.dxf = SimpleNamespace(**dxf_values)
        if not hasattr(self.dxf, "handle"):
            self.dxf.handle = ""
        if not hasattr(self.dxf, "layer"):
            self.dxf.layer = "0"
        self.attribs = []
        self.closed = bool(dxf_values.get("closed", False))
        self.is_closed = self.closed
        self.vertices = []
        self.text = str(dxf_values.get("text", "") or "")
        self._points: list[tuple[float, float]] = []

    def dxftype(self):
        return self._kind

    def get_points(self):
        return [(float(x), float(y)) for x, y in self._points]

    def plain_text(self):
        return self.text


class CadSnapshotBlockRef(CadSnapshotEntity):
    def __init__(self, name: str, insert, **dxf_values):
        super().__init__("INSERT", name=str(name), insert=_vec(insert), **dxf_values)

    def add_attrib(self, tag: str, text: str, insert=None):
        item = CadSnapshotEntity(
            "ATTRIB",
            tag=str(tag),
            text=str(text),
            insert=_vec(insert or (0.0, 0.0, 0.0)),
            layer=str(getattr(self.dxf, "layer", "0") or "0"),
            handle="",
        )
        self.attribs.append(item)
        return item


class CadSnapshotModelSpace(list):
    def _append(self, entity):
        self.append(entity)
        return entity

    def add_line(self, start, end, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        attrs.update(start=_vec(start), end=_vec(end))
        return self._append(CadSnapshotEntity("LINE", **attrs))

    def add_lwpolyline(self, points, close=False, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        entity = CadSnapshotEntity("LWPOLYLINE", closed=bool(close), **attrs)
        entity.closed = bool(close)
        entity.is_closed = bool(close)
        entity._points = [(float(p[0]), float(p[1])) for p in points]
        return self._append(entity)

    def add_text(self, text, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        attrs["insert"] = _vec(attrs.get("insert", (0.0, 0.0, 0.0)))
        attrs["text"] = str(text)
        entity = CadSnapshotEntity("TEXT", **attrs)
        entity.text = str(text)
        return self._append(entity)

    def add_mtext(self, text, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        attrs["insert"] = _vec(attrs.get("insert", (0.0, 0.0, 0.0)))
        attrs["text"] = str(text)
        entity = CadSnapshotEntity("MTEXT", **attrs)
        entity.text = str(text)
        return self._append(entity)

    def add_blockref(self, name, insert, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        return self._append(CadSnapshotBlockRef(name, insert, **attrs))

    def add_circle(self, center, radius, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        attrs.update(center=_vec(center), radius=float(radius))
        return self._append(CadSnapshotEntity("CIRCLE", **attrs))

    def add_arc(self, center, radius, start_angle, end_angle, dxfattribs=None):
        attrs = dict(dxfattribs or {})
        attrs.update(
            center=_vec(center), radius=float(radius),
            start_angle=float(start_angle), end_angle=float(end_angle),
        )
        return self._append(CadSnapshotEntity("ARC", **attrs))

    def query(self, query_text: str):
        wanted = {part.strip().upper() for part in str(query_text or "").replace(",", " ").split() if part.strip()}
        if not wanted:
            return []
        return [entity for entity in self if entity.dxftype() in wanted]


class CadSnapshotLayer:
    def __init__(self, name: str):
        self.dxf = SimpleNamespace(name=str(name), color=7)
        self.color = 7
        self.rgb = None
        self._off = False
        self._frozen = False

    def is_off(self):
        return self._off

    def is_frozen(self):
        return self._frozen


class CadSnapshotLayerTable:
    def __init__(self):
        self._items: dict[str, CadSnapshotLayer] = {}
        self.add("0")

    def add(self, name: str):
        key = str(name or "0").casefold()
        if key not in self._items:
            self._items[key] = CadSnapshotLayer(str(name or "0"))
        return self._items[key]

    def get(self, name: str):
        return self.add(name)


class CadSnapshotBlockTable:
    def __init__(self):
        self._names: dict[str, str] = {}

    def __contains__(self, name):
        return str(name).casefold() in self._names

    def new(self, name: str):
        self._names[str(name).casefold()] = str(name)
        return str(name)


class CadSnapshotDocument:
    """CAD 직접 읽기용 경량 문서.

    실제 DXF를 만들지 않고 기존 실/문자 분석 함수가 기대하는 최소 API만 제공한다.
    원본 CAD 배경은 ``_cad_background_primitives``에 직접 보존한다.
    """

    is_cad_snapshot = True

    def __init__(self):
        self._msp = CadSnapshotModelSpace()
        self.layouts = []
        self.layers = CadSnapshotLayerTable()
        self.blocks = CadSnapshotBlockTable()
        self._cad_background_primitives: list[dict] = []

    def modelspace(self):
        return self._msp
