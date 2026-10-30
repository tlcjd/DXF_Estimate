from __future__ import annotations

# CAD 업무 레이어 기본값을 한 곳에서 관리한다.
# 실제 기능 코드는 레이어명을 직접 하드코딩하지 않고 설정 키를 참조한다.
LAYER_DEFINITIONS = {
    "drawing_division_layer": {
        "group": "공통", "label": "도면구분 레이어",
        "name": "--■ DXF_공통_도면구분", "color": "#FFFFFF",
        "ini_name": "DrawingDivisionLayer", "ini_color": "DrawingDivisionLayerColor",
    },
    "structure_slab_layer": {
        "group": "구조", "label": "슬라브 라인 레이어",
        "name": "--■ DXF_구조_슬라브 라인", "color": "#FFFFFF",
        "ini_name": "StructureSlabLayer", "ini_color": "StructureSlabLayerColor",
    },
    "structure_wall_layer": {
        "group": "구조", "label": "벽체선 레이어",
        "name": "--■ DXF_구조_벽체선", "color": "#FFFFFF",
        "ini_name": "StructureWallLayer", "ini_color": "StructureWallLayerColor",
    },
    "boundary_layer": {
        "group": "마감", "label": "실구분선 레이어",
        "name": "--■ DXF_마감_실구분선", "color": "#FFFFFF",
        "ini_name": "BoundaryLayer", "ini_color": "BoundaryLayerColor",
    },
    "deduction_layer": {
        "group": "마감", "label": "실공제선 레이어",
        "name": "--■ DXF_마감_실구분선-공제", "color": "#FF00FF",
        "ini_name": "DeductionLayer", "ini_color": "DeductionLayerColor",
    },
    "tag_layer": {
        "group": "마감", "label": "실명블록 레이어",
        "name": "--■ DXF_마감_실명", "color": "#FFFF00",
        "ini_name": "TagLayer", "ini_color": "TagLayerColor",
    },
    "wall_finish_layer": {
        "group": "마감", "label": "벽마감선 레이어",
        "name": "--■ DXF_마감_벽마감선", "color": "#FF00FF",
        "ini_name": "WallFinishLayer", "ini_color": "WallFinishLayerColor",
    },
    "exterior_wall_layer": {
        "group": "외부", "label": "외벽선 레이어",
        "name": "--■ DXF_외부_외벽선", "color": "#FFFFFF",
        "ini_name": "ExteriorWallLayer", "ini_color": "ExteriorWallLayerColor",
    },
    "room_wall_link_wall_layer": {
        "group": "실/벽 연동", "label": "벽 연동 레이어",
        "name": "--■ DXF_실벽연동-벽", "color": "#FFFFFF",
        "ini_name": "RoomWallLinkWallLayer", "ini_color": "RoomWallLinkWallLayerColor",
    },
    "room_wall_link_floor_layer": {
        "group": "실/벽 연동", "label": "바닥 연동 레이어",
        "name": "--■ DXF_실벽연동-바닥", "color": "#FFFFFF",
        "ini_name": "RoomWallLinkFloorLayer", "ini_color": "RoomWallLinkFloorLayerColor",
    },
    "room_wall_link_ceiling_layer": {
        "group": "실/벽 연동", "label": "천정 연동 레이어",
        "name": "--■ DXF_실벽연동-천정", "color": "#FFFFFF",
        "ini_name": "RoomWallLinkCeilingLayer", "ini_color": "RoomWallLinkCeilingLayerColor",
    },
    "room_wall_link_other_layer": {
        "group": "실/벽 연동", "label": "기타 연동 레이어",
        "name": "--■ DXF_실벽연동-기타", "color": "#FFFFFF",
        "ini_name": "RoomWallLinkOtherLayer", "ini_color": "RoomWallLinkOtherLayerColor",
    },
}

LAYER_GROUPS = ("공통", "구조", "마감", "외부", "실/벽 연동")
LAYER_KEYS = tuple(LAYER_DEFINITIONS)

# 예전 기본명만 자동 변환 대상으로 둔다. 사용자가 직접 지정한 이름은 건드리지 않는다.
LEGACY_DEFAULT_NAMES = {
    "drawing_division_layer": {"DXF_도면구분", "--■ DXF_도면구분"},
    "boundary_layer": {"DXF_실구분선", "--■ DXF_실구분선"},
    "deduction_layer": {"--■ DXF_실구분선-공제", "DXF_실구분선-공제"},
    "tag_layer": {"DXF_실명", "--■ DXF_실명"},
    "wall_finish_layer": {"DXF_벽마감선", "--■ DXF_벽마감선"},
}

def default_layer_name(key: str) -> str:
    return str(LAYER_DEFINITIONS[key]["name"])

def default_layer_color(key: str) -> str:
    return str(LAYER_DEFINITIONS[key]["color"])


def migrate_legacy_layer_settings(settings) -> bool:
    """예전 기본 레이어명만 새 체계로 이동한다. 사용자 정의 이름은 보존한다."""
    changed = False
    for key, spec in LAYER_DEFINITIONS.items():
        ini_name = spec["ini_name"]
        current = str(settings.get("CAD", ini_name, "") or "").strip()
        if not current:
            settings.set("CAD", ini_name, default_layer_name(key))
            changed = True
        elif current in LEGACY_DEFAULT_NAMES.get(key, set()):
            settings.set("CAD", ini_name, default_layer_name(key))
            changed = True
        ini_color = spec["ini_color"]
        if not str(settings.get("CAD", ini_color, "") or "").strip():
            settings.set("CAD", ini_color, default_layer_color(key))
            changed = True
    if changed:
        settings.save()
    return changed
