from __future__ import annotations

import heapq
import math
from dataclasses import dataclass


def polygon_centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Return the area centroid of a polygon (fallback to bbox center)."""
    pts = list(points or [])
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
    for index, (x0, y0) in enumerate(pts):
        x1, y1 = pts[(index + 1) % len(pts)]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    if abs(area2) < 1e-9:
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    return cx / (3.0 * area2), cy / (3.0 * area2)


def point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    """Even-odd point-in-polygon test. Boundary-adjacent points are treated as inside."""
    pts = list(points or [])
    if len(pts) < 3:
        return False
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        # Ray crossing test.
        if ((yi > y) != (yj > y)):
            denom = (yj - yi)
            if abs(denom) > 1e-15:
                x_cross = (xj - xi) * (y - yi) / denom + xi
                if x <= x_cross:
                    inside = not inside
        j = i
    return inside


def _segment_distance_sq(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx = bx - ax
    dy = by - ay
    if dx != 0.0 or dy != 0.0:
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        if t > 1.0:
            ax, ay = bx, by
        elif t > 0.0:
            ax += dx * t
            ay += dy * t
    dx = px - ax
    dy = py - ay
    return dx * dx + dy * dy


def _point_to_polygon_signed_distance(x: float, y: float, points: list[tuple[float, float]]) -> float:
    inside = False
    min_dist_sq = float("inf")
    j = len(points) - 1
    for i in range(len(points)):
        ax, ay = points[i]
        bx, by = points[j]
        if ((ay > y) != (by > y)) and (x < (bx - ax) * (y - ay) / ((by - ay) or 1e-30) + ax):
            inside = not inside
        min_dist_sq = min(min_dist_sq, _segment_distance_sq(x, y, ax, ay, bx, by))
        j = i
    distance = math.sqrt(min_dist_sq) if math.isfinite(min_dist_sq) else 0.0
    return distance if inside else -distance


@dataclass(order=True)
class _QueueCell:
    # heapq is a min-heap, so use negative potential first.
    priority: float
    x: float
    y: float
    h: float
    d: float
    max_d: float


def _make_cell(x: float, y: float, h: float, points: list[tuple[float, float]]) -> _QueueCell:
    d = _point_to_polygon_signed_distance(x, y, points)
    max_d = d + h * math.sqrt(2.0)
    return _QueueCell(-max_d, x, y, h, d, max_d)


def polygon_label_point(
    points: list[tuple[float, float]],
    precision: float | None = None,
) -> tuple[float, float]:
    """Find a visually useful interior label point (Mapbox polylabel-style).

    Unlike an area centroid, the returned point is inside the polygon and tends to
    sit in the roomiest part of concave/L/U-shaped rooms. Coordinates are returned
    in the same unit as ``points`` (the project normally uses mm).
    """
    pts = list(points or [])
    if not pts:
        return 0.0, 0.0
    if len(pts) < 3:
        return polygon_centroid(pts)

    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x
    height = max_y - min_y
    if width <= 1e-9 or height <= 1e-9:
        return polygon_centroid(pts)

    # 1 mm minimum accuracy; for very large plans stop at about 0.05% of span.
    if precision is None:
        precision = max(1.0, max(width, height) * 0.0005)
    precision = max(0.1, float(precision))

    cell_size = min(width, height)
    if cell_size <= 1e-9:
        return polygon_centroid(pts)
    h = cell_size / 2.0

    queue: list[_QueueCell] = []
    x = min_x
    while x < max_x:
        y = min_y
        while y < max_y:
            heapq.heappush(queue, _make_cell(x + h, y + h, h, pts))
            y += cell_size
        x += cell_size

    cx, cy = polygon_centroid(pts)
    best = _make_cell(cx, cy, 0.0, pts)
    bbox = _make_cell(min_x + width / 2.0, min_y + height / 2.0, 0.0, pts)
    if bbox.d > best.d:
        best = bbox

    while queue:
        cell = heapq.heappop(queue)
        if cell.d > best.d:
            best = cell
        if cell.max_d - best.d <= precision:
            continue
        nh = cell.h / 2.0
        if nh <= 1e-9:
            continue
        heapq.heappush(queue, _make_cell(cell.x - nh, cell.y - nh, nh, pts))
        heapq.heappush(queue, _make_cell(cell.x + nh, cell.y - nh, nh, pts))
        heapq.heappush(queue, _make_cell(cell.x - nh, cell.y + nh, nh, pts))
        heapq.heappush(queue, _make_cell(cell.x + nh, cell.y + nh, nh, pts))

    if best.d < 0:
        return polygon_centroid(pts)
    return best.x, best.y
