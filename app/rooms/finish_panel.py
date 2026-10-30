from __future__ import annotations

import ast
import re
from copy import deepcopy

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QHeaderView,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.common.models import normalize_room_name
from app.finish_materials import FinishMaterialsPanel
from app.common.material_units import normalize_material_unit
from app.common.finish_calculations import normalize_formula_text
from app.cad.colors import color_to_aci


DEFAULT_ROOM_FINISH_SECTIONS = ("바닥", "천정", "벽", "기타")
ROOM_FINISH_SCHEMA_VERSION = 10
FINISH_GROUP_TYPES = ("마감", "단열", "방수", "외부", "기타")
DEFAULT_FINISH_GROUP_TYPE_LABELS = {value: value for value in FINISH_GROUP_TYPES}
DEFAULT_FINISH_GROUP_DIVISIONS = {
    "마감": ["바탕", "마감"],
    "단열": ["단열"],
    "방수": ["방수"],
    "외부": ["외부"],
    "기타": ["기타"],
}


def default_finish_group_division(group_type: str) -> str:
    key = normalize_finish_group_type(group_type)
    values = list(DEFAULT_FINISH_GROUP_DIVISIONS.get(key, []) or [])
    return str(values[-1] if values else key)


def normalize_finish_group_division(value: str, group_type: str = "마감") -> str:
    text = str(value or "").strip()
    return text or default_finish_group_division(group_type)


def normalize_finish_group_type(value: str) -> str:
    """재료마감 대분류 탭을 안정적인 5개 내부키로 정규화한다.

    V23.26의 구형 '바탕' 타입은 V23.29 구조에서는 '마감' 탭의 '바탕' 구분으로
    해석한다. 화면 탭 이름은 별도 표시명이라 바뀌어도 내부키는 변하지 않는다.
    """
    text = str(value or "").strip()
    if text in FINISH_GROUP_TYPES:
        return text
    if text == "바탕":
        return "마감"
    return "마감"


