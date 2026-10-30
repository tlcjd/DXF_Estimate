from __future__ import annotations

import math
from collections import OrderedDict

from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QLineF
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
    QFont,
    QFontMetricsF,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView

from app.common.models import RoomRecord, normalize_room_name, room_name_is_missing
from app.common.geometry import polygon_label_point
from app.rooms.finish_panel import first_room_finish_material, normalize_finish_group_type, normalize_finish_group_division, FINISH_GROUP_TYPES
from app.rooms.background_style import parse_width_map, width_for_aci


BACKGROUND_CACHE_LIMIT = 12


def polygon_centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    if not pts:
        return 0.0, 0.0
    if len(pts) < 3:
        return (
            sum(x for x, _ in pts) / len(pts),
            sum(y for _, y in pts) / len(pts),
        )

    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i, (x0, y0) in enumerate(pts):
        x1, y1 = pts[(i + 1) % len(pts)]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    if abs(area2) < 1e-9:
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0

    return cx / (3.0 * area2), cy / (3.0 * area2)


def clip_line_to_rect(x1, y1, x2, y2, xmin, ymin, xmax, ymax):
    """Cohen-Sutherland 방식으로 선분을 사각형 범위에 자른다."""
    left, right, bottom, top = 1, 2, 4, 8

    def code(x, y):
        result = 0
        if x < xmin:
            result |= left
        elif x > xmax:
            result |= right
        if y < ymin:
            result |= bottom
        elif y > ymax:
            result |= top
        return result

    c1 = code(x1, y1)
    c2 = code(x2, y2)
    while True:
        if not (c1 | c2):
            return x1, y1, x2, y2
        if c1 & c2:
            return None
        out = c1 or c2
        if out & top:
            if abs(y2 - y1) < 1e-12:
                return None
            x = x1 + (x2 - x1) * (ymax - y1) / (y2 - y1)
            y = ymax
        elif out & bottom:
            if abs(y2 - y1) < 1e-12:
                return None
            x = x1 + (x2 - x1) * (ymin - y1) / (y2 - y1)
            y = ymin
        elif out & right:
            if abs(x2 - x1) < 1e-12:
                return None
            y = y1 + (y2 - y1) * (xmax - x1) / (x2 - x1)
            x = xmax
        else:
            if abs(x2 - x1) < 1e-12:
                return None
            y = y1 + (y2 - y1) * (xmin - x1) / (x2 - x1)
            x = xmin

        if out == c1:
            x1, y1 = x, y
            c1 = code(x1, y1)
        else:
            x2, y2 = x, y
            c2 = code(x2, y2)



class CachedVectorBackgroundItem(QGraphicsItem):
    """도곽 단위 벡터 캐시를 한 개의 QGraphicsItem으로 렌더링한다."""
    def __init__(self, info: dict, style: str, opacity: int, default_width: float,
                 aci_widths: str, keep_red: bool, show_original_text: bool,
                 white_mode: bool = False, display_rect=None, display_polygons=None,
                 zoomout_width_weight: float = 0.25, zoom_ratio_provider=None, parent=None):
        super().__init__(parent)
        self.info = dict(info or {})
        self.style = 'cad' if str(style) == 'cad' else 'gray'
        self.default_width = float(default_width)
        self.width_map = parse_width_map(str(aci_widths or ''))
        self.keep_red = bool(keep_red)
        self.show_original_text = bool(show_original_text)
        self.white_mode = bool(white_mode)
        self.zoomout_width_weight = max(0.0, min(1.0, float(zoomout_width_weight)))
        self._zoom_ratio_provider = zoom_ratio_provider if callable(zoom_ratio_provider) else None
        self._grouped_mode = bool(self.info.get('groups'))
        self.group_settings = {
            str(key): {
                'visible': bool(dict(value or {}).get('visible', True)),
                'opacity_offset': int(dict(value or {}).get('opacity_offset', 0) or 0),
                'absolute_opacity': dict(value or {}).get('absolute_opacity'),
            }
            for key, value in dict(self.info.get('group_settings') or {}).items()
        }
        source_rect = tuple(self.info.get('crop_rect') or (0.0, 0.0, 1.0, 1.0))
        sxmin, symin, sxmax, symax = [float(v) for v in source_rect]
        if display_rect and len(display_rect) == 4:
            dxmin, dymin, dxmax, dymax = [float(v) for v in display_rect]
            self.xmin = max(sxmin, dxmin)
            self.ymin = max(symin, dymin)
            self.xmax = min(sxmax, dxmax)
            self.ymax = min(symax, dymax)
            if self.xmax <= self.xmin or self.ymax <= self.ymin:
                self.xmin, self.ymin, self.xmax, self.ymax = sxmin, symin, sxmax, symax
        else:
            self.xmin, self.ymin, self.xmax, self.ymax = sxmin, symin, sxmax, symax
        self._display_clip_path = None
        if display_polygons:
            clip_path = QPainterPath()
            for raw_points in display_polygons:
                try:
                    points = [(float(x), float(y)) for x, y in raw_points]
                except Exception:
                    continue
                if len(points) < 3:
                    continue
                polygon = QPolygonF([QPointF(x-self.xmin, self.ymax-y) for x, y in points])
                clip_path.addPolygon(polygon)
            if not clip_path.isEmpty():
                self._display_clip_path = clip_path
        self.base_opacity = max(0, min(100, int(opacity)))
        self.setOpacity(1.0 if self._grouped_mode else self.base_opacity / 100.0)
        self.setZValue(-100)
        # V24.15: 최적화 도면의 투명도 보정은 QGraphicsOpacityEffect/DeviceCoordinateCache를
        # 사용하지 않는다. 해당 방식은 큰 도곽을 확대할 때 장치좌표 래스터 캐시가 확대되어
        # 선두께/클리핑이 깨지는 문제가 있었다. 대신 현재 노출 영역만 그룹별 투명 이미지에
        # 100% 불투명으로 그린 뒤 그 이미지에 그룹 투명도를 한 번만 적용한다.
        # 따라서 그룹 내부 중첩부 alpha 누적은 막으면서 Pan/Zoom 때 매번 현재 배율로 다시 그린다.
        if self._grouped_mode:
            self._group_paths = {}
            for group_key, raw in dict(self.info.get('groups') or {}).items():
                data = dict(raw or {})
                self._group_paths[str(group_key)] = {
                    'segments': self._build_segment_paths(data.get('segments') or []),
                    'annotation_segments': self._build_segment_paths(data.get('annotation_segments') or []),
                    'polylines': self._build_polyline_paths(data.get('polylines') or []),
                    'annotation_polylines': self._build_polyline_paths(data.get('annotation_polylines') or []),
                    'fills': self._build_fill_paths(data.get('fills') or []),
                    'texts': [dict(v) for v in (data.get('texts') or [])],
                }
            self._static_segment_paths = {}
            self._static_annotation_paths = {}
            self._static_fill_paths = {}
        else:
            self._group_paths = {}
            self._static_segment_paths = self._build_segment_paths(self.info.get('segments') or [])
            self._static_annotation_paths = self._build_segment_paths(self.info.get('annotation_segments') or [])
            self._static_fill_paths = self._build_fill_paths(self.info.get('fills') or [])

    def boundingRect(self):
        return QRectF(0.0, 0.0, max(self.xmax-self.xmin, 1.0), max(self.ymax-self.ymin, 1.0))

    def _color(self, rgb, aci=None):
        try:
            if self.style == 'cad' or (self.style == 'gray' and self.keep_red and aci == 1):
                color = QColor(*[int(v) for v in rgb[:3]])
            else:
                color = QColor(150, 150, 150)
            if self.white_mode:
                color = QColor(255-color.red(), 255-color.green(), 255-color.blue())
            return color
        except Exception:
            return QColor(105,105,105) if self.white_mode else QColor(150,150,150)

    def _build_segment_paths(self, segments):
        grouped = {}
        for segment in segments or []:
            try:
                x1,y1,x2,y2 = [float(v) for v in segment[:4]]
                clipped = clip_line_to_rect(x1, y1, x2, y2, self.xmin, self.ymin, self.xmax, self.ymax)
                if clipped is None:
                    continue
                x1, y1, x2, y2 = clipped
                rgb = tuple(segment[4:7]) if len(segment) >= 7 else (170,170,170)
                aci = int(segment[7]) if len(segment) >= 8 and int(segment[7]) >= 0 else None
                width = max(0.1, width_for_aci(aci, self.width_map, self.default_width))
                key = (tuple(int(v) for v in rgb[:3]), aci, round(width, 4))
                path = grouped.get(key)
                if path is None:
                    path = QPainterPath()
                    grouped[key] = path
                path.moveTo(QPointF(x1-self.xmin, self.ymax-y1))
                path.lineTo(QPointF(x2-self.xmin, self.ymax-y2))
            except Exception:
                continue
        return grouped

    def _build_polyline_paths(self, polylines):
        grouped = {}
        for raw in polylines or []:
            try:
                pts = [(float(x), float(y)) for x, y in (raw.get('points') or [])]
                if len(pts) < 2:
                    continue
                rgb = tuple(raw.get('rgb') or (170,170,170))
                aci = int(raw.get('aci', -1)); aci = None if aci < 0 else aci
                width = max(0.1, width_for_aci(aci, self.width_map, self.default_width))
                key = (tuple(int(v) for v in rgb[:3]), aci, round(width, 4))
                path = grouped.get(key)
                if path is None:
                    path = QPainterPath()
                    grouped[key] = path
                started = False
                last = None
                for i in range(len(pts)-1):
                    clipped = clip_line_to_rect(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], self.xmin, self.ymin, self.xmax, self.ymax)
                    if clipped is None:
                        started = False; last = None
                        continue
                    x1, y1, x2, y2 = clipped
                    p1 = QPointF(x1-self.xmin, self.ymax-y1)
                    p2 = QPointF(x2-self.xmin, self.ymax-y2)
                    key1 = (round(p1.x(), 6), round(p1.y(), 6))
                    if not started or last != key1:
                        path.moveTo(p1)
                    path.lineTo(p2)
                    started = True
                    last = (round(p2.x(), 6), round(p2.y(), 6))
            except Exception:
                continue
        return grouped

    def _build_fill_paths(self, fills):
        grouped = {}
        for raw in fills or []:
            try:
                pts = [(float(x), float(y)) for x, y in (raw.get('points') or [])]
                if len(pts) < 3:
                    continue
                rgb = tuple(raw.get('rgb') or (170,170,170))
                aci = int(raw.get('aci', -1)); aci = None if aci < 0 else aci
                key = (tuple(int(v) for v in rgb[:3]), aci)
                path = grouped.get(key)
                if path is None:
                    path = QPainterPath()
                    grouped[key] = path
                polygon = QPolygonF([QPointF(x-self.xmin, self.ymax-y) for x,y in pts])
                path.addPolygon(polygon)
            except Exception:
                continue
        return grouped

    def _zoomout_width_factor(self) -> float:
        """전체보기보다 축소했을 때만 cosmetic 선을 부드럽게 가늘게 만든다.

        0.0 = 기존처럼 화면 px 고정, 1.0 = 축소 배율에 최대한 비례.
        확대(ratio >= 1)는 원래 선두께를 유지한다.
        """
        if self.zoomout_width_weight <= 1e-9 or self._zoom_ratio_provider is None:
            return 1.0
        try:
            ratio = max(1e-6, float(self._zoom_ratio_provider()))
        except Exception:
            return 1.0
        if ratio >= 1.0:
            return 1.0
        factor = ratio ** self.zoomout_width_weight
        return max(0.25, min(1.0, factor))

    def _draw_cached_segments(self, painter, grouped):
        painter.setBrush(Qt.NoBrush)
        zoom_factor = self._zoomout_width_factor()
        for (rgb, aci, width), path in grouped.items():
            pen = QPen(self._color(rgb, aci))
            pen.setWidthF(max(0.1, float(width) * zoom_factor))
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(path)

    def _draw_cached_fills(self, painter):
        painter.setPen(Qt.NoPen)
        for (rgb, aci), path in self._static_fill_paths.items():
            painter.setBrush(QBrush(self._color(rgb, aci)))
            painter.drawPath(path)

    def _draw_text_item(self, painter, raw):
        """CAD 문자를 장면 좌표의 벡터 외곽선으로 그린다.

        QPainter.drawText()에 수백~수천 px 폰트를 넣고 장면을 축소하면 Qt가 큰
        글리프 래스터를 다시 축소하면서 선보다 문자가 흐려지는 현상이 생긴다.
        QPainterPath.addText()로 글리프 윤곽을 만든 뒤 현재 장면 변환에서 직접
        채우면 확대/축소 시 선과 같은 벡터 품질을 유지한다.
        """
        text = str(raw.get('text','') or '')
        if not text:
            return
        x=float(raw.get('x',0)); y=float(raw.get('y',0))
        if not (self.xmin <= x <= self.xmax and self.ymin <= y <= self.ymax):
            return
        height=max(1.0,float(raw.get('height',200) or 200))
        rotation=float(raw.get('rotation',0) or 0)
        rgb=tuple(raw.get('rgb') or (170,170,170))
        aci=int(raw.get('aci',-1)); aci=None if aci < 0 else aci
        halign=int(raw.get('halign',0) or 0)
        valign=int(raw.get('valign',0) or 0)
        attachment=int(raw.get('attachment',0) or 0)
        lines = text.replace('\\P', '\n').splitlines() or [text]

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.translate(QPointF(x-self.xmin, self.ymax-y))
        painter.rotate(-rotation)

        font=painter.font()
        font.setPixelSize(max(1, min(5000, int(round(height)))))
        try:
            font.setHintingPreference(QFont.PreferFullHinting)
        except Exception:
            pass
        fm=QFontMetricsF(font)
        ascent=fm.ascent(); descent=fm.descent(); line_step=max(1.0, fm.height()) * 1.15
        color=self._color(rgb, aci)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(color))

        for line_no, line in enumerate(lines):
            if not line:
                continue
            width=max(1.0, fm.horizontalAdvance(line))
            # TEXT: halign 0=Left, 1=Center, 2=Right, 4=Middle.
            # MTEXT: attachment 1~9 = 좌/중/우 x 상/중/하.
            if attachment:
                col=(attachment-1)%3
                row=(attachment-1)//3
                xoff = 0.0 if col == 0 else (-width/2.0 if col == 1 else -width)
                if row == 0:
                    yoff = ascent
                elif row == 1:
                    yoff = (ascent-descent)/2.0
                else:
                    yoff = -descent
            else:
                xoff = -width/2.0 if halign in (1,4) else (-width if halign == 2 else 0.0)
                if valign == 3:
                    yoff = ascent
                elif valign == 2:
                    yoff = (ascent-descent)/2.0
                elif valign == 1:
                    yoff = -descent
                else:
                    yoff = 0.0
            glyph_path = QPainterPath()
            glyph_path.addText(QPointF(xoff, yoff + line_no * line_step), font, line)
            painter.drawPath(glyph_path)
        painter.restore()

    def _group_opacity(self, group_key: str) -> float:
        cfg = dict(self.group_settings.get(str(group_key)) or {})
        # 그룹 편집 같은 임시 표시에서는 저장값을 건드리지 않고 절대 불투명도를 지정할 수 있다.
        if cfg.get('absolute_opacity') is not None:
            try:
                value = float(cfg.get('absolute_opacity'))
            except Exception:
                value = 100.0
            return max(0.0, min(100.0, value)) / 100.0
        value = self.base_opacity + int(cfg.get('opacity_offset', 0) or 0)
        return max(0.0, min(100.0, float(value))) / 100.0

    def _group_visible(self, group_key: str) -> bool:
        return bool(dict(self.group_settings.get(str(group_key)) or {}).get('visible', True))

    def _draw_group_content(self, painter, data: dict):
        data = dict(data or {})
        if data.get('fills'):
            painter.setPen(Qt.NoPen)
            for (rgb, aci), path in dict(data.get('fills') or {}).items():
                painter.setBrush(QBrush(self._color(rgb, aci)))
                painter.drawPath(path)
        self._draw_cached_segments(painter, data.get('polylines') or {})
        self._draw_cached_segments(painter, data.get('annotation_polylines') or {})
        # V2/호환 캐시에 segment가 남아 있으면 함께 표시한다.
        self._draw_cached_segments(painter, data.get('segments') or {})
        self._draw_cached_segments(painter, data.get('annotation_segments') or {})
        if self.show_original_text:
            for raw in data.get('texts') or []:
                try:
                    self._draw_text_item(painter, raw)
                except Exception:
                    continue

    def _draw_group_composited(self, painter, option, data: dict, opacity: float):
        data = dict(data or {})
        if not any(data.get(key) for key in ("segments", "annotation_segments", "polylines", "annotation_polylines", "fills", "texts")):
            return
        opacity = max(0.0, min(1.0, float(opacity)))
        if opacity <= 0.0:
            return
        if opacity >= 0.999:
            self._draw_group_content(painter, data)
            return

        # 현재 화면에 노출된 영역만 매 배율에서 새로 렌더한다.
        exposed = QRectF(self.boundingRect())
        try:
            candidate = QRectF(option.exposedRect)
            if candidate.isValid() and not candidate.isEmpty():
                exposed = exposed.intersected(candidate)
        except Exception:
            pass
        if not exposed.isValid() or exposed.isEmpty():
            return

        transform = painter.worldTransform()
        sx = max(1e-6, math.hypot(float(transform.m11()), float(transform.m12())))
        sy = max(1e-6, math.hypot(float(transform.m21()), float(transform.m22())))
        width = max(1, int(math.ceil(exposed.width() * sx)) + 4)
        height = max(1, int(math.ceil(exposed.height() * sy)) + 4)

        # 비정상적으로 큰 offscreen 버퍼는 만들지 않는다. 이 경우만 기존 alpha 방식으로
        # 안전하게 fallback하고 다음 프레임에서 정상 노출영역이 들어오면 다시 합성한다.
        if width > 8192 or height > 8192 or width * height > 24_000_000:
            painter.save()
            painter.setOpacity(opacity)
            self._draw_group_content(painter, data)
            painter.restore()
            return

        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        layer_painter = QPainter(image)
        try:
            layer_painter.setRenderHint(QPainter.Antialiasing, True)
            layer_painter.setRenderHint(QPainter.TextAntialiasing, True)
            # item 좌표 -> offscreen pixel 좌표. cosmetic pen은 화면 px 기준 두께를 유지한다.
            layer_painter.scale(sx, sy)
            layer_painter.translate(-exposed.left(), -exposed.top())
            layer_painter.setClipRect(exposed, Qt.IntersectClip)
            self._draw_group_content(layer_painter, data)
        finally:
            layer_painter.end()

        painter.save()
        painter.setOpacity(opacity)
        painter.drawImage(exposed, image)
        painter.restore()

    def paint(self, painter, option, widget=None):
        painter.save()
        if self._display_clip_path is not None:
            painter.setClipPath(self._display_clip_path, Qt.IntersectClip)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        if self._grouped_mode:
            for group_key in ('HATCH', 'LINE', 'WINDOW', 'BLOCK', 'DIMENSION', 'TEXT'):
                if not self._group_visible(group_key):
                    continue
                data = dict(self._group_paths.get(group_key) or {})
                self._draw_group_composited(painter, option, data, self._group_opacity(group_key))
        else:
            self._draw_cached_fills(painter)
            self._draw_cached_segments(painter, self._static_segment_paths)
            if self.show_original_text:
                self._draw_cached_segments(painter, self._static_annotation_paths)
                for raw in self.info.get('texts') or []:
                    try:
                        self._draw_text_item(painter, raw)
                    except Exception:
                        continue
        painter.restore()


