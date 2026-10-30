from __future__ import annotations

from dataclasses import dataclass, field
import hashlib


MISSING_ROOM_NAME = "-"
LEGACY_MISSING_ROOM_NAMES = {"", "-", "(실명 미입력)", "(실명미입력)"}


def room_name_is_missing(value) -> bool:
    return str(value or "").strip() in LEGACY_MISSING_ROOM_NAMES


def normalize_room_name(value) -> str:
    text = str(value or "").strip()
    return MISSING_ROOM_NAME if text in LEGACY_MISSING_ROOM_NAMES else text


def _canonical_polygon_token(points, precision: float = 0.1) -> str:
    """핸들과 무관한 실 형상 식별용 정규화 토큰.

    시작점/방향이 달라도 같은 폐합선이면 같은 토큰이 되도록 회전/역순을 모두 비교한다.
    """
    clean = []
    for raw in points or []:
        try:
            point = (round(float(raw[0]) / precision), round(float(raw[1]) / precision))
        except Exception:
            continue
        if not clean or point != clean[-1]:
            clean.append(point)
    if len(clean) > 1 and clean[0] == clean[-1]:
        clean.pop()
    if not clean:
        return ""
    variants = []
    for seq in (clean, list(reversed(clean))):
        for index in range(len(seq)):
            variants.append(tuple(seq[index:] + seq[:index]))
    return repr(min(variants))


def make_room_program_id(floor: str, points) -> str:
    """CAD HANDLE을 사용하지 않는 프로그램 실 고유 ID.

    같은 층/같은 실구획 형상이면 재읽기 후에도 동일 ID가 나오도록 결정적으로 생성한다.
    """
    payload = f"{str(floor or '').strip().upper()}|{_canonical_polygon_token(points)}"
    digest = hashlib.sha1(payload.encode('utf-8', errors='ignore')).hexdigest()[:20].upper()
    return f"R-{digest}"


@dataclass(frozen=True)
class TextRecord:
    raw: str
    normalized: str
    layer: str
    entity_type: str
    handle: str
    x: float
    y: float


@dataclass
class ResultRow:
    normalized: str
    raw_example: str
    material: str
    thickness: str
    count: int
    layers: str
    category: str
    entity_types: str
    handles: str


@dataclass
class RoomRecord:
    floor: str
    room_name: str
    room_number: str
    points: list[tuple[float, float]]
    perimeter_raw: float
    area_raw: float
    boundary_handle: str
    tag_handle: str
    boundary_layer: str
    tag_layer: str
    tag_x: float
    tag_y: float
    is_closed: bool
    changed: bool = False
    drawing_type: str = "미구분"
    drawing_title: str = ""
    deduction_area_raw: float = 0.0
    deduction_perimeter_raw: float = 0.0
    deduction_count: int = 0
    applicable_floors: list[str] = field(default_factory=list)
    floor_scope: str = ""
    repeat_count: int = 1
    deduction_points: list[list[tuple[float, float]]] = field(default_factory=list)
    room_category: str = ""
    ceiling_height_m: float | None = None
    floor_height_m: float | None = None
    room_id: str = ""
    # 프로그램 화면에서 실 구분선 절점 추가/이동/삭제 등 형상을 수정했는지 표시한다.
    # 현재는 구획벽 나누기(절점 추가)가 이 플래그를 사용한다.
    geometry_changed: bool = False

    def __post_init__(self):
        # room_id가 없는 구버전 캐시/새 CAD 읽기 모두 HANDLE 대신 형상 기반 ID를 자동 부여한다.
        if not str(self.room_id or "").strip():
            self.room_id = make_room_program_id(self.floor, self.points)

    @property
    def perimeter_m(self) -> float:
        return self.perimeter_raw / 1000.0

    @property
    def area_m2(self) -> float:
        return self.area_raw / 1_000_000.0

    @property
    def quantity_perimeter_m(self) -> float:
        return self.perimeter_m * max(1, int(self.repeat_count or 1))

    @property
    def quantity_area_m2(self) -> float:
        return self.area_m2 * max(1, int(self.repeat_count or 1))