def evaluate_finish_formula(formula: str, variables: dict[str, float]) -> float | None:
    """허용된 사칙연산과 A/A0/L/H/H1/W0 변수만 안전하게 계산한다."""
    text = normalize_formula_text(formula)
    if not text:
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError):
        return None

    def value(node):
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in variables:
            return float(variables[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            number = value(node.operand)
            return number if isinstance(node.op, ast.UAdd) else -number
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = value(node.left), value(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right if right else 0.0
        raise ValueError("unsupported formula")

    try:
        return float(value(tree))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def calculate_room_finish_value(
    room, material: dict, wall_length: float | None = None,
    formula_override: str | None = None, apply_repeat_count: bool = True,
) -> float | None:
    """현재 실의 기본변수로 산식을 계산한다.

    apply_repeat_count=True이면 층범위/반복 개소까지 반영한 최종값을 반환하고,
    False이면 사용자가 입력한 산식 자체의 1개소 계산값을 반환한다.

    A  = 실면적(M2)
    L  = 둘레(m). 벽 그룹은 그룹에 지정된 벽 길이를 사용한다.
    H  = 층높이(m). 목록의 '층-전체' 행 천정고 칸에 입력한 값이다.
    H1 = 실 천정고(m). 개별 실의 천정고 열 값이다.
    A0/W0는 기존 산식 호환을 위해 유지한다.
    """
    if room is None:
        return None
    deduction_area = float(getattr(room, "deduction_area_raw", 0.0) or 0.0) / 1_000_000.0
    perimeter = float(wall_length) if wall_length is not None else float(getattr(room, "perimeter_m", 0.0) or 0.0)
    variables = {
        "A": float(getattr(room, "area_m2", 0.0) or 0.0),
        "A0": deduction_area,
        "L": perimeter,
        "H": float(getattr(room, "floor_height_m", 0.0) or 0.0),
        "H1": float(getattr(room, "ceiling_height_m", 0.0) or 0.0),
        "W0": 0.0,
    }
    formula = normalize_formula_text(formula_override) or normalize_formula_text(material.get("method", ""))
    result = evaluate_finish_formula(formula, variables)
    if result is None:
        return None
    if not apply_repeat_count:
        return result
    return result * max(1, int(getattr(room, "repeat_count", 1) or 1))


def normalize_room_finish_section(value: str) -> str:
    text = str(value or "").strip()
    if text == "천장":
        return "천정"
    # 창호는 V23.27부터 내부 재료마감 부위에서 분리해 메인 창호탭으로 관리한다.
    # 기존 프로젝트의 창호 부위 재료는 삭제하지 않고 '기타'로 이관한다.
    if text == "창호":
        return "기타"
    return text


def order_room_finish_sections(values) -> list[str]:
    """기본 부위는 바닥→천정→벽→기타 순으로 고정하고 사용자 부위는 뒤에 유지한다."""
    source: list[str] = []
    for raw in values or []:
        name = normalize_room_finish_section(raw)
        if name and name not in source:
            source.append(name)
    ordered = [name for name in DEFAULT_ROOM_FINISH_SECTIONS if name in source]
    ordered.extend(name for name in source if name not in DEFAULT_ROOM_FINISH_SECTIONS)
    return ordered


def normalize_room_finish_assignment(value) -> dict:
    """기존 구분→재료 1개 형식과 새 구분→재료목록 형식을 하나로 통일한다."""
    raw = dict(value or {}) if isinstance(value, dict) else {}
    if isinstance(raw.get("items"), dict):
        raw_items = dict(raw.get("items") or {})
        raw_sections = list(raw.get("sections") or [])
    else:
        raw_items = {
            key: item for key, item in raw.items()
            if not str(key).startswith("__") and key not in {"schema_version", "sections"}
        }
        raw_sections = list(raw.get("sections") or raw.get("__sections__") or [])

    # sections가 명시된 새 형식은 사용자가 삭제한 기본 구분도 그대로 존중한다.
    # sections가 없는 기존/신규 공란 데이터에만 기본 5개 구분을 보충한다.
    section_seed = raw_sections if "sections" in raw else [*DEFAULT_ROOM_FINISH_SECTIONS, *raw_sections]
    sections: list[str] = []
    for section in [*section_seed, *raw_items.keys()]:
        name = normalize_room_finish_section(section)
        if name and name not in sections:
            sections.append(name)

    items: dict[str, list[dict]] = {section: [] for section in sections}
    for section, raw_value in raw_items.items():
        name = normalize_room_finish_section(section)
        if not name:
            continue
        if name not in items:
            sections.append(name)
            items[name] = []
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for material in values:
            if isinstance(material, dict) and (material.get("id") or material.get("name")):
                copied = deepcopy(material)
                copied["method"] = normalize_formula_text(copied.get("method", ""))
                if "group_formula" in copied:
                    copied["group_formula"] = normalize_formula_text(copied.get("group_formula", ""))
                if isinstance(copied.get("scope_methods"), dict):
                    copied["scope_methods"] = {
                        str(key): normalize_formula_text(value) for key, value in copied.get("scope_methods", {}).items()
                    }
                if copied.get("group_id"):
                    raw_group_type = str(copied.get("group_type", "") or "").strip()
                    group_type = normalize_finish_group_type(raw_group_type)
                    raw_group_division = copied.get("group_division", "")
                    if not str(raw_group_division or "").strip() and raw_group_type == "바탕":
                        raw_group_division = "바탕"
                    copied["group_type"] = group_type
                    copied["group_division"] = normalize_finish_group_division(raw_group_division, group_type)
                items[name].append(copied)

    groups: list[dict] = []
    seen_group_ids: set[str] = set()
    for group in list(raw.get("groups") or []):
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id", "") or "").strip()
        section = normalize_room_finish_section(group.get("section", ""))
        if not group_id or group_id in seen_group_ids or not section:
            continue
        if section not in sections:
            sections.append(section)
            items.setdefault(section, [])
        seen_group_ids.add(group_id)
        raw_group_type = str(group.get("group_type", "") or "").strip()
        group_type = normalize_finish_group_type(raw_group_type)
        raw_group_division = group.get("group_division", "")
        if not str(raw_group_division or "").strip() and raw_group_type == "바탕":
            raw_group_division = "바탕"
        groups.append({
            "id": group_id, "name": str(group.get("name", "") or "그룹"), "section": section,
            "color": str(group.get("color", "") or ""),
            "aci": color_to_aci(group.get("color", ""), group.get("aci", group.get("color_aci"))),
            "formula": normalize_formula_text(group.get("formula", "")),
            "group_type": group_type,
            "group_division": normalize_finish_group_division(raw_group_division, group_type),
        })
    # 구버전 또는 중간 저장본은 재료의 group_id에서 그룹 부모를 복원한다.
    for section, materials in items.items():
        for material in materials:
            group_id = str(material.get("group_id", "") or "").strip()
            if group_id and group_id not in seen_group_ids:
                seen_group_ids.add(group_id)
                raw_group_type = str(material.get("group_type", "") or "").strip()
                group_type = normalize_finish_group_type(raw_group_type)
                raw_group_division = material.get("group_division", "")
                if not str(raw_group_division or "").strip() and raw_group_type == "바탕":
                    raw_group_division = "바탕"
                groups.append({
                    "id": group_id,
                    "name": str(material.get("group_name", "") or "그룹"),
                    "section": section,
                    "color": str(material.get("group_color", "") or ""),
                    "aci": color_to_aci(material.get("group_color", ""), material.get("group_aci")),
                    "formula": normalize_formula_text(material.get("group_formula", "")),
                    "group_type": group_type,
                    "group_division": normalize_finish_group_division(raw_group_division, group_type),
                })

    sections = order_room_finish_sections(sections)
    return {
        "schema_version": ROOM_FINISH_SCHEMA_VERSION,
        "sections": sections,
        "items": items,
        "groups": groups,
    }


def room_finish_materials(value, section: str) -> list[dict]:
    normalized = normalize_room_finish_assignment(value)
    name = normalize_room_finish_section(section)
    return list(normalized["items"].get(name, []))


def first_room_finish_material(value, section: str) -> dict:
    materials = room_finish_materials(value, section)
    return dict(materials[0]) if materials else {}


class SubtleGridDelegate(QStyledItemDelegate):
    """재료 입력 영역의 행/열 경계를 아주 연한 점선으로 표시한다."""
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        painter.save()
        pen = QPen(QColor(125, 125, 125, 62))
        pen.setWidth(1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        rect = option.rect
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.restore()


class FinishAssignmentList(QTreeWidget):
    items_changed = Signal()
    group_create_requested = Signal(str)
    group_delete_requested = Signal(str)
    group_break_requested = Signal(str)
    group_color_requested = Signal(str)
    group_rename_requested = Signal(str)
    group_division_requested = Signal(str)
    group_formula_requested = Signal(str, str)
    material_formula_requested = Signal(object, str)
    formula_cursor_changed = Signal(object, int)
    material_picker_requested = Signal(str)
    group_section_move_requested = Signal(str, str, str)
    group_order_changed = Signal(str, str, object)

    def __init__(self, section: str, value_resolver=None, group_type_label_resolver=None, parent=None):
        super().__init__(parent)
        self.section = section
        self.value_resolver = value_resolver
        self.group_type_label_resolver = group_type_label_resolver
        self.setColumnCount(7)
        self.setHeaderLabels(["그룹", "품명", "규격", "단위", "산식", "값", "층범위 계"])
        header = self.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate((90, 130, 95, 50, 260, 72, 82)):
            self.setColumnWidth(column, width)
        header.setStretchLastSection(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 산식 셀은 F2일 때만 셀 자체에서 직접 편집한다.
        # 더블클릭/Enter는 별도 산식 입력창을 사용하므로 기본 편집 트리거는 끈다.
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._inline_formula_item = None
        self._inline_formula_old = ""
        self.itemChanged.connect(self._on_inline_formula_item_changed)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setMinimumHeight(105)
        # 부위 목록 높이는 우측 재료마감 패널의 세로 splitter에서 사용자가 직접 조절한다.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(False)
        self._grid_delegate = SubtleGridDelegate(self)
        self.setItemDelegate(self._grid_delegate)
        self._grid_delegate.closeEditor.connect(self._on_inline_formula_editor_closed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_context_menu)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        # 산식 열에 마우스/선택 커서가 오면 우측 하단에서 현재 변수 치환값을 확인한다.
        self.setMouseTracking(True)
        self.itemEntered.connect(lambda item, column: self.formula_cursor_changed.emit(item, int(column)))
        self.itemClicked.connect(lambda item, column: self.formula_cursor_changed.emit(item, int(column)))

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self.formula_cursor_changed.emit(self.itemAt(event.position().toPoint()), int(index.column()))
        else:
            self.formula_cursor_changed.emit(None, -1)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.formula_cursor_changed.emit(None, -1)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        # 부위 목록의 실제 빈 영역을 더블클릭하면 F5와 같은 재료 선택창을 연다.
        # 기존 행/그룹의 더블클릭 기능(산식 입력 등)은 그대로 유지한다.
        pos = event.position().toPoint()
        if self.itemAt(pos) is None:
            self.clearSelection()
            self.setCurrentItem(None)
            self.material_picker_requested.emit(self.section)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    @staticmethod
    def _display_text(material: dict) -> str:
        parts = [str(material.get("name", "") or "").strip() or "(품명 없음)"]
        spec = str(material.get("spec", "") or "").strip()
        unit = normalize_material_unit(material.get("unit", ""))
        if spec:
            parts.append(spec)
        if unit:
            parts.append(unit)
        return " / ".join(parts)

    def make_item(self, material: dict) -> QTreeWidgetItem:
        formula = normalize_formula_text(material.get("method", ""))
        resolved = self.value_resolver(material, self.section) if self.value_resolver else None
        if isinstance(resolved, (tuple, list)) and len(resolved) >= 2:
            base_value, range_total = resolved[0], resolved[1]
        else:
            base_value = range_total = resolved
        value_text = "" if base_value is None else f"{float(base_value):,.3f}"
        range_text = "" if range_total is None else f"{float(range_total):,.3f}"
        item = QTreeWidgetItem([
            "",
            str(material.get("name", "") or ""),
            str(material.get("spec", "") or ""),
            normalize_material_unit(material.get("unit", "")),
            formula,
            value_text,
            range_text,
        ])
        item.setData(0, Qt.UserRole, {"kind": "material", "material": deepcopy(material)})
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setToolTip(4, "F2: 셀에서 직접수정 · 더블클릭/Enter: 산식 입력창 · A=실면적, L=둘레, H=층높이, H1=천정고")
        item.setToolTip(
            0,
            f"품명: {material.get('name', '')}\n"
            f"규격: {material.get('spec', '')}\n"
            f"단위: {normalize_material_unit(material.get('unit', ''))}\n"
            f"산출식: {formula}\n"
            f"값: {value_text}\n"
            f"층범위 계: {range_text}"
        )
        return item

    def _group_item(
        self, group_id: str, group_name: str, group_color: str = "#E8F5E9",
        formula: str = "", group_type: str = "마감", group_division: str = "", group_aci=None,
    ) -> QTreeWidgetItem:
        formula = normalize_formula_text(formula)
        group_type = normalize_finish_group_type(group_type)
        group_division = normalize_finish_group_division(group_division, group_type)
        # 그룹 행은 의미가 바로 보이도록 구분/ACI 색상번호를 명시한다.
        aci = color_to_aci(group_color, group_aci)
        item = QTreeWidgetItem([group_name, f"구분 : {group_division}", f"색상 : {aci}", "", formula, "", ""])
        color = QColor(group_color)
        if not color.isValid():
            color = QColor("#E8F5E9")
        item.setData(0, Qt.UserRole, {
            "kind": "group", "id": group_id, "name": group_name,
            "color": color.name(), "aci": aci, "formula": formula, "group_type": group_type,
            "group_division": group_division,
        })
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setToolTip(4, "F2: 셀에서 직접수정 · 더블클릭/Enter: 그룹 산식 입력창 · A=실면적, L=둘레, H=층높이, H1=천정고")
        for column in range(7):
            font = item.font(column)
            font.setBold(True)
            item.setFont(column, font)
            background = QColor(color)
            background.setAlpha(95)
            item.setBackground(column, QBrush(background))
        item.setExpanded(True)
        # 그룹도 드래그 가능. 같은 부위에서는 표시 순서만 바꾸고,
        # 다른 부위로 드롭할 때는 상위 패널에서 확인 후 객체지정을 해제하고 이동한다.
        item.setFlags(item.flags() | Qt.ItemIsDragEnabled)
        return item

    @staticmethod
    def _node_data(item: QTreeWidgetItem | None) -> dict:
        return dict(item.data(0, Qt.UserRole) or {}) if item is not None else {}

    def groups(self) -> list[dict]:
        result = []
        for row in range(self.topLevelItemCount()):
            data = self._node_data(self.topLevelItem(row))
            if data.get("kind") == "group":
                result.append({
                    "id": data.get("id", ""), "name": data.get("name", "그룹"),
                    "section": self.section, "color": data.get("color", ""),
                    "aci": color_to_aci(data.get("color", ""), data.get("aci")),
                    "formula": data.get("formula", ""),
                    "group_type": normalize_finish_group_type(data.get("group_type", "")),
                    "group_division": normalize_finish_group_division(
                        data.get("group_division", ""), data.get("group_type", "")
                    ),
                })
        return result

    def materials(self) -> list[dict]:
        result: list[dict] = []
        for row in range(self.topLevelItemCount()):
            top = self.topLevelItem(row)
            top_data = self._node_data(top)
            if top_data.get("kind") == "material":
                result.append(deepcopy(top_data.get("material") or {}))
                continue
            if top_data.get("kind") != "group":
                continue
            for child_row in range(top.childCount()):
                child_data = self._node_data(top.child(child_row))
                material = deepcopy(child_data.get("material") or {})
                if not material:
                    continue
                material["group_id"] = str(top_data.get("id", "") or "")
                material["group_name"] = str(top_data.get("name", "") or "그룹")
                material["group_type"] = normalize_finish_group_type(top_data.get("group_type", ""))
                material["group_division"] = normalize_finish_group_division(
                    top_data.get("group_division", ""), material["group_type"]
                )
                result.append(material)
        return result

    def count(self) -> int:
        return len(self.materials())

    def set_materials(self, materials, groups=None):
        self.clear()
        group_items: dict[str, QTreeWidgetItem] = {}
        for group in groups or []:
            group_id = str(group.get("id", "") or "")
            if not group_id:
                continue
            parent = self._group_item(
                group_id, str(group.get("name", "") or "그룹"),
                str(group.get("color", "") or ""), str(group.get("formula", "") or ""),
                normalize_finish_group_type(group.get("group_type", "")),
                normalize_finish_group_division(group.get("group_division", ""), group.get("group_type", "")),
                group.get("aci", group.get("color_aci")),
            )
            self.addTopLevelItem(parent)
            group_items[group_id] = parent
        for material in materials or []:
            if isinstance(material, dict):
                group_id = str(material.get("group_id", "") or "")
                if group_id and group_id not in group_items:
                    parent = self._group_item(
                        group_id, str(material.get("group_name", "") or "그룹"),
                        str(material.get("group_color", "") or ""), str(material.get("group_formula", "") or ""),
                        normalize_finish_group_type(material.get("group_type", "")),
                        normalize_finish_group_division(material.get("group_division", ""), material.get("group_type", "")),
                        material.get("group_aci"),
                    )
                    self.addTopLevelItem(parent)
                    group_items[group_id] = parent
                item = self.make_item(material)
                if group_id:
                    group_items[group_id].addChild(item)
                else:
                    self.addTopLevelItem(item)
        self.expandAll()

    def add_material(self, material: dict):
        group_id = str(material.get("group_id", "") or "")
        if group_id:
            for row in range(self.topLevelItemCount()):
                parent = self.topLevelItem(row)
                data = self._node_data(parent)
                if data.get("kind") == "group" and data.get("id") == group_id:
                    parent.addChild(self.make_item(material))
                    parent.setExpanded(True)
                    return
        self.addTopLevelItem(self.make_item(material))

    def move_selected_to(self, target: "FinishAssignmentList", insert_row: int | None = None) -> bool:
        if target is self:
            return False
        selected = [item for item in self.selectedItems() if self._node_data(item).get("kind") == "material"]
        if not selected:
            return False
        materials = [deepcopy(self._node_data(item).get("material") or {}) for item in selected]
        for item in selected:
            parent = item.parent()
            if parent is not None:
                parent.takeChild(parent.indexOfChild(item))
            else:
                self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        for material in materials:
            material.pop("group_id", None)
            material.pop("group_name", None)
            target.add_material(material)
        self.items_changed.emit()
        target.items_changed.emit()
        return True

    def keyPressEvent(self, event):
        item = self.currentItem()
        column = int(self.currentColumn())
        data = self._node_data(item)
        formula_target = item is not None and column == 4 and data.get("kind") in {"group", "material"}

        # Excel처럼 F2는 현재 산식 셀 안에서 바로 수정한다.
        if event.key() == Qt.Key_F2 and formula_target:
            self._start_inline_formula_edit(item)
            event.accept()
            return

        # 산식 셀에서 Enter는 셀 편집이 아니라 기존 상세 입력창을 연다.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and formula_target:
            if data.get("kind") == "group":
                self._edit_group_formula(item)
            else:
                self._edit_material_formula(item)
            event.accept()
            return

        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_F4):
            targets = [item for item in list(self.selectedItems()) if self._node_data(item).get("kind") == "material"]
            if event.key() == Qt.Key_F4 and targets:
                count = len(targets)
                text = "현재 항목을 삭제하시겠습니까?" if count == 1 else f"선택한 {count}개 항목을 삭제하시겠습니까?"
                if QMessageBox.question(
                    self, "목록 삭제", text,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                ) != QMessageBox.Yes:
                    return
            removed = False
            for item in targets:
                parent = item.parent()
                if parent is not None:
                    parent.takeChild(parent.indexOfChild(item))
                else:
                    self.takeTopLevelItem(self.indexOfTopLevelItem(item))
                removed = True
            if removed:
                self.items_changed.emit()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _formula_is_valid(formula: str) -> bool:
        formula = normalize_formula_text(formula)
        if not formula:
            return True
        return evaluate_finish_formula(
            formula, {"A": 1.0, "A0": 1.0, "L": 1.0, "H": 1.0, "H1": 1.0, "W0": 1.0}
        ) is not None

    def _start_inline_formula_edit(self, item: QTreeWidgetItem | None) -> None:
        data = self._node_data(item)
        if item is None or data.get("kind") not in {"group", "material"}:
            return
        self._inline_formula_item = item
        self._inline_formula_old = str(item.text(4) or "")
        self.setCurrentItem(item, 4)
        self.editItem(item, 4)

    def _on_inline_formula_editor_closed(self, *_args) -> None:
        # 값 변경이 없거나 Esc로 닫힌 경우에도 다음 일반 갱신을 인라인 편집으로 오인하지 않는다.
        self._inline_formula_item = None
        self._inline_formula_old = ""

    def _on_inline_formula_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if item is None or int(column) != 4 or item is not self._inline_formula_item:
            return
        formula = normalize_formula_text(item.text(4))
        old_formula = normalize_formula_text(self._inline_formula_old)
        data = self._node_data(item)
        if data.get("kind") not in {"group", "material"}:
            return

        # 외부 슬롯이 패널을 다시 그릴 수 있으므로 먼저 인라인 상태를 해제한다.
        self._inline_formula_item = None
        self._inline_formula_old = ""

        blocked = self.blockSignals(True)
        item.setText(4, formula)
        self.blockSignals(blocked)

        if not self._formula_is_valid(formula):
            blocked = self.blockSignals(True)
            item.setText(4, old_formula)
            self.blockSignals(blocked)
            QMessageBox.warning(
                self, "산식",
                "산식을 확인해 주세요. 사용할 수 있는 기본변수는 A, L, H, H1입니다.\n"
                "기존 호환변수 A0, W0도 사용할 수 있습니다.",
            )
            return
        if formula == old_formula:
            return

        if data.get("kind") == "group":
            data["formula"] = formula
            item.setData(0, Qt.UserRole, data)
            item.setToolTip(4, "F2: 셀에서 직접수정 · 더블클릭/Enter: 그룹 산식 입력창 · A=실면적, L=둘레, H=층높이, H1=천정고")
            self.group_formula_requested.emit(str(data.get("id", "") or ""), formula)
            return

        material = deepcopy(data.get("material") or {})
        material["method"] = formula
        data["material"] = deepcopy(material)
        item.setData(0, Qt.UserRole, data)
        item.setToolTip(4, "F2: 셀에서 직접수정 · 더블클릭/Enter: 산식 입력창 · A=실면적, L=둘레, H=층높이, H1=천정고")
        self.material_formula_requested.emit(material, formula)

    def _selected_group_item(self) -> QTreeWidgetItem | None:
        current = self.currentItem()
        if current is not None and current.parent() is None and self._node_data(current).get("kind") == "group":
            return current
        for item in self.selectedItems():
            if item.parent() is None and self._node_data(item).get("kind") == "group":
                return item
        return None

    def _emit_group_order(self, group_type: str = "") -> None:
        ordered_ids: list[str] = []
        resolved_type = normalize_finish_group_type(group_type) if str(group_type or "").strip() else ""
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            data = self._node_data(item)
            if data.get("kind") != "group":
                continue
            group_id = str(data.get("id", "") or "")
            if group_id:
                ordered_ids.append(group_id)
            if not resolved_type:
                resolved_type = normalize_finish_group_type(data.get("group_type", ""))
        if ordered_ids:
            self.group_order_changed.emit(self.section, resolved_type or "마감", ordered_ids)

    def _reorder_group_drop(self, group_item: QTreeWidgetItem, event) -> bool:
        old_index = self.indexOfTopLevelItem(group_item)
        if old_index < 0:
            return False
        pos = event.position().toPoint()
        target = self.itemAt(pos)
        if target is not None and target.parent() is not None:
            target = target.parent()
        if target is None:
            insert_index = self.topLevelItemCount()
        else:
            target_index = self.indexOfTopLevelItem(target)
            indicator = self.dropIndicatorPosition()
            if indicator == QAbstractItemView.DropIndicatorPosition.AboveItem:
                insert_index = target_index
            else:
                insert_index = target_index + 1
        data = self._node_data(group_item)
        group_type = normalize_finish_group_type(data.get("group_type", ""))
        taken = self.takeTopLevelItem(old_index)
        if old_index < insert_index:
            insert_index -= 1
        insert_index = max(0, min(insert_index, self.topLevelItemCount()))
        self.insertTopLevelItem(insert_index, taken)
        self.setCurrentItem(taken)
        taken.setSelected(True)
        self.items_changed.emit()
        self._emit_group_order(group_type)
        return True

    def dropEvent(self, event):
        source = event.source()
        if isinstance(source, FinishAssignmentList):
            group_item = source._selected_group_item()
            if group_item is not None:
                data = source._node_data(group_item)
                group_id = str(data.get("id", "") or "")
                if source is self:
                    if self._reorder_group_drop(group_item, event):
                        event.setDropAction(Qt.MoveAction)
                        event.accept()
                        return
                elif group_id:
                    name = str(data.get("name", "") or "그룹")
                    answer = QMessageBox.question(
                        self,
                        "그룹 부위 이동",
                        f"'{name}' 그룹을 {source.section} → {self.section}(으)로 이동할까요?\n\n"
                        "다른 부위로 이동하면 기존 도곽 객체 지정은 모두 해제됩니다.\n"
                        "그룹명·재료·산식·구분·색상은 그대로 유지됩니다.",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if answer != QMessageBox.Yes:
                        event.ignore()
                        return
                    # 실제 데이터 이동은 RoomsTab에서 전역 그룹 정의/객체 연결을 갱신한다.
                    self.group_section_move_requested.emit(group_id, source.section, self.section)
                    event.setDropAction(Qt.MoveAction)
                    event.accept()
                    return
            if source is not self:
                source.move_selected_to(self)
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return
        super().dropEvent(event)
        self.items_changed.emit()

    def _edit_group_formula(self, item: QTreeWidgetItem | None) -> None:
        data = self._node_data(item)
        if data.get("kind") != "group":
            return
        current = normalize_formula_text(data.get("formula", ""))
        formula, ok = QInputDialog.getText(
            self,
            "그룹 산식 입력",
            "산식 (A=실면적, L=둘레, H=층높이, H1=천정고):",
            QLineEdit.Normal,
            current,
        )
        if not ok:
            return
        formula = normalize_formula_text(formula)
        if formula and evaluate_finish_formula(formula, {"A":1.0,"A0":1.0,"L":1.0,"H":1.0,"H1":1.0,"W0":1.0}) is None:
            QMessageBox.warning(
                self, "그룹 산식",
                "산식을 확인해 주세요. 사용할 수 있는 기본변수는 A, L, H, H1입니다.\n"
                "기존 호환변수 A0, W0도 사용할 수 있습니다.",
            )
            return
        data["formula"] = formula
        item.setData(0, Qt.UserRole, data)
        item.setText(4, formula)
        item.setToolTip(4, "F2: 셀에서 직접수정 · 더블클릭/Enter: 그룹 산식 입력창 · A=실면적, L=둘레, H=층높이, H1=천정고")
        self.group_formula_requested.emit(str(data.get("id", "") or ""), formula)

    def _edit_material_formula(self, item: QTreeWidgetItem | None) -> None:
        data = self._node_data(item)
        if data.get("kind") != "material":
            return
        material = deepcopy(data.get("material") or {})
        current = normalize_formula_text(material.get("method", ""))
        formula, ok = QInputDialog.getText(
            self,
            "재료 산식 입력",
            "산식 (A=실면적, L=둘레, H=층높이, H1=천정고):",
            QLineEdit.Normal,
            current,
        )
        if not ok:
            return
        formula = normalize_formula_text(formula)
        if formula and evaluate_finish_formula(formula, {"A":1.0,"A0":1.0,"L":1.0,"H":1.0,"H1":1.0,"W0":1.0}) is None:
            QMessageBox.warning(
                self, "재료 산식",
                "산식을 확인해 주세요. 사용할 수 있는 기본변수는 A, L, H, H1입니다.\n"
                "기존 호환변수 A0, W0도 사용할 수 있습니다.",
            )
            return
        self.material_formula_requested.emit(material, formula)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if int(column) != 4:
            return
        kind = self._node_data(item).get("kind")
        if kind == "group":
            self._edit_group_formula(item)
        elif kind == "material":
            self._edit_material_formula(item)

    def _open_context_menu(self, pos):
        item = self.itemAt(pos)
        data = self._node_data(item)
        menu = QMenu(self)
        if data.get("kind") == "group":
            rename_action = menu.addAction("그룹 이름 변경")
            division_action = menu.addAction("그룹 구분 변경")
            formula_action = menu.addAction("그룹 산식 입력")
            color_action = menu.addAction("그룹 색상 변경")
            menu.addSeparator()
            delete_action = menu.addAction("그룹 삭제")
            break_action = menu.addAction("이 실에서 그룹 깨기")
            chosen = menu.exec(self.viewport().mapToGlobal(pos))
            if chosen == rename_action:
                self.group_rename_requested.emit(str(data.get("id", "") or ""))
            elif chosen == division_action:
                self.group_division_requested.emit(str(data.get("id", "") or ""))
            elif chosen == formula_action:
                self._edit_group_formula(item)
            elif chosen == delete_action:
                self.group_delete_requested.emit(str(data.get("id", "") or ""))
            elif chosen == break_action:
                self.group_break_requested.emit(str(data.get("id", "") or ""))
            elif chosen == color_action:
                self.group_color_requested.emit(str(data.get("id", "") or ""))
            return
        if data.get("kind") == "material":
            formula_action = menu.addAction("재료 산식 입력")
            chosen = menu.exec(self.viewport().mapToGlobal(pos))
            if chosen == formula_action:
                self._edit_material_formula(item)
            return
        create_action = menu.addAction("그룹 생성")
        if menu.exec(self.viewport().mapToGlobal(pos)) == create_action:
            self.group_create_requested.emit(self.section)


class FinishSectionWidget(QWidget):
    changed = Signal()
    expanded_changed = Signal(str, bool)
    group_create_requested = Signal(str)
    group_delete_requested = Signal(str)
    group_break_requested = Signal(str)
    group_color_requested = Signal(str)
    group_rename_requested = Signal(str)
    group_division_requested = Signal(str)
    group_formula_requested = Signal(str, str)
    material_formula_requested = Signal(object, str)
    material_picker_requested = Signal(str)
    group_section_move_requested = Signal(str, str, str)
    group_order_changed = Signal(str, str, object)

    def __init__(self, section: str, value_resolver=None, group_type_label_resolver=None, parent=None):
        super().__init__(parent)
        self.section = section
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        # V24.04.1: 바닥/천정/벽/기타 부위 헤더 문자는 좌측 정렬해 세로 시작선을 맞춘다.
        self.toggle.setStyleSheet("QToolButton { text-align: left; padding-left: 8px; }")
        # 접힌 부위 헤더는 글자 수(특히 "벽")에 따라 폭이 달라 보이지 않도록
        # 공통 최소폭을 둔다.
        self.toggle.setMinimumWidth(92)
        self.items = FinishAssignmentList(
            section, value_resolver=value_resolver, group_type_label_resolver=group_type_label_resolver
        )
        self._last_expanded_height = 150
        self.toggle.toggled.connect(self._set_expanded)
        self.items.items_changed.connect(self._on_items_changed)
        self.items.group_create_requested.connect(self.group_create_requested.emit)
        self.items.group_delete_requested.connect(self.group_delete_requested.emit)
        self.items.group_break_requested.connect(self.group_break_requested.emit)
        self.items.group_color_requested.connect(self.group_color_requested.emit)
        self.items.group_rename_requested.connect(self.group_rename_requested.emit)
        self.items.group_division_requested.connect(self.group_division_requested.emit)
        self.items.group_formula_requested.connect(self.group_formula_requested.emit)
        self.items.material_picker_requested.connect(self.material_picker_requested.emit)
        self.items.material_formula_requested.connect(self.material_formula_requested.emit)
        self.items.group_section_move_requested.connect(self.group_section_move_requested.emit)
        self.items.group_order_changed.connect(self.group_order_changed.emit)
        layout.addWidget(self.toggle)
        layout.addWidget(self.items)
        self._refresh_title()

    def collapsed_height(self) -> int:
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else None
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        return max(24, int(self.toggle.sizeHint().height()) + int(vertical_margins))

    def _set_expanded(self, expanded: bool):
        # 중요: 접기 때 items를 먼저 숨기면 QSplitter가 즉시 남은 공간을 다른 부위에
        # 재분배한다. 부모가 '접기 직전 높이'를 저장한 뒤 splitter 높이를 조정하도록
        # 먼저 신호를 보내고, 마지막에 내용 표시상태를 바꾼다.
        if not expanded:
            self._last_expanded_height = max(110, int(self.height()))
        self._refresh_title()
        self.expanded_changed.emit(self.section, bool(expanded))
        self.items.setVisible(expanded)
        self.updateGeometry()

    def apply_expanded_constraints(self, expanded: bool) -> None:
        if expanded:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
        else:
            collapsed = self.collapsed_height()
            self.setMinimumHeight(collapsed)
            self.setMaximumHeight(collapsed)
        self.updateGeometry()

    def _refresh_title(self):
        arrow = "▼" if self.toggle.isChecked() else "▶"
        self.toggle.setText(f"{arrow} {self.section} ({self.items.count()})")

    def _on_items_changed(self):
        self._refresh_title()
        self.changed.emit()

    def set_materials(self, materials, groups=None):
        self.items.set_materials(materials, groups)
        self._refresh_title()


class SelectedMaterialsTable(QTableWidget):
    """재료 목록에서 끌어온 항목을 임시로 모아 두는 표."""

    def __init__(self, picker: "MaterialPickerDialog"):
        super().__init__(0, 6, picker)
        self.picker = picker
        self.setHorizontalHeaderLabels(["구분", "공종", "품명", "규격", "단위", "산출식"])
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.verticalHeader().setVisible(False)
        self.setMinimumHeight(125)
        self.setColumnWidth(0, 75)
        self.setColumnWidth(1, 105)
        self.setColumnWidth(2, 210)
        self.setColumnWidth(3, 145)
        self.setColumnWidth(4, 55)
        self.horizontalHeader().setStretchLastSection(True)

    def dragEnterEvent(self, event):
        if event.source() in self.picker.library.tables.values():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.source() in self.picker.library.tables.values():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        source = event.source()
        if source in self.picker.library.tables.values():
            self.picker.stage_selected_rows(source)
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        super().dropEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_F4):
            if event.key() == Qt.Key_F4:
                count = len(self.selectedItems())
                if count > 0:
                    text = "현재 항목을 삭제하시겠습니까?" if count == 1 else f"선택한 {count}개 항목을 삭제하시겠습니까?"
                    if QMessageBox.question(
                        self, "목록 삭제", text,
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                    ) != QMessageBox.Yes:
                        return
            self.picker.remove_selected_staged()
            return
        super().keyPressEvent(event)


class MaterialPickerDialog(QDialog):
    def __init__(self, data_path, formula_codes=None, finish_categories=None, material_trades=None, material_units=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("재료마감 선택")
        self.resize(960, 760)
        self.setMinimumSize(780, 620)
        self.selected_materials: list[dict] = []
        self._staged_materials: dict[str, dict] = {}
        self._staged_order: list[str] = []
        self._table_categories: dict[QWidget, str] = {}

        layout = QVBoxLayout(self)
        self.library = FinishMaterialsPanel(
            data_path,
            assignment_enabled=False,
            formula_codes=formula_codes,
            finish_categories=finish_categories,
            material_trades=material_trades,
            material_units=material_units,
            picker_mode=True,
        )
        for category, table in self.library.tables.items():
            table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            table.setDragEnabled(True)
            table.setDragDropMode(QAbstractItemView.DragOnly)
            table.setDefaultDropAction(Qt.CopyAction)
            # 선택창에서는 무거운 셀 QComboBox만 생략하고 기존 목록 편집 기능은 유지한다.
            table.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
            table.installEventFilter(self)
            if isinstance(table, QTreeWidget):
                table.itemDoubleClicked.connect(
                    lambda item, _column, t=table: self.toggle_tree_item(t, item)
                )
            else:
                table.cellDoubleClicked.connect(
                    lambda row, _column, t=table: self.toggle_source_row(t, row)
                )
            self._table_categories[table] = category
        layout.addWidget(self.library, 3)

        self.selected_group = QGroupBox("선택 항목 (0)")
        selected_layout = QVBoxLayout(self.selected_group)
        selected_layout.setContentsMargins(6, 8, 6, 6)
        self.selected_table = SelectedMaterialsTable(self)
        self.selected_table.cellDoubleClicked.connect(
            lambda _row, _column: self.remove_selected_staged()
        )
        selected_layout.addWidget(self.selected_table)
        hint = QLabel("선택된 원본 행: 연한 빨간 배경  |  더블클릭 또는 드래그: 선택/취소  |  Shift+↓/↑: 연속 선택")
        hint.setStyleSheet("color:#555555;")
        selected_layout.addWidget(hint)
        layout.addWidget(self.selected_group, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        register_button = buttons.addButton("등록", QDialogButtonBox.AcceptRole)
        buttons.button(QDialogButtonBox.Cancel).setText("닫기")
        register_button.clicked.connect(self._register)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _register(self):
        if not self._staged_order:
            QMessageBox.information(self, "재료마감 등록", "아래 선택 항목에 등록할 재료를 담아 주세요.")
            return
        self.selected_materials = [
            deepcopy(self._staged_materials[key]) for key in self._staged_order
        ]
        self.accept()

    def eventFilter(self, watched, event):
        if watched in self._table_categories and event.type() == QEvent.KeyPress:
            if event.modifiers() & Qt.ShiftModifier and event.key() in (Qt.Key_Down, Qt.Key_Up):
                self._handle_shift_arrow(watched, event.key())
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _material_key(material: dict) -> str:
        material_id = str(material.get("id", "") or "").strip()
        if material_id:
            return material_id
        return "\x1f".join(
            str(material.get(field, "") or "").strip()
            for field in ("category", "trade_code", "name", "spec", "unit", "method")
        )

    def _material_at(self, table: QTableWidget, row: int) -> dict | None:
        category = self._table_categories.get(table)
        if category is None or row < 0:
            return None
        if isinstance(table, QTreeWidget):
            if row >= table.topLevelItemCount():
                return None
            return self.library._composite_item_material(table.topLevelItem(row))
        if row >= table.rowCount():
            return None
        return self.library._row_material(category, row)

    def toggle_tree_item(self, table: QTreeWidget, item):
        if item is not None and item.parent() is not None:
            item = item.parent()
        material = self.library._composite_item_material(item)
        if material:
            self.toggle_material(material)

    def toggle_source_row(self, table: QTableWidget, row: int):
        material = self._material_at(table, row)
        if material:
            self.toggle_material(material)

    def toggle_material(self, material: dict):
        key = self._material_key(material)
        if not key:
            return
        if key in self._staged_materials:
            self._staged_materials.pop(key, None)
            self._staged_order = [value for value in self._staged_order if value != key]
        else:
            self._staged_materials[key] = deepcopy(material)
            self._staged_order.append(key)
        self._refresh_staged_table()

    def stage_selected_rows(self, table: QTableWidget):
        if isinstance(table, QTreeWidget):
            parents = []
            for item in table.selectedItems():
                parent = item.parent() or item
                if parent not in parents:
                    parents.append(parent)
            if not parents and table.currentItem() is not None:
                current = table.currentItem()
                parents = [current.parent() or current]
            changed = False
            for item in parents:
                material = self.library._composite_item_material(item)
                if not material:
                    continue
                key = self._material_key(material)
                if key and key not in self._staged_materials:
                    self._staged_materials[key] = deepcopy(material)
                    self._staged_order.append(key); changed = True
            if changed:
                self._refresh_staged_table()
            return
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        changed = False
        for row in rows:
            material = self._material_at(table, row)
            if not material:
                continue
            key = self._material_key(material)
            if key and key not in self._staged_materials:
                self._staged_materials[key] = deepcopy(material)
                self._staged_order.append(key)
                changed = True
        if changed:
            self._refresh_staged_table()

    def remove_selected_staged(self):
        rows = sorted({index.row() for index in self.selected_table.selectionModel().selectedRows()}, reverse=True)
        if not rows and self.selected_table.currentRow() >= 0:
            rows = [self.selected_table.currentRow()]
        for row in rows:
            if 0 <= row < len(self._staged_order):
                key = self._staged_order.pop(row)
                self._staged_materials.pop(key, None)
        self._refresh_staged_table()

    def _handle_shift_arrow(self, table: QTableWidget, key):
        if isinstance(table, QTreeWidget):
            current = table.currentItem()
            if current is None or table.topLevelItemCount() == 0:
                return
            current = current.parent() or current
            row = table.indexOfTopLevelItem(current)
            target = row if key == Qt.Key_Down else max(row - 1, 0)
            next_row = min(row + 1, table.topLevelItemCount() - 1) if key == Qt.Key_Down else target
            self.toggle_tree_item(table, table.topLevelItem(target))
            table.setCurrentItem(table.topLevelItem(next_row))
            return
        row = table.currentRow()
        if row < 0 or table.rowCount() == 0:
            return
        if key == Qt.Key_Down:
            target = row
            next_row = min(row + 1, table.rowCount() - 1)
        else:
            target = max(row - 1, 0)
            next_row = target
        self.toggle_source_row(table, target)
        table.setCurrentCell(next_row, max(table.currentColumn(), 0))

    def _refresh_staged_table(self):
        self.selected_table.setRowCount(0)
        for key in self._staged_order:
            material = self._staged_materials[key]
            row = self.selected_table.rowCount()
            self.selected_table.insertRow(row)
            values = [
                material.get("category", ""),
                material.get("trade", ""),
                material.get("name", ""),
                material.get("spec", ""),
                normalize_material_unit(material.get("unit", "")),
                material.get("method", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if column == 0:
                    item.setData(Qt.UserRole, key)
                self.selected_table.setItem(row, column, item)
        self.selected_group.setTitle(f"선택 항목 ({len(self._staged_order)})")
        self._refresh_source_highlights()

    @staticmethod
    def _apply_source_highlight(item: QTableWidgetItem, selected: bool):
        if item is None:
            return
        item.setBackground(QBrush(QColor("#FFD9D9" if selected else "#FFFFFF")))
        item.setForeground(QBrush(QColor("#C62828" if selected else "#111111")))

    def _refresh_source_highlights(self):
        """하단 선택 항목에 들어간 재료를 원본 목록에서도 즉시 식별시킨다."""
        selected_keys = set(self._staged_order)
        for category, table in self.library.tables.items():
            if isinstance(table, QTreeWidget):
                table.blockSignals(True)
                try:
                    for row in range(table.topLevelItemCount()):
                        tree_item = table.topLevelItem(row)
                        material = self.library._composite_item_material(tree_item)
                        key = self._material_key(material) if material else ""
                        selected = bool(key and key in selected_keys)
                        for column in range(table.columnCount()):
                            tree_item.setBackground(column, QBrush(QColor("#FFD9D9" if selected else "#FFFFFF")))
                            tree_item.setForeground(column, QBrush(QColor("#C62828" if selected else "#111111")))
                finally:
                    table.blockSignals(False)
                table.viewport().update()
                continue
            table.blockSignals(True)
            try:
                for row in range(table.rowCount()):
                    material = self.library._row_material(category, row)
                    key = self._material_key(material) if material else ""
                    selected = bool(key and key in selected_keys)
                    for column in range(table.columnCount()):
                        self._apply_source_highlight(table.item(row, column), selected)
            finally:
                table.blockSignals(False)
            table.viewport().update()


class RoomFinishPanel(QWidget):
    assignments_changed = Signal(str, object)
    materials_changed = Signal()
    material_delete_requested = Signal(str, object)
    material_units_changed = Signal(object)
    group_create_requested = Signal(str, bool)
    group_delete_requested = Signal(str)
    group_break_requested = Signal(str)
    group_color_requested = Signal(str)
    group_rename_requested = Signal(str)
    group_division_requested = Signal(str)
    group_formula_requested = Signal(str, str)
    material_formula_requested = Signal(object, str)
    group_selected = Signal(str)
    group_materials_add_requested = Signal(str, object)
    wall_group_mode_requested = Signal()
    group_type_labels_changed = Signal(object)
    group_type_changed = Signal(str)
    group_tab_settings_changed = Signal(object)
    group_section_move_requested = Signal(str, str, str)
    group_order_changed = Signal(str, str, object)

    def __init__(self, data_path, formula_codes=None, finish_categories=None, material_trades=None, material_units=None, parent=None):
        super().__init__(parent)
        self.data_path = data_path
        self.formula_codes = formula_codes
        self.finish_categories = finish_categories
        self.material_trades = material_trades
        self.material_units = material_units
        self._current_handle = ""
        self._current_room = None
        self._group_wall_lengths: dict[str, float] = {}
        self._current_data = normalize_room_finish_assignment({})
        self._loading = False
        self.section_widgets: dict[str, FinishSectionWidget] = {}
        # 최초 표시에서는 모든 부위를 접어 두고, 사용자가 펼친 상태는 탭/부위별로 세션 동안 기억한다.
        self._section_expanded_state: dict[str, bool] = {}
        self._group_type_labels = dict(DEFAULT_FINISH_GROUP_TYPE_LABELS)
        self._group_divisions = {key: list(values) for key, values in DEFAULT_FINISH_GROUP_DIVISIONS.items()}
        self._show_other_group_filters = {key: False for key in FINISH_GROUP_TYPES}
        self._active_group_type = FINISH_GROUP_TYPES[0]

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        title = QLabel("재료마감")
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        root.addWidget(title)
        self.current_room_label = QLabel("현재 실: -")
        self.current_room_label.setStyleSheet("color:#555555;")
        self.current_room_label.setWordWrap(True)
        root.addWidget(self.current_room_label)

        # 타입은 고정 내부키(마감/단열/방수/외부/기타), 탭에 보이는 이름만 사용자가 바꾼다.
        # QTabWidget 페이지를 복제하지 않고 QTabBar + 공용 편집영역을 써서
        # 현재 타입만 필터링한다. 데이터는 모든 타입을 한 assignment에 계속 보존한다.
        self.type_tabs = QTabBar()
        self.type_tabs.setExpanding(True)
        for group_type in FINISH_GROUP_TYPES:
            index = self.type_tabs.addTab(self._group_type_labels[group_type])
            self.type_tabs.setTabData(index, group_type)
        self.type_tabs.setCurrentIndex(0)
        self.type_tabs.setToolTip("탭 이름은 더블클릭하여 변경할 수 있습니다.")
        root.addWidget(self.type_tabs, 0)

        finish_page = QWidget()
        finish_layout = QVBoxLayout(finish_page)
        finish_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.section_host = QWidget()
        section_host_layout = QVBoxLayout(self.section_host)
        section_host_layout.setContentsMargins(2, 2, 2, 2)
        section_host_layout.setSpacing(0)
        # 각 부위(바닥/벽/천정...) 목록 사이 손잡이를 잡아 높이를 직접 조절한다.
        self.section_splitter = QSplitter(Qt.Vertical)
        self.section_splitter.setChildrenCollapsible(False)
        self.section_splitter.setHandleWidth(5)
        # splitter 자체 높이는 각 부위의 실제 높이 합계로 관리한다. 남는 세로공간은
        # 아래쪽 빈 공간으로 보내 접힌 헤더가 화면 가운데/하단으로 날아가지 않게 한다.
        self.section_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        section_host_layout.addWidget(self.section_splitter, 0, Qt.AlignTop)
        section_host_layout.addStretch(1)
        self._section_height_by_name: dict[str, int] = {}
        # QSplitter는 기본적으로 주어진 높이 안에서 위/아래 패널 높이만 나눠 가진다.
        # 따라서 아래 패널들이 최소높이에 도달하면 손잡이가 더 내려가지 못한다.
        # 재료마감에서는 그 시점부터 부모 스크롤 콘텐츠 자체를 늘려 아래 부위들을
        # 밀어내고 세로 스크롤 범위가 계속 커지도록 손잡이 드래그를 보정한다.
        self._section_base_min_height = 0
        self._section_growth_by_handle: dict[int, int] = {}
        self._section_drag_last_global_y: int | None = None
        self._section_drag_handle_index = 0
        self.scroll.setWidget(self.section_host)
        finish_layout.addWidget(self.scroll, 1)

        # 산식 변수 안내는 하단 조작 버튼 바로 위에 둔다.
        # 버튼은 재료마감 패널의 최하단 라인에 붙여 중앙 하단 패널과 높이를 맞춘다.
        self.formula_preview_label = QLabel("")
        self.formula_preview_label.setMinimumHeight(22)
        self.formula_preview_label.setWordWrap(True)
        self.formula_preview_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.formula_preview_label.setStyleSheet("color:#0066CC; background:transparent; border:0;")
        finish_layout.addWidget(self.formula_preview_label, 0)

        section_buttons = QHBoxLayout()
        self.add_section_button = QPushButton("부위추가")
        self.delete_section_button = QPushButton("부위삭제")
        self.floor_group_button = QPushButton("바닥지정")
        self.ceiling_group_button = QPushButton("천정지정")
        self.wall_group_button = QPushButton("벽지정")
        for button in (
            self.add_section_button, self.delete_section_button,
            self.floor_group_button, self.ceiling_group_button, self.wall_group_button,
        ):
            button.setFixedHeight(24)
            button.setMaximumWidth(68)
        self.add_section_button.clicked.connect(self.add_section)
        self.delete_section_button.clicked.connect(self.delete_section)
        self.floor_group_button.clicked.connect(lambda: self.group_create_requested.emit("바닥", True))
        self.ceiling_group_button.clicked.connect(lambda: self.group_create_requested.emit("천정", True))
        self.wall_group_button.clicked.connect(self.wall_group_mode_requested.emit)
        section_buttons.addWidget(self.add_section_button)
        section_buttons.addWidget(self.delete_section_button)
        section_buttons.addStretch(1)
        finish_layout.addLayout(section_buttons)
        group_buttons = QHBoxLayout()
        group_buttons.addWidget(self.floor_group_button)
        group_buttons.addWidget(self.ceiling_group_button)
        group_buttons.addWidget(self.wall_group_button)
        group_buttons.addStretch(1)
        self.tab_settings_button = QPushButton("탭별 설정")
        self.tab_settings_button.setFixedHeight(24)
        self.tab_settings_button.setMinimumWidth(76)
        self.tab_settings_button.clicked.connect(self.open_group_tab_settings)
        group_buttons.addWidget(self.tab_settings_button)
        finish_layout.addLayout(group_buttons)
        root.addWidget(finish_page, 1)
        self.type_tabs.currentChanged.connect(self._on_group_type_tab_changed)
        self.type_tabs.tabBarDoubleClicked.connect(self._rename_group_type_tab)
        self._rebuild_sections()

    def group_type_label(self, group_type: str) -> str:
        key = normalize_finish_group_type(group_type)
        return str(self._group_type_labels.get(key, key) or key)

    def current_group_type(self) -> str:
        return normalize_finish_group_type(self._active_group_type)

    def group_type_labels(self) -> dict[str, str]:
        return {key: self.group_type_label(key) for key in FINISH_GROUP_TYPES}

    def set_group_type_labels(self, labels=None, emit: bool = False) -> None:
        source = dict(labels or {})
        used: set[str] = set()
        for index, key in enumerate(FINISH_GROUP_TYPES):
            label = str(source.get(key, self._group_type_labels.get(key, key)) or key).strip() or key
            # 표시명 중복은 탭/도면 필터 선택이 헷갈리므로 자동으로 기본명으로 되돌린다.
            if label.casefold() in used:
                label = key
            used.add(label.casefold())
            self._group_type_labels[key] = label
            if hasattr(self, "type_tabs") and index < self.type_tabs.count():
                self.type_tabs.setTabText(index, label)
        if emit:
            self.group_type_labels_changed.emit(self.group_type_labels())
        if hasattr(self, "section_splitter"):
            self._rebuild_sections()

    def _rename_group_type_tab(self, index: int) -> None:
        if index < 0 or index >= self.type_tabs.count():
            return
        key = str(self.type_tabs.tabData(index) or "")
        if key not in FINISH_GROUP_TYPES:
            return
        current = self.group_type_label(key)
        name, ok = QInputDialog.getText(
            self, "재료마감 탭 이름 변경",
            "새 탭 이름:\n(내부 구분은 그대로 유지되어 기존 지정 데이터는 바뀌지 않습니다.)",
            QLineEdit.Normal, current,
        )
        name = str(name or "").strip()
        if not ok or not name or name == current:
            return
        if any(
            other != key and self.group_type_label(other).casefold() == name.casefold()
            for other in FINISH_GROUP_TYPES
        ):
            QMessageBox.information(self, "탭 이름 변경", "다른 탭과 같은 이름은 사용할 수 없습니다.")
            return
        self._group_type_labels[key] = name
        self.type_tabs.setTabText(index, name)
        self.group_type_labels_changed.emit(self.group_type_labels())
        # 그룹 행 품명 칸에도 바뀐 표시명을 즉시 반영한다.
        self._rebuild_sections()

    @staticmethod
    def _material_group_type(material: dict, groups_by_id=None) -> str:
        value = dict(material or {})
        group_id = str(value.get("group_id", "") or "")
        group = dict((groups_by_id or {}).get(group_id, {}) or {}) if group_id else {}
        if group:
            return normalize_finish_group_type(group.get("group_type", ""))
        return normalize_finish_group_type(value.get("group_type", ""))

    def _on_group_type_tab_changed(self, index: int) -> None:
        if self._loading or index < 0:
            return
        new_type = str(self.type_tabs.tabData(index) or "")
        if new_type not in FINISH_GROUP_TYPES or new_type == self._active_group_type:
            return
        # 화면에 보이던 이전 타입 편집값을 전체 데이터에 먼저 병합한 뒤 새 타입을 표시한다.
        if self.section_widgets:
            self._current_data = normalize_room_finish_assignment(self.assignment_data())
        self._active_group_type = new_type
        self._rebuild_sections()
        self.group_selected.emit("")
        self.group_type_changed.emit(new_type)

    def group_divisions(self, group_type: str | None = None) -> list[str]:
        key = normalize_finish_group_type(group_type or self._active_group_type)
        values = []
        for value in self._group_divisions.get(key, []) or []:
            text = str(value or "").strip()
            if text and text.casefold() not in {item.casefold() for item in values}:
                values.append(text)
        if not values:
            values = [default_finish_group_division(key)]
        return values

    def show_other_group_filters(self, group_type: str | None = None) -> bool:
        key = normalize_finish_group_type(group_type or self._active_group_type)
        return bool(self._show_other_group_filters.get(key, False))

    def group_tab_settings(self) -> dict:
        return {
            "divisions": {key: self.group_divisions(key) for key in FINISH_GROUP_TYPES},
            "show_other_filters": {key: self.show_other_group_filters(key) for key in FINISH_GROUP_TYPES},
        }

    def set_group_tab_settings(self, settings=None, emit: bool = False) -> None:
        raw = dict(settings or {})
        raw_divisions = dict(raw.get("divisions", {}) or {})
        raw_show_other = dict(raw.get("show_other_filters", {}) or {})
        for key in FINISH_GROUP_TYPES:
            values = []
            for value in raw_divisions.get(key, self._group_divisions.get(key, [])) or []:
                text = str(value or "").strip()
                if text and text.casefold() not in {item.casefold() for item in values}:
                    values.append(text)
            if not values:
                values = list(DEFAULT_FINISH_GROUP_DIVISIONS.get(key, [key]))
            self._group_divisions[key] = values
            if key in raw_show_other:
                self._show_other_group_filters[key] = bool(raw_show_other.get(key))
        # 현재 로드된 자료에 설정에 없는 구분이 있으면 숨기지 말고 목록에 자동 복원한다.
        for group in self._current_data.get("groups", []) or []:
            group_type = normalize_finish_group_type(group.get("group_type", ""))
            division = normalize_finish_group_division(group.get("group_division", ""), group_type)
            if division.casefold() not in {item.casefold() for item in self._group_divisions[group_type]}:
                self._group_divisions[group_type].append(division)
        if emit:
            self.group_tab_settings_changed.emit(self.group_tab_settings())
        if hasattr(self, "section_splitter"):
            self._rebuild_sections()

    def ensure_group_division(self, group_type: str, division: str) -> str:
        key = normalize_finish_group_type(group_type)
        text = normalize_finish_group_division(division, key)
        existing = self.group_divisions(key)
        for value in existing:
            if value.casefold() == text.casefold():
                return value
        self._group_divisions[key] = existing + [text]
        return text

    def open_group_tab_settings(self) -> None:
        """5개 재료마감 대분류의 구분을 한 창에서 관리한다.

        하단 공용 버튼은 마지막으로 포커스를 둔 목록에 작동하며, 각 목록의
        우클릭 메뉴에서도 추가/이름변경/삭제를 바로 실행할 수 있다.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("재료마감 탭별 설정")
        dialog.resize(780, 680)
        layout = QVBoxLayout(dialog)

        guide = QLabel(
            "5개 탭의 구분을 한 번에 관리합니다. 목록을 클릭한 뒤 아래 버튼을 사용하거나 "
            "목록에서 우클릭하세요."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        layout.addLayout(grid, 1)

        lists: dict[str, QListWidget] = {}
        checks: dict[str, QCheckBox] = {}
        boxes: dict[str, QGroupBox] = {}
        active = {"key": self.current_group_type()}

        def set_active(group_type: str) -> None:
            if group_type not in FINISH_GROUP_TYPES:
                return
            active["key"] = group_type
            for key, box in boxes.items():
                label = self.group_type_label(key)
                box.setTitle(("▶ " if key == group_type else "") + label)

        for index, group_type in enumerate(FINISH_GROUP_TYPES):
            box = QGroupBox(self.group_type_label(group_type))
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(6, 8, 6, 6)
            division_list = QListWidget()
            division_list.setContextMenuPolicy(Qt.CustomContextMenu)
            for value in self.group_divisions(group_type):
                item = QListWidgetItem(value)
                # 최초 이름을 보존해 여러 번 이름을 바꿔도 원래 구분과 연결한다.
                item.setData(Qt.UserRole, value)
                division_list.addItem(item)
            if division_list.count():
                division_list.setCurrentRow(0)
            box_layout.addWidget(division_list, 1)

            show_other_check = QCheckBox("화면표시에 다른 탭 구분도 표시")
            show_other_check.setChecked(self.show_other_group_filters(group_type))
            show_other_check.setToolTip(
                "체크하면 이 탭을 작업할 때 중앙 화면표시 목록에 다른 탭의 구분/전체도 함께 표시합니다."
            )
            box_layout.addWidget(show_other_check)

            row, col = divmod(index, 2)
            grid.addWidget(box, row, col)
            lists[group_type] = division_list
            checks[group_type] = show_other_check
            boxes[group_type] = box

        def target_type(explicit: str | None = None) -> str:
            key = str(explicit or active.get("key") or self.current_group_type())
            return key if key in FINISH_GROUP_TYPES else self.current_group_type()

        def add_division(group_type: str | None = None) -> None:
            key = target_type(group_type)
            set_active(key)
            division_list = lists[key]
            name, ok = QInputDialog.getText(dialog, "구분 추가", f"[{self.group_type_label(key)}] 구분 이름:")
            name = str(name or "").strip()
            if not ok or not name:
                return
            if any(
                division_list.item(row).text().strip().casefold() == name.casefold()
                for row in range(division_list.count())
            ):
                QMessageBox.information(dialog, "구분 추가", "이미 같은 구분이 있습니다.")
                return
            item = QListWidgetItem(name)
            # 새 항목은 원본 구분이 없으므로 UserRole을 비워 둔다.
            item.setData(Qt.UserRole, "")
            division_list.addItem(item)
            division_list.setCurrentItem(item)
            division_list.setFocus()

        def rename_division(group_type: str | None = None) -> None:
            key = target_type(group_type)
            set_active(key)
            division_list = lists[key]
            item = division_list.currentItem()
            if item is None:
                return
            old_text = str(item.text() or "").strip()
            name, ok = QInputDialog.getText(
                dialog, "구분 이름 변경",
                f"[{self.group_type_label(key)}] 새 구분 이름:",
                QLineEdit.Normal, old_text,
            )
            name = str(name or "").strip()
            if not ok or not name or name == old_text:
                return
            if any(
                division_list.item(row) is not item
                and division_list.item(row).text().strip().casefold() == name.casefold()
                for row in range(division_list.count())
            ):
                QMessageBox.information(dialog, "구분 이름 변경", "이미 같은 구분이 있습니다.")
                return
            item.setText(name)
            division_list.setCurrentItem(item)
            division_list.setFocus()

        def delete_division(group_type: str | None = None) -> None:
            key = target_type(group_type)
            set_active(key)
            division_list = lists[key]
            item = division_list.currentItem()
            if item is None:
                return
            if division_list.count() <= 1:
                QMessageBox.information(dialog, "구분 삭제", "각 탭에는 구분이 최소 1개 있어야 합니다.")
                return
            row = division_list.row(item)
            division_list.takeItem(row)
            if division_list.count():
                division_list.setCurrentRow(min(row, division_list.count() - 1))
                division_list.setFocus()

        def show_context_menu(group_type: str, pos) -> None:
            key = target_type(group_type)
            set_active(key)
            division_list = lists[key]
            item = division_list.itemAt(pos)
            if item is not None:
                division_list.setCurrentItem(item)
            menu = QMenu(division_list)
            add_action = menu.addAction("추가")
            rename_action = menu.addAction("이름변경")
            delete_action = menu.addAction("삭제")
            rename_action.setEnabled(division_list.currentItem() is not None)
            delete_action.setEnabled(division_list.currentItem() is not None)
            chosen = menu.exec(division_list.viewport().mapToGlobal(pos))
            if chosen is add_action:
                add_division(key)
            elif chosen is rename_action:
                rename_division(key)
            elif chosen is delete_action:
                delete_division(key)

        # 어느 목록을 마지막으로 조작했는지가 하단 공용 버튼의 대상이다.
        for key, division_list in lists.items():
            division_list.itemPressed.connect(lambda _item, k=key: set_active(k))
            division_list.itemClicked.connect(lambda _item, k=key: set_active(k))
            division_list.itemDoubleClicked.connect(lambda _item, k=key: rename_division(k))
            division_list.customContextMenuRequested.connect(lambda pos, k=key: show_context_menu(k, pos))

        set_active(self.current_group_type())

        button_row = QHBoxLayout()
        add_btn = QPushButton("추가")
        rename_btn = QPushButton("이름변경")
        delete_btn = QPushButton("삭제")
        add_btn.clicked.connect(lambda: add_division())
        rename_btn.clicked.connect(lambda: rename_division())
        delete_btn.clicked.connect(lambda: delete_division())
        button_row.addWidget(add_btn)
        button_row.addWidget(rename_btn)
        button_row.addWidget(delete_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("확인")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.rejected.connect(dialog.reject)

        def collect_values(key: str) -> list[str]:
            division_list = lists[key]
            return [
                division_list.item(row).text().strip()
                for row in range(division_list.count())
                if division_list.item(row).text().strip()
            ]

        def collect_rename_map(key: str) -> dict[str, str]:
            division_list = lists[key]
            result: dict[str, str] = {}
            for row in range(division_list.count()):
                item = division_list.item(row)
                original = str(item.data(Qt.UserRole) or "").strip()
                current = str(item.text() or "").strip()
                if original and current and original.casefold() != current.casefold():
                    result[original.casefold()] = current
            return result

        def validate_and_accept() -> None:
            for key in FINISH_GROUP_TYPES:
                if not collect_values(key):
                    QMessageBox.information(dialog, "탭별 설정", f"{self.group_type_label(key)} 탭에는 구분이 최소 1개 있어야 합니다.")
                    return

            # 현재 패널에 로드된 실제 그룹에서 사용 중인 구분은 이름변경 없이 삭제하지 못하게 한다.
            for key in FINISH_GROUP_TYPES:
                new_values = collect_values(key)
                rename_map = collect_rename_map(key)
                old_values = self.group_divisions(key)
                new_keys = {value.casefold() for value in new_values}
                removed = {
                    value.casefold(): value
                    for value in old_values
                    if value.casefold() not in new_keys and value.casefold() not in rename_map
                }
                if not removed:
                    continue
                used_removed = []
                for group in self._current_data.get("groups", []) or []:
                    if normalize_finish_group_type(group.get("group_type", "")) != key:
                        continue
                    division = normalize_finish_group_division(group.get("group_division", ""), key)
                    if division.casefold() in removed:
                        used_removed.append(division)
                if used_removed:
                    QMessageBox.information(
                        dialog, "구분 삭제",
                        f"[{self.group_type_label(key)}] 현재 프로젝트에서 사용 중인 구분은 삭제할 수 없습니다.\n"
                        + ", ".join(sorted(set(used_removed)))
                    )
                    return
            dialog.accept()

        buttons.accepted.connect(validate_and_accept)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        old_divisions = {key: self.group_divisions(key) for key in FINISH_GROUP_TYPES}
        old_show_other = {key: self.show_other_group_filters(key) for key in FINISH_GROUP_TYPES}
        new_divisions = {key: collect_values(key) for key in FINISH_GROUP_TYPES}
        rename_maps = {key: collect_rename_map(key) for key in FINISH_GROUP_TYPES}

        # 현재 패널 자료에도 5개 탭의 이름변경을 즉시 반영한다.
        for group in self._current_data.get("groups", []) or []:
            key = normalize_finish_group_type(group.get("group_type", ""))
            division = normalize_finish_group_division(group.get("group_division", ""), key)
            renamed = rename_maps.get(key, {}).get(division.casefold())
            if renamed:
                group["group_division"] = renamed
        for materials in self._current_data.get("items", {}).values():
            for material in materials:
                key = normalize_finish_group_type(material.get("group_type", ""))
                division = normalize_finish_group_division(material.get("group_division", ""), key)
                renamed = rename_maps.get(key, {}).get(division.casefold())
                if renamed:
                    material["group_division"] = renamed

        changed_types = []
        for key in FINISH_GROUP_TYPES:
            self._group_divisions[key] = list(new_divisions[key])
            self._show_other_group_filters[key] = checks[key].isChecked()
            if (
                old_divisions[key] != new_divisions[key]
                or old_show_other[key] != self._show_other_group_filters[key]
                or bool(rename_maps.get(key))
            ):
                changed_types.append(key)

        self._rebuild_sections()
        payload = self.group_tab_settings()
        payload["changed_group_types"] = changed_types
        payload["rename_maps"] = rename_maps
        # 단일 탭 처리만 아는 구형 수신부와의 호환 정보도 남긴다.
        if len(changed_types) == 1:
            payload["changed_group_type"] = changed_types[0]
            payload["rename_map"] = rename_maps.get(changed_types[0], {})
        self.group_tab_settings_changed.emit(payload)

    def set_calculation_settings(self, formula_codes=None, finish_categories=None, material_trades=None, material_units=None):
        self.formula_codes = formula_codes
        self.finish_categories = finish_categories
        self.material_trades = material_trades
        self.material_units = material_units

    def reload(self):
        return

    def set_selected_wall_count(self, _count: int):
        return

    def set_selected_room_count(self, _count: int):
        return

    def set_current_room(self, room, assignment=None):
        self._current_room = room
        self._current_handle = str(getattr(room, "boundary_handle", "") or "") if room is not None else ""
        self._current_data = normalize_room_finish_assignment(assignment or {})
        for group in self._current_data.get("groups", []) or []:
            group_type = normalize_finish_group_type(group.get("group_type", ""))
            self.ensure_group_division(
                group_type, normalize_finish_group_division(group.get("group_division", ""), group_type)
            )
        self._set_formula_preview("")
        if room is None:
            self.current_room_label.setText("현재 실: -")
        else:
            floor = str(getattr(room, "floor", "") or "")
            number = str(getattr(room, "room_number", "") or "")
            name = normalize_room_name(getattr(room, "room_name", ""))
            self.current_room_label.setText(f"현재 실: {floor} / {number or '-'} / {name}")
        self._rebuild_sections()
        self.group_selected.emit("")

    def set_group_wall_lengths(self, values=None):
        self._group_wall_lengths = {str(key): float(value) for key, value in dict(values or {}).items()}

    def _material_value(self, material: dict, section: str):
        room = self._current_room
        if room is None:
            return None, None
        group_id = str(material.get("group_id", "") or "")
        perimeter = self._group_wall_lengths.get(group_id, float(getattr(room, "perimeter_m", 0.0) or 0.0))
        group_formula = ""
        if group_id:
            for group in self._current_data.get("groups", []) or []:
                if str(group.get("id", "") or "") == group_id:
                    group_formula = str(group.get("formula", "") or "")
                    break
        # 개별 재료 산식을 입력하면 그룹 산식보다 우선하고, 공란일 때만 그룹 산식을 기본값으로 쓴다.
        effective_group_formula = "" if str(material.get("method", "") or "").strip() else group_formula
        base_value = calculate_room_finish_value(
            room, material, wall_length=perimeter, formula_override=effective_group_formula,
            apply_repeat_count=False,
        )
        range_total = calculate_room_finish_value(
            room, material, wall_length=perimeter, formula_override=effective_group_formula,
            apply_repeat_count=True,
        )
        return base_value, range_total

    @staticmethod
    def _formula_number(value: float) -> str:
        text = f"{float(value):.3f}".rstrip("0").rstrip(".")
        return text if text not in {"", "-0"} else "0"

    def _set_formula_preview(self, text: str) -> None:
        label = getattr(self, "formula_preview_label", None)
        if label is not None:
            label.setText(str(text or ""))

    def _formula_preview_text(self, tree: FinishAssignmentList, item: QTreeWidgetItem | None, column: int) -> str:
        if item is None or int(column) != 4:
            return ""
        formula = normalize_formula_text(item.text(4))
        if not formula:
            return ""

        # 층-전체/전층은 특정 실 숫자로 고정하지 않고 변수의 의미를 한글로 보여준다.
        if self._current_room is None:
            names = {
                "A": "면적", "A0": "공제면적", "L": "둘레",
                "H": "층높이", "H1": "천정고", "W0": "W0",
            }
            converted = re.sub(
                r"\b(A0|H1|W0|A|L|H)\b",
                lambda match: names.get(match.group(1), match.group(1)),
                formula,
            )
            return f"산식 변수 확인: ({converted})"

        room = self._current_room
        data = tree._node_data(item)
        group_id = ""
        if data.get("kind") == "group":
            group_id = str(data.get("id", "") or "")
        elif data.get("kind") == "material":
            material = dict(data.get("material") or {})
            group_id = str(material.get("group_id", "") or "")
            if not group_id and item.parent() is not None:
                parent_data = tree._node_data(item.parent())
                group_id = str(parent_data.get("id", "") or "") if parent_data.get("kind") == "group" else ""

        perimeter = float(getattr(room, "perimeter_m", 0.0) or 0.0)
        if normalize_room_finish_section(getattr(tree, "section", "")) == "벽" and group_id:
            perimeter = float(self._group_wall_lengths.get(group_id, perimeter) or 0.0)
        deduction_area = float(getattr(room, "deduction_area_raw", 0.0) or 0.0) / 1_000_000.0
        values = {
            "A": float(getattr(room, "area_m2", 0.0) or 0.0),
            "A0": deduction_area,
            "L": perimeter,
            "H": float(getattr(room, "floor_height_m", 0.0) or 0.0),
            "H1": float(getattr(room, "ceiling_height_m", 0.0) or 0.0),
            "W0": 0.0,
        }
        converted = re.sub(
            r"\b(A0|H1|W0|A|L|H)\b",
            lambda match: self._formula_number(values.get(match.group(1), 0.0)),
            formula,
        )
        return f"산식 변수 확인: ({converted})"

    def _on_formula_cursor_changed(self, tree: FinishAssignmentList, item, column: int) -> None:
        self._set_formula_preview(self._formula_preview_text(tree, item, column))

    def clear_current_room(self):
        self.set_current_room(None, {})

    def set_group_catalog(self, groups, title: str = "전층 그룹 목록") -> None:
        """전층 또는 층 합계행에서 해당 범위의 마감그룹을 부위별로 표시한다."""
        self._set_formula_preview("")
        sections = list(DEFAULT_ROOM_FINISH_SECTIONS)
        items = {section: [] for section in sections}
        group_rows = []
        for raw in groups or []:
            group = dict(raw or {})
            group_id = str(group.get("id", "") or "")
            if not group_id:
                continue
            section = normalize_room_finish_section(group.get("section", "")) or "기타"
            if section not in sections:
                sections.append(section)
                items[section] = []
            name = str(group.get("name", "") or "그룹")
            color = str(group.get("color", "") or "")
            formula = str(group.get("formula", "") or "")
            group_type = normalize_finish_group_type(group.get("group_type", ""))
            group_division = normalize_finish_group_division(group.get("group_division", ""), group_type)
            self.ensure_group_division(group_type, group_division)
            group_rows.append({
                "id": group_id, "name": name, "section": section, "color": color,
                "formula": formula, "group_type": group_type, "group_division": group_division,
            })
            for material in group.get("materials", []) or []:
                value = deepcopy(material)
                value["group_id"] = group_id
                value["group_name"] = name
                value["group_color"] = color
                value["group_type"] = group_type
                value["group_division"] = group_division
                items[section].append(value)
        self._current_room = None
        self._current_handle = ""
        self._current_data = normalize_room_finish_assignment({
            "sections": sections, "items": items, "groups": group_rows,
        })
        self.current_room_label.setText(f"{title}: {len(group_rows):,}개")
        self._rebuild_sections()
        self.group_selected.emit("")

    def eventFilter(self, watched, event):
        handle_index = 0
        for index in range(1, self.section_splitter.count()):
            if watched is self.section_splitter.handle(index):
                handle_index = index
                break
        if handle_index:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._section_drag_handle_index = handle_index
                self._section_drag_last_global_y = int(event.globalPosition().y())
            elif event.type() == QEvent.MouseMove and (event.buttons() & Qt.LeftButton):
                current_y = int(event.globalPosition().y())
                if self._section_drag_handle_index != handle_index or self._section_drag_last_global_y is None:
                    self._section_drag_handle_index = handle_index
                    self._section_drag_last_global_y = current_y
                delta = current_y - int(self._section_drag_last_global_y)
                self._section_drag_last_global_y = current_y
                if delta:
                    self._prepare_section_splitter_drag(handle_index, delta)
            elif event.type() == QEvent.MouseButtonRelease:
                self._section_drag_last_global_y = None
                self._section_drag_handle_index = 0
        return super().eventFilter(watched, event)

    def _section_widget_min_height(self, index: int) -> int:
        widget = self.section_splitter.widget(index)
        if widget is None:
            return 80
        if isinstance(widget, FinishSectionWidget) and not widget.toggle.isChecked():
            return int(widget.collapsed_height())
        hint = widget.minimumSizeHint().height()
        return max(80, int(widget.minimumHeight()), int(hint if hint > 0 else 0))

    def _section_total_height(self, sizes=None) -> int:
        if sizes is None:
            sizes = list(self.section_splitter.sizes())
        handle_space = max(0, self.section_splitter.count() - 1) * int(self.section_splitter.handleWidth())
        return max(0, sum(max(0, int(value)) for value in sizes) + handle_space)

    def _set_section_splitter_height(self, splitter_height: int) -> None:
        splitter_height = max(0, int(splitter_height))
        self.section_splitter.setFixedHeight(splitter_height)
        margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        # scroll 위젯은 viewport보다 작아질 수 없지만 splitter는 상단 고정 높이이므로
        # 남는 공간이 아래로만 생긴다.
        self.section_host.setMinimumHeight(splitter_height + vertical_margins)
        self.section_host.updateGeometry()
        self.section_splitter.updateGeometry()

    def _set_section_content_min_height(self, height: int) -> None:
        # 기존 드래그 확장 코드와의 호환 래퍼. height는 host 전체 높이 기준이다.
        margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        splitter_height = max(0, int(height) - vertical_margins)
        self._set_section_splitter_height(splitter_height)

    def _prepare_section_splitter_drag(self, handle_index: int, delta: int) -> None:
        """아래로 더 늘릴 공간이 없으면 스크롤 콘텐츠 자체를 확장한다.

        handle_index=1은 첫 번째/두 번째 부위 사이 손잡이다. 아래쪽 부위들이
        최소높이에 닿기 전까지는 QSplitter 기본 동작을 그대로 쓰고, 그 이후
        드래그분만 section_host 높이에 추가해 아래 부위가 밀리도록 한다.
        """
        if not delta or self.section_splitter.count() < 2:
            return
        sizes = self.section_splitter.sizes()
        if len(sizes) < self.section_splitter.count():
            return

        if delta > 0:
            # 손잡이를 아래로 내리면 handle_index 이후의 부위들이 먼저 줄어든다.
            # 그 부위들이 더 줄어들 여유보다 드래그량이 크면 초과분만 전체 높이를 늘린다.
            lower_slack = 0
            for index in range(handle_index, self.section_splitter.count()):
                lower_slack += max(0, int(sizes[index]) - self._section_widget_min_height(index))
            grow = max(0, int(delta) - int(lower_slack))
            if grow <= 0:
                return
            margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
            vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
            current_min = int(self.section_splitter.height()) + vertical_margins
            self._section_growth_by_handle[handle_index] = int(self._section_growth_by_handle.get(handle_index, 0)) + grow
            self._set_section_content_min_height(current_min + grow)
            return

        # 같은 손잡이로 위쪽으로 되돌릴 때는 이 손잡이가 늘린 스크롤 높이부터 회수한다.
        grown = int(self._section_growth_by_handle.get(handle_index, 0))
        if grown <= 0:
            return
        shrink = min(-int(delta), grown)
        self._section_growth_by_handle[handle_index] = grown - shrink
        margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        current_min = max(int(self._section_base_min_height), int(self.section_splitter.height()) + vertical_margins - shrink)
        self._set_section_content_min_height(current_min)

    def _install_section_handle_filters(self) -> None:
        for index in range(1, self.section_splitter.count()):
            handle = self.section_splitter.handle(index)
            handle.removeEventFilter(self)
            handle.installEventFilter(self)

    def _remember_section_heights(self):
        sizes = self.section_splitter.sizes()
        for index in range(self.section_splitter.count()):
            widget = self.section_splitter.widget(index)
            section = str(getattr(widget, "section", "") or "")
            # 접힌 헤더 높이(약 20~30px)가 사용자가 잡아 둔 펼침 높이를
            # 덮어쓰지 않도록 펼쳐진 부위만 기억한다.
            if isinstance(widget, FinishSectionWidget) and not widget.toggle.isChecked():
                continue
            if section and index < len(sizes) and sizes[index] > 0:
                self._section_height_by_name[section] = int(sizes[index])

    def _clear_section_layout(self):
        self._remember_section_heights()
        while self.section_splitter.count():
            widget = self.section_splitter.widget(0)
            widget.setParent(None)
            widget.deleteLater()

    def _rebuild_sections(self):
        self._loading = True
        try:
            self._clear_section_layout()
            self.section_widgets = {}
            requested_sizes = []
            for section in self._current_data["sections"]:
                section_widget = FinishSectionWidget(
                    section, value_resolver=self._material_value,
                    group_type_label_resolver=self.group_type_label,
                )
                state_key = f"{self._active_group_type}|{section}"
                expanded = bool(self._section_expanded_state.get(state_key, False))
                section_widget.toggle.blockSignals(True)
                section_widget.toggle.setChecked(expanded)
                section_widget.toggle.blockSignals(False)
                section_widget.items.setVisible(expanded)
                section_widget._refresh_title()
                section_widget.apply_expanded_constraints(expanded)
                all_groups = list(self._current_data.get("groups", []) or [])
                groups_by_id = {str(group.get("id", "") or ""): group for group in all_groups}
                section_groups = [
                    group for group in all_groups
                    if normalize_room_finish_section(group.get("section", "")) == section
                    and normalize_finish_group_type(group.get("group_type", "")) == self._active_group_type
                ]
                section_materials = [
                    material for material in self._current_data["items"].get(section, [])
                    if self._material_group_type(material, groups_by_id) == self._active_group_type
                ]
                section_widget.set_materials(section_materials, section_groups)
                section_widget.changed.connect(self._emit_assignments)
                section_widget.expanded_changed.connect(self._on_section_expanded_changed)
                section_widget.group_create_requested.connect(
                    lambda name, multi=False: self.group_create_requested.emit(name, multi)
                )
                section_widget.group_delete_requested.connect(self.group_delete_requested.emit)
                section_widget.group_break_requested.connect(self.group_break_requested.emit)
                section_widget.group_color_requested.connect(self.group_color_requested.emit)
                section_widget.group_rename_requested.connect(self.group_rename_requested.emit)
                section_widget.group_division_requested.connect(self.group_division_requested.emit)
                section_widget.group_formula_requested.connect(self.group_formula_requested.emit)
                section_widget.material_picker_requested.connect(self._open_material_picker_for_section)
                section_widget.material_formula_requested.connect(self.material_formula_requested.emit)
                section_widget.group_section_move_requested.connect(self.group_section_move_requested.emit)
                section_widget.group_order_changed.connect(self.group_order_changed.emit)
                section_widget.items.formula_cursor_changed.connect(
                    lambda item, column, tree=section_widget.items: self._on_formula_cursor_changed(tree, item, column)
                )
                section_widget.items.currentItemChanged.connect(
                    lambda current, _previous, tree=section_widget.items: self._on_group_tree_current_changed(tree, current)
                )
                self.section_widgets[section] = section_widget
                self.section_splitter.addWidget(section_widget)
                if expanded:
                    requested_sizes.append(max(110, int(self._section_height_by_name.get(section, 150))))
                else:
                    requested_sizes.append(int(section_widget.collapsed_height()))
            if requested_sizes:
                # 재선택/새로고침으로 목록이 재구성돼도 사용자가 잡아 늘린 높이를 최대한 유지한다.
                self.section_splitter.setSizes(requested_sizes)
                for widget in self.section_widgets.values():
                    widget.apply_expanded_constraints(widget.toggle.isChecked())
                splitter_height = self._section_total_height(requested_sizes)
                layout_margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
                vertical_margins = (layout_margins.top() + layout_margins.bottom()) if layout_margins is not None else 0
                self._section_base_min_height = splitter_height + vertical_margins
                self._section_growth_by_handle = {}
                self._set_section_splitter_height(splitter_height)
                self.section_splitter.setSizes(requested_sizes)
                self._install_section_handle_filters()
            else:
                self._section_base_min_height = 0
                self._section_growth_by_handle = {}
                self.section_splitter.setMinimumHeight(0)
                self.section_splitter.setMaximumHeight(16777215)
                self.section_host.setMinimumHeight(0)
            self.add_section_button.setEnabled(bool(self._current_handle))
            self.delete_section_button.setEnabled(bool(self._current_handle) and bool(self.section_widgets))
        finally:
            self._loading = False

    def _on_group_tree_current_changed(self, tree, item):
        if self._loading:
            return
        # 다른 부위 트리에 남아 있던 selection 때문에 F5 대상 그룹이 꼬이지 않게
        # 실제로 현재 클릭한 트리만 선택상태를 유지한다.
        for widget in self.section_widgets.values():
            other = widget.items
            if other is tree:
                continue
            other.blockSignals(True)
            try:
                other.clearSelection()
                other.setCurrentItem(None)
            finally:
                other.blockSignals(False)

        data = tree._node_data(item)
        if data.get("kind") == "material" and item is not None and item.parent() is not None:
            data = tree._node_data(item.parent())
        group_id = str(data.get("id", "") or "") if data.get("kind") == "group" else ""
        self.group_selected.emit(group_id)

    def _on_section_expanded_changed(self, section: str, expanded: bool) -> None:
        """접힘/펼침 때 다른 부위 높이는 그대로 두고 전체 splitter 높이만 증감한다."""
        if self._loading:
            return
        self._section_expanded_state[f"{self._active_group_type}|{section}"] = bool(expanded)
        target_index = -1
        target_widget = None
        for index in range(self.section_splitter.count()):
            widget = self.section_splitter.widget(index)
            if str(getattr(widget, "section", "") or "") == str(section or ""):
                target_index = index
                target_widget = widget
                break
        if target_index < 0 or not isinstance(target_widget, FinishSectionWidget):
            return

        # 자식 위젯에서는 아직 높이 제약을 바꾸지 않았으므로 이 시점 sizes가
        # 접기 직전의 다른 부위 높이를 정확히 보존한다.
        sizes = list(self.section_splitter.sizes())
        if len(sizes) != self.section_splitter.count():
            return

        if expanded:
            desired = max(110, int(self._section_height_by_name.get(section, target_widget._last_expanded_height)))
            target_widget.apply_expanded_constraints(True)
            sizes[target_index] = desired
        else:
            previous = max(110, int(sizes[target_index]), int(target_widget._last_expanded_height))
            self._section_height_by_name[section] = previous
            sizes[target_index] = int(target_widget.collapsed_height())

        # 나머지 펼쳐진 부위의 현재 높이를 그대로 기억한다. 접은 부위의 남는 공간을
        # 다른 부위에 나눠주지 않고 splitter 자체 높이를 줄이기 때문에 화면이 흔들리지 않는다.
        for index, value in enumerate(sizes):
            widget = self.section_splitter.widget(index)
            if index == target_index:
                continue
            if isinstance(widget, FinishSectionWidget) and widget.toggle.isChecked():
                name = str(widget.section or "")
                if name and int(value) > 0:
                    self._section_height_by_name[name] = int(value)

        splitter_height = self._section_total_height(sizes)
        margins = self.section_host.layout().contentsMargins() if self.section_host.layout() else None
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        self._section_base_min_height = splitter_height + vertical_margins
        self._section_growth_by_handle = {}
        self._set_section_splitter_height(splitter_height)
        if not expanded:
            target_widget.apply_expanded_constraints(False)
        self.section_splitter.setSizes(sizes)
        self.section_splitter.updateGeometry()
        self.section_host.updateGeometry()

    def _open_material_picker_for_section(self, section: str) -> None:
        section = normalize_room_finish_section(section)
        if section not in self.section_widgets:
            return
        # 빈 영역 더블클릭은 기존 그룹 선택과 무관하게 해당 부위에 새 재료를 넣는 동작이다.
        for widget in self.section_widgets.values():
            widget.items.clearSelection()
            widget.items.setCurrentItem(None)
        self.group_selected.emit("")
        self.open_material_picker(section)

    def assignment_data(self) -> dict:
        """현재 보이는 타입 편집값을 나머지 4개 타입 데이터와 합쳐 반환한다."""
        sections = list(self.section_widgets)
        active_type = self.current_group_type()
        current_groups = list(self._current_data.get("groups", []) or [])
        current_groups_by_id = {str(group.get("id", "") or ""): group for group in current_groups}
        preserved_groups = [
            deepcopy(group) for group in current_groups
            if normalize_finish_group_type(group.get("group_type", "")) != active_type
        ]
        visible_groups = []
        for widget in self.section_widgets.values():
            for group in widget.items.groups():
                value = deepcopy(group)
                value["group_type"] = active_type
                visible_groups.append(value)

        items: dict[str, list[dict]] = {}
        for section, widget in self.section_widgets.items():
            preserved = [
                deepcopy(material)
                for material in self._current_data.get("items", {}).get(section, [])
                if self._material_group_type(material, current_groups_by_id) != active_type
            ]
            visible = []
            for material in widget.items.materials():
                value = deepcopy(material)
                value["group_type"] = active_type
                visible.append(value)
            items[section] = preserved + visible

        return {
            "schema_version": ROOM_FINISH_SCHEMA_VERSION,
            "sections": sections,
            "groups": preserved_groups + visible_groups,
            "items": items,
        }

    def _emit_assignments(self):
        if self._loading or not self._current_handle:
            return
        self._current_data = normalize_room_finish_assignment(self.assignment_data())
        self.assignments_changed.emit(self._current_handle, deepcopy(self._current_data))

    def add_section(self):
        if not self._current_handle:
            QMessageBox.information(self, "마감 부위추가", "먼저 현재 실을 선택해 주세요.")
            return
        name, ok = QInputDialog.getText(self, "마감 부위추가", "추가할 부위명:")
        name = normalize_room_finish_section(name)
        if not ok or not name:
            return
        if name in self.section_widgets:
            QMessageBox.information(self, "마감 부위추가", "이미 등록된 부위입니다.")
            return
        self._current_data["sections"].append(name)
        self._current_data["items"][name] = []
        self._rebuild_sections()
        self._emit_assignments()

    def delete_section(self):
        if not self._current_handle:
            QMessageBox.information(self, "마감 부위삭제", "먼저 현재 실을 선택해 주세요.")
            return
        current = self.assignment_data()
        sections = list(current.get("sections") or [])
        if not sections:
            QMessageBox.information(self, "마감 부위삭제", "삭제할 부위가 없습니다.")
            return
        if len(sections) <= 1:
            QMessageBox.information(self, "마감 부위삭제", "마지막 부위는 삭제할 수 없습니다.")
            return
        name, ok = QInputDialog.getItem(
            self,
            "마감 부위삭제",
            "삭제할 부위:",
            sections,
            0,
            False,
        )
        name = normalize_room_finish_section(name)
        if not ok or name not in sections:
            return

        materials = list(current.get("items", {}).get(name, []) or [])
        delete_materials = False
        if materials:
            answer = QMessageBox.question(
                self,
                "구분 및 포함 재료 처리",
                f"'{name}' 구분에 재료 {len(materials):,}개가 들어 있습니다.\n\n"
                "구분과 함께 포함 재료도 삭제할까요?\n\n"
                "아니오를 누르면 재료를 남아 있는 맨 아래 구분으로 이동합니다.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.No,
            )
            if answer == QMessageBox.Cancel:
                return
            delete_materials = answer == QMessageBox.Yes

        remaining = [section for section in sections if section != name]
        items = {
            section: list(current.get("items", {}).get(section, []) or [])
            for section in remaining
        }
        if materials and not delete_materials:
            destination = remaining[-1]
            items[destination].extend(deepcopy(materials))

        self._current_data = {
            "schema_version": ROOM_FINISH_SCHEMA_VERSION,
            "sections": remaining,
            "items": items,
            "groups": [
                group for group in current.get("groups", [])
                if normalize_room_finish_section(group.get("section", "")) != name
            ],
        }
        self._rebuild_sections()
        self._emit_assignments()

    def _target_section(self, material: dict) -> str:
        source = normalize_room_finish_section(
            material.get("target_category", material.get("category", ""))
        )
        if source in self.section_widgets:
            return source
        return next(reversed(self.section_widgets), "")

    def _selected_group_target(self) -> tuple[str, str, str] | None:
        for section, widget in self.section_widgets.items():
            item = widget.items.currentItem()
            data = widget.items._node_data(item)
            if data.get("kind") == "material" and item is not None and item.parent() is not None:
                data = widget.items._node_data(item.parent())
            if data.get("kind") == "group":
                return section, str(data.get("id", "") or ""), str(data.get("name", "") or "그룹")
        return None

    def register_materials(self, materials, target_section: str | None = None) -> int:
        if not self._current_handle:
            return 0
        added = 0
        target_section = normalize_room_finish_section(target_section or "")
        selected_group = None if target_section else self._selected_group_target()
        self._loading = True
        try:
            for material in materials or []:
                if not isinstance(material, dict):
                    continue
                material = deepcopy(material)
                material["group_type"] = self.current_group_type()
                if selected_group:
                    section, group_id, group_name = selected_group
                    material["group_id"] = group_id
                    material["group_name"] = group_name
                else:
                    section = target_section if target_section in self.section_widgets else self._target_section(material)
                target = self.section_widgets.get(section)
                if target is None:
                    continue
                material_id = str(material.get("id", "") or "")
                existing_ids = {
                    str(item.get("id", "") or "")
                    for item in target.items.materials()
                    if str(item.get("group_id", "") or "") == str(material.get("group_id", "") or "")
                }
                if material_id and material_id in existing_ids:
                    continue
                target.items.add_material(material)
                target._refresh_title()
                added += 1
        finally:
            self._loading = False
        if added:
            self._emit_assignments()
        return added

    def open_material_picker(self, target_section: str | None = None):
        # 그룹은 전층 공통 데이터다. 실/층/전층 어디에서 보더라도 그룹 행이
        # 선택되어 있으면 F5로 그 그룹 자체에 재료를 등록할 수 있어야 한다.
        target_section = normalize_room_finish_section(target_section or "")
        selected_group = None if target_section else self._selected_group_target()
        if not self._current_handle and not selected_group:
            QMessageBox.information(
                self, "재료마감 선택",
                "실 또는 마감그룹을 먼저 선택해 주세요."
            )
            return
        dialog = MaterialPickerDialog(
            self.data_path,
            formula_codes=self.formula_codes,
            finish_categories=self.finish_categories,
            material_trades=self.material_trades,
            material_units=self.material_units,
            parent=self,
        )
        dialog.library.materials_changed.connect(self.materials_changed.emit)
        dialog.library.material_units_changed.connect(self.material_units_changed.emit)
        dialog.library.material_delete_requested.connect(self.material_delete_requested.emit)
        if dialog.exec() != QDialog.Accepted:
            return
        expanded = self._expand_composite_materials(
            dialog.selected_materials, dialog.library._base_materials()
        )
        if selected_group and not self._current_handle:
            _section, group_id, _group_name = selected_group
            self.group_materials_add_requested.emit(group_id, expanded)
            return
        self.register_materials(expanded, target_section=target_section)

    @staticmethod
    def _expand_composite_materials(materials, base_materials) -> list[dict]:
        """복합재료 자체 대신 연결된 일반 재료 자식들을 실 마감에 등록한다."""
        base_by_id = {
            str(material.get("id", "") or ""): material
            for material in base_materials or []
        }
        expanded = []
        for material in materials or []:
            component_ids = list(material.get("component_ids") or [])
            if material.get("is_composite") or component_ids:
                expanded.extend(deepcopy(base_by_id[value]) for value in component_ids if value in base_by_id)
            else:
                expanded.append(deepcopy(material))
        return expanded