class RoomPreview(QGraphicsView):
    room_clicked = Signal(object)
    empty_clicked = Signal()
    room_edit_requested = Signal(object)
    wall_segment_clicked = Signal(object, bool)
    wall_segments_box_selected = Signal(object, bool, bool)
    group_room_clicked = Signal(str)
    group_rooms_box_selected = Signal(object, bool, bool)
    escape_pressed = Signal()
    opening_selection_changed = Signal(object)
    opening_delete_requested = Signal(object)
    opening_move_preview_changed = Signal(object)
    opening_move_requested = Signal(object, float, float)
    opening_nudge_requested = Signal(object, float, float)
    opening_code_edit_requested = Signal(object)
    opening_catalog_requested = Signal()
    opening_catalog_drop_requested = Signal(str)
    boundary_split_hover_changed = Signal(object)
    boundary_segment_split_requested = Signal(object, int, float)
    boundary_merge_hover_changed = Signal(object)
    boundary_vertex_merge_requested = Signal(object, int)
    vector_direct_selection_changed = Signal(object)
    vector_direct_undo_requested = Signal()
    vector_direct_off_requested = Signal()
    vector_group_edit_selection_changed = Signal(object)
    vector_group_edit_transition_requested = Signal()
    vector_group_edit_undo_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QBrush(Qt.white))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setMinimumSize(220, 180)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

        self._fit_rect = None
        self._fit_scale = 1.0
        self.label_font_size = 100
        self.label_area_scale = 1.0
        self.label_scale_mode = "room_area"
        self._current_display_width = 30000.0
        # 현재 장면의 실제 도면 표시범위(world 좌표).
        # 화면표시 옵션만 바뀌는 공통 redraw에서는 이 범위가 같으면
        # sceneRect/scrollbar를 그대로 재사용해 도곽이 밀리지 않게 한다.
        self._current_display_bounds = None
        self.edge_length_font_size = 45
        self.edge_length_offset = 150
        self.edge_length_width_ratio = 80
        self.edge_length_show_unit_m = True
        self.label_full_view_weight = 0.70
        self.label_selected_view_weight = 1.00
        self._label_view_mode = "floor"
        self.floor_horizontal_pan_margin_ratio = 1.5
        self.floor_vertical_pan_margin_ratio = 1.5
        self.background_max_pixels = 1800
        self._current_rooms: list[RoomRecord] = []
        self._current_selected: RoomRecord | None = None
        self._current_selected_room_handles: set[str] = set()
        self._show_edge_lengths = False
        self._room_polygons: list[tuple[RoomRecord, QPolygonF]] = []
        self._room_display_items: dict[int, tuple[object, object]] = {}
        self._deduction_display_items: list[tuple[object, QColor, float, object]] = []
        self._ceiling_display_items: list[tuple[object, QColor, float, object]] = []
        self._edge_length_items: list[tuple[object, float]] = []
        self._current_minx = 0.0
        self._current_miny = 0.0
        self._mouse_press_pos = None
        self._middle_panning = False
        self._middle_pan_pos = None
        self.cad_background = []
        self.show_cad_background = True
        self.cad_background_mode = "black"
        self.show_room_overlay = True
        self.show_original_text = True
        self.background_style = "gray"
        self.background_line_width = 1.5
        self.background_zoomout_width_weight = 0.25
        self.background_aci_widths = ""
        self.background_keep_red = False
        self.background_opacity = 100
        self.boundary_color = QColor("#0066FF")
        self.deduction_color = QColor("#FF00FF")
        # V18.2: 실명 또는 실번호가 없는 미완성 실은 전체보기에서 회색톤으로 낮춰 구분한다.
        self.incomplete_room_color = QColor("#8A8A8A")
        self.boundary_width = 9
        self.deduction_width = 1
        self.render_quality = "high"
        self.selection_color = QColor("#FF0000")
        self.selection_width = 18
        self.wall_selection_color = QColor("#FF00FF")
        self.wall_selection_width = 2
        self.opening_line_color = QColor("#8ED4E3")
        self.opening_line_width = 4
        # 도곽 오버레이 선 공통 줌 배율.
        # 0.0=화면 px 고정, 1.0=전체보기 대비 줌 배율에 완전 비례.
        # 기존 WallSelectionZoomScale 설정값과 호환하기 위해 속성명은 유지한다.
        self.wall_selection_zoom_scale = 0.35
        self.show_group_overlay = True
        self.group_overlay_opacity = 35
        # 재료마감 표시 필터는 대분류 탭 + 탭 내부 구분으로 관리한다.
        # division="*"이면 해당 탭의 모든 구분, 빈 type이면 표시 없음이다.
        self.finish_group_filter_type = "마감"
        self.finish_group_filter_division = "*"
        self._background_generation = 0
        self._background_cache: OrderedDict[tuple, tuple[QPixmap, tuple[float, float, float, float]]] = OrderedDict()
        self.floor_background_files: dict[str, dict] = {}
        self.floor_vector_cache: dict[str, dict] = {}
        self.background_render_mode = "png"
        self.wall_finish_mode = False
        self.room_group_mode = False
        self.wall_finish_assignments: dict[str, dict] = {}
        self.selected_wall_keys: set[str] = set()
        self._wall_segments: list[tuple[str, QPointF, QPointF]] = []
        self._wall_segment_items: dict[str, tuple[object, dict]] = {}
        self.selected_group_room_handles: set[str] = set()
        self.room_finish_assignments: dict[str, dict] = {}
        self.finish_groups: dict[str, dict] = {}
        self.focused_finish_group_id = ""
        self._wall_group_by_key: dict[str, dict] = {}
        self._wall_groups_by_key: dict[str, dict[str, dict[str, dict]]] = {}
        self._selection_rect_item = None
        self._selection_start_scene = None
        # V23.46: 연결 CAD 창호 읽기 결과. CAD 배경 위에 임시 부호만 표시한다.
        self.opening_test_floor = ""
        self.opening_test_markers: list[dict] = []
        self.opening_edit_mode = False
        self.selected_opening_ids: set[str] = set()
        self._opening_marker_items: dict[str, object] = {}
        self._opening_marker_aux_items: dict[str, list[object]] = {}
        self._opening_drag_start_scene = None
        self._opening_drag_anchor_scene = None
        self._opening_drag_ids: set[str] = set()
        self._opening_drag_item_positions: dict[str, QPointF] = {}
        self._opening_drag_aux_positions: dict[str, list[QPointF]] = {}
        self._opening_drag_last_delta = (0.0, 0.0)
        self._opening_drag_feedback_items: list[object] = []
        # 층 전체보기에서 실 구분선 선분을 마우스 위치 기준 10mm 스냅으로 나누는 편집모드.
        self.boundary_split_mode = False
        self.boundary_merge_mode = False
        self._boundary_split_highlight_items: list[object] = []
        self._boundary_split_endpoint_markers: dict[int, object] = {}
        self._boundary_split_reference_index = 0
        self._boundary_split_hover_key = None
        self._boundary_split_hover_payload = None
        # 도면정리 > 도면 직접선택 모드
        self.vector_direct_select_mode = False
        self.vector_direct_all_layers_view = False
        self.selected_vector_filter_keys: set[tuple[str, str]] = set()
        self._vector_direct_highlight_items: list[object] = []
        self.vector_group_edit_mode = False
        self.vector_group_edit_group = ""
        self.selected_vector_group_edit_ids: set[str] = set()
        self._vector_group_edit_highlight_items: list[object] = []
        self._current_background_floor = ""


    def set_vector_direct_select_mode(self, enabled: bool) -> None:
        self.vector_direct_select_mode = bool(enabled)
        self.selected_vector_filter_keys.clear()
        self._clear_vector_direct_highlights()
        if self.vector_direct_select_mode:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()
            self.setDragMode(QGraphicsView.NoDrag if (self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode or self.boundary_split_mode or self.boundary_merge_mode) else QGraphicsView.ScrollHandDrag)
        self.vector_direct_selection_changed.emit(set(self.selected_vector_filter_keys))

    def set_vector_direct_all_layers_view(self, enabled: bool) -> None:
        self.vector_direct_all_layers_view = bool(enabled)
        self._refresh_vector_direct_highlights()

    def clear_vector_direct_selection(self) -> None:
        self.selected_vector_filter_keys.clear()
        self._refresh_vector_direct_highlights()
        self.vector_direct_selection_changed.emit(set())

    def _clear_vector_direct_highlights(self) -> None:
        scene = self.scene()
        for item in list(self._vector_direct_highlight_items):
            try:
                scene.removeItem(item)
            except Exception:
                pass
        self._vector_direct_highlight_items = []

    @staticmethod
    def _vector_filter_key(meta: dict):
        meta = dict(meta or {})
        block = str(meta.get("block_name", "") or "").strip()
        if block:
            return ("block", block)
        layer = str(meta.get("layer", "") or "").strip()
        return ("layer", layer) if layer else None

    @staticmethod
    def _segment_meta_for_pick(seg):
        try:
            return dict(seg[9]) if len(seg) >= 10 and isinstance(seg[9], dict) else {}
        except Exception:
            return {}

    def _current_vector_floor_info(self):
        floor = str(self._current_background_floor or "").strip()
        if not floor and self._current_rooms:
            floor = str(self._current_rooms[0].floor or "").strip()
        if floor and floor in self.floor_vector_cache:
            return floor, dict(self.floor_vector_cache.get(floor) or {})
        bounds = tuple(self._current_display_bounds or ())
        if len(bounds) == 4:
            for name, raw in self.floor_vector_cache.items():
                crop = tuple(dict(raw or {}).get("crop_rect") or ())
                if len(crop) == 4 and all(abs(float(a)-float(b)) < 1e-3 for a,b in zip(crop,bounds)):
                    return str(name), dict(raw or {})
        return "", {}

    def _iter_vector_pick_geometry(self):
        _floor, info = self._current_vector_floor_info()
        if not info:
            return
        groups = dict(info.get("groups") or {})
        datasets = [dict(v or {}) for v in groups.values()] if groups else [info]
        for data in datasets:
            for name in ("segments", "annotation_segments"):
                for seg in data.get(name) or []:
                    try:
                        meta = self._segment_meta_for_pick(seg)
                        key = self._vector_filter_key(meta)
                        if not key: continue
                        yield key, "line", [(float(seg[0]), float(seg[1])), (float(seg[2]), float(seg[3]))]
                    except Exception:
                        continue
            for name in ("polylines", "annotation_polylines"):
                for poly in data.get(name) or []:
                    try:
                        raw = dict(poly or {})
                        key = self._vector_filter_key(raw)
                        pts = [(float(x),float(y)) for x,y in (raw.get("points") or [])]
                        if key and len(pts) >= 2:
                            yield key, "polyline", pts
                    except Exception:
                        continue
            for raw in data.get("fills") or []:
                try:
                    raw = dict(raw or {}); meta = dict(raw.get("meta") or raw)
                    key = self._vector_filter_key(meta)
                    pts = [(float(x),float(y)) for x,y in (raw.get("points") or [])]
                    if key and len(pts) >= 3:
                        yield key, "fill", pts
                except Exception:
                    continue
            for raw in data.get("texts") or []:
                try:
                    raw = dict(raw or {}); meta = dict(raw.get("meta") or raw)
                    key = self._vector_filter_key(meta)
                    if not key: continue
                    x=float(raw.get("x",0)); y=float(raw.get("y",0)); h=max(20.0,float(raw.get("height",200) or 200))
                    w=max(h, len(str(raw.get("text","") or ""))*h*0.6)
                    yield key, "text", [(x-w*0.5,y-h*0.5),(x+w*0.5,y+h*0.5)]
                except Exception:
                    continue

    def _world_point_to_scene(self, pt):
        x,y=pt
        return QPointF(float(x)-self._current_minx, self._current_miny-float(y))

    @staticmethod
    def _point_segment_distance(p: QPointF, a: QPointF, b: QPointF) -> float:
        vx=b.x()-a.x(); vy=b.y()-a.y(); wx=p.x()-a.x(); wy=p.y()-a.y()
        l2=vx*vx+vy*vy
        if l2 <= 1e-12: return math.hypot(wx,wy)
        t=max(0.0,min(1.0,(wx*vx+wy*vy)/l2))
        return math.hypot(p.x()-(a.x()+t*vx), p.y()-(a.y()+t*vy))

    def _vector_keys_at_point(self, scene_pos: QPointF):
        tol = 10.0 / max(abs(self.transform().m11()), 1e-6)
        best = None
        for key, kind, pts in self._iter_vector_pick_geometry() or []:
            sp=[self._world_point_to_scene(v) for v in pts]
            distance=None
            if kind in ("line","polyline"):
                # RoomPreview에는 기존 벽체 선택용 6인자 거리 함수가 있으므로
                # QPointF 전용 호출을 하지 않고 좌표 6개를 명시해 단순 클릭 오류를 막는다.
                values=[
                    self._point_segment_distance(
                        scene_pos.x(), scene_pos.y(),
                        sp[i].x(), sp[i].y(), sp[i+1].x(), sp[i+1].y()
                    )
                    for i in range(len(sp)-1)
                ]
                distance=min(values) if values else None
            elif kind == "fill":
                if QPolygonF(sp).containsPoint(scene_pos, Qt.OddEvenFill): distance=0.0
            elif kind == "text":
                if QRectF(sp[0],sp[1]).normalized().contains(scene_pos): distance=0.0
            if distance is not None and distance <= tol and (best is None or distance < best[0]):
                best=(distance,key)
        return {best[1]} if best else set()

    def _vector_keys_in_rect(self, rect: QRectF, left_to_right: bool):
        keys=set()
        for key, kind, pts in self._iter_vector_pick_geometry() or []:
            sp=[self._world_point_to_scene(v) for v in pts]
            bounds = QPolygonF(sp).boundingRect() if kind == "fill" else QRectF(sp[0],sp[-1]).normalized()
            if kind in ("line","polyline"):
                xs=[p.x() for p in sp]; ys=[p.y() for p in sp]; bounds=QRectF(min(xs),min(ys),max(xs)-min(xs),max(ys)-min(ys))
            selected = rect.contains(bounds) if left_to_right else rect.intersects(bounds)
            if selected: keys.add(key)
        return keys

    def _refresh_vector_direct_highlights(self) -> None:
        self._clear_vector_direct_highlights()
        if not self.vector_direct_select_mode or not self.selected_vector_filter_keys:
            self.viewport().update(); return
        if bool(getattr(self, "vector_direct_all_layers_view", False)):
            color = QColor(0, 120, 255)
            brush_color = QColor(0, 120, 255, 45)
        else:
            color = QColor(255, 59, 48)
            brush_color = QColor(255, 59, 48, 45)
        pen=QPen(color); pen.setWidthF(max(2.0, 5.0/max(abs(self.transform().m11()),1e-6))); pen.setCosmetic(False)
        brush=QBrush(brush_color)
        for key, kind, pts in self._iter_vector_pick_geometry() or []:
            if key not in self.selected_vector_filter_keys: continue
            sp=[self._world_point_to_scene(v) for v in pts]
            try:
                if kind in ("line","polyline"):
                    path=QPainterPath(sp[0])
                    for pt in sp[1:]: path.lineTo(pt)
                    item=self.scene().addPath(path,pen); item.setZValue(180); self._vector_direct_highlight_items.append(item)
                elif kind == "fill":
                    item=self.scene().addPolygon(QPolygonF(sp),pen,brush); item.setZValue(180); self._vector_direct_highlight_items.append(item)
                elif kind == "text":
                    item=self.scene().addRect(QRectF(sp[0],sp[1]).normalized(),pen,brush); item.setZValue(180); self._vector_direct_highlight_items.append(item)
            except Exception:
                pass
        self.viewport().update()

    def set_vector_group_edit_mode(self, enabled: bool, group_key: str = "") -> None:
        self.vector_group_edit_mode = bool(enabled)
        self.vector_group_edit_group = str(group_key or "") if enabled else ""
        self.selected_vector_group_edit_ids.clear()
        self._clear_vector_group_edit_highlights()
        if self.vector_group_edit_mode:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()
            self.setDragMode(QGraphicsView.NoDrag if (self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode or self.boundary_split_mode or self.boundary_merge_mode) else QGraphicsView.ScrollHandDrag)
        self.vector_group_edit_selection_changed.emit(set())

    def clear_vector_group_edit_selection(self) -> None:
        self.selected_vector_group_edit_ids.clear()
        self._refresh_vector_group_edit_highlights()
        self.vector_group_edit_selection_changed.emit(set())

    def _clear_vector_group_edit_highlights(self) -> None:
        scene = self.scene()
        for item in list(self._vector_group_edit_highlight_items):
            try: scene.removeItem(item)
            except Exception: pass
        self._vector_group_edit_highlight_items = []

    def _iter_vector_group_edit_geometry(self):
        _floor, info = self._current_vector_floor_info()
        if not info:
            return
        group_key = str(getattr(self, "vector_group_edit_group", "") or "")
        data = dict(dict(info.get("groups") or {}).get(group_key) or {})
        for collection in ("polylines", "annotation_polylines"):
            for raw in data.get(collection) or []:
                try:
                    raw = dict(raw or {})
                    edit_id = str(raw.get("_edit_id", "") or "")
                    pts = [(float(x), float(y)) for x, y in (raw.get("points") or [])]
                    if edit_id and len(pts) >= 2:
                        yield edit_id, "polyline", pts
                except Exception: continue
        for raw in data.get("fills") or []:
            try:
                raw = dict(raw or {}); edit_id = str(raw.get("_edit_id", "") or "")
                pts = [(float(x), float(y)) for x, y in (raw.get("points") or [])]
                if edit_id and len(pts) >= 3: yield edit_id, "fill", pts
            except Exception: continue
        for raw in data.get("texts") or []:
            try:
                raw = dict(raw or {}); edit_id = str(raw.get("_edit_id", "") or "")
                if not edit_id: continue
                x=float(raw.get("x",0)); y=float(raw.get("y",0)); h=max(20.0,float(raw.get("height",200) or 200))
                w=max(h, len(str(raw.get("text","") or ""))*h*0.6)
                yield edit_id, "text", [(x-w*0.5,y-h*0.5),(x+w*0.5,y+h*0.5)]
            except Exception: continue

    def _vector_group_edit_ids_at_point(self, scene_pos: QPointF):
        tol = 10.0 / max(abs(self.transform().m11()), 1e-6)
        best = None
        for edit_id, kind, pts in self._iter_vector_group_edit_geometry() or []:
            sp=[self._world_point_to_scene(v) for v in pts]
            distance=None
            if kind == "polyline":
                vals=[self._point_segment_distance(scene_pos.x(),scene_pos.y(),sp[i].x(),sp[i].y(),sp[i+1].x(),sp[i+1].y()) for i in range(len(sp)-1)]
                distance=min(vals) if vals else None
            elif kind == "fill":
                if QPolygonF(sp).containsPoint(scene_pos, Qt.OddEvenFill): distance=0.0
            elif kind == "text":
                if QRectF(sp[0],sp[1]).normalized().contains(scene_pos): distance=0.0
            if distance is not None and distance <= tol and (best is None or distance < best[0]):
                best=(distance,edit_id)
        return {best[1]} if best else set()

    def _vector_group_edit_ids_in_rect(self, rect: QRectF, left_to_right: bool):
        ids=set()
        for edit_id, kind, pts in self._iter_vector_group_edit_geometry() or []:
            sp=[self._world_point_to_scene(v) for v in pts]
            if kind == "fill": bounds=QPolygonF(sp).boundingRect()
            elif kind == "text": bounds=QRectF(sp[0],sp[1]).normalized()
            else:
                xs=[p.x() for p in sp]; ys=[p.y() for p in sp]; bounds=QRectF(min(xs),min(ys),max(xs)-min(xs),max(ys)-min(ys))
            if (rect.contains(bounds) if left_to_right else rect.intersects(bounds)):
                ids.add(edit_id)
        return ids

    def _refresh_vector_group_edit_highlights(self):
        self._clear_vector_group_edit_highlights()
        if not self.vector_group_edit_mode or not self.selected_vector_group_edit_ids:
            self.viewport().update(); return
        pen=QPen(QColor(0,120,255)); pen.setWidthF(max(2.0,5.0/max(abs(self.transform().m11()),1e-6))); pen.setCosmetic(False)
        brush=QBrush(QColor(0,120,255,45))
        for edit_id, kind, pts in self._iter_vector_group_edit_geometry() or []:
            if edit_id not in self.selected_vector_group_edit_ids: continue
            sp=[self._world_point_to_scene(v) for v in pts]
            try:
                if kind == "polyline":
                    path=QPainterPath(sp[0])
                    for pt in sp[1:]: path.lineTo(pt)
                    item=self.scene().addPath(path,pen)
                elif kind == "fill": item=self.scene().addPolygon(QPolygonF(sp),pen,brush)
                else: item=self.scene().addRect(QRectF(sp[0],sp[1]).normalized(),pen,brush)
                item.setZValue(185); self._vector_group_edit_highlight_items.append(item)
            except Exception: pass
        self.viewport().update()

    def set_boundary_split_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.boundary_split_mode:
            return
        self.boundary_split_mode = enabled
        self.clear_boundary_split_highlight()
        if enabled:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()
            self.setDragMode(
                QGraphicsView.NoDrag
                if (self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode or self.boundary_merge_mode)
                else QGraphicsView.ScrollHandDrag
            )

    def set_boundary_merge_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.boundary_merge_mode:
            return
        self.boundary_merge_mode = enabled
        self.clear_boundary_split_highlight()
        if enabled:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()
            self.setDragMode(
                QGraphicsView.NoDrag
                if (self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode or self.boundary_split_mode)
                else QGraphicsView.ScrollHandDrag
            )

    def clear_boundary_split_highlight(self) -> None:
        scene = self.scene()
        for item in list(self._boundary_split_highlight_items):
            try:
                scene.removeItem(item)
            except Exception:
                pass
        self._boundary_split_highlight_items = []
        self._boundary_split_endpoint_markers = {}
        self._boundary_split_reference_index = 0
        self._boundary_split_hover_key = None
        self._boundary_split_hover_payload = None
        self.viewport().update()

    def set_boundary_split_reference(self, index: int) -> None:
        # V23.56부터 마우스 위치를 기준으로 바로 나누므로 호환용으로만 유지한다.
        self._boundary_split_reference_index = 1 if int(index) == 1 else 0

    @staticmethod
    def _snap_10(value: float) -> float:
        return math.floor(float(value) / 10.0 + 0.5) * 10.0

    def _boundary_split_candidate(self, scene_pos: QPointF):
        best = None
        for room, polygon in self._room_polygons:
            points = list(getattr(room, "points", []) or [])
            count = min(len(polygon), len(points))
            for index in range(count):
                p1 = polygon[index]
                p2 = polygon[(index + 1) % count]
                vx = p2.x() - p1.x(); vy = p2.y() - p1.y()
                length2 = vx * vx + vy * vy
                if length2 <= 1e-12:
                    continue
                wx = scene_pos.x() - p1.x(); wy = scene_pos.y() - p1.y()
                t = max(0.0, min(1.0, (wx * vx + wy * vy) / length2))
                px = p1.x() + t * vx; py = p1.y() + t * vy
                hit_distance = math.hypot(scene_pos.x() - px, scene_pos.y() - py)
                if best is None or hit_distance < best[0]:
                    world_p1 = points[index]
                    world_p2 = points[(index + 1) % len(points)]
                    length_mm = math.hypot(float(world_p2[0]) - float(world_p1[0]), float(world_p2[1]) - float(world_p1[1]))
                    if length_mm < 20.0:
                        continue
                    raw_distance = t * length_mm
                    snapped = self._snap_10(raw_distance)
                    min_distance = 10.0
                    max_distance = math.floor((length_mm - 10.0) / 10.0) * 10.0
                    if max_distance < min_distance:
                        continue
                    snapped = max(min_distance, min(max_distance, snapped))
                    best = (hit_distance, room, index, snapped, length_mm)
        if best is None:
            return None
        tolerance = 14.0 / max(abs(self.transform().m11()), 1e-6)
        return best if best[0] <= tolerance else None

    @staticmethod
    def _merge_vertex_is_collinear(points, vertex_index: int, max_offset_mm: float = 0.5) -> bool:
        count = len(points or [])
        if count < 4 or not (0 <= int(vertex_index) < count):
            return False
        i = int(vertex_index)
        prev = points[(i - 1) % count]
        curr = points[i]
        nxt = points[(i + 1) % count]
        ax = float(curr[0]) - float(prev[0]); ay = float(curr[1]) - float(prev[1])
        bx = float(nxt[0]) - float(curr[0]); by = float(nxt[1]) - float(curr[1])
        la = math.hypot(ax, ay); lb = math.hypot(bx, by)
        if la <= 1e-9 or lb <= 1e-9:
            return False
        # 같은 방향으로 이어지는 두 선만 허용한다. 되꺾이는 중복선은 합치지 않는다.
        if ax * bx + ay * by <= 0.0:
            return False
        chord_x = float(nxt[0]) - float(prev[0]); chord_y = float(nxt[1]) - float(prev[1])
        chord = math.hypot(chord_x, chord_y)
        if chord <= 1e-9:
            return False
        # CAD 부동소수 오차만 허용하고 실제 꺾인 선은 합치지 않도록 공유 절점의
        # 직선 이탈거리를 0.5mm 이하로 제한한다.
        offset = abs(chord_x * (float(prev[1]) - float(curr[1])) - (float(prev[0]) - float(curr[0])) * chord_y) / chord
        return offset <= float(max_offset_mm)

    def _boundary_merge_candidate(self, scene_pos: QPointF):
        best = None
        for room, polygon in self._room_polygons:
            points = list(getattr(room, "points", []) or [])
            count = min(len(polygon), len(points))
            if count < 4:
                continue
            for index in range(count):
                if not self._merge_vertex_is_collinear(points, index):
                    continue
                point = polygon[index]
                hit_distance = math.hypot(scene_pos.x() - point.x(), scene_pos.y() - point.y())
                if best is None or hit_distance < best[0]:
                    prev = points[(index - 1) % len(points)]
                    curr = points[index]
                    nxt = points[(index + 1) % len(points)]
                    len1 = math.hypot(float(curr[0]) - float(prev[0]), float(curr[1]) - float(prev[1]))
                    len2 = math.hypot(float(nxt[0]) - float(curr[0]), float(nxt[1]) - float(curr[1]))
                    best = (hit_distance, room, index, len1, len2)
        if best is None:
            return None
        tolerance = 14.0 / max(abs(self.transform().m11()), 1e-6)
        return best if best[0] <= tolerance else None

    def show_boundary_merge_hover(self, room: RoomRecord, vertex_index: int) -> None:
        points = list(getattr(room, "points", []) or [])
        if len(points) < 4 or not (0 <= int(vertex_index) < len(points)):
            return
        i = int(vertex_index)
        if not self._merge_vertex_is_collinear(points, i):
            return
        prevw = points[(i - 1) % len(points)]
        currw = points[i]
        nextw = points[(i + 1) % len(points)]
        len1 = math.hypot(float(currw[0]) - float(prevw[0]), float(currw[1]) - float(prevw[1]))
        len2 = math.hypot(float(nextw[0]) - float(currw[0]), float(nextw[1]) - float(currw[1]))
        key = ("merge", id(room), i)
        if key == self._boundary_split_hover_key:
            return
        self.clear_boundary_split_highlight()
        self._boundary_split_hover_key = key
        prev = QPointF(float(prevw[0]) - self._current_minx, -(float(prevw[1]) - self._current_miny))
        curr = QPointF(float(currw[0]) - self._current_minx, -(float(currw[1]) - self._current_miny))
        nxt = QPointF(float(nextw[0]) - self._current_minx, -(float(nextw[1]) - self._current_miny))
        scene = self.scene()
        line1 = scene.addLine(QLineF(prev, curr), self._overlay_pen(QColor("#6E8BFF"), max(3, self.selection_width)))
        line2 = scene.addLine(QLineF(curr, nxt), self._overlay_pen(QColor("#00A86B"), max(3, self.selection_width)))
        line1.setZValue(260); line2.setZValue(260)
        marker = scene.addEllipse(-6.0, -6.0, 12.0, 12.0, QPen(QColor("#B00020"), 1.8), QBrush(QColor("#FFF36B")))
        marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); marker.setPos(curr); marker.setZValue(276)
        info = scene.addText(f"합치기 {len1:,.0f} + {len2:,.0f} = {len1 + len2:,.0f} mm")
        f = info.font(); f.setBold(True); f.setPointSize(9); info.setFont(f)
        info.setDefaultTextColor(QColor("#7A1730")); info.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        info.setPos(curr + QPointF(10.0, -25.0)); info.setZValue(278)
        self._boundary_split_highlight_items = [line1, line2, marker, info]
        payload = {"room": room, "vertex_index": i, "length1": len1, "length2": len2, "length": len1 + len2}
        self._boundary_split_hover_payload = payload
        self.boundary_merge_hover_changed.emit(payload)
        self.viewport().update()

    def show_boundary_merge_confirmation(self, room: RoomRecord, merged_segment_index: int, removed_world_point) -> None:
        points = list(getattr(room, "points", []) or [])
        if len(points) < 3 or not (0 <= int(merged_segment_index) < len(points)):
            return
        i = int(merged_segment_index)
        p1w = points[i]
        p2w = points[(i + 1) % len(points)]
        p1 = QPointF(float(p1w[0]) - self._current_minx, -(float(p1w[1]) - self._current_miny))
        p2 = QPointF(float(p2w[0]) - self._current_minx, -(float(p2w[1]) - self._current_miny))
        removed = QPointF(float(removed_world_point[0]) - self._current_minx, -(float(removed_world_point[1]) - self._current_miny))
        self.clear_boundary_split_highlight()
        scene = self.scene()
        line = scene.addLine(QLineF(p1, p2), self._overlay_pen(QColor("#00A86B"), max(3, self.selection_width)))
        line.setZValue(260)
        marker = scene.addEllipse(-5.0, -5.0, 10.0, 10.0, QPen(QColor("#666666"), 1.4), QBrush(QColor("#FFFFFF")))
        marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); marker.setPos(removed); marker.setZValue(276)
        txt = scene.addText("합치기 완료")
        f = txt.font(); f.setBold(True); f.setPointSize(9); txt.setFont(f)
        txt.setDefaultTextColor(QColor("#087F4D")); txt.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        txt.setPos(removed + QPointF(10.0, -22.0)); txt.setZValue(278)
        self._boundary_split_highlight_items = [line, marker, txt]
        self.viewport().update()

    def _add_fixed_marker(self, point: QPointF, label: str, selected: bool = False):
        scene = self.scene()
        marker = scene.addEllipse(-6.0, -6.0, 12.0, 12.0, QPen(QColor("#C43C00"), 1.8), QBrush(QColor("#FFD24A" if selected else "#FFFFFF")))
        marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        marker.setPos(point)
        marker.setZValue(272 if selected else 268)
        text = scene.addText(label)
        text.setParentItem(marker)
        f = text.font(); f.setBold(True); f.setPointSize(9)
        text.setFont(f)
        text.setDefaultTextColor(QColor("#C43C00"))
        text.setPos(8.0, -18.0)
        text.setZValue(2)
        return marker

    def show_boundary_split_hover(self, room: RoomRecord, segment_index: int, distance_from_p1: float) -> None:
        points = list(getattr(room, "points", []) or [])
        if len(points) < 2 or not (0 <= int(segment_index) < len(points)):
            return
        i = int(segment_index)
        p1w = points[i]; p2w = points[(i + 1) % len(points)]
        length_mm = math.hypot(float(p2w[0]) - float(p1w[0]), float(p2w[1]) - float(p1w[1]))
        if length_mm <= 0.0:
            return
        d1 = max(0.0, min(length_mm, float(distance_from_p1)))
        key = (id(room), i, round(d1, 3))
        if key == self._boundary_split_hover_key:
            return
        self.clear_boundary_split_highlight()
        self._boundary_split_hover_key = key
        ratio = d1 / length_mm
        p1 = QPointF(float(p1w[0]) - self._current_minx, -(float(p1w[1]) - self._current_miny))
        p2 = QPointF(float(p2w[0]) - self._current_minx, -(float(p2w[1]) - self._current_miny))
        cut = QPointF(p1.x() + (p2.x() - p1.x()) * ratio, p1.y() + (p2.y() - p1.y()) * ratio)
        scene = self.scene()
        line = scene.addLine(QLineF(p1, p2), self._overlay_pen(QColor("#FF5500"), max(3, self.selection_width)))
        line.setZValue(260)
        m1 = self._add_fixed_marker(p1, "①")
        m2 = self._add_fixed_marker(p2, "②")
        cut_marker = scene.addEllipse(-5.0, -5.0, 10.0, 10.0, QPen(QColor("#FFFFFF"), 1.5), QBrush(QColor("#FF5500")))
        cut_marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        cut_marker.setPos(cut)
        cut_marker.setZValue(276)
        info = scene.addText(f"① {d1:,.0f}  |  ② {length_mm - d1:,.0f} mm")
        f = info.font(); f.setBold(True); f.setPointSize(9); info.setFont(f)
        info.setDefaultTextColor(QColor("#C43C00"))
        info.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        info.setPos(cut + QPointF(10.0, -25.0))
        info.setZValue(278)
        self._boundary_split_endpoint_markers = {0: m1, 1: m2}
        self._boundary_split_highlight_items = [line, m1, m2, cut_marker, info]
        payload = {"room": room, "segment_index": i, "distance1": d1, "distance2": length_mm - d1, "length": length_mm}
        self._boundary_split_hover_payload = payload
        self.boundary_split_hover_changed.emit(payload)
        self.viewport().update()

    def show_boundary_split_confirmation(self, room: RoomRecord, split_index: int) -> None:
        """절점 추가 직후 나뉜 두 구간과 공유 절점을 잠시 동시에 보여준다."""
        points = list(getattr(room, "points", []) or [])
        if len(points) < 3 or not (0 <= int(split_index) < len(points)):
            return
        i = int(split_index)
        p1w = points[i]
        cutw = points[(i + 1) % len(points)]
        p2w = points[(i + 2) % len(points)]
        p1 = QPointF(float(p1w[0]) - self._current_minx, -(float(p1w[1]) - self._current_miny))
        cut = QPointF(float(cutw[0]) - self._current_minx, -(float(cutw[1]) - self._current_miny))
        p2 = QPointF(float(p2w[0]) - self._current_minx, -(float(p2w[1]) - self._current_miny))
        self.clear_boundary_split_highlight()
        scene = self.scene()
        line1 = scene.addLine(QLineF(p1, cut), self._overlay_pen(QColor("#FF5500"), max(3, self.selection_width)))
        line2 = scene.addLine(QLineF(cut, p2), self._overlay_pen(QColor("#FF8A00"), max(3, self.selection_width)))
        line1.setZValue(260); line2.setZValue(260)
        # 같은 실제 좌표를 공유하는 두 새 구간의 끝점을 화면상 두 점으로 벌려 확인 가능하게 표시한다.
        dot1 = scene.addEllipse(-10.0, -4.0, 8.0, 8.0, QPen(Qt.NoPen), QBrush(QColor("#FF5500")))
        dot2 = scene.addEllipse(2.0, -4.0, 8.0, 8.0, QPen(Qt.NoPen), QBrush(QColor("#FF8A00")))
        for dot in (dot1, dot2):
            dot.setFlag(QGraphicsItem.ItemIgnoresTransformations, True); dot.setPos(cut); dot.setZValue(280)
        txt = scene.addText("분할점 2구간")
        f = txt.font(); f.setBold(True); f.setPointSize(9); txt.setFont(f)
        txt.setDefaultTextColor(QColor("#C43C00")); txt.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        txt.setPos(cut + QPointF(12.0, 7.0)); txt.setZValue(282)
        self._boundary_split_highlight_items = [line1, line2, dot1, dot2, txt]
        self.viewport().update()

    @staticmethod
    def _project_scene_point_to_segment(point: QPointF, p1: QPointF, p2: QPointF):
        dx = p2.x() - p1.x(); dy = p2.y() - p1.y()
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return QPointF(p1), 0.0, 0.0, math.hypot(point.x() - p1.x(), point.y() - p1.y())
        length = math.sqrt(length_sq)
        t_raw = ((point.x() - p1.x()) * dx + (point.y() - p1.y()) * dy) / length_sq
        t = max(0.0, min(1.0, t_raw))
        hit = QPointF(p1.x() + t * dx, p1.y() + t * dy)
        return hit, t_raw, length, math.hypot(point.x() - hit.x(), point.y() - hit.y())

    def _opening_snap_scene_anchor(self, point: QPointF, snap_step: float = 10.0) -> QPointF:
        """창호 드래그 미리보기를 가장 가까운 벽축/평행벽 중간축에 10mm 스냅한다."""
        if not self._wall_segments:
            return QPointF(
                round(point.x() / snap_step) * snap_step,
                round(point.y() / snap_step) * snap_step,
            ) if snap_step > 0 else QPointF(point)

        nearest = None
        for _key, p1, p2 in self._wall_segments:
            if ":d" in str(_key):
                continue
            hit, _t_raw, length, distance = self._project_scene_point_to_segment(point, p1, p2)
            if length <= 1e-9:
                continue
            if nearest is None or distance < nearest[0]:
                nearest = (distance, p1, p2, length, hit)
        if nearest is None:
            return QPointF(point)

        _distance, p1, p2, length, _hit = nearest
        ux = (p2.x() - p1.x()) / length; uy = (p2.y() - p1.y()) / length
        along = (point.x() - p1.x()) * ux + (point.y() - p1.y()) * uy
        if snap_step > 0:
            along = round(along / snap_step) * snap_step
        along = max(0.0, min(length, along))
        q1 = QPointF(p1.x() + ux * along, p1.y() + uy * along)

        secondary = None
        best_gap = float("inf")
        sin_limit = math.sin(math.radians(7.0))
        for _key2, c1, c2 in self._wall_segments:
            if ":d" in str(_key2):
                continue
            if (abs(c1.x() - p1.x()) < 1e-6 and abs(c1.y() - p1.y()) < 1e-6 and
                    abs(c2.x() - p2.x()) < 1e-6 and abs(c2.y() - p2.y()) < 1e-6):
                continue
            cdx = c2.x() - c1.x(); cdy = c2.y() - c1.y(); clen = math.hypot(cdx, cdy)
            if clen <= 1e-9:
                continue
            vux, vuy = cdx / clen, cdy / clen
            if abs(ux * vuy - uy * vux) > sin_limit:
                continue
            q2, t_raw, _clen, _dist = self._project_scene_point_to_segment(q1, c1, c2)
            if t_raw < -0.08 or t_raw > 1.08:
                continue
            gap_dx, gap_dy = q2.x() - q1.x(), q2.y() - q1.y()
            gap = math.hypot(gap_dx, gap_dy)
            if gap < 20.0 or gap > 800.0:
                continue
            if abs(gap_dx * ux + gap_dy * uy) > max(80.0, gap * 0.35):
                continue
            if gap < best_gap:
                best_gap = gap; secondary = q2
        if secondary is not None:
            return QPointF((q1.x() + secondary.x()) / 2.0, (q1.y() + secondary.y()) / 2.0)
        return q1

    def _clear_opening_drag_feedback(self) -> None:
        scene = self.scene()
        for item in list(self._opening_drag_feedback_items):
            try:
                scene.removeItem(item)
            except Exception:
                pass
        self._opening_drag_feedback_items = []

    def _opening_hit_id(self, scene_pos: QPointF) -> str:
        hit_id = ""
        best_distance = float("inf")
        tolerance = 14.0 / max(abs(self.transform().m11()), 1e-6)
        for read_id, item in self._opening_marker_items.items():
            bounds = item.sceneBoundingRect()
            if bounds.adjusted(-4, -4, 4, 4).contains(scene_pos):
                return read_id
            for aux in self._opening_marker_aux_items.get(read_id, []):
                if aux.sceneBoundingRect().adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(scene_pos):
                    return read_id
            center = bounds.center()
            distance = math.hypot(scene_pos.x() - center.x(), scene_pos.y() - center.y())
            if distance < best_distance:
                best_distance = distance; hit_id = read_id
        if hit_id and best_distance <= tolerance:
            return hit_id
        return ""

    def _update_opening_drag_feedback(self, dx: float, dy: float) -> None:
        self._clear_opening_drag_feedback()
        if not self._opening_drag_ids:
            return
        first_id = next(iter(self._opening_drag_ids), "")
        item = self._opening_marker_items.get(first_id)
        if item is None:
            return
        scene = self.scene()
        center = item.sceneBoundingRect().center()
        info = scene.addText(f"ΔX {dx:+,.0f}  ΔY {dy:+,.0f}  거리 {math.hypot(dx, dy):,.0f} mm")
        f = info.font(); f.setBold(True); f.setPointSize(9); info.setFont(f)
        info.setDefaultTextColor(QColor("#D00000")); info.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        info.setPos(center + QPointF(12.0, -25.0)); info.setZValue(310)
        self._opening_drag_feedback_items = [info]

    def refresh_current_geometry(self) -> None:
        """실 geometry가 바뀐 뒤 현재 zoom/PAN을 유지한 채 장면만 다시 만든다."""
        self.clear_boundary_split_highlight()
        self._redraw_current(preserve_layout=True)


    @staticmethod
    def _point_segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
        dx = float(x2) - float(x1)
        dy = float(y2) - float(y1)
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            return math.hypot(float(px) - float(x1), float(py) - float(y1))
        t = ((float(px) - float(x1)) * dx + (float(py) - float(y1)) * dy) / denom
        t = max(0.0, min(1.0, t))
        qx = float(x1) + t * dx
        qy = float(y1) + t * dy
        return math.hypot(float(px) - qx, float(py) - qy)

    def _opening_marker_touches_rooms(self, marker: dict, rooms: list[RoomRecord]) -> bool:
        """선택실 보기에서는 현재 표시 실의 벽에 걸린 창호만 남긴다.

        최신 창호 캐시는 wall_refs의 room_handle로 정확히 판정하고, 구버전 캐시처럼
        벽 링크 정보가 부족한 경우에는 wall_center/벽 부착점이 현재 실 경계선에
        실제로 닿는지 좌표로 한 번 더 확인한다.
        """
        rooms = list(rooms or [])
        if not rooms:
            return False
        handles = {str(room.boundary_handle or "") for room in rooms if str(room.boundary_handle or "")}
        for ref in list(marker.get("wall_refs", []) or []):
            if not isinstance(ref, dict):
                continue
            if str(ref.get("room_handle", "") or "") in handles:
                return True

        candidates = []
        for key in ("wall_attach_primary", "wall_attach_secondary"):
            value = marker.get(key)
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                try:
                    candidates.append((float(value[0]), float(value[1])))
                except (TypeError, ValueError):
                    pass
        if not candidates:
            try:
                candidates.append((
                    float(marker.get("wall_center_x", marker.get("x"))),
                    float(marker.get("wall_center_y", marker.get("y"))),
                ))
            except (TypeError, ValueError):
                return False

        # 벽 스냅 좌표는 실구분선 위에 있어야 하므로 25mm 이내만 같은 실 창호로 본다.
        tolerance = 25.0
        for room in rooms:
            points = list(getattr(room, "points", None) or [])
            if len(points) < 2:
                continue
            for px, py in candidates:
                for index, (x1, y1) in enumerate(points):
                    x2, y2 = points[(index + 1) % len(points)]
                    if self._point_segment_distance(px, py, x1, y1, x2, y2) <= tolerance:
                        return True
        return False

    def _opening_dimension_room_segment(self, marker: dict, rooms: list[RoomRecord]):
        """선택실 창호치수의 기준 벽구간을 찾는다."""
        rooms = list(rooms or [])
        room_by_handle = {str(room.boundary_handle or ""): room for room in rooms if str(room.boundary_handle or "")}
        for ref in list(marker.get("wall_refs", []) or []):
            if not isinstance(ref, dict):
                continue
            room = room_by_handle.get(str(ref.get("room_handle", "") or ""))
            if room is None:
                continue
            try:
                index = int(ref.get("segment_index"))
            except (TypeError, ValueError):
                continue
            points = list(room.points or [])
            if len(points) >= 2 and 0 <= index < len(points):
                return room, index

        # 구버전 캐시 fallback: wall_center에서 가장 가까운 현재 실 벽을 기준으로 한다.
        try:
            px = float(marker.get("wall_center_x", marker.get("x")))
            py = float(marker.get("wall_center_y", marker.get("y")))
        except (TypeError, ValueError):
            return None, -1
        best = None
        best_distance = float("inf")
        for room in rooms:
            points = list(room.points or [])
            if len(points) < 2:
                continue
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                distance = self._point_segment_distance(px, py, x1, y1, x2, y2)
                if distance < best_distance:
                    best_distance = distance
                    best = (room, index)
        return best if best is not None and best_distance <= 25.0 else (None, -1)

    def _add_opening_length_dimension(self, scene, marker: dict, rooms: list[RoomRecord], length_mm: float, minx: float, miny: float, shared_font_size=None) -> None:
        """선택실 보기의 창호 길이를 실 치수 옵셋의 2배 위치에 표시한다."""
        if length_mm <= 0.0:
            return
        room, segment_index = self._opening_dimension_room_segment(marker, rooms)
        if room is None or segment_index < 0:
            return
        points = list(room.points or [])
        if len(points) < 2:
            return
        x1, y1 = points[segment_index]
        x2, y2 = points[(segment_index + 1) % len(points)]
        dx = float(x2) - float(x1)
        dy = float(y2) - float(y1)
        seg_len = math.hypot(dx, dy)
        if seg_len <= 1e-9:
            return

        try:
            cx = float(marker.get("wall_center_x", marker.get("x")))
            cy = float(marker.get("wall_center_y", marker.get("y")))
        except (TypeError, ValueError):
            cx, cy = (float(x1) + float(x2)) / 2.0, (float(y1) + float(y2)) / 2.0
        # 창호 중심을 해당 실의 벽선 위로 투영한다.
        t = ((cx - float(x1)) * dx + (cy - float(y1)) * dy) / (seg_len * seg_len)
        t = max(0.0, min(1.0, t))
        wx = float(x1) + t * dx
        wy = float(y1) + t * dy

        signed_area2 = 0.0
        for j, (px, py) in enumerate(points):
            qx, qy = points[(j + 1) % len(points)]
            signed_area2 += float(px) * float(qy) - float(qx) * float(py)
        if signed_area2 >= 0:
            nx_world, ny_world = dy / seg_len, -dx / seg_len
        else:
            nx_world, ny_world = -dy / seg_len, dx / seg_len
        opening_offset = float(self.edge_length_offset) * 2.0
        tx = (wx + nx_world * opening_offset) - minx
        ty = -((wy + ny_world * opening_offset) - miny)

        length_text = f"{float(length_mm) / 1000.0:,.3f}" + (" m" if self.edge_length_show_unit_m else "")
        item = scene.addText(length_text)
        font = item.font()
        font.setPointSize(shared_font_size if shared_font_size is not None else self._effective_edge_font_size(room))
        font.setBold(True)
        item.setFont(font)
        color = QColor(self.opening_line_color)
        if not color.isValid():
            color = QColor("#7A1FA2")
        item.setDefaultTextColor(color.darker(170))
        item.setZValue(98)
        angle = math.degrees(math.atan2(-dy, dx))
        if angle > 90:
            angle -= 180
        elif angle < -90:
            angle += 180
        rect = item.boundingRect()
        item.setTransformOriginPoint(rect.width() / 2.0, rect.height() / 2.0)
        item.setTransform(self._edge_text_transform(angle))
        item.setPos(tx - rect.width() / 2.0, ty - rect.height() / 2.0)
        item.setRotation(angle)
        item.setToolTip(f"창호 길이 {float(length_mm):,.0f}mm · 실 치수 옵셋의 2배")
        self._edge_length_items.append((item, angle))

    def set_opening_edit_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.opening_edit_mode:
            return
        self.opening_edit_mode = enabled
        self.selected_opening_ids.clear()
        if not enabled and self._opening_drag_start_scene is not None:
            for rid, origin in self._opening_drag_item_positions.items():
                item = self._opening_marker_items.get(rid)
                if item is not None:
                    item.setPos(origin)
            for rid, origins in self._opening_drag_aux_positions.items():
                for aux, origin in zip(self._opening_marker_aux_items.get(rid, []), origins):
                    aux.setPos(origin)
        self._opening_drag_start_scene = None
        self._opening_drag_anchor_scene = None
        self._opening_drag_ids.clear()
        self._opening_drag_item_positions = {}
        self._opening_drag_aux_positions = {}
        self._opening_drag_last_delta = (0.0, 0.0)
        self._clear_opening_drag_feedback()
        self.setDragMode(QGraphicsView.NoDrag if enabled else (QGraphicsView.NoDrag if (self.wall_finish_mode or self.room_group_mode or self.boundary_split_mode or self.boundary_merge_mode) else QGraphicsView.ScrollHandDrag))
        self._update_opening_marker_selection()
        self.opening_selection_changed.emit(set())

    def set_selected_opening_ids(self, ids) -> None:
        self.selected_opening_ids = {str(value or "") for value in (ids or []) if str(value or "")}
        self._update_opening_marker_selection()

    def _update_opening_marker_selection(self) -> None:
        for read_id, item in dict(self._opening_marker_items or {}).items():
            try:
                selected = read_id in self.selected_opening_ids
                item.setDefaultTextColor(QColor("#FF0000") if selected else QColor("#FF00FF"))
                font = item.font(); font.setUnderline(selected); item.setFont(font)
                item.setZValue(96 if selected else 88)
                line_color = QColor("#FF3A30") if selected else QColor("#8ED4E3")
                for aux in self._opening_marker_aux_items.get(read_id, []):
                    if hasattr(aux, "setPen"):
                        aux.setPen(self._overlay_pen(line_color, 4.0))
                    aux.setZValue(95 if selected else 87)
            except Exception:
                pass
        self.viewport().update()

    def set_opening_test_markers(self, floor: str, markers) -> None:
        self.opening_test_floor = str(floor or "")
        self.opening_test_markers = [dict(value or {}) for value in (markers or [])]
        valid_ids = {str(value.get("read_id", "") or "") for value in self.opening_test_markers}
        self.selected_opening_ids.intersection_update(valid_ids)
        self._redraw_current()
        if self.opening_edit_mode:
            self.opening_selection_changed.emit(set(self.selected_opening_ids))

    def clear_opening_test_markers(self) -> None:
        if not self.opening_test_floor and not self.opening_test_markers:
            return
        self.opening_test_floor = ""
        self.opening_test_markers = []
        self.selected_opening_ids.clear()
        self._redraw_current()
        if self.opening_edit_mode:
            self.opening_selection_changed.emit(set())

    def set_wall_finish_mode(self, enabled: bool):
        self.wall_finish_mode = bool(enabled)
        self.setDragMode(QGraphicsView.NoDrag if (enabled or self.boundary_split_mode or self.boundary_merge_mode) else QGraphicsView.ScrollHandDrag)
        self._redraw_current()

    def set_room_group_mode(self, enabled: bool):
        enabled = bool(enabled)
        changed = enabled != self.room_group_mode
        self.room_group_mode = enabled
        self.setDragMode(QGraphicsView.NoDrag if (enabled or self.boundary_split_mode or self.boundary_merge_mode) else QGraphicsView.ScrollHandDrag)
        # 모드 진입/종료 때는 천정 점선 등 별도 오버레이까지 다시 구성해야 한다.
        # 편집 중 선택 추가/해제는 set_selected_group_room_handles()에서 부분 갱신한다.
        if changed:
            self._redraw_current()
        else:
            self._update_group_room_selection()

    def set_selected_group_room_handles(self, handles):
        self.selected_group_room_handles = set(handles or [])
        self._update_group_room_selection()

    def set_wall_finish_assignments(self, assignments: dict):
        self.wall_finish_assignments = dict(assignments or {})
        self._redraw_current()

    def set_room_finish_assignments(self, assignments: dict):
        self.room_finish_assignments = dict(assignments or {})
        self._redraw_current()

    def set_finish_groups(self, groups: dict):
        self.finish_groups = {str(key): dict(value) for key, value in dict(groups or {}).items()}
        # 같은 벽체에 같은 탭의 서로 다른 구분(예: 바탕+마감)이 함께 존재할 수 있으므로
        # 벽체키 -> 탭 -> 구분 -> 그룹의 3단계로 보관한다.
        self._wall_group_by_key = {}
        self._wall_groups_by_key = {}
        for group in self.finish_groups.values():
            group_type = normalize_finish_group_type(group.get("group_type", ""))
            division = normalize_finish_group_division(group.get("group_division", ""), group_type)
            group["group_type"] = group_type
            group["group_division"] = division
            for wall_key in group.get("wall_keys", []) or []:
                key = str(wall_key)
                self._wall_groups_by_key.setdefault(key, {}).setdefault(group_type, {})[division] = group
        for key in self._wall_groups_by_key:
            group = self._group_for_wall_key(key)
            if group:
                self._wall_group_by_key[key] = group
        if self.focused_finish_group_id not in self.finish_groups:
            self.focused_finish_group_id = ""
        self._redraw_current()

    def set_finish_group_filter(self, group_type: str = "", division: str = "*"):
        """도면 그룹 오버레이를 탭/구분 단위로 제한한다. division='*'는 현재 탭 전체다."""
        key = str(group_type or "").strip()
        if key not in FINISH_GROUP_TYPES:
            self.finish_group_filter_type = ""
            self.finish_group_filter_division = ""
        else:
            self.finish_group_filter_type = key
            self.finish_group_filter_division = str(division or "*").strip() or "*"
        self._redraw_current()

    def _group_matches_filter(self, group: dict) -> bool:
        if not group or self.finish_group_filter_type not in FINISH_GROUP_TYPES:
            return False
        group_type = normalize_finish_group_type(group.get("group_type", ""))
        if group_type != self.finish_group_filter_type:
            return False
        wanted = self.finish_group_filter_division
        if wanted == "*":
            return True
        division = normalize_finish_group_division(group.get("group_division", ""), group_type)
        return division == wanted

    def _legacy_filter_matches(self) -> bool:
        # 그룹 구분 정보가 전혀 없던 구형 재료는 마감 탭의 '마감' 구분으로 간주한다.
        return (
            self.finish_group_filter_type == "마감"
            and self.finish_group_filter_division in {"*", "마감"}
        )

    def set_focused_finish_group(self, group_id: str):
        """그룹목록에서 선택한 그룹의 면/벽을 선택상태처럼 즉시 강조한다."""
        group_id = str(group_id or "")
        self.focused_finish_group_id = group_id if group_id in self.finish_groups else ""
        self._update_group_room_selection()
        self._update_wall_selection_in_place()

    def _focused_group(self) -> dict:
        return self.finish_groups.get(self.focused_finish_group_id, {})

    def _room_in_focused_area_group(self, room) -> bool:
        # 바닥/천정 그룹 지정 편집 중에는 저장된 기존 그룹 강조보다
        # 현재 편집용 선택 집합(selected_group_room_handles)을 기준으로 표시한다.
        # 그래야 기존 지정 실을 Ctrl+드래그/클릭으로 해제했을 때 즉시 강조도 사라진다.
        if self.room_group_mode:
            return False
        group = self._focused_group()
        if not group:
            return False
        section = str(group.get("section", "") or "").strip()
        if section not in ("바닥", "천정"):
            return False
        handle = str(getattr(room, "boundary_handle", "") or "")
        return handle in {str(value) for value in group.get("room_handles", []) or []}

    def _wall_in_focused_group(self, key: str) -> bool:
        # 벽 그룹 지정 편집 중에는 저장된 기존 그룹 강조를 끄고
        # selected_wall_keys만 선택 강조로 사용한다.
        if self.wall_finish_mode:
            return False
        group = self._focused_group()
        if not group or str(group.get("section", "") or "").strip() != "벽":
            return False
        return str(key) in {str(value) for value in group.get("wall_keys", []) or []}

    def set_show_group_overlay(self, enabled: bool):
        self.show_group_overlay = bool(enabled)
        self._redraw_current()

    def set_group_overlay_opacity(self, opacity: int, redraw: bool = True):
        self.group_overlay_opacity = max(0, min(100, int(opacity)))
        if redraw:
            self._redraw_current()

    def _groups_for_room_section(self, room, section: str) -> list[dict]:
        assignment = dict(self.room_finish_assignments.get(room.boundary_handle, {}) or {})
        result = []
        for entry in assignment.get("groups", []) or []:
            if str(entry.get("section", "") or "") != section:
                continue
            group = self.finish_groups.get(str(entry.get("id", "") or ""), dict(entry))
            if group:
                result.append(group)
        return result

    def _group_for_room_section(self, room, section: str) -> dict:
        if self.finish_group_filter_type not in FINISH_GROUP_TYPES:
            return {}
        for group in self._groups_for_room_section(room, section):
            if self._group_matches_filter(group):
                return group
        return {}

    def _group_for_wall_key(self, key: str) -> dict:
        group_type = self.finish_group_filter_type
        if group_type not in FINISH_GROUP_TYPES:
            return {}
        divided = self._wall_groups_by_key.get(str(key), {}).get(group_type, {})
        wanted = self.finish_group_filter_division
        if wanted == "*":
            return next(iter(divided.values()), {})
        return divided.get(wanted, {})

    def _has_any_room_group_for_section(self, room, section: str) -> bool:
        return bool(self._groups_for_room_section(room, section))

    def _has_any_wall_group(self, key: str) -> bool:
        return bool(self._wall_groups_by_key.get(str(key), {}))

    def _overlay_color(self, value: str, fallback: str = "#4E79A7") -> QColor:
        color = QColor(str(value or fallback))
        if not color.isValid():
            color = QColor(fallback)
        color.setAlpha(round(255 * self.group_overlay_opacity / 100.0))
        return color

    def set_selected_wall_keys(self, keys):
        self.selected_wall_keys = set(keys or [])
        self._update_wall_selection_in_place()

    def set_wall_selection_style(self, color: str, width: int, zoom_scale: float | None = None, redraw: bool = True):
        value = QColor(color)
        if value.isValid():
            self.wall_selection_color = value
        self.wall_selection_width = max(1, min(100, int(width)))
        if zoom_scale is not None:
            try:
                self.wall_selection_zoom_scale = max(0.0, min(1.0, float(zoom_scale)))
            except Exception:
                pass
        if redraw:
            self._update_all_overlay_pens_in_place()

    def set_opening_style(self, color: str, width: int, redraw: bool = True):
        value = QColor(str(color or ""))
        if value.isValid():
            self.opening_line_color = value
        self.opening_line_width = max(1, min(100, int(width)))
        if redraw:
            self._redraw_current()

    def _overlay_zoom_ratio(self) -> float:
        """전체보기 배율(1.0) 대비 현재 줌 배율을 반환한다."""
        fit_scale = max(float(self._fit_scale), 1e-12)
        current_scale = max(abs(float(self.transform().m11())), 1e-12)
        return current_scale / fit_scale

    def _overlay_screen_pen_width(self, base_pixels: float) -> float:
        """표시설정의 공통 줌 배율을 모든 도곽 오버레이 선에 적용한다.

        0.0 -> 화면 px 고정, 0.5 -> 줌 변화의 절반 정도, 1.0 -> 완전 비례.
        """
        ratio = max(self._overlay_zoom_ratio(), 1e-12)
        zoom_scale = max(0.0, min(1.0, float(self.wall_selection_zoom_scale)))
        scaled_ratio = ratio ** zoom_scale
        return max(0.25, float(base_pixels) * scaled_ratio)

    def _overlay_pen(self, color: QColor, base_pixels: float, style=Qt.SolidLine) -> QPen:
        pen = QPen(color)
        pen.setWidthF(self._overlay_screen_pen_width(base_pixels))
        pen.setStyle(style)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        return pen

    def _wall_pen(self, color: QColor, base_pixels: float) -> QPen:
        return self._overlay_pen(color, base_pixels)

    def _room_is_selected(self, room: RoomRecord) -> bool:
        handle = str(getattr(room, "boundary_handle", "") or "")
        return bool(handle and handle in self._current_selected_room_handles)

    def _update_static_overlay_pens_in_place(self):
        for item, color, width, style in self._deduction_display_items:
            item.setPen(self._overlay_pen(color, width, style))
        for item, color, width, style in self._ceiling_display_items:
            item.setPen(self._overlay_pen(color, width, style))

    def _update_all_overlay_pens_in_place(self):
        self._update_group_room_selection()
        self._update_static_overlay_pens_in_place()
        self._update_wall_selection_in_place()

    def _update_wall_selection_in_place(self):
        for key, (line_item, material) in self._wall_segment_items.items():
            selected = key in self.selected_wall_keys
            focused = self._wall_in_focused_group(key)
            group = self._group_for_wall_key(key)
            # 현재 편집 중인 기존 그룹의 과거 지정선 오버레이는 숨긴다.
            # 선택 집합에서 제거된 선이 그룹색으로 남아 선택처럼 보이지 않게 한다.
            editing_focused_group = (
                self.wall_finish_mode
                and bool(group)
                and str(group.get("id", "") or "") == self.focused_finish_group_id
            )
            grouped = bool(group) and not editing_focused_group
            if selected or focused:
                color = self.wall_selection_color
                width = self.wall_selection_width
                visible = True
            elif grouped and self.show_group_overlay:
                color = self._overlay_color(group.get("color", ""))
                # 저장된 벽 그룹 오버레이도 표시 설정의 '벽 지정 선택선 두께'를
                # 그대로 사용한다. 이전에는 4px 고정값이라 설정을 바꿔도
                # 이미 지정된 벽(노랑/그룹색) 두께가 전혀 변하지 않았다.
                width = self.wall_selection_width
                visible = True
            else:
                has_typed_group = self._has_any_wall_group(key)
                legacy_material = bool(material) and not has_typed_group and self._legacy_filter_matches()
                # 지정모드에서는 선택 가능한 모든 벽을 보여주되, 현재 필터와 다른
                # 다른 타입 그룹은 일반 1px 선으로 낮춰 현재 타입 기지정선을 구분한다.
                if legacy_material:
                    color = QColor(material.get("color", "#808080"))
                    width = self.wall_selection_width
                else:
                    color = QColor("#808080")
                    width = 1
                visible = self.wall_finish_mode
            pen = self._wall_pen(color, width)
            line_item.setPen(pen)
            line_item.setVisible(visible)
        self.viewport().update()

    def _update_group_room_selection(self):
        for room in self._current_rooms:
            items = self._room_display_items.get(id(room))
            if not items:
                continue
            polygon_item, _text_item = items
            handle = str(room.boundary_handle or "")
            focused_area = self._room_in_focused_area_group(room)
            if self.room_group_mode and handle in self.selected_group_room_handles:
                polygon_item.setPen(self._overlay_pen(QColor("#00AEEF"), 3))
                fill = QColor("#00AEEF")
                fill.setAlpha(45)
                polygon_item.setBrush(QBrush(fill))
            else:
                is_selected = self._room_is_selected(room)
                base_color = self._room_base_color(room)
                polygon_item.setPen(self._overlay_pen(
                    self.selection_color if (is_selected or focused_area) else base_color,
                    self.selection_width if (is_selected or focused_area) else self.boundary_width,
                ))
                room_assign = self.room_finish_assignments.get(room.boundary_handle, {})
                floor_material = first_room_finish_material(room_assign, "바닥")
                brush = QBrush(Qt.transparent)
                if focused_area:
                    fill = QColor(self.selection_color)
                    fill.setAlpha(55)
                    brush = QBrush(fill)
                else:
                    floor_group = self._group_for_room_section(room, "바닥")
                    editing_focused_floor_group = (
                        self.room_group_mode
                        and bool(floor_group)
                        and str(floor_group.get("id", "") or "") == self.focused_finish_group_id
                    )
                    if floor_group and not editing_focused_floor_group:
                        if self.show_group_overlay:
                            fill = self._overlay_color(floor_group.get("color", ""))
                            brush = QBrush(fill)
                    elif (
                        floor_material
                        and not editing_focused_floor_group
                        and not self._has_any_room_group_for_section(room, "바닥")
                        and self._legacy_filter_matches()
                    ):
                        # 그룹 정보가 없는 구형 재료지정은 마감으로 간주한다.
                        fill = QColor(floor_material.get("color", "#5B8FF9"))
                        fill.setAlpha(55)
                        brush = QBrush(fill)
                polygon_item.setBrush(brush)
        self.viewport().update()

    def apply_cad_style(self, style: str, opacity: int, boundary_color: str, boundary_width: int, render_quality: str, selection_color: str, selection_width: int, background_line_width: float = 1.5, background_aci_widths: str = "", background_keep_red: bool = False, background_zoomout_width_weight: float = 0.25):
        style = "cad" if style == "cad" else "gray"
        opacity = max(0, min(100, int(opacity)))
        boundary_width = max(1, min(100, int(boundary_width)))
        selection_width = max(1, min(100, int(selection_width)))
        background_line_width = max(0.0, min(20.0, float(background_line_width)))
        background_zoomout_width_weight = max(0.0, min(1.0, float(background_zoomout_width_weight)))
        changed_cache = (style != self.background_style or abs(background_line_width - self.background_line_width) > 1e-9 or str(background_aci_widths) != self.background_aci_widths or bool(background_keep_red) != self.background_keep_red or abs(background_zoomout_width_weight - self.background_zoomout_width_weight) > 1e-9)
        self.background_style = style
        self.background_line_width = background_line_width
        self.background_zoomout_width_weight = background_zoomout_width_weight
        self.background_aci_widths = str(background_aci_widths)
        self.background_keep_red = bool(background_keep_red)
        self.background_opacity = opacity
        boundary = QColor(boundary_color)
        if boundary.isValid():
            self.boundary_color = boundary
        self.boundary_width = boundary_width
        self.render_quality = render_quality if render_quality in ("normal", "antialias", "high") else "high"
        self.setRenderHint(QPainter.Antialiasing, self.render_quality != "normal")
        self.setRenderHint(QPainter.TextAntialiasing, self.render_quality != "normal")
        self.setRenderHint(QPainter.SmoothPixmapTransform, self.render_quality == "high")
        color = QColor(selection_color)
        if color.isValid():
            self.selection_color = color
        self.selection_width = selection_width
        self._update_canvas_background(self.show_cad_background)
        if changed_cache:
            self.clear_background_cache()

        # V24.10: 표시설정의 투명도/색상/선두께 적용은 현재 층을 즉시 다시 그린다.
        # 일반 _redraw_current()는 층 전체보기 + 치수표시 상태에서 배경을 숨길 수 있으므로
        # 표시설정 적용에서는 force_background=None으로 현재 CAD 배경 표시상태를 그대로 존중한다.
        selected_rooms = [room for room in self._current_rooms if self._room_is_selected(room)]
        self._draw_rooms(
            self._current_rooms,
            self._current_selected,
            selected_rooms=selected_rooms,
            show_edge_lengths=self._show_edge_lengths,
            force_background=None,
            preserve_layout=True,
            preserve_view_override=True,
        )

    def set_deduction_style(self, color: str, width: int, redraw: bool = True):
        """내부 실구분 미리보기의 공제선 표시만 독립적으로 설정한다."""
        deduction_color = QColor(color)
        if deduction_color.isValid():
            self.deduction_color = deduction_color
        self.deduction_width = max(1, min(100, int(width)))
        if redraw:
            self._redraw_current()

    def set_cad_background(self, segments):
        self.cad_background = list(segments or [])
        self._background_generation += 1
        self.clear_background_cache()
        self._redraw_current(preserve_layout=False)

    def set_background_cache_files(self, background_files: dict):
        self.floor_background_files = dict(background_files or {})
        self.clear_background_cache()
        self._redraw_current(preserve_layout=False)

    def capture_view_state(self):
        """현재 CAD 뷰의 줌/중심을 world 좌표 기준으로 저장한다.

        레이어/최적화 그룹 표시를 바꾸면 배경 scene이 다시 만들어질 수 있으므로
        QGraphicsView의 scene 좌표가 아니라 CAD/world 중심을 저장해야 한다.
        """
        try:
            transform = QTransform(self.transform())
            world_center = self._capture_world_view_center()
        except Exception:
            return None
        return {
            "transform": transform,
            "world_center": QPointF(world_center),
        }

    def restore_view_state(self, state) -> None:
        """capture_view_state()로 저장한 줌/화면 중심을 현재 scene에 복원한다."""
        if not state:
            return
        transform = state.get("transform")
        world_center = state.get("world_center")
        if transform is None or world_center is None:
            return
        try:
            self.setTransform(QTransform(transform))
            target_scene_center = self._world_to_scene_center(QPointF(world_center), self._current_minx, self._current_miny)
            self.centerOn(target_scene_center)
            # QGraphicsView의 스크롤바는 정수 픽셀이라 centerOn()만 반복하면
            # 재렌더 때 반올림 오차가 다음 캡처에 누적될 수 있다.
            # 마지막 남은 sub-pixel 오차는 transform 평행이동으로 직접 상쇄해
            # 같은 world 포커스 좌표가 계속 유지되게 한다.
            viewport_center = self.viewport().rect().center()
            actual_scene_center = self.mapToScene(viewport_center)
            dx = float(target_scene_center.x() - actual_scene_center.x())
            dy = float(target_scene_center.y() - actual_scene_center.y())
            if abs(dx) > 1e-12 or abs(dy) > 1e-12:
                self.translate(-dx, -dy)
            self._update_all_overlay_pens_in_place()
            self.viewport().update()
        except Exception:
            pass

    def set_vector_background_cache(self, floor_vectors: dict, preserve_layout: bool = False):
        self.floor_vector_cache = {str(k): dict(v or {}) for k, v in dict(floor_vectors or {}).items()}
        self.clear_background_cache()
        self._redraw_current(preserve_layout=bool(preserve_layout))

    def set_background_render_mode(self, mode: str):
        mode = str(mode or "png").strip().lower()
        if mode not in {"png", "vector"}:
            mode = "png"
        if mode == self.background_render_mode:
            return
        self.background_render_mode = mode
        self._redraw_current(preserve_layout=False)

    def clear_background_sources(self):
        self.cad_background = []
        self.floor_background_files = {}
        self.floor_vector_cache = {}
        self._background_generation += 1
        self.clear_background_cache()
        self._redraw_current(preserve_layout=False)

    def clear_background_cache(self):
        self._background_cache.clear()

    def set_show_cad_background(self, enabled: bool):
        # 구버전 호출 호환: 켜기는 기존 검은색, 끄기는 없음으로 해석한다.
        self.set_cad_background_mode("black" if bool(enabled) else "none")

    def set_cad_background_mode(self, mode: str):
        mode = str(mode or "none").strip().lower()
        if mode not in {"none", "black", "white"}:
            mode = "black"
        self.cad_background_mode = mode
        self.show_cad_background = mode != "none"
        self._update_canvas_background(self.show_cad_background)

        # V24.09.2: 검정/하양 전환은 현재 층의 CAD 배경 자체를 숨기는 동작이 아니다.
        # _redraw_current()는 층 전체보기 + 치수표시 상태에서 force_background=False를
        # 사용하기 때문에 색상만 바꿨는데 벡터 도곽이 사라지고, 층을 다시 선택해야
        # 나타나는 회귀가 생겼다. 색상 전환에서는 기존 줌/PAN을 보존하면서
        # force_background=None으로 현재 장면을 즉시 다시 만들어 배경 표시상태를 유지한다.
        selected_rooms = [room for room in self._current_rooms if self._room_is_selected(room)]
        self._draw_rooms(
            self._current_rooms,
            self._current_selected,
            selected_rooms=selected_rooms,
            show_edge_lengths=self._show_edge_lengths,
            force_background=None,
            preserve_layout=True,
            preserve_view_override=True,
        )

    def _update_canvas_background(self, background_visible: bool):
        """상단 CAD 배경 모드에 따라 캔버스 자체도 검정/흰색으로 고정한다."""
        use_black = bool(background_visible) and self.cad_background_mode == "black"
        self.setBackgroundBrush(QBrush(Qt.black if use_black else Qt.white))

    def _display_background_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """하얀색 모드는 CAD 배경 RGB만 반전한다. 알파(투명도)는 보존한다."""
        if self.cad_background_mode != "white" or pixmap.isNull():
            return pixmap
        image = pixmap.toImage()
        try:
            image.invertPixels()
        except Exception:
            return pixmap
        return QPixmap.fromImage(image)

    def set_show_room_overlay(self, enabled: bool):
        self.show_room_overlay = bool(enabled)
        self._redraw_current()

    def set_show_original_text(self, enabled: bool):
        self.show_original_text = bool(enabled)
        self._redraw_current()

    def set_background_max_pixels(self, size: int):
        size = max(400, min(5000, int(size)))
        if size == self.background_max_pixels:
            return
        self.background_max_pixels = size
        self.clear_background_cache()
        self._redraw_current()

    def set_edge_length_offset(self, offset: int):
        offset = max(-5000, min(5000, int(offset)))
        if offset == self.edge_length_offset:
            return
        self.edge_length_offset = offset
        if self._show_edge_lengths:
            self._redraw_current()

    def _edge_text_transform(self, angle: float) -> QTransform:
        """치수문자의 화면상 글자 폭만 조절한다.

        수평 문자는 로컬 X축을, 수직에 가까운 문자는 로컬 Y축을 조절한다.
        따라서 90도 회전된 치수의 문자열 진행방향(화면 세로 길이)은 유지되고
        글자 자체의 가로 폭만 좁아지거나 넓어진다.
        """
        ratio = self.edge_length_width_ratio / 100.0
        radians = math.radians(float(angle))
        if abs(math.sin(radians)) > abs(math.cos(radians)):
            return QTransform.fromScale(1.0, ratio)
        return QTransform.fromScale(ratio, 1.0)

    def _update_edge_widths_in_place(self):
        if not self._edge_length_items:
            return
        for item, angle in self._edge_length_items:
            try:
                item.setTransform(self._edge_text_transform(angle))
            except Exception:
                pass
        self.viewport().update()

    def set_edge_length_width_ratio(self, ratio: int):
        ratio = max(10, min(200, int(ratio)))
        if ratio == self.edge_length_width_ratio:
            return
        self.edge_length_width_ratio = ratio
        if self._show_edge_lengths:
            # V16.1: 폭 변경은 scene 재생성/fit을 하지 않는다.
            # 현재 줌, 중심, PAN 위치를 그대로 유지한다.
            self._update_edge_widths_in_place()

    def set_edge_length_show_unit_m(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self.edge_length_show_unit_m:
            return
        self.edge_length_show_unit_m = enabled
        if self._show_edge_lengths:
            self._redraw_current()

    def set_label_scale_mode(self, mode: str):
        mode = "full_width" if str(mode) == "full_width" else "room_area"
        if mode == self.label_scale_mode:
            return
        self.label_scale_mode = mode
        self._update_label_fonts_in_place()

    def set_label_view_weights(self, full_weight: float, selected_weight: float):
        full_weight = max(0.10, min(3.00, float(full_weight)))
        selected_weight = max(0.10, min(3.00, float(selected_weight)))
        if abs(full_weight - self.label_full_view_weight) < 1e-9 and abs(selected_weight - self.label_selected_view_weight) < 1e-9:
            return
        self.label_full_view_weight = full_weight
        self.label_selected_view_weight = selected_weight
        self._update_label_fonts_in_place()

    def set_label_view_mode(self, mode: str):
        mode = "selected" if str(mode) == "selected" else "floor"
        if mode == self._label_view_mode:
            return
        self._label_view_mode = mode
        self._update_label_fonts_in_place()

    def set_edge_length_font_size(self, size: int):
        size = max(6, min(300, int(size)))
        if size == self.edge_length_font_size:
            return
        self.edge_length_font_size = size
        if self._show_edge_lengths:
            self._redraw_current()

    def _edge_font_size_from_span(self, span: float) -> int:
        """치수문자 기준 크기를 CAD 표시범위 크기에 비례해 계산한다."""
        scale = max(float(span), 1.0) / 5000.0
        scale = max(0.40, min(4.00, scale))
        return max(8, min(300, int(round(self.edge_length_font_size * scale))))

    def _effective_edge_font_size(self, room: RoomRecord) -> int:
        """개별 실 보기의 치수문자 크기를 해당 실 크기에 비례해 계산한다."""
        if not room or not room.points:
            return self.edge_length_font_size
        xs = [point[0] for point in room.points]
        ys = [point[1] for point in room.points]
        room_span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        return self._edge_font_size_from_span(room_span)

    def set_edge_lengths_visible(self, enabled: bool):
        """치수 표시만 변경하고 현재 zoom/PAN/sceneRect는 그대로 유지한다."""
        enabled = bool(enabled)
        if enabled == self._show_edge_lengths:
            return
        selected_rooms = [room for room in self._current_rooms if self._room_is_selected(room)]
        self._draw_rooms(
            self._current_rooms,
            self._current_selected,
            selected_rooms=selected_rooms,
            show_edge_lengths=enabled,
            force_background=(None if self._label_view_mode == "selected" else (False if enabled else None)),
            preserve_layout=True,
            preserve_view_override=True,
        )

    def _redraw_current(self, preserve_layout: bool = True):
        """현재 장면을 다시 만들되 화면 위치는 기본적으로 완전히 고정한다.

        표시 설정/마감 필터/오버레이 변경처럼 실제 도면 범위가 바뀌지 않는
        대부분의 재그리기는 기존 sceneRect와 스크롤바 값을 그대로 재사용한다.
        QGraphicsScene.itemsBoundingRect()가 표시 객체의 선두께/문자/채움에 따라
        달라지면서 sceneRect가 미세하게 변하고 도곽이 한쪽으로 밀리던 현상을
        공통 redraw 단계에서 차단한다.

        CAD 배경 파일 자체가 교체되는 등 실제 표시범위를 다시 계산해야 할 때만
        preserve_layout=False를 사용한다.
        """
        selected_rooms = [room for room in self._current_rooms if self._room_is_selected(room)]
        self._draw_rooms(
            self._current_rooms,
            self._current_selected,
            selected_rooms=selected_rooms,
            show_edge_lengths=self._show_edge_lengths,
            force_background=(None if self._label_view_mode == "selected" else (False if self._show_edge_lengths else None)),
            preserve_layout=bool(preserve_layout),
        )

    @staticmethod
    def _rooms_bounds(rooms: list[RoomRecord]):
        # V19.0: 비정상/빈 실 geometry가 섞여 있어도 층 전환 때문에 프로그램이 종료되지 않게 방어.
        all_points = [point for room in rooms for point in (getattr(room, "points", None) or [])]
        if not all_points:
            return None
        return (
            min(x for x, _ in all_points),
            min(y for _, y in all_points),
            max(x for x, _ in all_points),
            max(y for _, y in all_points),
        )

    def _background_key(self, rooms, crop_rect, floor_override: str = ""):
        floor = str(floor_override or (rooms[0].floor if rooms else "") or "")
        if floor in self.floor_background_files:
            info = self.floor_background_files[floor]
            return ("disk", floor, info.get("path", ""))
        rounded_crop = tuple(round(value, 2) for value in crop_rect)
        return (
            self._background_generation,
            floor,
            rounded_crop,
            self.background_max_pixels,
            self.background_style,
            round(self.background_line_width, 3),
            self.background_aci_widths,
            self.background_keep_red,
        )

    @staticmethod
    def _crop_cached_floor_pixmap(pixmap: QPixmap, source_rect, target_rect):
        """층 전체 PNG 캐시에서 요청한 world 범위만 잘라 선택실 배경으로 사용한다."""
        if pixmap is None or pixmap.isNull():
            return pixmap, tuple(source_rect)
        try:
            sxmin, symin, sxmax, symax = [float(v) for v in source_rect]
            txmin, tymin, txmax, tymax = [float(v) for v in target_rect]
        except Exception:
            return pixmap, tuple(source_rect)
        ixmin = max(sxmin, txmin)
        iymin = max(symin, tymin)
        ixmax = min(sxmax, txmax)
        iymax = min(symax, tymax)
        sw = sxmax - sxmin
        sh = symax - symin
        if sw <= 1e-9 or sh <= 1e-9 or ixmax <= ixmin or iymax <= iymin:
            return pixmap, tuple(source_rect)
        # 요청범위가 사실상 전체 캐시와 같으면 불필요한 copy를 생략한다.
        if (
            abs(ixmin - sxmin) <= 1e-6 and abs(iymin - symin) <= 1e-6
            and abs(ixmax - sxmax) <= 1e-6 and abs(iymax - symax) <= 1e-6
        ):
            return pixmap, (sxmin, symin, sxmax, symax)
        pw = max(int(pixmap.width()), 1)
        ph = max(int(pixmap.height()), 1)
        left = int(round((ixmin - sxmin) / sw * pw))
        right = int(round((ixmax - sxmin) / sw * pw))
        top = int(round((symax - iymax) / sh * ph))
        bottom = int(round((symax - iymin) / sh * ph))
        left = max(0, min(pw - 1, left))
        top = max(0, min(ph - 1, top))
        right = max(left + 1, min(pw, right))
        bottom = max(top + 1, min(ph, bottom))
        return pixmap.copy(left, top, right - left, bottom - top), (ixmin, iymin, ixmax, iymax)

    def _get_floor_background(self, rooms, crop_rect, floor_override: str = ""):
        key = self._background_key(rooms, crop_rect, floor_override)
        cached = self._background_cache.get(key)
        if cached is not None:
            self._background_cache.move_to_end(key)
            return cached

        floor = str(floor_override or (rooms[0].floor if rooms else "") or "")
        if floor in self.floor_background_files:
            info = self.floor_background_files[floor]
            pixmap = QPixmap(str(info.get("path", "")))
            source_rect = tuple(info.get("crop_rect", crop_rect))
            pixmap, visible_rect = self._crop_cached_floor_pixmap(pixmap, source_rect, crop_rect)
            value = (pixmap, visible_rect)
            self._background_cache[key] = value
            self._background_cache.move_to_end(key)
            while len(self._background_cache) > BACKGROUND_CACHE_LIMIT:
                self._background_cache.popitem(last=False)
            return value

        xmin, ymin, xmax, ymax = crop_rect
        world_width = max(xmax - xmin, 1.0)
        world_height = max(ymax - ymin, 1.0)
        longest = max(world_width, world_height)
        pixel_width = max(1, round(self.background_max_pixels * world_width / longest))
        pixel_height = max(1, round(self.background_max_pixels * world_height / longest))

        image = QImage(pixel_width, pixel_height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        sx = pixel_width / world_width
        sy = pixel_height / world_height
        width_map = parse_width_map(self.background_aci_widths)
        for segment in self.cad_background:
            x1, y1, x2, y2 = segment[:4]
            rgb = segment[4:7] if len(segment) >= 7 else (170, 170, 170)
            aci = segment[7] if len(segment) >= 8 and int(segment[7]) >= 0 else None
            if self.background_style == "cad" or (self.background_style == "gray" and self.background_keep_red and aci == 1):
                color = QColor(*rgb)
            else:
                color = QColor(150, 150, 150)
            pen = QPen(color); pen.setWidthF(width_for_aci(aci, width_map, self.background_line_width)); painter.setPen(pen)
            if max(x1, x2) < xmin or min(x1, x2) > xmax:
                continue
            if max(y1, y2) < ymin or min(y1, y2) > ymax:
                continue
            clipped = clip_line_to_rect(x1, y1, x2, y2, xmin, ymin, xmax, ymax)
            if clipped is None:
                continue
            cx1, cy1, cx2, cy2 = clipped
            painter.drawLine(
                QPointF((cx1 - xmin) * sx, (ymax - cy1) * sy),
                QPointF((cx2 - xmin) * sx, (ymax - cy2) * sy),
            )
        painter.end()

        value = (QPixmap.fromImage(image), crop_rect)
        self._background_cache[key] = value
        self._background_cache.move_to_end(key)
        while len(self._background_cache) > BACKGROUND_CACHE_LIMIT:
            self._background_cache.popitem(last=False)
        return value

    def _add_floor_background(self, scene, rooms, minx, miny, maxx, maxy, floor_override: str = ""):
        width = max(maxx - minx, 1.0)
        height = max(maxy - miny, 1.0)
        padding = max(width, height) * 0.15
        crop_rect = (
            minx - padding,
            miny - padding,
            maxx + padding,
            maxy + padding,
        )
        floor = str(floor_override or (rooms[0].floor if rooms else "") or "")
        if self.background_render_mode == "vector" and floor in self.floor_vector_cache:
            info = dict(self.floor_vector_cache.get(floor) or {})
            try:
                selected_clip_polygons = None
                if self._label_view_mode == "selected" and rooms:
                    selected_clip_polygons = [list(room.points) for room in rooms if len(room.points) >= 3]
                item = CachedVectorBackgroundItem(
                    info, self.background_style, self.background_opacity, self.background_line_width,
                    self.background_aci_widths, self.background_keep_red, self.show_original_text,
                    self.cad_background_mode == "white", display_rect=crop_rect,
                    display_polygons=selected_clip_polygons,
                    zoomout_width_weight=self.background_zoomout_width_weight,
                    zoom_ratio_provider=self._overlay_zoom_ratio,
                )
                item.setPos(item.xmin-minx, miny-item.ymax)
                scene.addItem(item)
                return
            except Exception:
                pass
        pixmap, (crop_minx, crop_miny, crop_maxx, crop_maxy) = self._get_floor_background(
            rooms, crop_rect, floor
        )
        pixmap = self._display_background_pixmap(pixmap)
        item = scene.addPixmap(pixmap)
        item.setPos(crop_minx - minx, miny - crop_maxy)
        item.setTransform(
            QTransform.fromScale(
                (crop_maxx - crop_minx) / max(pixmap.width(), 1),
                (crop_maxy - crop_miny) / max(pixmap.height(), 1),
            )
        )
        item.setOpacity(self.background_opacity / 100.0)
        item.setZValue(-100)

        if self.show_original_text and floor:
            text_path = str(self.floor_background_files.get(floor, {}).get("text_path", "") or "")
            if text_path:
                text_pixmap = QPixmap(text_path)
                if not text_pixmap.isNull():
                    source_rect = tuple(self.floor_background_files.get(floor, {}).get("crop_rect", (crop_minx, crop_miny, crop_maxx, crop_maxy)))
                    text_pixmap, _text_rect = self._crop_cached_floor_pixmap(
                        text_pixmap, source_rect, (crop_minx, crop_miny, crop_maxx, crop_maxy)
                    )
                    text_pixmap = self._display_background_pixmap(text_pixmap)
                    text_item = scene.addPixmap(text_pixmap)
                    text_item.setPos(crop_minx - minx, miny - crop_maxy)
                    text_item.setTransform(
                        QTransform.fromScale(
                            (crop_maxx - crop_minx) / max(text_pixmap.width(), 1),
                            (crop_maxy - crop_miny) / max(text_pixmap.height(), 1),
                        )
                    )
                    text_item.setOpacity(self.background_opacity / 100.0)
                    text_item.setZValue(-90)

    def _floor_display_bounds(self, rooms: list[RoomRecord], show_edge_lengths: bool):
        """층 전체보기에서는 평면도 도곽 캐시 범위를 우선 사용한다."""
        room_bounds = self._rooms_bounds(rooms)
        if show_edge_lengths or self._label_view_mode == "selected" or not rooms:
            return room_bounds, False
        floor = rooms[0].floor
        info = self.floor_vector_cache.get(floor, {}) if self.background_render_mode == "vector" else self.floor_background_files.get(floor, {})
        crop = info.get("crop_rect")
        if info.get("crop_source") == "drawing_frame" and crop and len(crop) == 4:
            try:
                return tuple(float(value) for value in crop), True
            except (TypeError, ValueError):
                pass
        return room_bounds, False

    @staticmethod
    def _floor_view_key(rooms: list[RoomRecord], show_edge_lengths: bool):
        """층 전체보기의 동일 내용 여부 판단용 키.

        show_floor()의 불필요한 재그리기 방지에만 사용한다. 화면 위치 보존은
        실 Handle/점 개수 변화와 분리해서 _view_scope_key()로 판단한다.
        """
        if show_edge_lengths or not rooms:
            return None
        return (
            rooms[0].floor,
            tuple(sorted(room.boundary_handle for room in rooms)),
        )

    @staticmethod
    def _view_scope_key(rooms: list[RoomRecord], show_edge_lengths: bool):
        """새로고침 전후에 같은 도면을 보고 있는지 판단한다.

        층 전체보기는 같은 층이면 실 Handle/좌표가 갱신되어도 같은 화면으로 본다.
        선택 실 보기는 같은 실 Handle일 때만 위치를 보존한다.
        """
        if not rooms:
            return None
        floor = str(rooms[0].floor or "")
        if show_edge_lengths and len(rooms) == 1:
            return ("room", floor, str(rooms[0].boundary_handle or ""))
        if show_edge_lengths:
            return ("dimension-floor", floor)
        return ("floor", floor)

    def _capture_world_view_center(self):
        """현재 viewport 중심을 CAD/world 좌표로 보관한다.

        scene 좌표는 매번 display bounds의 minx/miny를 원점으로 다시 만들기 때문에
        scene 중심값 자체를 저장하면 도곽/실 좌표가 조금만 달라져도 화면이 밀린다.
        반드시 world 좌표로 변환해서 보관해야 한다.
        """
        scene_center = self.mapToScene(self.viewport().rect().center())
        return QPointF(
            scene_center.x() + self._current_minx,
            self._current_miny - scene_center.y(),
        )

    @staticmethod
    def _world_to_scene_center(world_center: QPointF, minx: float, miny: float) -> QPointF:
        return QPointF(
            world_center.x() - minx,
            miny - world_center.y(),
        )

    def _room_base_color(self, room: RoomRecord) -> QColor:
        incomplete = room_name_is_missing(room.room_name) or not str(room.room_number or "").strip()
        return self.incomplete_room_color if incomplete else self.boundary_color

    def _add_wall_segment_item(self, scene, key: str, p1: QPointF, p2: QPointF, tip: str):
        # 모든 벽 세그먼트 item은 만들어 두고 필요할 때만 보이게 한다.
        # 그래야 그룹 행을 클릭할 때 장면 전체를 다시 그리지 않고 즉시 강조할 수 있다.
        group = self._group_for_wall_key(key)
        material = self.wall_finish_assignments.get(key, {})
        selected = key in self.selected_wall_keys
        focused = self._wall_in_focused_group(key)
        if selected or focused:
            color = self.wall_selection_color
            width = self.wall_selection_width
            visible = True
        elif (
            group
            and self.show_group_overlay
            and not (
                self.wall_finish_mode
                and str(group.get("id", "") or "") == self.focused_finish_group_id
            )
        ):
            color = self._overlay_color(group.get("color", ""))
            # 기존 지정 그룹 표시도 벽 지정 선택선 두께 옵션을 사용한다.
            width = self.wall_selection_width
            visible = True
        else:
            has_typed_group = self._has_any_wall_group(key)
            legacy_material = bool(material) and not has_typed_group and self._legacy_filter_matches()
            if legacy_material:
                color = QColor(material.get("color", "#808080"))
                width = self.wall_selection_width
            else:
                color = QColor("#808080")
                width = 1
            visible = self.wall_finish_mode
        pen = self._wall_pen(color, width)
        line_item = scene.addLine(p1.x(), p1.y(), p2.x(), p2.y(), pen)
        line_item.setZValue(40)
        line_item.setToolTip(tip)
        line_item.setVisible(visible)
        self._wall_segments.append((key, p1, p2))
        self._wall_segment_items[key] = (line_item, dict(material))

    def _draw_rooms(
        self,
        rooms,
        selected=None,
        selected_rooms=None,
        show_edge_lengths=False,
        force_background=None,
        preserve_layout: bool = False,
        preserve_view_override: bool = False,
    ):
        rooms = list(rooms)

        # V23.28: 화면표시용 redraw는 sceneRect/스크롤바까지 보존한다.
        # 기존의 transform+centerOn 복원만으로는 scene.itemsBoundingRect()가
        # 표시 객체마다 달라질 때 QGraphicsView가 스크롤 범위를 재계산하면서
        # 화면이 조금씩 한쪽으로 밀릴 수 있었다.
        old_scene = self.scene()
        old_scene_rect = QRectF(old_scene.sceneRect()) if old_scene is not None else QRectF()
        old_fit_rect = QRectF(self._fit_rect) if self._fit_rect is not None else None
        old_hscroll = self.horizontalScrollBar().value()
        old_vscroll = self.verticalScrollBar().value()
        old_display_bounds = tuple(self._current_display_bounds) if self._current_display_bounds else None

        # V22.8: 모든 장면 재생성 경로에서 "같은 층/같은 실"이면 화면을 고정한다.
        # 이전 방식은 scene 중심 좌표를 그대로 저장했는데, scene 원점이 minx/miny에
        # 따라 다시 잡히므로 도곽/실 좌표가 갱신될 때마다 화면이 좌상단으로 밀릴 수 있었다.
        # 이제 viewport 중심을 CAD/world 좌표로 저장한 뒤 새 minx/miny 기준 scene 좌표로
        # 환산해 복원한다. 실 좌표/Handle 목록이 바뀌어도 같은 층이면 PAN/줌을 유지한다.
        old_scope_key = self._view_scope_key(self._current_rooms, self._show_edge_lengths)
        new_scope_key = self._view_scope_key(rooms, bool(show_edge_lengths))
        preserve_view = (
            old_scope_key is not None and old_scope_key == new_scope_key
        ) or bool(preserve_view_override)

        old_transform = QTransform(self.transform()) if preserve_view else None
        old_world_center = self._capture_world_view_center() if preserve_view else None

        self._current_rooms = rooms
        if rooms:
            self._current_background_floor = str(rooms[0].floor or "")
        self._current_selected = selected
        selected_rooms = list(selected_rooms or ([selected] if selected is not None else []))
        self._current_selected_room_handles = {
            str(room.boundary_handle or "") for room in selected_rooms if str(room.boundary_handle or "")
        }
        self._show_edge_lengths = bool(show_edge_lengths)
        draw_background = self.show_cad_background if force_background is None else bool(force_background)
        self._update_canvas_background(draw_background)

        scene = self.scene()
        scene.clear()
        self._room_polygons = []
        self._room_display_items = {}
        self._deduction_display_items = []
        self._ceiling_display_items = []
        self._edge_length_items = []
        self._wall_segments = []
        self._wall_segment_items = {}
        self._boundary_split_highlight_items = []
        self._boundary_split_endpoint_markers = {}
        if not rooms:
            self._fit_rect = None
            self._current_display_bounds = None
            return

        display_bounds, using_drawing_frame = self._floor_display_bounds(rooms, bool(show_edge_lengths))
        if not display_bounds:
            # 배경 도곽도 실 geometry도 없는 비정상 층은 빈 화면으로만 처리한다.
            self._fit_rect = None
            self._current_display_bounds = None
            return
        minx, miny, maxx, maxy = display_bounds
        new_display_bounds = (float(minx), float(miny), float(maxx), float(maxy))
        same_display_bounds = bool(
            old_display_bounds
            and len(old_display_bounds) == 4
            and all(abs(float(a) - float(b)) <= 1e-6 for a, b in zip(old_display_bounds, new_display_bounds))
        )
        reuse_view_layout = bool(
            preserve_layout
            and preserve_view
            and same_display_bounds
            and not old_scene_rect.isNull()
            and old_fit_rect is not None
            and not old_fit_rect.isNull()
        )
        self._current_display_bounds = new_display_bounds
        self._current_minx = minx
        self._current_miny = miny
        self._current_display_width = max(maxx - minx, 1.0)
        # 여러 실의 치수를 한 화면에 표시할 때는 실마다 크기를 따로 계산하지 않는다.
        # 현재 전체 표시범위(span)를 하나의 기준으로 사용해 모든 치수문자 크기를 통일한다.
        shared_edge_font_size = None
        if show_edge_lengths and len(rooms) > 1:
            display_span = max(maxx - minx, maxy - miny, 1.0)
            shared_edge_font_size = self._edge_font_size_from_span(display_span)
        has_background = bool(self.cad_background) or bool(self.floor_background_files) or bool(self.floor_vector_cache)
        if draw_background and has_background:
            self._add_floor_background(scene, rooms, minx, miny, maxx, maxy)

        # 층 전체보기의 "실 라인" OFF는 프로그램이 만든 실 경계/실명/마감 오버레이를
        # 모두 숨겨 CAD 배경만 보이게 한다. 선택 실 보기에서는 기존 표시를 유지한다.
        overlay_rooms = rooms if (self._label_view_mode == "selected" or show_edge_lengths or self.show_room_overlay) else []
        for room in overlay_rooms:
            poly = QPolygonF([QPointF(x - minx, -(y - miny)) for x, y in room.points])
            is_selected = self._room_is_selected(room)
            focused_area = self._room_in_focused_area_group(room)
            base_color = self._room_base_color(room)
            room_pen = self._overlay_pen(
                self.selection_color if (is_selected or focused_area) else base_color,
                self.selection_width if (is_selected or focused_area) else self.boundary_width,
            )
            room_assign = self.room_finish_assignments.get(room.boundary_handle, {})
            floor_material = first_room_finish_material(room_assign, "바닥")
            floor_brush = QBrush(Qt.transparent)
            if focused_area:
                fill_color = QColor(self.selection_color)
                fill_color.setAlpha(55)
                floor_brush = QBrush(fill_color)
            else:
                floor_group = self._group_for_room_section(room, "바닥")
                editing_focused_floor_group = (
                    self.room_group_mode
                    and bool(floor_group)
                    and str(floor_group.get("id", "") or "") == self.focused_finish_group_id
                )
                if floor_group and not editing_focused_floor_group:
                    if self.show_group_overlay:
                        floor_brush = QBrush(self._overlay_color(floor_group.get("color", "")))
                elif (
                    floor_material
                    and not editing_focused_floor_group
                    and not self._has_any_room_group_for_section(room, "바닥")
                    and self._legacy_filter_matches()
                ):
                    # 그룹 정보가 없는 구형 재료지정은 마감으로 간주한다.
                    fill_color = QColor(floor_material.get("color", "#5B8FF9"))
                    fill_color.setAlpha(55)
                    floor_brush = QBrush(fill_color)
            polygon_item = scene.addPolygon(poly, room_pen, floor_brush)
            # 실공제선은 CAD 배경 제외 패턴에 포함돼도 내부 실구분 화면에서
            # 확인할 수 있도록 RoomRecord 좌표를 별도 오버레이로 그린다.
            for deduction_index, deduction_points in enumerate(getattr(room, "deduction_points", []) or []):
                if len(deduction_points) < 3:
                    continue
                deduction_poly = QPolygonF([
                    QPointF(x - minx, -(y - miny)) for x, y in deduction_points
                ])
                deduction_pen = self._overlay_pen(self.deduction_color, self.deduction_width)
                deduction_item = scene.addPolygon(deduction_poly, deduction_pen, QBrush(Qt.transparent))
                self._deduction_display_items.append((deduction_item, QColor(self.deduction_color), float(self.deduction_width), Qt.SolidLine))
                deduction_item.setZValue(18)
                deduction_item.setToolTip(
                    f"실 공제선 {deduction_index + 1} · 면적/둘레 산출 반영"
                )
                for segment_index, p1 in enumerate(deduction_poly):
                    p2 = deduction_poly[(segment_index + 1) % len(deduction_poly)]
                    key = f"{room.boundary_handle}:d{deduction_index}:{segment_index}"
                    self._add_wall_segment_item(
                        scene, key, p1, p2,
                        f"공제 벽 구간 {deduction_index + 1}-{segment_index + 1}",
                    )
            ceiling_material = first_room_finish_material(room_assign, "천정")
            ceiling_group = self._group_for_room_section(room, "천정")
            editing_focused_ceiling_group = (
                self.room_group_mode
                and bool(ceiling_group)
                and str(ceiling_group.get("id", "") or "") == self.focused_finish_group_id
            )
            legacy_ceiling = (
                not ceiling_group
                and bool(ceiling_material)
                and not self._has_any_room_group_for_section(room, "천정")
                and self._legacy_filter_matches()
            )
            if (ceiling_group and self.show_group_overlay and not editing_focused_ceiling_group) or legacy_ceiling:
                ceiling_color = self._overlay_color(ceiling_group.get("color", "")) if ceiling_group else QColor(ceiling_material.get("color", "#5AD8A6"))
                ceiling_width = max(2, self.boundary_width // 2)
                ceiling_pen = self._overlay_pen(ceiling_color, ceiling_width, Qt.DashLine)
                ceiling_item = scene.addPolygon(poly, ceiling_pen, QBrush(Qt.transparent))
                self._ceiling_display_items.append((ceiling_item, QColor(ceiling_color), float(ceiling_width), Qt.DashLine))
                ceiling_item.setZValue(15)
            polygon_item.setToolTip(f"{room.floor} / {room.room_number} / {normalize_room_name(room.room_name)}")
            self._room_polygons.append((room, poly))

            if len(room.points) >= 2:
                for segment_index, (x1, y1) in enumerate(room.points):
                    x2, y2 = room.points[(segment_index + 1) % len(room.points)]
                    p1 = QPointF(x1 - minx, -(y1 - miny))
                    p2 = QPointF(x2 - minx, -(y2 - miny))
                    key = f"{room.boundary_handle}:{segment_index}"
                    material = self.wall_finish_assignments.get(key, {})
                    tip = material.get("name", "미지정")
                    self._add_wall_segment_item(scene, key, p1, p2, f"벽 구간 {segment_index + 1} / {tip}")

            label = normalize_room_name(room.room_name)
            if room.room_number.strip():
                label = f"{room.room_number}\n{label}"
            text_item = scene.addText(label)
            font = text_item.font()
            font.setPointSize(self._label_point_size(room))
            text_item.setFont(font)
            text_item.setDefaultTextColor(self.selection_color if is_selected else base_color)
            cx, cy = polygon_label_point(room.points)
            text_rect = text_item.boundingRect()
            text_item.setPos(
                (cx - minx) - text_rect.width() / 2,
                (-(cy - miny)) - text_rect.height() / 2,
            )
            text_item.setZValue(20)
            self._room_display_items[id(room)] = (polygon_item, text_item)

            if show_edge_lengths and len(room.points) >= 2:
                for i, (x1, y1) in enumerate(room.points):
                    x2, y2 = room.points[(i + 1) % len(room.points)]
                    length_m = math.hypot(x2 - x1, y2 - y1) / 1000.0
                    if length_m < 0.01:
                        continue
                    length_text = f"{length_m:,.3f}" + (" m" if self.edge_length_show_unit_m else "")
                    length_item = scene.addText(length_text)
                    edge_font = length_item.font()
                    edge_font.setPointSize(
                        shared_edge_font_size
                        if shared_edge_font_size is not None
                        else self._effective_edge_font_size(room)
                    )
                    length_item.setFont(edge_font)
                    length_item.setDefaultTextColor(Qt.darkBlue)
                    length_item.setZValue(30)
                    mid_x = ((x1 + x2) / 2.0) - minx
                    mid_y = -(((y1 + y2) / 2.0) - miny)
                    dx = x2 - x1
                    dy = y2 - y1
                    seg_len = math.hypot(dx, dy)
                    if seg_len > 1e-9 and self.edge_length_offset:
                        signed_area2 = 0.0
                        for j, (px, py) in enumerate(room.points):
                            qx, qy = room.points[(j + 1) % len(room.points)]
                            signed_area2 += px * qy - qx * py
                        if signed_area2 >= 0:
                            nx_world, ny_world = dy / seg_len, -dx / seg_len
                        else:
                            nx_world, ny_world = -dy / seg_len, dx / seg_len
                        mid_x += nx_world * self.edge_length_offset
                        mid_y += -ny_world * self.edge_length_offset
                    angle = math.degrees(math.atan2(-(y2 - y1), x2 - x1))
                    if angle > 90:
                        angle -= 180
                    elif angle < -90:
                        angle += 180
                    rect = length_item.boundingRect()
                    length_item.setTransformOriginPoint(rect.width() / 2.0, rect.height() / 2.0)
                    length_item.setTransform(self._edge_text_transform(angle))
                    length_item.setPos(mid_x - rect.width() / 2.0, mid_y - rect.height() / 2.0)
                    length_item.setRotation(angle)
                    self._edge_length_items.append((length_item, angle))

        self._opening_marker_items = {}
        self._opening_marker_aux_items = {}

        # 연결 CAD에서 현재층만 임시 검출한 문/창 중심점.
        # 창호 형상은 CAD 배경이 이미 보여주므로 프로그램에는 부호만 덧씌운다.
        current_floor = str(rooms[0].floor or "") if rooms else ""
        if current_floor and current_floor == self.opening_test_floor:
            for marker in self.opening_test_markers:
                # 선택실 보기에서는 현재 화면에 표시된 실 벽에 걸린 창호만 보여준다.
                if self._label_view_mode == "selected" and not self._opening_marker_touches_rooms(marker, rooms):
                    continue
                try:
                    wx = float(marker.get("x"))
                    wy = float(marker.get("y"))
                except (TypeError, ValueError):
                    continue
                label_text = str(marker.get("label", "") or ("D" if marker.get("kind") == "door" else "W"))
                read_id = str(marker.get("read_id", "") or "")
                selected = bool(read_id and read_id in self.selected_opening_ids)

                # 창호는 중심 부호만 떠다니지 않게 실제 벽면 위에 길이 오버레이를 그린다.
                # 적용 창길이가 있으면 적용값, 없으면 검출 창길이를 사용한다.
                try:
                    length_mm = float(marker.get("display_length_mm", 0.0) or marker.get("applied_length_mm", 0.0) or marker.get("detected_length_mm", 0.0) or 0.0)
                except (TypeError, ValueError):
                    length_mm = 0.0
                direction = marker.get("wall_direction")
                try:
                    ux = float(direction[0]); uy = float(direction[1])
                    dlen = math.hypot(ux, uy)
                    if dlen <= 1e-9:
                        raise ValueError
                    ux /= dlen; uy /= dlen
                except Exception:
                    ux, uy = 1.0, 0.0
                aux_items = []
                if length_mm > 0.0:
                    centers = []
                    for key in ("wall_attach_primary", "wall_attach_secondary"):
                        value = marker.get(key)
                        if isinstance(value, (list, tuple)) and len(value) >= 2:
                            try:
                                centers.append((float(value[0]), float(value[1])))
                            except (TypeError, ValueError):
                                pass
                    if not centers:
                        centers = [(wx, wy)]
                    half = length_mm / 2.0
                    line_color = self.selection_color if selected else self.opening_line_color
                    # 창호선 자체 두께는 선택 여부와 관계없이 창호선 설정값을 유지한다.
                    line_width = self.opening_line_width
                    for cx, cy in centers[:2]:
                        ax, ay = cx - ux * half, cy - uy * half
                        bx, by = cx + ux * half, cy + uy * half
                        line = scene.addLine(
                            ax - minx, -(ay - miny), bx - minx, -(by - miny),
                            self._overlay_pen(line_color, line_width),
                        )
                        line.setZValue(95 if selected else 87)
                        aux_items.append(line)

                if self._label_view_mode == "selected" and show_edge_lengths and length_mm > 0.0:
                    self._add_opening_length_dimension(
                        scene, marker, rooms, length_mm, minx, miny, shared_edge_font_size
                    )

                text_item = scene.addText(label_text)
                font = text_item.font()
                font.setBold(True)
                font.setPointSize(max(18, int(self.label_font_size * 0.72)))
                font.setUnderline(selected)
                text_item.setFont(font)
                # 창호 편집에서는 적용부호 지정 여부를 도면 글씨색으로도 즉시 구분한다.
                # 선택 중인 창호는 기존 빨강, 지정 완료는 자홍, 미지정은 주황으로 표시한다.
                has_applied_code = bool(str(marker.get("applied_code", "") or "").strip())
                label_color = QColor("#FF0000") if selected else QColor("#FF00FF" if has_applied_code else "#FFB000")
                text_item.setDefaultTextColor(label_color)
                rect = text_item.boundingRect()
                text_item.setPos((wx - minx) - rect.width() / 2.0, (-(wy - miny)) - rect.height() / 2.0)
                text_item.setZValue(96 if selected else 88)
                if read_id:
                    text_item.setData(0, read_id)
                    self._opening_marker_items[read_id] = text_item
                    self._opening_marker_aux_items[read_id] = aux_items
                kind_text = "문 후보" if marker.get("kind") == "door" else "창 후보"
                detail = [kind_text, f"신뢰도 {marker.get('confidence', 0)}", str(marker.get("source", "") or "")]
                if marker.get("block_name"):
                    detail.append(f"블록 {marker.get('block_name')}")
                if marker.get("aci"):
                    detail.append(f"ACI {marker.get('aci')}")
                if length_mm > 0:
                    try:
                        applied_length = float(marker.get("applied_length_mm", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        applied_length = 0.0
                    source_name = "적용" if applied_length > 0 else "검출"
                    detail.append(f"{source_name} 창길이 {length_mm:.0f}mm")
                if marker.get("wall_snapped"):
                    pair_gap = float(marker.get("wall_pair_gap", 0.0) or 0.0)
                    detail.append("양쪽 벽 물림" if pair_gap > 0 else "벽 스냅")
                tooltip = " · ".join(value for value in detail if value)
                text_item.setToolTip(tooltip)
                for aux in aux_items:
                    aux.setToolTip(tooltip)

        base_span = max(maxx - minx, maxy - miny, 1.0)
        # 선택실 보기는 치수문자까지 답답하게 붙지 않도록 기본 여백을 넉넉히 준다.
        visible_ratio = 0.12 if show_edge_lengths and len(rooms) == 1 else (0.015 if using_drawing_frame else 0.03)
        margin = base_span * visible_ratio + (100 if show_edge_lengths and len(rooms) == 1 else 50)
        if self._label_view_mode == "selected" and rooms:
            # 선택실만 보기는 주변 CAD 객체/다른 실이 scene에 있어도 확대 기준에 끌려가지 않는다.
            # 현재 선택된 실(다중선택이면 선택 묶음)의 geometry만 fit 기준으로 사용한다.
            content_rect = QRectF(0.0, -(maxy - miny), maxx - minx, maxy - miny).adjusted(-margin, -margin, margin, margin)
        elif using_drawing_frame:
            # 도곽 안쪽 전체를 기준으로 맞추고, 실구획선이 없는 영역도 화면에 포함한다.
            content_rect = QRectF(0.0, -(maxy - miny), maxx - minx, maxy - miny).adjusted(-margin, -margin, margin, margin)
            content_rect = content_rect.united(scene.itemsBoundingRect().adjusted(-margin, -margin, margin, margin))
        else:
            content_rect = scene.itemsBoundingRect().adjusted(-margin, -margin, margin, margin)

        # fit 대상과 PAN 가능영역을 분리한다. 선택실은 처음에는 적당히 확대해서 보되,
        # sceneRect는 훨씬 크게 만들어 가운데버튼으로 좌우/상하 여백까지 이동할 수 있게 한다.
        if show_edge_lengths and len(rooms) == 1:
            pan_x = max(content_rect.width() * 0.75, 800.0)
            pan_y = max(content_rect.height() * 0.75, 800.0)
            scene_rect = content_rect.adjusted(-pan_x, -pan_y, pan_x, pan_y)
        elif self._label_view_mode == "floor":
            # 층 전체보기는 도면 자체의 맞춤 배율은 유지하되 좌우에 빈 장면을 둔다.
            # 따라서 처음에는 전체 도면이 그대로 보이고, 손바닥 드래그로 도면을
            # 좌우로 밀어 빈 공간까지 확인할 수 있다.
            pan_x = max(content_rect.width() * self.floor_horizontal_pan_margin_ratio, 800.0)
            pan_y = max(content_rect.height() * self.floor_vertical_pan_margin_ratio, 800.0)
            scene_rect = content_rect.adjusted(-pan_x, -pan_y, pan_x, pan_y)
        else:
            scene_rect = content_rect
        if reuse_view_layout:
            # 화면표시만 바뀐 redraw에서는 장면 범위 자체를 바꾸지 않는다.
            # 이전 scrollbar 값이 같은 scene 좌표를 가리키므로 PAN 위치까지
            # 픽셀 단위로 그대로 유지된다.
            scene.setSceneRect(old_scene_rect)
            self._fit_rect = old_fit_rect
        else:
            scene.setSceneRect(scene_rect)
            self._fit_rect = content_rect

        if preserve_view and old_transform is not None and old_world_center is not None:
            self.setTransform(old_transform)
            if reuse_view_layout:
                self.horizontalScrollBar().setValue(old_hscroll)
                self.verticalScrollBar().setValue(old_vscroll)
            else:
                self.centerOn(self._world_to_scene_center(old_world_center, minx, miny))
        else:
            self.reset_view()
        # reset_view()에서 확정된 전체보기 배율을 기준으로 도곽 오버레이 선 전체를 갱신한다.
        self._update_all_overlay_pens_in_place()

    def _label_point_size(self, room: RoomRecord) -> int:
        """실명 문자 크기 계산.

        room_area: 기존처럼 각 실 면적에 비례한다.
        full_width: 층 전체보기에서 현재 표시되는 전체 폭에 비례한 하나의 크기를
        모든 실에 동일 적용한다. 선택실 보기는 기존 실면적 기준을 유지한다.
        """
        view_weight = self.label_selected_view_weight if self._label_view_mode == "selected" else self.label_full_view_weight

        if self.label_scale_mode == "full_width" and self._label_view_mode == "floor":
            # 약 30m 폭 도면을 기준(1.0)으로 잡는다. 화면 fit 배율이 폭에 반비례하므로
            # 문자 scene 크기를 폭에 비례시키면 전체보기에서 실마다 동일한 화면상 크기를 유지한다.
            width_factor = self._current_display_width / 30000.0
            width_factor = max(0.35, min(4.00, width_factor))
            factor = width_factor
        else:
            area_factor = math.sqrt(max(room.area_m2, 0.01) / 25.0)
            factor = max(0.50, min(1.50, area_factor))

        label_size = int(round(self.label_font_size * factor * self.label_area_scale * view_weight))
        return max(6, min(500, label_size))

    def _update_label_fonts_in_place(self):
        """라벨 크기만 갱신하고 장면/도곽/현재 뷰 중심은 건드리지 않는다."""
        for room in self._current_rooms:
            items = self._room_display_items.get(id(room))
            if not items:
                continue
            _polygon_item, text_item = items
            font = text_item.font()
            font.setPointSize(self._label_point_size(room))
            text_item.setFont(font)
            cx, cy = polygon_label_point(room.points)
            text_rect = text_item.boundingRect()
            text_item.setPos(
                (cx - self._current_minx) - text_rect.width() / 2,
                (-(cy - self._current_miny)) - text_rect.height() / 2,
            )
        self.viewport().update()

    def set_label_font_size(self, size: int):
        size = max(6, min(500, int(size)))
        if size == self.label_font_size:
            return
        self.label_font_size = size
        # V15.1: 글자 크기 변경만으로 sceneRect/fitInView를 다시 계산하지 않는다.
        # QGraphicsView 스크롤바 정수 반올림 누적으로 도곽이 조금씩 밀리는 현상을 방지한다.
        self._update_label_fonts_in_place()

    def set_label_area_scale(self, value: float):
        self.label_area_scale = max(0.1, min(10.0, float(value)))
        self._redraw_current()

    def show_room(self, room: RoomRecord | None, show_edge_lengths: bool = True):
        self._label_view_mode = "selected"
        self._draw_rooms([room] if room else [], room, [room] if room else [], show_edge_lengths=bool(room) and bool(show_edge_lengths), force_background=None)

    def show_selected_scope(self, rooms: list[RoomRecord], selected: RoomRecord | None = None, selected_rooms=None, show_edge_lengths: bool = True):
        """선택 실만 보기에서 다중선택/층 전체에도 선택적으로 치수를 표시한다."""
        self._label_view_mode = "selected"
        rooms = list(rooms or [])
        selected_rooms = [room for room in list(selected_rooms or []) if room in rooms]
        if selected_rooms:
            current = selected if selected in selected_rooms else selected_rooms[0]
            self._draw_rooms(
                selected_rooms, current, selected_rooms,
                show_edge_lengths=bool(show_edge_lengths), force_background=None,
            )
            return
        if selected is not None:
            self.show_room(selected, show_edge_lengths=show_edge_lengths)
            return
        self._draw_rooms(rooms, None, [], show_edge_lengths=bool(show_edge_lengths), force_background=None)

    def _update_floor_selection(self, selected: RoomRecord | None, selected_rooms=None):
        """층 전체보기에서 장면을 다시 만들지 않고 다중 선택 강조만 갱신한다."""
        self._current_selected = selected
        selected_rooms = list(selected_rooms or ([selected] if selected is not None else []))
        self._current_selected_room_handles = {
            str(room.boundary_handle or "") for room in selected_rooms if str(room.boundary_handle or "")
        }
        self._update_group_room_selection()
        for room in self._current_rooms:
            items = self._room_display_items.get(id(room))
            if not items:
                continue
            _polygon_item, text_item = items
            is_selected = self._room_is_selected(room)
            base_color = self._room_base_color(room)
            text_item.setDefaultTextColor(self.selection_color if is_selected else base_color)
        self.viewport().update()

    def update_room_label(self, room: RoomRecord) -> bool:
        """장면 전체를 다시 만들지 않고 지정 실의 라벨과 툴팁만 갱신한다."""
        items = self._room_display_items.get(id(room))
        if not items:
            return False
        polygon_item, text_item = items
        label = normalize_room_name(room.room_name)
        if room.room_number.strip():
            label = f"{room.room_number}\n{label}"
        text_item.setPlainText(label)
        polygon_item.setToolTip(f"{room.floor} / {room.room_number} / {normalize_room_name(room.room_name)}")
        cx, cy = polygon_label_point(room.points)
        text_rect = text_item.boundingRect()
        text_item.setPos(
            (cx - self._current_minx) - text_rect.width() / 2,
            (-(cy - self._current_miny)) - text_rect.height() / 2,
        )
        text_item.setToolTip(f"{room.floor} / {room.room_number} / {normalize_room_name(room.room_name)}")
        is_selected = self._room_is_selected(room)
        base_color = self._room_base_color(room)
        polygon_item.setPen(self._overlay_pen(
            self.selection_color if is_selected else base_color,
            self.selection_width if is_selected else self.boundary_width,
        ))
        text_item.setDefaultTextColor(self.selection_color if is_selected else base_color)
        self.viewport().update()
        return True

    def show_background_floor(self, floor: str):
        """실구분선이 없는 초기 단계에서 도곽/층 배경만 표시한다."""
        floor = str(floor or "").strip()
        self._current_background_floor = floor
        info = {}
        if self.background_render_mode == "vector":
            info = dict(self.floor_vector_cache.get(floor) or {})
        if not info:
            info = dict(self.floor_background_files.get(floor) or {})
        crop = info.get("crop_rect") or ()
        try:
            minx, miny, maxx, maxy = [float(v) for v in crop[:4]]
        except Exception:
            self._current_rooms = []
            self.scene().clear()
            self._fit_rect = None
            self._current_display_bounds = None
            return

        self._label_view_mode = "floor"
        self._current_rooms = []
        self._current_selected = None
        self._current_selected_room_handles = set()
        self._show_edge_lengths = False
        self._update_canvas_background(self.show_cad_background)
        scene = self.scene()
        scene.clear()
        self._room_polygons = []
        self._room_display_items = {}
        self._deduction_display_items = []
        self._ceiling_display_items = []
        self._edge_length_items = []
        self._wall_segments = []
        self._wall_segment_items = {}
        self._current_display_bounds = (minx, miny, maxx, maxy)
        self._current_minx = minx
        self._current_miny = miny
        self._current_display_width = max(maxx - minx, 1.0)

        if self.show_cad_background:
            self._add_floor_background(scene, [], minx, miny, maxx, maxy, floor_override=floor)

        span = max(maxx - minx, maxy - miny, 1.0)
        margin = span * 0.015 + 50.0
        content_rect = QRectF(0.0, -(maxy - miny), maxx - minx, maxy - miny).adjusted(-margin, -margin, margin, margin)
        content_rect = content_rect.united(scene.itemsBoundingRect().adjusted(-margin, -margin, margin, margin))
        pan_x = max(content_rect.width() * self.floor_horizontal_pan_margin_ratio, 800.0)
        pan_y = max(content_rect.height() * self.floor_vertical_pan_margin_ratio, 800.0)
        scene.setSceneRect(content_rect.adjusted(-pan_x, -pan_y, pan_x, pan_y))
        self._fit_rect = content_rect
        self.reset_view()

    def show_floor(self, rooms: list[RoomRecord], selected: RoomRecord | None = None, selected_rooms=None):
        self._label_view_mode = "floor"
        rooms = list(rooms)
        selected_rooms = [room for room in list(selected_rooms or []) if room in rooms]
        current_key = self._floor_view_key(self._current_rooms, self._show_edge_lengths)
        requested_key = self._floor_view_key(rooms, False)
        if (
            current_key is not None
            and current_key == requested_key
            and len(self._room_display_items) == len(rooms)
        ):
            self._update_floor_selection(selected, selected_rooms)
            return
        self._draw_rooms(rooms, selected, selected_rooms, show_edge_lengths=False, force_background=None)

    def keyPressEvent(self, event):
        if self.opening_edit_mode:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                if self.selected_opening_ids:
                    self.opening_delete_requested.emit(set(self.selected_opening_ids))
                event.accept()
                return
            if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and self.selected_opening_ids:
                moves = {
                    Qt.Key_Left: (-10.0, 0.0), Qt.Key_Right: (10.0, 0.0),
                    Qt.Key_Up: (0.0, 10.0), Qt.Key_Down: (0.0, -10.0),
                }
                dx, dy = moves[event.key()]
                self.opening_nudge_requested.emit(set(self.selected_opening_ids), dx, dy)
                event.accept()
                return
            if event.key() == Qt.Key_F2 and self.selected_opening_ids:
                self.opening_code_edit_requested.emit(set(self.selected_opening_ids))
                event.accept()
                return
            if event.key() == Qt.Key_F5:
                self.opening_catalog_requested.emit()
                event.accept()
                return
        if self.vector_group_edit_mode and event.key() == Qt.Key_Space:
            self.vector_group_edit_transition_requested.emit()
            event.accept()
            return
        if self.vector_group_edit_mode and event.key() == Qt.Key_Z and bool(event.modifiers() & Qt.ControlModifier):
            self.vector_group_edit_undo_requested.emit()
            event.accept()
            return
        if self.vector_direct_select_mode and event.key() == Qt.Key_Space:
            self.vector_direct_off_requested.emit()
            event.accept()
            return
        if self.vector_direct_select_mode and event.key() == Qt.Key_Z and bool(event.modifiers() & Qt.ControlModifier):
            self.vector_direct_undo_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if self.opening_edit_mode and event.mimeData().hasFormat("application/x-dxf-window-code"):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self.opening_edit_mode and event.mimeData().hasFormat("application/x-dxf-window-code"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self.opening_edit_mode and event.mimeData().hasFormat("application/x-dxf-window-code"):
            try:
                code = bytes(event.mimeData().data("application/x-dxf-window-code")).decode("utf-8").strip().upper()
            except Exception:
                code = ""
            if code:
                self.opening_catalog_drop_requested.emit(code)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._middle_panning = True
            self._middle_pan_pos = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._mouse_press_pos = event.position()
            if self.vector_group_edit_mode:
                self._selection_start_scene = self.mapToScene(event.position().toPoint())
                self._selection_rect_item = self.scene().addRect(
                    QRectF(self._selection_start_scene, self._selection_start_scene),
                    QPen(QColor("#00AEEF"), 1, Qt.DashLine), QBrush(QColor(0, 174, 239, 35))
                )
                self._selection_rect_item.setZValue(200)
                event.accept()
                return
            if self.vector_direct_select_mode:
                self._selection_start_scene = self.mapToScene(event.position().toPoint())
                self._selection_rect_item = self.scene().addRect(
                    QRectF(self._selection_start_scene, self._selection_start_scene),
                    QPen(QColor("#00AEEF"), 1, Qt.DashLine), QBrush(QColor(0, 174, 239, 35))
                )
                self._selection_rect_item.setZValue(200)
                event.accept()
                return
            if self.opening_edit_mode:
                scene_pos = self.mapToScene(event.position().toPoint())
                hit_id = self._opening_hit_id(scene_pos)
                if hit_id:
                    remove = bool(event.modifiers() & Qt.ControlModifier)
                    additive = bool(event.modifiers() & Qt.ShiftModifier) and not remove
                    if remove:
                        self.selected_opening_ids.discard(hit_id)
                    elif additive:
                        self.selected_opening_ids.add(hit_id)
                    elif hit_id not in self.selected_opening_ids:
                        self.selected_opening_ids = {hit_id}
                    self._update_opening_marker_selection()
                    self.opening_selection_changed.emit(set(self.selected_opening_ids))
                    if hit_id in self.selected_opening_ids and not remove:
                        self._opening_drag_start_scene = scene_pos
                        self._opening_drag_ids = set(self.selected_opening_ids)
                        self._opening_drag_item_positions = {
                            rid: QPointF(self._opening_marker_items[rid].pos())
                            for rid in self._opening_drag_ids if rid in self._opening_marker_items
                        }
                        self._opening_drag_aux_positions = {
                            rid: [QPointF(aux.pos()) for aux in self._opening_marker_aux_items.get(rid, [])]
                            for rid in self._opening_drag_ids
                        }
                        anchor_item = self._opening_marker_items.get(hit_id)
                        self._opening_drag_anchor_scene = anchor_item.sceneBoundingRect().center() if anchor_item is not None else scene_pos
                        self._opening_drag_last_delta = (0.0, 0.0)
                    event.accept()
                    return
                self._selection_start_scene = scene_pos
                self._selection_rect_item = self.scene().addRect(
                    QRectF(self._selection_start_scene, self._selection_start_scene),
                    QPen(QColor("#00AEEF"), 1, Qt.DashLine), QBrush(QColor(0, 174, 239, 35))
                )
                self._selection_rect_item.setZValue(200)
                event.accept()
                return
            if self.wall_finish_mode or self.room_group_mode:
                self._selection_start_scene = self.mapToScene(event.position().toPoint())
                self._selection_rect_item = self.scene().addRect(
                    QRectF(self._selection_start_scene, self._selection_start_scene),
                    QPen(QColor("#00AEEF"), 1, Qt.DashLine), QBrush(QColor(0, 174, 239, 35))
                )
                self._selection_rect_item.setZValue(200)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._middle_panning and self._middle_pan_pos is not None:
            delta = event.position() - self._middle_pan_pos
            self._middle_pan_pos = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(round(delta.x())))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(round(delta.y())))
            event.accept()
            return
        if self.boundary_split_mode:
            scene_pos = self.mapToScene(event.position().toPoint())
            candidate = self._boundary_split_candidate(scene_pos)
            if candidate is not None:
                _hit, room, index, snapped, _length = candidate
                self.show_boundary_split_hover(room, index, snapped)
            elif self._boundary_split_hover_key is not None:
                self.clear_boundary_split_highlight()
                self.boundary_split_hover_changed.emit({})
            event.accept()
            return
        if self.boundary_merge_mode:
            scene_pos = self.mapToScene(event.position().toPoint())
            candidate = self._boundary_merge_candidate(scene_pos)
            if candidate is not None:
                _hit, room, index, _len1, _len2 = candidate
                self.show_boundary_merge_hover(room, index)
            elif self._boundary_split_hover_key is not None:
                self.clear_boundary_split_highlight()
                self.boundary_merge_hover_changed.emit({})
            event.accept()
            return
        if self.opening_edit_mode and self._opening_drag_start_scene is not None and self._opening_drag_ids:
            current = self.mapToScene(event.position().toPoint())
            anchor = self._opening_drag_anchor_scene or self._opening_drag_start_scene
            proposed = anchor + (current - self._opening_drag_start_scene)
            snapped = self._opening_snap_scene_anchor(proposed, 10.0)
            dx = self._snap_10(snapped.x() - anchor.x())
            dy = self._snap_10(-(snapped.y() - anchor.y()))
            self._opening_drag_last_delta = (dx, dy)
            scene_delta = QPointF(dx, -dy)
            for rid, origin in self._opening_drag_item_positions.items():
                item = self._opening_marker_items.get(rid)
                if item is not None:
                    item.setPos(origin + scene_delta)
                origins = self._opening_drag_aux_positions.get(rid, [])
                for aux, aux_origin in zip(self._opening_marker_aux_items.get(rid, []), origins):
                    aux.setPos(aux_origin + scene_delta)
            self._update_opening_drag_feedback(dx, dy)
            self.opening_move_preview_changed.emit({
                "ids": set(self._opening_drag_ids), "dx": dx, "dy": dy, "distance": math.hypot(dx, dy)
            })
            event.accept()
            return
        if (self.vector_group_edit_mode or self.vector_direct_select_mode or self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode) and self._selection_rect_item is not None and self._selection_start_scene is not None:
            current = self.mapToScene(event.position().toPoint())
            self._selection_rect_item.setRect(QRectF(self._selection_start_scene, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._middle_panning:
            self._middle_panning = False
            self._middle_pan_pos = None
            self.viewport().unsetCursor()
            event.accept()
            return
        if event.button() != Qt.LeftButton or self._mouse_press_pos is None:
            super().mouseReleaseEvent(event)
            return
        press_pos = self._mouse_press_pos
        move_distance = (event.position() - press_pos).manhattanLength()
        self._mouse_press_pos = None

        if self.opening_edit_mode and self._opening_drag_start_scene is not None:
            dx, dy = self._opening_drag_last_delta
            ids = set(self._opening_drag_ids)
            item_origins = dict(self._opening_drag_item_positions)
            aux_origins = {rid: list(values) for rid, values in self._opening_drag_aux_positions.items()}
            self._opening_drag_start_scene = None
            self._opening_drag_anchor_scene = None
            self._opening_drag_ids.clear()
            self._opening_drag_item_positions = {}
            self._opening_drag_aux_positions = {}
            self._opening_drag_last_delta = (0.0, 0.0)
            self._clear_opening_drag_feedback()
            if move_distance > 3 and ids and (abs(dx) >= 0.1 or abs(dy) >= 0.1):
                self.opening_move_requested.emit(ids, float(dx), float(dy))
            else:
                # 클릭만 한 경우 벽 스냅 미리보기 때문에 임시로 움직인 그래픽을 원위치한다.
                for rid, origin in item_origins.items():
                    item = self._opening_marker_items.get(rid)
                    if item is not None:
                        item.setPos(origin)
                    for aux, aux_origin in zip(self._opening_marker_aux_items.get(rid, []), aux_origins.get(rid, [])):
                        aux.setPos(aux_origin)
                self.opening_move_preview_changed.emit({})
            event.accept()
            return

        if (self.vector_group_edit_mode or self.vector_direct_select_mode or self.wall_finish_mode or self.room_group_mode or self.opening_edit_mode) and self._selection_rect_item is not None and self._selection_start_scene is not None:
            end_scene = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._selection_start_scene, end_scene).normalized()
            self.scene().removeItem(self._selection_rect_item)
            self._selection_rect_item = None
            start_scene = self._selection_start_scene
            self._selection_start_scene = None
            if move_distance > 6:
                left_to_right = end_scene.x() >= start_scene.x()
                remove = bool(event.modifiers() & Qt.ControlModifier)
                additive = bool(event.modifiers() & Qt.ShiftModifier) and not remove
                if self.vector_group_edit_mode:
                    hits = self._vector_group_edit_ids_in_rect(rect, left_to_right)
                    if remove:
                        self.selected_vector_group_edit_ids.difference_update(hits)
                    else:
                        self.selected_vector_group_edit_ids.update(hits)
                    self._refresh_vector_group_edit_highlights()
                    self.vector_group_edit_selection_changed.emit(set(self.selected_vector_group_edit_ids))
                    event.accept()
                    return
                if self.vector_direct_select_mode:
                    hits = self._vector_keys_in_rect(rect, left_to_right)
                    if remove:
                        self.selected_vector_filter_keys.difference_update(hits)
                    else:
                        # 직접선택 모드는 기본 클릭/드래그가 계속 누적된다.
                        self.selected_vector_filter_keys.update(hits)
                    self._refresh_vector_direct_highlights()
                    self.vector_direct_selection_changed.emit(set(self.selected_vector_filter_keys))
                    event.accept()
                    return
                if self.opening_edit_mode:
                    hits = {read_id for read_id, item in self._opening_marker_items.items() if rect.intersects(item.sceneBoundingRect())}
                    if remove:
                        self.selected_opening_ids.difference_update(hits)
                    elif additive:
                        self.selected_opening_ids.update(hits)
                    else:
                        self.selected_opening_ids = set(hits)
                    self._update_opening_marker_selection()
                    self.opening_selection_changed.emit(set(self.selected_opening_ids))
                    event.accept()
                    return
                if self.room_group_mode:
                    handles = set()
                    for room, polygon in self._room_polygons:
                        bounds = polygon.boundingRect()
                        selected = rect.contains(bounds) if left_to_right else rect.intersects(bounds)
                        if selected:
                            handles.add(str(room.boundary_handle or ""))
                    self.group_rooms_box_selected.emit(handles, additive, remove)
                    event.accept()
                    return
                keys = set()
                for key, p1, p2 in self._wall_segments:
                    if left_to_right:
                        selected = rect.contains(p1) and rect.contains(p2)
                    else:
                        line = QLineF(p1, p2)
                        selected = rect.contains(p1) or rect.contains(p2)
                        if not selected:
                            edges = [
                                QLineF(rect.topLeft(), rect.topRight()),
                                QLineF(rect.topRight(), rect.bottomRight()),
                                QLineF(rect.bottomRight(), rect.bottomLeft()),
                                QLineF(rect.bottomLeft(), rect.topLeft()),
                            ]
                            selected = any(line.intersects(edge)[0] == QLineF.BoundedIntersection for edge in edges)
                    if selected:
                        keys.add(key)
                self.wall_segments_box_selected.emit(keys, additive, remove)
                event.accept()
                return

        super().mouseReleaseEvent(event)
        if move_distance > 6:
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        if self.vector_group_edit_mode:
            hits = self._vector_group_edit_ids_at_point(scene_pos)
            remove = bool(event.modifiers() & Qt.ControlModifier)
            if remove:
                self.selected_vector_group_edit_ids.difference_update(hits)
            else:
                self.selected_vector_group_edit_ids.update(hits)
            self._refresh_vector_group_edit_highlights()
            self.vector_group_edit_selection_changed.emit(set(self.selected_vector_group_edit_ids))
            event.accept()
            return
        if self.vector_direct_select_mode:
            hits = self._vector_keys_at_point(scene_pos)
            remove = bool(event.modifiers() & Qt.ControlModifier)
            if remove:
                self.selected_vector_filter_keys.difference_update(hits)
            else:
                self.selected_vector_filter_keys.update(hits)
            self._refresh_vector_direct_highlights()
            self.vector_direct_selection_changed.emit(set(self.selected_vector_filter_keys))
            event.accept()
            return
        if self.boundary_split_mode:
            candidate = self._boundary_split_candidate(scene_pos)
            if candidate is not None:
                _hit, room, index, snapped, _length = candidate
                self.boundary_segment_split_requested.emit(room, int(index), float(snapped))
            event.accept()
            return
        if self.boundary_merge_mode:
            candidate = self._boundary_merge_candidate(scene_pos)
            if candidate is not None:
                _hit, room, index, _len1, _len2 = candidate
                self.boundary_vertex_merge_requested.emit(room, int(index))
            event.accept()
            return
        if self.opening_edit_mode:
            hit_id = self._opening_hit_id(scene_pos)
            remove = bool(event.modifiers() & Qt.ControlModifier)
            additive = bool(event.modifiers() & Qt.ShiftModifier)
            if hit_id:
                if remove:
                    self.selected_opening_ids.discard(hit_id)
                elif additive:
                    self.selected_opening_ids.add(hit_id)
                else:
                    self.selected_opening_ids = {hit_id}
            elif not (remove or additive):
                self.selected_opening_ids.clear()
            self._update_opening_marker_selection()
            self.opening_selection_changed.emit(set(self.selected_opening_ids))
            event.accept()
            return
        if self.wall_finish_mode and self._wall_segments:
            best_key = None
            best_distance = float("inf")
            for key, p1, p2 in self._wall_segments:
                vx = p2.x() - p1.x(); vy = p2.y() - p1.y()
                wx = scene_pos.x() - p1.x(); wy = scene_pos.y() - p1.y()
                length2 = vx * vx + vy * vy
                if length2 <= 1e-12:
                    continue
                t = max(0.0, min(1.0, (wx * vx + wy * vy) / length2))
                px = p1.x() + t * vx; py = p1.y() + t * vy
                distance = math.hypot(scene_pos.x() - px, scene_pos.y() - py)
                if distance < best_distance:
                    best_distance = distance; best_key = key
            tolerance = 12.0 / max(abs(self.transform().m11()), 1e-6)
            if best_key is not None and best_distance <= tolerance:
                additive = bool(event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
                self.wall_segment_clicked.emit(best_key, additive)
                return

        matches = [(room.area_raw, room) for room, polygon in self._room_polygons
                   if polygon.containsPoint(scene_pos, Qt.OddEvenFill)]
        if matches:
            matches.sort(key=lambda value: value[0])
            if self.room_group_mode:
                self.group_room_clicked.emit(str(matches[0][1].boundary_handle or ""))
                return
            self.room_clicked.emit(matches[0][1])
            return

        # 일반 실 선택 상태에서 실 폴리곤 밖의 빈 공간을 클릭하면
        # 현재 층의 '전체' 행으로 돌아갈 수 있도록 상위 위젯에 알린다.
        if not self.wall_finish_mode and not self.room_group_mode:
            self.empty_clicked.emit()

    def contextMenuEvent(self, event):
        """도곽/실 미리보기 우클릭 시 현재 실의 No/실명 편집을 요청한다.

        우클릭 지점이 다른 실 안이면 그 실을 우선 선택 대상으로 삼고, 빈 공간이면
        현재 선택 실을 그대로 사용한다. 벽체 마감 선택 모드에서는 기존 작업을 방해하지 않는다.
        """
        if self.vector_direct_select_mode or self.wall_finish_mode or self.opening_edit_mode or self.boundary_split_mode or self.boundary_merge_mode:
            super().contextMenuEvent(event)
            return
        scene_pos = self.mapToScene(event.pos())
        matches = [(room.area_raw, room) for room, polygon in self._room_polygons
                   if polygon.containsPoint(scene_pos, Qt.OddEvenFill)]
        room = None
        if matches:
            matches.sort(key=lambda value: value[0])
            room = matches[0][1]
            if room is not self._current_selected:
                self.room_clicked.emit(room)
        elif self._current_selected is not None:
            room = self._current_selected
        if room is not None:
            self.room_edit_requested.emit(room)
            event.accept()
            return
        super().contextMenuEvent(event)

    def reset_view(self):
        if self._fit_rect is not None and not self._fit_rect.isNull():
            self.resetTransform()
            self.fitInView(self._fit_rect, Qt.KeepAspectRatio)
            self._fit_scale = max(abs(self.transform().m11()), 1e-12)

    def wheelEvent(self, event):
        if not self.scene() or not self.scene().items():
            event.ignore()
            return

        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        if delta == 0:
            event.ignore()
            return

        factor = 1.20 if delta > 0 else 1 / 1.20
        current_scale = max(abs(self.transform().m11()), 1e-12)
        base_scale = max(self._fit_scale, 1e-12)
        next_scale = current_scale * factor

        # 도면 좌표가 큰 경우 전체보기 배율이 0.02보다 훨씬 작을 수 있다.
        # 절대 배율 제한 대신 전체보기 배율을 기준으로 확대·축소 범위를 정한다.
        min_scale = base_scale * 0.02
        max_scale = base_scale * 5000.0
        if min_scale <= next_scale <= max_scale:
            # V23.59: 구획벽 나누기/합치기 등 편집모드에서도 마우스가 가리키는
            # 도면 좌표가 확대 전후 같은 화면 위치에 남도록 직접 보정한다.
            viewport_pos = event.position().toPoint()
            scene_before = self.mapToScene(viewport_pos)
            old_anchor = self.transformationAnchor()
            self.setTransformationAnchor(QGraphicsView.NoAnchor)
            self.scale(factor, factor)
            scene_after = self.mapToScene(viewport_pos)
            current_center = self.mapToScene(self.viewport().rect().center())
            self.centerOn(current_center + (scene_before - scene_after))
            self.setTransformationAnchor(old_anchor)
            # 도곽 오버레이 선 전체가 공통 줌 배율을 사용하므로 즉시 갱신한다.
            self._update_all_overlay_pens_in_place()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        # V23.66: 좌클릭 더블클릭으로 전체보기/축소되는 동작은 완전히 제거한다.
        # 사용자가 유지 요청한 가운데(휠) 버튼 더블클릭만 전체보기로 동작한다.
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        if event.button() == Qt.MiddleButton:
            self.reset_view()
            self._update_all_overlay_pens_in_place()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
