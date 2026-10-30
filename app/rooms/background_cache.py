from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from app.rooms.background_style import parse_width_map, width_for_aci
from app.rooms.background_source_cache import floor_frame_rects

from app.common.models import RoomRecord

CACHE_VERSION = 20  # V23.82: 대형 PNG를 타일 캐시로 분할하여 10,000~20,000px 표시 안정화
RASTER_TILE_SIZE = 4096
RASTER_TILE_THRESHOLD = 6000


def clip_line_to_rect(x1, y1, x2, y2, xmin, ymin, xmax, ymax):
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


def _rooms_bounds(rooms: list[RoomRecord]):
    all_points = [point for room in rooms for point in (getattr(room, "points", None) or [])]
    if not all_points:
        return None
    return (
        min(x for x, _ in all_points),
        min(y for _, y in all_points),
        max(x for x, _ in all_points),
        max(y for _, y in all_points),
    )


def _bbox_of_points(points):
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_overlap_area(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0:
        return 0.0
    return float(w * h)


def _bbox_center(box):
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _floor_frame(divisions, floor: str, room_bounds=None, applicable_floors=None):
    """해당 층의 평면도 도곽을 우선 선택한다.

    V19.0: 배경 PNG는 실 bbox가 아니라 평면도 도곽 전체를 기준으로 만든다.
    층명이 일치하는 도곽이 있으면 실 bbox와의 겹침이 작아도 버리지 않는다.
    동일 층 후보가 여러 개일 때만 실 위치를 이용해 가장 가까운 도곽을 고른다.
    """
    requested = {str(floor or '').strip()}
    for value in applicable_floors or []:
        value = str(value or '').strip()
        if value:
            requested.add(value)

    candidates = []
    for frame in divisions or []:
        if str(frame.get("drawing_type", "")).strip() != "평면도":
            continue
        points = frame.get("points") or []
        if len(points) < 3:
            continue
        floors = [str(value).strip() for value in (frame.get("floors") or [])]
        single_floor = str(frame.get("floor", "")).strip()
        if single_floor and single_floor not in floors:
            floors.append(single_floor)
        frame_box = _bbox_of_points(points)
        frame_area = float(frame.get("area", 0) or 0.0)
        floor_match = bool(requested.intersection(floors))
        candidates.append((frame_box, frame_area, floor_match, frame))

    if not candidates:
        return None

    matched = [item for item in candidates if item[2]]
    pool = matched or candidates

    if not room_bounds:
        return min(pool, key=lambda item: item[1] if item[1] > 0 else float('inf'))[3]

    room_center = _bbox_center(room_bounds)
    room_area = max((room_bounds[2] - room_bounds[0]) * (room_bounds[3] - room_bounds[1]), 1.0)

    def score(item):
        frame_box, frame_area, _floor_match, _frame = item
        overlap = _bbox_overlap_area(frame_box, room_bounds)
        overlap_ratio = overlap / room_area
        contains = 1 if (
            frame_box[0] <= room_center[0] <= frame_box[2]
            and frame_box[1] <= room_center[1] <= frame_box[3]
        ) else 0
        frame_center = _bbox_center(frame_box)
        distance = math.hypot(frame_center[0] - room_center[0], frame_center[1] - room_center[1])
        return (-overlap_ratio, -contains, distance, frame_area)

    # 층명 일치 후보가 있으면 무조건 그 후보군에서 선택한다.
    return min(pool, key=score)[3]


def _floor_crop_info(rooms: list[RoomRecord], divisions=None, floor_override: str = ""):
    floor = str(floor_override or (rooms[0].floor if rooms else "") or "").strip()
    room_bounds = _rooms_bounds(rooms)
    if room_bounds:
        minx, miny, maxx, maxy = room_bounds
    else:
        minx = miny = 0.0
        maxx = maxy = 1.0
    applicable = []
    for room in rooms:
        for value in getattr(room, 'applicable_floors', []) or []:
            value = str(value or '').strip()
            if value and value not in applicable:
                applicable.append(value)
    frame = _floor_frame(divisions, floor, room_bounds, applicable)
    if frame:
        points = [(float(x), float(y)) for x, y in frame.get("points", [])]
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        # V18.3: 도곽의 외접 사각형까지만 crop하고 QPainter polygon clip은 하지 않는다.
        # 닫힘 방향/자기교차/비정상 vertex 때문에 배경 전체가 투명해지는 회귀를 차단한다.
        return (min(xs), min(ys), max(xs), max(ys)), None, "drawing_frame"

    width = max(maxx - minx, 1.0)
    height = max(maxy - miny, 1.0)
    padding = max(width, height) * 0.15
    return (minx - padding, miny - padding, maxx + padding, maxy + padding), None, "rooms"


def _safe_filename(name: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in name.strip())
    return safe or 'floor'


def cache_dir_for_source(cache_root: Path, source_path: str) -> Path:
    resolved = str(Path(source_path).resolve())
    digest = hashlib.sha1(resolved.encode('utf-8')).hexdigest()[:12]
    return cache_root / f"{Path(source_path).stem}_{digest}"


def source_signature(source_path: str):
    stat = Path(source_path).stat()
    return {
        'source_size': int(stat.st_size),
        'source_mtime_ns': int(stat.st_mtime_ns),
    }


def build_background_cache(source_path: str, rooms: list[RoomRecord], segments, cache_root: Path, background_max_pixels: int, background_style: str, background_filter_signature: str = "", divisions=None, background_line_width: float = 1.5, background_aci_widths: str = "", background_keep_red: bool = False, background_texts=None, preserve_existing: bool = False, color_diagnostic=None, background_annotations=None):
    background_style = 'cad' if background_style == 'cad' else 'gray'
    background_max_pixels = max(400, min(5000, int(background_max_pixels)))
    background_line_width = max(0.0, min(20.0, float(background_line_width)))
    width_map = parse_width_map(background_aci_widths)
    background_keep_red = bool(background_keep_red)
    cache_dir = cache_dir_for_source(cache_root, source_path)
    old_meta = None
    if preserve_existing and cache_dir.exists():
        try:
            old_meta_path = cache_dir / 'cache_info.json'
            if old_meta_path.exists():
                old_meta = json.loads(old_meta_path.read_text(encoding='utf-8'))
        except Exception:
            old_meta = None
        # 캐시 포맷이 바뀐 버전에서 현재층만 재생성하면 다른 층의 구버전 PNG가
        # 새 meta에 섞여 들어갈 수 있으므로, 버전 불일치 시 부분보존을 해제한다.
        if old_meta is not None and int(old_meta.get('version', 0)) != CACHE_VERSION:
            old_meta = None
            preserve_existing = False
    if not preserve_existing and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    floors: dict[str, list[RoomRecord]] = {}
    for room in rooms:
        floor_name = str(room.floor or "").strip()
        if floor_name:
            floors.setdefault(floor_name, []).append(room)

    # V23.93: 실구분선이 아직 하나도 없어도 평면도 도곽에 지정된 층을 기준으로
    # 배경 캐시를 생성한다. 도곽/층 지정이 배경 생성의 기준이며 실 데이터는 필수가 아니다.
    for floor_name in floor_frame_rects(divisions, rooms).keys():
        floor_name = str(floor_name or "").strip()
        if floor_name:
            floors.setdefault(floor_name, [])

    meta = {
        'version': CACHE_VERSION,
        'source_path': str(Path(source_path).resolve()),
        'background_max_pixels': background_max_pixels,
        'background_style': background_style,
        'background_line_width': background_line_width,
        'background_aci_widths': str(background_aci_widths),
        'background_keep_red': background_keep_red,
        'background_filter_signature': str(background_filter_signature),
        'floors': dict((old_meta or {}).get('floors', {})) if preserve_existing else {},
        **source_signature(source_path),
    }

    for floor, floor_rooms in floors.items():
        crop_rect, clip_polygon, crop_source = _floor_crop_info(floor_rooms, divisions, floor)
        xmin, ymin, xmax, ymax = crop_rect
        world_width = max(xmax - xmin, 1.0)
        world_height = max(ymax - ymin, 1.0)
        longest = max(world_width, world_height)
        pixel_width = max(1, round(background_max_pixels * world_width / longest))
        pixel_height = max(1, round(background_max_pixels * world_height / longest))

        def _tile_clip_path(px0: int, py0: int):
            if not clip_polygon:
                return None
            path = QPainterPath()
            polygon = QPolygonF([
                QPointF((x - xmin) * sx - px0, (ymax - y) * sy - py0)
                for x, y in clip_polygon
            ])
            path.addPolygon(polygon)
            path.closeSubpath()
            return path

        def _tile_world_rect(px0: int, py0: int, pw: int, ph: int):
            return (
                xmin + px0 / sx,
                ymax - (py0 + ph) / sy,
                xmin + (px0 + pw) / sx,
                ymax - py0 / sy,
            )

        def _save_checked(image: QImage, path: Path, label: str):
            if image.isNull():
                raise MemoryError(f"{label} 이미지 메모리 할당 실패: {image.width()}x{image.height()}")
            ok = image.save(str(path), 'PNG')
            if not ok or not path.exists() or path.stat().st_size <= 0:
                raise RuntimeError(f"{label} PNG 저장 실패: {path}")

        def _render_base_tile(px0: int, py0: int, pw: int, ph: int):
            image = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
            if image.isNull():
                raise MemoryError(f"CAD 배경 타일 메모리 할당 실패: {pw}x{ph}")
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            tile_clip = _tile_clip_path(px0, py0)
            if tile_clip is not None:
                painter.setClipPath(tile_clip)
            wxmin, wymin, wxmax, wymax = _tile_world_rect(px0, py0, pw, ph)
            for segment in segments or []:
                x1, y1, x2, y2 = segment[:4]
                if max(x1, x2) < wxmin or min(x1, x2) > wxmax or max(y1, y2) < wymin or min(y1, y2) > wymax:
                    continue
                clipped = clip_line_to_rect(x1, y1, x2, y2, wxmin, wymin, wxmax, wymax)
                if clipped is None:
                    continue
                rgb = segment[4:7] if len(segment) >= 7 else (170, 170, 170)
                aci = segment[7] if len(segment) >= 8 and int(segment[7]) >= 0 else None
                if background_style == 'cad' or (background_style == 'gray' and background_keep_red and aci == 1):
                    color = QColor(*rgb)
                else:
                    color = QColor(150, 150, 150)
                line_width = width_for_aci(aci, width_map, background_line_width)
                pen = QPen(color); pen.setWidthF(line_width); painter.setPen(pen)
                trace_handle = str(segment[8] if len(segment) >= 9 else "").strip().upper()
                if color_diagnostic is not None:
                    try:
                        color_diagnostic.log(
                            "PNG_RENDER", floor=floor, handle=trace_handle,
                            input_rgb=tuple(int(v) for v in rgb[:3]), input_aci=aci,
                            style=background_style,
                            final_rgb=(color.red(), color.green(), color.blue()),
                            line_width=line_width,
                        )
                    except Exception:
                        pass
                cx1, cy1, cx2, cy2 = clipped
                painter.drawLine(
                    QPointF((cx1 - xmin) * sx - px0, (ymax - cy1) * sy - py0),
                    QPointF((cx2 - xmin) * sx - px0, (ymax - cy2) * sy - py0),
                )
            painter.end()
            return image

        def _render_text_tile(px0: int, py0: int, pw: int, ph: int):
            image = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
            if image.isNull():
                raise MemoryError(f"CAD 문자 타일 메모리 할당 실패: {pw}x{ph}")
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            tile_clip = _tile_clip_path(px0, py0)
            if tile_clip is not None:
                painter.setClipPath(tile_clip)
            wxmin, wymin, wxmax, wymax = _tile_world_rect(px0, py0, pw, ph)

            # CAD DIMENSION 그래픽도 원본문자 오버레이와 같은 타일에 저장한다.
            for segment in background_annotations or []:
                try:
                    x1, y1, x2, y2 = segment[:4]
                    if max(x1, x2) < wxmin or min(x1, x2) > wxmax or max(y1, y2) < wymin or min(y1, y2) > wymax:
                        continue
                    clipped = clip_line_to_rect(x1, y1, x2, y2, wxmin, wymin, wxmax, wymax)
                    if clipped is None:
                        continue
                    rgb = segment[4:7] if len(segment) >= 7 else (170, 170, 170)
                    aci = segment[7] if len(segment) >= 8 and int(segment[7]) >= 0 else None
                    if background_style == 'cad' or (background_style == 'gray' and background_keep_red and aci == 1):
                        color = QColor(*rgb)
                    else:
                        color = QColor(130, 130, 130)
                    line_width = width_for_aci(aci, width_map, background_line_width)
                    pen = QPen(color); pen.setWidthF(line_width); painter.setPen(pen)
                    cx1, cy1, cx2, cy2 = clipped
                    painter.drawLine(
                        QPointF((cx1 - xmin) * sx - px0, (ymax - cy1) * sy - py0),
                        QPointF((cx2 - xmin) * sx - px0, (ymax - cy2) * sy - py0),
                    )
                except Exception:
                    pass

            for text_item in background_texts or []:
                try:
                    x = float(text_item.get('x', 0.0)); y = float(text_item.get('y', 0.0))
                    text = str(text_item.get('text', '') or '').strip()
                    if not text:
                        continue
                    height_world = max(10.0, float(text_item.get('height', 200.0) or 200.0))
                    lines = text.replace('\\P', '\n').splitlines() or [text]
                    max_chars = max((len(line) for line in lines), default=1)
                    # 회전문자까지 고려해 앵커 주변을 넉넉히 잡고, 해당 타일과 멀리 떨어진 문자는 생략한다.
                    margin = max(height_world * 3.0, height_world * max_chars * 1.1)
                    if x + margin < wxmin or x - margin > wxmax or y + margin < wymin or y - margin > wymax:
                        continue
                    rgb = text_item.get('rgb') or [150, 150, 150]
                    aci = int(text_item.get('aci', -1))
                    if background_style == 'cad' or (background_style == 'gray' and background_keep_red and aci == 1):
                        color = QColor(*rgb[:3])
                    else:
                        color = QColor(130, 130, 130)
                    if color_diagnostic is not None:
                        try:
                            color_diagnostic.log(
                                "PNG_TEXT", floor=floor,
                                handle=str(text_item.get('handle', '') or '').strip().upper(),
                                input_rgb=tuple(int(v) for v in rgb[:3]), input_aci=aci,
                                style=background_style,
                                final_rgb=(color.red(), color.green(), color.blue()), text=text[:80],
                            )
                        except Exception:
                            pass
                    painter.setPen(QPen(color))
                    font = QFont()
                    font.setPixelSize(max(1, min(160, int(round(height_world * sy)))))
                    painter.setFont(font)
                    pxx = (x - xmin) * sx - px0; pyy = (ymax - y) * sy - py0
                    painter.save()
                    painter.translate(pxx, pyy)
                    painter.rotate(-float(text_item.get('rotation', 0.0) or 0.0))
                    for line_no, line in enumerate(lines):
                        painter.drawText(QPointF(0, line_no * font.pixelSize() * 1.15), line)
                    painter.restore()
                except Exception:
                    continue
            painter.end()
            return image

        floor_meta = {
            'crop_rect': [float(v) for v in crop_rect],
            'crop_source': crop_source,
            'clip_polygon': [[float(x), float(y)] for x, y in clip_polygon] if clip_polygon else [],
            'pixel_size': [int(pixel_width), int(pixel_height)],
        }

        # 큰 PNG/QPixmap 한 장은 Windows/Qt에서 생성은 성공해도 표시 시 null/빈 화면이 될 수 있다.
        # 6,000px 초과부터 4,096px 이하 타일로 저장해 10,000~20,000px 설정도 안정적으로 표시한다.
        tiled = max(pixel_width, pixel_height) > RASTER_TILE_THRESHOLD
        if tiled:
            tile_entries = []
            text_tile_entries = []
            safe_floor = _safe_filename(floor)
            for py0 in range(0, pixel_height, RASTER_TILE_SIZE):
                ph = min(RASTER_TILE_SIZE, pixel_height - py0)
                for px0 in range(0, pixel_width, RASTER_TILE_SIZE):
                    pw = min(RASTER_TILE_SIZE, pixel_width - px0)
                    row = py0 // RASTER_TILE_SIZE
                    col = px0 // RASTER_TILE_SIZE
                    world_rect = _tile_world_rect(px0, py0, pw, ph)

                    file_name = f"{safe_floor}__r{row}_c{col}.png"
                    image_path = cache_dir / file_name
                    image = _render_base_tile(px0, py0, pw, ph)
                    _save_checked(image, image_path, "CAD 배경 타일")
                    del image
                    tile_entries.append({
                        'image': file_name,
                        'pixel_rect': [int(px0), int(py0), int(pw), int(ph)],
                        'world_rect': [float(v) for v in world_rect],
                    })

                    text_file_name = f"{safe_floor}__text_r{row}_c{col}.png"
                    text_image_path = cache_dir / text_file_name
                    text_image = _render_text_tile(px0, py0, pw, ph)
                    _save_checked(text_image, text_image_path, "CAD 문자 타일")
                    del text_image
                    text_tile_entries.append({
                        'image': text_file_name,
                        'pixel_rect': [int(px0), int(py0), int(pw), int(ph)],
                        'world_rect': [float(v) for v in world_rect],
                    })
            floor_meta.update({
                'image': '', 'text_image': '',
                'tiles': tile_entries, 'text_tiles': text_tile_entries,
                'tiled': True,
            })
        else:
            file_name = f"{_safe_filename(floor)}.png"
            image_path = cache_dir / file_name
            image = _render_base_tile(0, 0, pixel_width, pixel_height)
            _save_checked(image, image_path, "CAD 배경")
            del image

            text_file_name = f"{_safe_filename(floor)}_text.png"
            text_image_path = cache_dir / text_file_name
            text_image = _render_text_tile(0, 0, pixel_width, pixel_height)
            _save_checked(text_image, text_image_path, "CAD 문자")
            del text_image
            floor_meta.update({
                'image': file_name, 'text_image': text_file_name,
                'tiles': [], 'text_tiles': [], 'tiled': False,
            })

        meta['floors'][floor] = floor_meta

    meta_path = cache_dir / 'cache_info.json'
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return cache_dir


def load_background_cache(source_path: str, cache_root: Path, background_max_pixels: int, background_style: str, background_filter_signature: str = "", background_line_width: float = 1.5, background_aci_widths: str = "", background_keep_red: bool = False):
    background_style = 'cad' if background_style == 'cad' else 'gray'
    background_line_width = max(0.0, min(20.0, float(background_line_width)))
    cache_dir = cache_dir_for_source(cache_root, source_path)
    meta_path = cache_dir / 'cache_info.json'
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if int(meta.get('version', 0)) != CACHE_VERSION:
            return None
        if int(meta.get('background_max_pixels', 0)) != int(background_max_pixels):
            return None
        if str(meta.get('background_style', 'gray')) != background_style:
            return None
        if abs(float(meta.get('background_line_width', 1.0)) - background_line_width) > 1e-9:
            return None
        if str(meta.get('background_aci_widths', '')) != str(background_aci_widths):
            return None
        if bool(meta.get('background_keep_red', False)) != bool(background_keep_red):
            return None
        if str(meta.get('background_filter_signature', '')) != str(background_filter_signature):
            return None
        # V18.7: 한 번 생성한 배경 PNG는 명시적으로 'CAD 배경 재생성'을 누르기 전까지 계속 사용한다.
        # 원본 파일의 mtime/size 변화만으로 자동 폐기하지 않는다.
        floors = meta.get('floors', {})
        result = {}
        for floor, info in floors.items():
            crop = tuple(float(v) for v in info['crop_rect'])
            tiles = []
            for tile in info.get('tiles', []) or []:
                tile_path = cache_dir / str(tile.get('image', ''))
                if not tile_path.exists() or tile_path.stat().st_size <= 0:
                    return None
                tiles.append({
                    'path': str(tile_path),
                    'pixel_rect': list(tile.get('pixel_rect', [])),
                    'world_rect': [float(v) for v in tile.get('world_rect', [])],
                })
            text_tiles = []
            for tile in info.get('text_tiles', []) or []:
                tile_path = cache_dir / str(tile.get('image', ''))
                if not tile_path.exists() or tile_path.stat().st_size <= 0:
                    return None
                text_tiles.append({
                    'path': str(tile_path),
                    'pixel_rect': list(tile.get('pixel_rect', [])),
                    'world_rect': [float(v) for v in tile.get('world_rect', [])],
                })

            image_path = None
            text_image_path = None
            if not tiles:
                image_name = str(info.get('image', '') or '')
                if not image_name:
                    return None
                image_path = cache_dir / image_name
                if not image_path.exists() or image_path.stat().st_size <= 0:
                    return None
                if info.get('text_image'):
                    text_image_path = cache_dir / str(info.get('text_image'))
                    if not text_image_path.exists() or text_image_path.stat().st_size <= 0:
                        text_image_path = None

            result[floor] = {
                'path': str(image_path) if image_path else '',
                'text_path': str(text_image_path) if text_image_path else '',
                'tiles': tiles,
                'text_tiles': text_tiles,
                'tiled': bool(tiles),
                'pixel_size': list(info.get('pixel_size', [])),
                'crop_rect': crop,
                'crop_source': str(info.get('crop_source', 'rooms')),
                'clip_polygon': info.get('clip_polygon', []),
            }
        return result
    except Exception:
        return None