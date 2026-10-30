from __future__ import annotations

import ast
import math
import re
import uuid
from copy import deepcopy

from PySide6.QtCore import Qt, Signal, QTimer, QPoint
from PySide6.QtGui import QKeySequence, QShortcut, QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QSplitter, QMenu, QColorDialog,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.core.paths import MATERIAL_FINISH_PATH
from app.rooms.finish_panel import MaterialPickerDialog
from app.common.material_units import normalize_material_unit


BASE_VARIABLE_NOTES = {
    "A": "가로치수",
    "B": "세로치수",
    "C": "면적값",
    "OC": "공제면적값",
    "BL": "베이스길이",
    "K": "문틀 도장배수",
    "K1": "문틀+문짝 도장배수",
}


def _number(value, default=0.0) -> float:
    try:
        text = str(value or "").strip().replace(",", "")
        return float(text) if text else float(default)
    except Exception:
        return float(default)


def _fmt(value, digits=3) -> str:
    try:
        number = float(value)
    except Exception:
        return ""
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _floor_key(value: str):
    text = str(value or "").strip().upper().replace(" ", "")
    m = re.fullmatch(r"B(\d+)F?", text)
    if m:
        return (0, -int(m.group(1)), text)
    m = re.fullmatch(r"(\d+)F?", text)
    if m:
        return (1, int(m.group(1)), text)
    if text in {"R", "RF"}:
        return (2, 0, text)
    m = re.fullmatch(r"PH(\d*)", text)
    if m:
        return (3, int(m.group(1) or 0), text)
    return (8, 0, text)


def safe_eval_formula(formula: str, variables: dict[str, float]) -> float | None:
    text = str(formula or "").strip().upper().replace("^", "**")
    if not text:
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except Exception:
        return None

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return float(variables.get(node.id.upper(), 0.0))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right if abs(right) > 1e-12 else 0.0
            if abs(right) > 10:
                raise ValueError
            return left ** right
        raise ValueError

    try:
        value = float(visit(tree))
        return value if math.isfinite(value) else None
    except Exception:
        return None


class SingleCharChoiceLineEdit(QLineEdit):
    """D/W처럼 1글자 선택값을 직접 입력할 때 기존 문자를 누적하지 않는다."""

    def __init__(self, allowed_values, parent=None):
        super().__init__(parent)
        self.allowed_values = {str(value).upper() for value in (allowed_values or []) if str(value)}
        self.setMaxLength(1)

    def keyPressEvent(self, event):
        text = str(event.text() or "").upper()
        if text in self.allowed_values:
            # 기존 W에서 D를 눌러도 WD가 되지 않고 즉시 D 한 글자로 교체한다.
            self.setText(text)
            self.selectAll()
            return
        if event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            return
        if text and text.isprintable():
            return
        super().keyPressEvent(event)


class SimpleComboDelegate(QStyledItemDelegate):
    def __init__(self, values_provider, editable=False, parent=None, single_char=False):
        super().__init__(parent)
        self.values_provider = values_provider
        self.editable = bool(editable)
        self.single_char = bool(single_char)

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        editor.setEditable(self.editable)
        values = self.values_provider() if callable(self.values_provider) else self.values_provider
        values = [str(value) for value in values or []]
        if self.editable and self.single_char:
            editor.setLineEdit(SingleCharChoiceLineEdit(values, editor))
        editor.addItems(values)
        return editor

    def setEditorData(self, editor, index):
        text = str(index.data(Qt.EditRole) or "")
        found = editor.findText(text)
        if found >= 0:
            editor.setCurrentIndex(found)
        elif editor.isEditable():
            editor.setEditText(text)
        if editor.isEditable() and editor.lineEdit() is not None:
            QTimer.singleShot(0, editor.lineEdit().selectAll)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText().strip(), Qt.EditRole)


class CopyPasteTable(QTableWidget):
    """복사/붙여넣기 + 공통 리스트 키동작을 제공하는 표.

    F9/F3/마지막행 ↓ 동작은 각 표가 연결한 signal로 실제 모델을 수정한다.
    Enter는 모델 갱신/재구성 뒤에도 현재 셀을 유지하도록 보조한다.
    F4 삭제는 확인 후 실행하며, ↓로 만든 마지막 빈 행에서는 빈 행을 연속 생성하지 않는다.
    """
    insert_above_requested = Signal(int)
    duplicate_row_requested = Signal(int)
    append_row_requested = Signal()
    delete_requested = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._down_append_guard = False
        self.itemChanged.connect(lambda _item: setattr(self, "_down_append_guard", False))

    def _last_row_is_effectively_blank(self) -> bool:
        if self.rowCount() <= 0:
            return True
        row = self.rowCount() - 1
        # 자동 계산 0값과 창호 신규행 기본 W는 '사용자가 입력한 내용'으로 보지 않는다.
        ignorable = {"", "0", "0.0", "0.00", "0.000", "W"}
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is None:
                continue
            text = str(item.text() or "").strip().upper()
            if text not in ignorable:
                return False
        return True

    def _confirm_f4_delete(self) -> bool:
        rows = {index.row() for index in self.selectionModel().selectedRows()}
        if not rows and self.currentRow() >= 0:
            rows = {self.currentRow()}
        count = max(1, len(rows))
        text = "현재 행을 삭제하시겠습니까?" if count == 1 else f"선택한 {count}개 행을 삭제하시겠습니까?"
        return QMessageBox.question(self, "목록 삭제", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes

    def _restore_current_later(self, row: int, col: int):
        def restore():
            if self.rowCount() <= 0 or self.columnCount() <= 0:
                return
            r = max(0, min(int(row), self.rowCount() - 1))
            c = max(0, min(int(col), self.columnCount() - 1))
            self.setCurrentCell(r, c)
        QTimer.singleShot(0, restore)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            ranges = self.selectedRanges()
            if ranges:
                area = ranges[0]
                lines = []
                for row in range(area.topRow(), area.bottomRow() + 1):
                    lines.append("\t".join(
                        self.item(row, col).text() if self.item(row, col) else ""
                        for col in range(area.leftColumn(), area.rightColumn() + 1)
                    ))
                QApplication.clipboard().setText("\n".join(lines))
            return
        if event.matches(QKeySequence.Paste):
            text = QApplication.clipboard().text()
            if text:
                sr = max(0, self.currentRow())
                sc = max(0, self.currentColumn())
                for roff, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
                    for coff, value in enumerate(line.split("\t")):
                        row, col = sr + roff, sc + coff
                        if row < self.rowCount() and col < self.columnCount():
                            item = self.item(row, col)
                            if item is not None and item.flags() & Qt.ItemIsEditable:
                                item.setText(value)
            return
        row, col = self.currentRow(), self.currentColumn()
        if event.key() == Qt.Key_F9 and row >= 0:
            self.insert_above_requested.emit(row)
            return
        if event.key() == Qt.Key_F3 and row >= 0:
            self.duplicate_row_requested.emit(row)
            return
        if event.key() == Qt.Key_F4 and row >= 0:
            if self._confirm_f4_delete():
                self._down_append_guard = False
                self.delete_requested.emit()
            return
        if event.key() == Qt.Key_Down and row >= 0 and row == self.rowCount() - 1:
            if self._last_row_is_effectively_blank():
                return
            self.append_row_requested.emit()
            self._down_append_guard = self._last_row_is_effectively_blank()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            super().keyPressEvent(event)
            if row >= 0 and col >= 0:
                self._restore_current_later(row, col)
            return
        super().keyPressEvent(event)


class MaterialGroupAssignDialog(QDialog):
    def __init__(self, sources: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("창호 산출 그룹지정")
        self.resize(440, 330)
        self.action = ""
        self.selected_source: tuple[str, str] | None = None
        root = QVBoxLayout(self)
        root.addWidget(QLabel("다른 창호에서 만든 그룹을 선택된 창호들에 복사하거나, 선택 그룹을 삭제합니다."))
        self.list = QListWidget()
        for code, group in sources:
            item_text = f"{code}  /  {group}"
            self.list.addItem(item_text)
            self.list.item(self.list.count()-1).setData(Qt.UserRole, [code, group])
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton("선택 창호에 복사")
        delete_btn = QPushButton("선택 창호에서 삭제")
        cancel_btn = QPushButton("취소")
        row.addWidget(copy_btn); row.addWidget(delete_btn); row.addStretch(1); row.addWidget(cancel_btn)
        root.addLayout(row)
        copy_btn.clicked.connect(lambda: self._finish("copy"))
        delete_btn.clicked.connect(lambda: self._finish("delete"))
        cancel_btn.clicked.connect(self.reject)

    def _finish(self, action: str):
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(self, "그룹지정", "그룹을 먼저 선택해 주세요.")
            return
        data = item.data(Qt.UserRole)
        if not isinstance(data, (list, tuple)) or len(data) < 2:
            return
        self.action = action
        self.selected_source = (str(data[0]), str(data[1]))
        self.accept()


class FloorWindowScheduleDialog(QDialog):
    """창호별 층 개소 입력용 별도창."""
    def __init__(self, owner: "WindowTab"):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("층별 창호리스트")
        self.resize(1120, 650)
        self.setModal(False)
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("창호별 층 사용 개수를 입력합니다. 개요 층수를 기본으로 하고 평면의 특수층을 추가합니다."))
        top.addStretch(1)
        self.read_plan_btn = QPushButton("평면 창호 읽기")
        self.refresh_btn = QPushButton("층 새로고침")
        top.addWidget(self.read_plan_btn)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)
        self.table = CopyPasteTable()
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_btn = QPushButton("닫기")
        bottom.addWidget(close_btn)
        root.addLayout(bottom)
        close_btn.clicked.connect(self.close)
        self.refresh_btn.clicked.connect(self.refresh)
        self.read_plan_btn.clicked.connect(self.owner.read_plan_opening_counts)
        self.refresh()

    def _on_item_changed(self, item):
        self.owner._on_floor_item_changed(item)

    def refresh(self):
        self.owner._populate_floor_table(self.table)


class WindowTab(QWidget):
    data_changed = Signal()
    catalog_changed = Signal()
    material_units_changed = Signal(object)
    materials_changed = Signal()

    MASTER_HEADERS = ["구분", "창호명", "가로", "세로", "면적", "공제면적", "BASE 길이", "D/W", "비고", "공종코드", "내/외"]
    MATERIAL_HEADERS = ["그룹", "품명", "규격", "단위", "산식", "값"]
    VARIABLE_HEADERS = ["변수", "산식", "값", "비고"]

    def __init__(self, categories, material_trades, material_units, recent_settings=None, rooms_provider=None, overview_provider=None, opening_provider=None, parent=None):
        super().__init__(parent)
        self.categories = list(categories or [])
        self.material_trades = list(material_trades or [])
        self.material_units = list(material_units or [])
        self.recent_settings = recent_settings
        self.rooms_provider = rooms_provider or (lambda: [])
        self.overview_provider = overview_provider or (lambda: {})
        self.opening_provider = opening_provider or (lambda: {})
        self.windows: list[dict] = []
        self.floor_counts: dict[str, dict[str, int]] = {}
        self._loading = False
        self._formula_syncing = False
        self._current_window_id = ""
        self._floor_dialog = None
        # 자동 이름정렬 목록에서도 F9 신규행이 현재행 바로 위에 보이도록 입력 중 위치만 임시 보존한다.
        self._pending_master_positions: dict[str, str | None] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        schedule_row = QHBoxLayout()
        self.floor_schedule_btn = QPushButton("층별 창호리스트")
        self.floor_schedule_btn.setToolTip("층별 창호 개소 입력창을 엽니다.")
        schedule_row.addWidget(self.floor_schedule_btn)
        schedule_row.addStretch(1)
        root.addLayout(schedule_row)

        main_split = QSplitter(Qt.Vertical)
        middle = QWidget()
        middle_root = QHBoxLayout(middle)
        middle_root.setContentsMargins(0, 0, 0, 0)
        middle_split = QSplitter(Qt.Horizontal)

        master_box = QGroupBox("창호 리스트")
        master_root = QVBoxLayout(master_box)
        master_buttons = QHBoxLayout()
        self.add_window_btn = QPushButton("추가")
        self.delete_window_btn = QPushButton("삭제")
        master_buttons.addWidget(self.add_window_btn)
        master_buttons.addWidget(self.delete_window_btn)
        master_buttons.addWidget(QLabel("※ 창호명 기준 자동 정렬 · 열 머리글 드래그로 순서 변경"))
        master_buttons.addStretch(1)
        master_root.addLayout(master_buttons)
        self.master_table = CopyPasteTable(0, len(self.MASTER_HEADERS))
        self.master_table.setHorizontalHeaderLabels(self.MASTER_HEADERS)
        self.master_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.master_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.master_table.verticalHeader().setVisible(False)
        self.master_table.verticalHeader().setDefaultSectionSize(19)
        header = self.master_table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.master_table.setItemDelegateForColumn(0, SimpleComboDelegate(lambda: [""] + list(self.categories or []), False, self.master_table))
        self.master_table.setItemDelegateForColumn(7, SimpleComboDelegate(lambda: ["D", "W"], True, self.master_table, single_char=True))
        self.master_table.setItemDelegateForColumn(9, SimpleComboDelegate(lambda: [str(row.get("code") or "") for row in self.material_trades if str(row.get("code") or "")], True, self.master_table))
        self.master_table.setItemDelegateForColumn(10, SimpleComboDelegate(lambda: ["내", "외"], False, self.master_table))
        self.master_table.itemChanged.connect(self._on_master_item_changed)
        self.master_table.itemSelectionChanged.connect(self._on_master_selection_changed)
        header.sectionMoved.connect(lambda *_: self._save_master_layout())
        header.sectionResized.connect(lambda *_: self._save_master_layout())
        master_root.addWidget(self.master_table)
        middle_split.addWidget(master_box)

        var_box = QGroupBox("변수 리스트")
        var_root = QVBoxLayout(var_box)
        var_btns = QHBoxLayout()
        self.add_var_btn = QPushButton("변수 추가")
        self.delete_var_btn = QPushButton("변수 삭제")
        var_btns.addWidget(self.add_var_btn); var_btns.addWidget(self.delete_var_btn); var_btns.addStretch(1)
        var_root.addLayout(var_btns)
        self.variable_table = CopyPasteTable(0, len(self.VARIABLE_HEADERS))
        self.variable_table.setHorizontalHeaderLabels(self.VARIABLE_HEADERS)
        self.variable_table.verticalHeader().setVisible(False)
        self.variable_table.verticalHeader().setDefaultSectionSize(19)
        var_header = self.variable_table.horizontalHeader()
        var_header.setSectionResizeMode(QHeaderView.Interactive)
        self.variable_table.setColumnWidth(0, 80)
        self.variable_table.setColumnWidth(1, 160)
        self.variable_table.setColumnWidth(2, 80)
        var_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.variable_table.itemChanged.connect(self._on_variable_item_changed)
        var_root.addWidget(self.variable_table)
        var_box.setMinimumWidth(330)
        middle_split.addWidget(var_box)
        middle_split.setStretchFactor(0, 5)
        middle_split.setStretchFactor(1, 2)
        middle_root.addWidget(middle_split)
        main_split.addWidget(middle)

        material_box = QGroupBox("창호 산출")
        material_root = QVBoxLayout(material_box)
        material_buttons = QHBoxLayout()
        self.material_f5_btn = QPushButton("재료 불러오기 (F5)")
        self.material_delete_btn = QPushButton("재료 삭제")
        self.group_assign_btn = QPushButton("그룹지정")
        self.group_formula_apply_btn = QPushButton("그룹산식 적용")
        material_buttons.addWidget(self.material_f5_btn)
        material_buttons.addWidget(self.material_delete_btn)
        material_buttons.addWidget(self.group_assign_btn)
        material_buttons.addWidget(self.group_formula_apply_btn)
        material_buttons.addStretch(1)
        material_buttons.addWidget(QLabel("그룹 산식 전파는 그룹산식 적용 버튼으로 수동 실행합니다."))
        material_root.addLayout(material_buttons)
        self.material_table = CopyPasteTable(0, len(self.MATERIAL_HEADERS))
        self.material_table.setHorizontalHeaderLabels(self.MATERIAL_HEADERS)
        self.material_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.material_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.material_table.verticalHeader().setVisible(False)
        self.material_table.verticalHeader().setDefaultSectionSize(19)
        self.material_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.material_table.setColumnWidth(0, 150)
        self.material_table.setColumnWidth(1, 170)
        self.material_table.setColumnWidth(2, 150)
        self.material_table.setColumnWidth(3, 70)
        self.material_table.setColumnWidth(4, 920)
        self.material_table.setColumnWidth(5, 90)
        self.material_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.material_table.customContextMenuRequested.connect(self._open_material_context_menu)
        self.material_table.cellDoubleClicked.connect(self._on_material_cell_double_clicked)
        self.material_table.itemChanged.connect(self._on_material_item_changed)
        material_root.addWidget(self.material_table)
        main_split.addWidget(material_box)
        main_split.setStretchFactor(0, 3)
        main_split.setStretchFactor(1, 2)
        root.addWidget(main_split, 1)

        self.add_window_btn.clicked.connect(self.add_window)
        self.delete_window_btn.clicked.connect(self.delete_selected_windows)
        self.add_var_btn.clicked.connect(self.add_variable)
        self.delete_var_btn.clicked.connect(self.delete_selected_variables)
        self.material_f5_btn.clicked.connect(self.open_material_picker)
        self.material_delete_btn.clicked.connect(self.delete_selected_materials)
        self.group_assign_btn.clicked.connect(self.open_group_assign_dialog)
        self.group_formula_apply_btn.clicked.connect(self.apply_selected_group_formulas)
        self.floor_schedule_btn.clicked.connect(self.open_floor_schedule_dialog)
        self.material_picker_shortcut = QShortcut(QKeySequence(Qt.Key_F5), self)
        self.material_picker_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.material_picker_shortcut.activated.connect(self.open_material_picker)
        self.master_table.insert_above_requested.connect(self._insert_master_above)
        self.master_table.duplicate_row_requested.connect(self._duplicate_master_row)
        self.master_table.append_row_requested.connect(lambda: self._insert_master_above(self.master_table.rowCount()))
        self.master_table.delete_requested.connect(self.delete_selected_windows)
        self.variable_table.insert_above_requested.connect(self._insert_variable_above)
        self.variable_table.duplicate_row_requested.connect(self._duplicate_variable_row)
        self.variable_table.append_row_requested.connect(lambda: self._insert_variable_above(self.variable_table.rowCount()))
        self.variable_table.delete_requested.connect(self.delete_selected_variables)
        self.material_table.insert_above_requested.connect(self._insert_material_above)
        self.material_table.duplicate_row_requested.connect(self._duplicate_material_row)
        self.material_table.append_row_requested.connect(lambda: self._insert_material_above(self.material_table.rowCount()))
        self.material_table.delete_requested.connect(self.delete_selected_materials)
        self._restore_master_layout()
        self.refresh_all()

    def set_calculation_settings(self, categories=None, material_trades=None, material_units=None):
        if categories is not None:
            self.categories = list(categories or [])
        if material_trades is not None:
            self.material_trades = list(material_trades or [])
        if material_units is not None:
            self.material_units = list(material_units or [])
        self.refresh_all()

    def _new_window(self) -> dict:
        return {
            "id": uuid.uuid4().hex,
            "category": "",
            "code": "",
            "width": 0.0,
            "height": 0.0,
            "area": 0.0,
            "deduction_area": 0.0,
            "deduction_manual": False,
            "base_length": 0.0,
            "base_manual": False,
            "dw": "W",
            "dw_manual": False,
            "note": "",
            "trade_code": "",
            "inout": "",
            "variables": [
                {"code": "K", "formula": "0.5", "note": BASE_VARIABLE_NOTES["K"]},
                {"code": "K1", "formula": "2.5", "note": BASE_VARIABLE_NOTES["K1"]},
            ],
            "materials": [],
            "material_groups": [],
        }

    def _normalize_window(self, raw: dict) -> dict:
        value = self._new_window()
        value.update(dict(raw or {}))
        value["id"] = str(value.get("id") or uuid.uuid4().hex)
        value["category"] = str(value.get("category") or "")
        value["code"] = str(value.get("code") or "").strip().upper()
        for key in ("width", "height", "area", "deduction_area", "base_length"):
            value[key] = _number(value.get(key))
        value["dw"] = str(value.get("dw") or "W").strip().upper() or "W"
        value["dw_manual"] = bool(value.get("dw_manual", True if "dw" in dict(raw or {}) else False))
        value["inout"] = str(value.get("inout") or "").strip()
        value["trade_code"] = str(value.get("trade_code") or "").strip().upper()
        variables = []
        seen = set()
        for row in list(value.get("variables") or []):
            code = re.sub(r"[^A-Z0-9]", "", str((row or {}).get("code") or "").upper())
            if not re.fullmatch(r"[A-Z][A-Z0-9]*", code or "") or code in {"A", "B", "C", "OC", "BL"} or code in seen:
                continue
            seen.add(code)
            variables.append({"code": code, "formula": str((row or {}).get("formula") or "").upper(), "note": str((row or {}).get("note") or "")})
        if "K" not in seen:
            variables.append({"code": "K", "formula": "0.5", "note": BASE_VARIABLE_NOTES["K"]})
        if "K1" not in seen:
            variables.append({"code": "K1", "formula": "2.5", "note": BASE_VARIABLE_NOTES["K1"]})
        value["variables"] = variables
        materials = []
        for row in list(value.get("materials") or []):
            item = dict(row or {})
            item["group"] = str(item.get("group") or "그룹1")
            item["name"] = str(item.get("name") or "")
            item["spec"] = str(item.get("spec") or "")
            item["unit"] = normalize_material_unit(item.get("unit", ""))
            item["formula"] = str(item.get("formula", item.get("method", "")) or "").upper()
            materials.append(item)
        groups = []
        known_ids = set()
        for raw_group in list(value.get("material_groups") or []):
            group = dict(raw_group or {})
            gid = str(group.get("id") or uuid.uuid4().hex)
            if gid in known_ids:
                gid = uuid.uuid4().hex
            known_ids.add(gid)
            groups.append({
                "id": gid, "link_id": str(group.get("link_id") or gid),
                "name": str(group.get("name") or "그룹"),
                "color": str(group.get("color") or "#E8F5E9"),
                "collapsed": bool(group.get("collapsed", False)),
            })
        # 구버전 flat group 문자열을 그룹 객체로 승격한다.
        by_name = {str(g.get("name") or ""): g for g in groups}
        for material in materials:
            gid = str(material.get("group_id") or "")
            group_name = str(material.get("group") or "그룹1")
            group = next((g for g in groups if str(g.get("id")) == gid), None) if gid else None
            if group is None:
                group = by_name.get(group_name)
            if group is None:
                gid = uuid.uuid4().hex
                group = {"id": gid, "link_id": gid, "name": group_name or "그룹1", "color": "#E8F5E9", "collapsed": False}
                groups.append(group); by_name[str(group["name"])] = group
            material["group_id"] = str(group.get("id"))
            material["group"] = str(group.get("name") or "그룹1")
        value["materials"] = materials
        value["material_groups"] = groups
        self._recalculate_window(value)
        return value

    def _recalculate_window(self, window: dict):
        old_area = _number(window.get("area"))
        area = _number(window.get("width")) * _number(window.get("height"))
        window["area"] = area
        if not bool(window.get("deduction_manual")) or (abs(_number(window.get("deduction_area")) - old_area) < 1e-9):
            window["deduction_area"] = area
        if not bool(window.get("base_manual")):
            if str(window.get("dw") or "").upper() == "D":
                window["base_length"] = _number(window.get("width"))
            else:
                window["base_length"] = 0.0

    def window_catalog(self) -> list[dict]:
        rows = []
        for window in sorted(self.windows, key=lambda row: str(row.get("code") or "").casefold()):
            code = str(window.get("code") or "").strip().upper()
            if not code:
                continue
            width = _number(window.get("width"))
            rows.append({
                "id": window.get("id"), "category": window.get("category", ""), "code": code,
                "width": width, "height": _number(window.get("height")), "area": _number(window.get("area")),
                "deduction_area": _number(window.get("deduction_area")), "base_length": _number(window.get("base_length")),
                "dw": window.get("dw", ""), "note": window.get("note", ""), "trade_code": window.get("trade_code", ""),
                "inout": window.get("inout", ""), "applied_length_mm": width * 1000.0 if width > 0 else 0.0,
            })
        return rows

    def catalog_by_code(self) -> dict[str, dict]:
        return {str(row.get("code") or "").upper(): row for row in self.window_catalog()}

    def data(self) -> dict:
        self._commit_current_tables()
        return {"windows": deepcopy(self.windows), "floor_counts": deepcopy(self.floor_counts)}

    def set_data(self, payload):
        payload = dict(payload or {}) if isinstance(payload, dict) else {}
        self.windows = [self._normalize_window(row) for row in list(payload.get("windows") or [])]
        self.floor_counts = {
            str(code).upper(): {str(floor): max(0, int(_number(count))) for floor, count in dict(values or {}).items()}
            for code, values in dict(payload.get("floor_counts") or {}).items()
        }
        self._current_window_id = ""
        self._pending_master_positions.clear()
        if self._floor_dialog is not None:
            self._floor_dialog.close()
            self._floor_dialog = None
        self.refresh_all()

    def refresh_all(self):
        self._loading = True
        try:
            self._refresh_master()
            self.refresh_floor_schedule()
            self._refresh_variable_table()
            self._refresh_material_table()
        finally:
            self._loading = False

    def _refresh_master(self, select_id: str = ""):
        select_id = select_id or self._current_window_id
        ordered = sorted(self.windows, key=lambda row: (str(row.get("code") or "").casefold(), str(row.get("category") or "")))
        # F9/마지막행↓로 만든 빈 행은 창호명이 입력되기 전까지만 요청 위치를 유지한다.
        for pending_id, before_id in list(self._pending_master_positions.items()):
            pending = next((w for w in ordered if str(w.get("id") or "") == pending_id), None)
            if pending is None or str(pending.get("code") or "").strip():
                self._pending_master_positions.pop(pending_id, None)
                continue
            ordered.remove(pending)
            if before_id:
                pos = next((i for i,w in enumerate(ordered) if str(w.get("id") or "") == before_id), len(ordered))
                ordered.insert(pos, pending)
            else:
                ordered.append(pending)
        self.windows[:] = ordered
        self.master_table.setRowCount(0)
        selected_row = -1
        for window in self.windows:
            row = self.master_table.rowCount()
            self.master_table.insertRow(row)
            values = [
                window.get("category", ""), window.get("code", ""), _fmt(window.get("width")), _fmt(window.get("height")),
                _fmt(window.get("area")), _fmt(window.get("deduction_area")), _fmt(window.get("base_length")),
                window.get("dw", ""), window.get("note", ""), window.get("trade_code", ""), window.get("inout", ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, window.get("id"))
                if col == 4:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.master_table.setItem(row, col, item)
            if str(window.get("id")) == select_id:
                selected_row = row
        if selected_row >= 0:
            self.master_table.selectRow(selected_row)
            self.master_table.setCurrentCell(selected_row, 1)
        elif self.master_table.rowCount() and not self._current_window_id:
            self.master_table.selectRow(0)
            self.master_table.setCurrentCell(0, 1)
            self._current_window_id = str(self.master_table.item(0, 0).data(Qt.UserRole) or "")

    def _window_by_id(self, window_id: str) -> dict | None:
        return next((row for row in self.windows if str(row.get("id")) == str(window_id)), None)

    def _current_window(self) -> dict | None:
        return self._window_by_id(self._current_window_id)

    def _on_master_selection_changed(self):
        rows = sorted({index.row() for index in self.master_table.selectionModel().selectedRows()})
        if not rows:
            return
        item = self.master_table.item(rows[0], 0)
        self._current_window_id = str(item.data(Qt.UserRole) or "") if item else ""
        if not self._loading:
            self._loading = True
            try:
                self._refresh_variable_table()
                self._refresh_material_table()
            finally:
                self._loading = False

    def _on_master_item_changed(self, item):
        if self._loading:
            return
        window_id = str(item.data(Qt.UserRole) or "")
        window = self._window_by_id(window_id)
        if window is None:
            return
        col = item.column()
        text = str(item.text() or "").strip()
        old_code = str(window.get("code") or "").upper()
        old_dw = str(window.get("dw") or "W").upper()
        old_trade = str(window.get("trade_code") or "").upper()
        if col == 0:
            window["category"] = text
        elif col == 1:
            code = text.upper()
            if code and any(str(row.get("code") or "").upper() == code and row is not window for row in self.windows):
                QMessageBox.warning(self, "창호명", f"이미 등록된 창호명입니다: {code}")
                self.refresh_all(); return
            window["code"] = code
            if code:
                self._pending_master_positions.pop(window_id, None)
            if not bool(window.get("dw_manual")):
                window["dw"] = "D" if re.match(r"^(SSD|FSD)", code) else "W"
            if old_code and code and old_code != code and old_code in self.floor_counts:
                self.floor_counts[code] = self.floor_counts.pop(old_code)
        elif col == 2:
            window["width"] = max(0.0, _number(text))
        elif col == 3:
            window["height"] = max(0.0, _number(text))
        elif col == 5:
            window["deduction_area"] = max(0.0, _number(text))
            window["deduction_manual"] = True
        elif col == 6:
            window["base_length"] = max(0.0, _number(text))
            window["base_manual"] = True
        elif col == 7:
            raw_dw = text.upper()
            # 편집기/붙여넣기 등 어떤 경로에서도 D/W는 반드시 한 글자만 남긴다.
            # 기존 W에 D를 입력해 WD가 들어오면 마지막 입력 D를, DW면 W를 사용한다.
            valid_chars = [ch for ch in raw_dw if ch in {"D", "W"}]
            if not valid_chars:
                QMessageBox.warning(self, "D/W", "D/W는 D(도어) 또는 W(창) 한 글자만 입력할 수 있습니다.")
                window["dw"] = old_dw
                self._loading = True; self._refresh_master(window_id); self._loading = False
                return
            window["dw"] = valid_chars[-1]
            window["dw_manual"] = True
        elif col == 8:
            window["note"] = text
        elif col == 9:
            trade = text.upper()
            valid_codes = {str(row.get("code") or "").strip().upper() for row in self.material_trades if str(row.get("code") or "").strip()}
            if trade and trade not in valid_codes:
                QMessageBox.warning(self, "공종코드", "설정 > 재료의 공종 리스트에 등록된 코드만 입력할 수 있습니다.")
                window["trade_code"] = old_trade
                self._loading = True; self._refresh_master(window_id); self._loading = False
                return
            window["trade_code"] = trade
        elif col == 10:
            window["inout"] = text
        self._recalculate_window(window)
        self._current_window_id = window_id
        keep_col = col
        # 창호명/구분은 자동 정렬 때문에 전체 목록 재구성이 필요하지만,
        # 수치/속성 수정은 현재 행만 갱신해서 편집 포커스가 날아가지 않게 한다.
        self._loading = True
        try:
            if col in (0, 1):
                self._refresh_master(window_id)
            else:
                keep_row = next((r for r in range(self.master_table.rowCount())
                                 if self.master_table.item(r, 0) is not None
                                 and str(self.master_table.item(r, 0).data(Qt.UserRole) or "") == window_id), -1)
                if keep_row >= 0:
                    values = [
                        window.get("category", ""), window.get("code", ""), _fmt(window.get("width")), _fmt(window.get("height")),
                        _fmt(window.get("area")), _fmt(window.get("deduction_area")), _fmt(window.get("base_length")),
                        window.get("dw", ""), window.get("note", ""), window.get("trade_code", ""), window.get("inout", ""),
                    ]
                    self.master_table.blockSignals(True)
                    try:
                        for c, value in enumerate(values):
                            cell = self.master_table.item(keep_row, c)
                            if cell is not None and cell.text() != str(value):
                                cell.setText(str(value))
                    finally:
                        self.master_table.blockSignals(False)
            self._refresh_variable_table()
            self._refresh_material_table()
            self.refresh_floor_schedule()
        finally:
            self._loading = False

        def restore_focus():
            keep_row = next((r for r in range(self.master_table.rowCount())
                             if self.master_table.item(r, 0) is not None
                             and str(self.master_table.item(r, 0).data(Qt.UserRole) or "") == window_id), -1)
            if keep_row >= 0:
                self.master_table.setCurrentCell(keep_row, max(0, min(keep_col, self.master_table.columnCount() - 1)))
                self.master_table.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(0, restore_focus)
        self.data_changed.emit(); self.catalog_changed.emit()

    def add_window(self):
        window = self._new_window()
        self.windows.append(window)
        self._current_window_id = window["id"]
        self.refresh_all()
        row = next((r for r in range(self.master_table.rowCount()) if str(self.master_table.item(r,0).data(Qt.UserRole)) == window["id"]), -1)
        if row >= 0:
            self.master_table.setCurrentCell(row, 1)
            self.master_table.editItem(self.master_table.item(row, 1))
        self.data_changed.emit(); self.catalog_changed.emit()

    def _selected_window_ids(self) -> list[str]:
        ids = []
        for index in self.master_table.selectionModel().selectedRows():
            item = self.master_table.item(index.row(), 0)
            value = str(item.data(Qt.UserRole) or "") if item else ""
            if value and value not in ids:
                ids.append(value)
        return ids

    def delete_selected_windows(self):
        ids = set(self._selected_window_ids())
        if not ids:
            return
        codes = {str(row.get("code") or "").upper() for row in self.windows if str(row.get("id")) in ids}
        self.windows = [row for row in self.windows if str(row.get("id")) not in ids]
        for code in codes:
            self.floor_counts.pop(code, None)
        self._current_window_id = ""
        self.refresh_all()
        self.data_changed.emit(); self.catalog_changed.emit()

    def _system_variables(self, window: dict) -> dict[str, float]:
        return {
            "A": _number(window.get("width")), "B": _number(window.get("height")),
            "C": _number(window.get("area")), "OC": _number(window.get("deduction_area")),
            "BL": _number(window.get("base_length")),
        }

    def _computed_variables(self, window: dict) -> dict[str, float]:
        variables = self._system_variables(window)
        pending = [dict(row or {}) for row in list(window.get("variables") or [])]
        for _ in range(max(2, len(pending) + 1)):
            changed = False
            for row in pending:
                code = str(row.get("code") or "").upper()
                value = safe_eval_formula(row.get("formula", ""), variables)
                if code and value is not None and variables.get(code) != value:
                    variables[code] = value; changed = True
            if not changed:
                break
        return variables

    def _refresh_variable_table(self):
        self.variable_table.setRowCount(0)
        window = self._current_window()
        if window is None:
            return
        variables = self._computed_variables(window)
        system = self._system_variables(window)
        rows = [
            {"code": code, "formula": _fmt(system[code]), "value": system[code], "note": BASE_VARIABLE_NOTES[code], "system": True}
            for code in ("A", "B", "C", "OC", "BL")
        ]
        for row in list(window.get("variables") or []):
            code = str(row.get("code") or "").upper()
            rows.append({"code": code, "formula": str(row.get("formula") or ""), "value": variables.get(code, 0.0), "note": str(row.get("note") or BASE_VARIABLE_NOTES.get(code, "")), "system": False})
        for row_data in rows:
            row = self.variable_table.rowCount(); self.variable_table.insertRow(row)
            vals = [row_data["code"], row_data["formula"], _fmt(row_data["value"]), row_data["note"]]
            for col, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, bool(row_data["system"]))
                if row_data["system"] or col == 2:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.variable_table.setItem(row, col, item)

    def _on_variable_item_changed(self, item):
        if self._loading:
            return
        window = self._current_window()
        if window is None or bool(item.data(Qt.UserRole)):
            return
        row = item.row()
        if row < 5:
            return
        idx = row - 5
        variables = list(window.get("variables") or [])
        if not (0 <= idx < len(variables)):
            return
        entry = variables[idx]
        if item.column() == 0:
            code = re.sub(r"[^A-Z0-9]", "", str(item.text() or "").upper())
            if not re.fullmatch(r"[A-Z][A-Z0-9]*", code or "") or code in {"A", "B", "C", "OC", "BL"}:
                QMessageBox.warning(self, "변수", "변수 코드는 영문 대문자로 시작하는 영문+숫자 조합으로 입력하고 A/B/C/OC/BL은 사용할 수 없습니다.")
                self._loading = True; self._refresh_variable_table(); self._loading = False; return
            if any(i != idx and str(row.get("code") or "").upper() == code for i, row in enumerate(variables)):
                QMessageBox.warning(self, "변수", f"이미 등록된 변수 코드입니다: {code}")
                self._loading = True; self._refresh_variable_table(); self._loading = False; return
            entry["code"] = code
        elif item.column() == 1:
            entry["formula"] = str(item.text() or "").upper()
        elif item.column() == 3:
            entry["note"] = str(item.text() or "")
        window["variables"] = variables
        keep_row, keep_col = item.row(), item.column()
        self._loading = True
        try:
            self._refresh_variable_table(); self._refresh_material_table()
            if 0 <= keep_row < self.variable_table.rowCount():
                self.variable_table.setCurrentCell(keep_row, keep_col)
        finally:
            self._loading = False
        self.data_changed.emit()

    def add_variable(self):
        window = self._current_window()
        if window is None:
            return
        existing = {str(row.get("code") or "").upper() for row in window.get("variables", [])}
        number = 1
        while f"V{number}" in existing:
            number += 1
        window.setdefault("variables", []).append({"code": f"V{number}", "formula": "0", "note": ""})
        self._loading = True; self._refresh_variable_table(); self._loading = False
        self.data_changed.emit()

    def delete_selected_variables(self):
        window = self._current_window()
        if window is None:
            return
        indexes = sorted({idx.row() - 5 for idx in self.variable_table.selectionModel().selectedRows() if idx.row() >= 5}, reverse=True)
        if not indexes and self.variable_table.currentRow() >= 5:
            indexes = [self.variable_table.currentRow() - 5]
        variables = list(window.get("variables") or [])
        for idx in indexes:
            if 0 <= idx < len(variables) and str(variables[idx].get("code") or "").upper() not in {"K", "K1"}:
                variables.pop(idx)
        window["variables"] = variables
        self._loading = True; self._refresh_variable_table(); self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _unique_variable_code(self, window: dict) -> str:
        existing = {str(row.get("code") or "").upper() for row in window.get("variables", [])}
        number = 1
        while f"V{number}" in existing:
            number += 1
        return f"V{number}"

    def _insert_master_above(self, row: int):
        row = int(row)
        before_id = None
        if 0 <= row < self.master_table.rowCount():
            item = self.master_table.item(row, 0)
            before_id = str(item.data(Qt.UserRole) or "") if item is not None else None
        window = self._new_window()
        self.windows.append(window)
        self._pending_master_positions[str(window["id"])] = before_id or None
        self._current_window_id = window["id"]
        self.refresh_all()
        target_row = next((r for r in range(self.master_table.rowCount()) if str(self.master_table.item(r,0).data(Qt.UserRole) or "") == str(window["id"])), -1)
        if target_row >= 0:
            self.master_table.setCurrentCell(target_row, 1)
            self.master_table.editItem(self.master_table.item(target_row, 1))
        self.data_changed.emit(); self.catalog_changed.emit()

    def _duplicate_master_row(self, row: int):
        item = self.master_table.item(int(row), 0) if 0 <= int(row) < self.master_table.rowCount() else None
        source = self._window_by_id(str(item.data(Qt.UserRole) or "")) if item is not None else None
        if source is None:
            return
        clone = deepcopy(source)
        clone["id"] = uuid.uuid4().hex
        base = str(clone.get("code") or "").strip().upper()
        if base:
            candidate = f"{base}_COPY"
            n = 2
            existing = {str(w.get("code") or "").upper() for w in self.windows}
            while candidate in existing:
                candidate = f"{base}_COPY{n}"; n += 1
            clone["code"] = candidate
        # 복제된 창호의 그룹은 같은 연결그룹을 유지한다.
        for group in clone.get("material_groups", []):
            group["id"] = uuid.uuid4().hex
        old_to_new = {}
        src_groups = list(source.get("material_groups", []))
        dst_groups = list(clone.get("material_groups", []))
        for a, b in zip(src_groups, dst_groups):
            old_to_new[str(a.get("id") or "")] = str(b.get("id") or "")
        for material in clone.get("materials", []):
            gid = str(material.get("group_id") or "")
            if gid in old_to_new:
                material["group_id"] = old_to_new[gid]
        self.windows.append(clone)
        self._current_window_id = clone["id"]
        self.refresh_all()
        self.data_changed.emit(); self.catalog_changed.emit()

    def _insert_variable_above(self, row: int):
        window = self._current_window()
        if window is None:
            return
        idx = max(0, int(row) - 5)
        entry = {"code": self._unique_variable_code(window), "formula": "", "note": ""}
        variables = list(window.get("variables") or [])
        idx = min(idx, len(variables))
        variables.insert(idx, entry)
        window["variables"] = variables
        self._loading = True
        try:
            self._refresh_variable_table(); self._refresh_material_table()
            self.variable_table.setCurrentCell(idx + 5, 0)
        finally:
            self._loading = False
        self.data_changed.emit()

    def _duplicate_variable_row(self, row: int):
        window = self._current_window()
        if window is None or int(row) < 5:
            return
        idx = int(row) - 5
        variables = list(window.get("variables") or [])
        if not (0 <= idx < len(variables)):
            return
        clone = deepcopy(variables[idx])
        clone["code"] = self._unique_variable_code(window)
        variables.insert(idx + 1, clone)
        window["variables"] = variables
        self._loading = True
        try:
            self._refresh_variable_table(); self._refresh_material_table()
            self.variable_table.setCurrentCell(idx + 6, 0)
        finally:
            self._loading = False
        self.data_changed.emit()

    @staticmethod
    def _window_group(window: dict, group_id: str) -> dict | None:
        target = str(group_id or "")
        return next((g for g in list(window.get("material_groups") or []) if str(g.get("id") or "") == target), None)

    def _ensure_material_group(self, window: dict, name: str = "그룹1", *, link_id: str = "", color: str = "#E8F5E9") -> dict:
        name = str(name or "그룹1").strip() or "그룹1"
        existing = next((g for g in list(window.get("material_groups") or []) if str(g.get("name") or "") == name), None)
        if existing is not None:
            return existing
        gid = uuid.uuid4().hex
        group = {"id": gid, "link_id": str(link_id or gid), "name": name, "color": str(color or "#E8F5E9"), "collapsed": False}
        window.setdefault("material_groups", []).append(group)
        return group

    def _material_meta_at_row(self, row: int) -> dict:
        item = self.material_table.item(int(row), 0) if 0 <= int(row) < self.material_table.rowCount() else None
        data = item.data(Qt.UserRole) if item is not None else None
        return dict(data or {}) if isinstance(data, dict) else {}

    def _refresh_material_table(self):
        current_meta = self._material_meta_at_row(self.material_table.currentRow()) if self.material_table.rowCount() else {}
        self.material_table.setRowCount(0)
        window = self._current_window()
        if window is None:
            return
        variables = self._computed_variables(window)
        groups = list(window.get("material_groups") or [])
        materials = list(window.get("materials") or [])
        # 예전 데이터나 외부 삽입으로 그룹 없는 재료가 생겼을 때 자동 복구한다.
        known = {str(g.get("id") or "") for g in groups}
        for material in materials:
            if str(material.get("group_id") or "") not in known:
                group = self._ensure_material_group(window, str(material.get("group") or "그룹1"))
                material["group_id"] = str(group.get("id") or "")
                material["group"] = str(group.get("name") or "그룹1")
        groups = list(window.get("material_groups") or [])
        target_row = -1
        for group in groups:
            gid = str(group.get("id") or "")
            group_name = str(group.get("name") or "그룹")
            collapsed = bool(group.get("collapsed", False))
            row = self.material_table.rowCount(); self.material_table.insertRow(row)
            meta = {"kind": "group", "group_id": gid}
            values = [("▶ " if collapsed else "▼ ") + group_name, "", "", "", "", ""]
            bg = QColor(str(group.get("color") or "#E8F5E9"))
            if not bg.isValid(): bg = QColor("#E8F5E9")
            bg.setAlpha(115)
            for col, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.UserRole, meta)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setBackground(QBrush(bg))
                font = item.font(); font.setBold(True); item.setFont(font)
                self.material_table.setItem(row, col, item)
            if current_meta.get("kind") == "group" and current_meta.get("group_id") == gid:
                target_row = row
            for material_index, material in enumerate(materials):
                if str(material.get("group_id") or "") != gid:
                    continue
                value = safe_eval_formula(material.get("formula", ""), variables)
                child_row = self.material_table.rowCount(); self.material_table.insertRow(child_row)
                child_meta = {"kind": "material", "group_id": gid, "material_index": material_index}
                vals = ["", material.get("name", ""), material.get("spec", ""), normalize_material_unit(material.get("unit", "")), material.get("formula", ""), _fmt(value) if value is not None else ""]
                for col, text in enumerate(vals):
                    item = QTableWidgetItem(str(text or ""))
                    item.setData(Qt.UserRole, child_meta)
                    if col in (0, 5):
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.material_table.setItem(child_row, col, item)
                self.material_table.setRowHidden(child_row, collapsed)
                if current_meta.get("kind") == "material" and current_meta.get("material_index") == material_index and current_meta.get("group_id") == gid:
                    target_row = child_row
        if target_row >= 0:
            self.material_table.setCurrentCell(target_row, max(0, self.material_table.currentColumn()))

    def _on_material_item_changed(self, item):
        if self._loading:
            return
        window = self._current_window()
        if window is None:
            return
        meta = dict(item.data(Qt.UserRole) or {}) if isinstance(item.data(Qt.UserRole), dict) else {}
        if meta.get("kind") != "material":
            return
        idx = int(meta.get("material_index", -1))
        materials = list(window.get("materials") or [])
        if not (0 <= idx < len(materials)):
            return
        material = materials[idx]
        columns = {1: "name", 2: "spec", 3: "unit", 4: "formula"}
        field = columns.get(item.column())
        if not field:
            return
        value = str(item.text() or "").strip()
        if field == "unit":
            value = normalize_material_unit(value)
        elif field == "formula":
            value = value.upper()
        material[field] = value
        window["materials"] = materials
        row, col = item.row(), item.column()
        self._loading = True
        try:
            self._refresh_material_table()
            if 0 <= row < self.material_table.rowCount():
                self.material_table.setCurrentCell(row, col)
        finally:
            self._loading = False
        self.data_changed.emit()

    def _material_identity(self, material: dict) -> tuple[str, str, str, str]:
        return (str(material.get("id") or ""), str(material.get("name") or ""), str(material.get("spec") or ""), normalize_material_unit(material.get("unit", "")))

    def _new_blank_material(self, group: dict) -> dict:
        return {"group": str(group.get("name") or "그룹1"), "group_id": str(group.get("id") or ""), "id": "", "name": "", "spec": "", "unit": "", "formula": ""}

    def _insert_material_above(self, row: int):
        window = self._current_window()
        if window is None:
            return
        meta = self._material_meta_at_row(row)
        if meta.get("kind") == "group":
            groups = list(window.get("material_groups") or [])
            pos = next((i for i,g in enumerate(groups) if str(g.get("id")) == str(meta.get("group_id"))), len(groups))
            gid = uuid.uuid4().hex
            groups.insert(pos, {"id": gid, "link_id": gid, "name": self._unique_group_name(window, "새 그룹"), "color": "#E8F5E9", "collapsed": False})
            window["material_groups"] = groups
        else:
            group = self._window_group(window, meta.get("group_id")) if meta else None
            if group is None:
                group = self._ensure_material_group(window, "그룹1")
            materials = list(window.get("materials") or [])
            idx = int(meta.get("material_index", len(materials))) if meta.get("kind") == "material" else len(materials)
            idx = max(0, min(idx, len(materials)))
            materials.insert(idx, self._new_blank_material(group))
            window["materials"] = materials
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _duplicate_material_row(self, row: int):
        window = self._current_window()
        if window is None:
            return
        meta = self._material_meta_at_row(row)
        if meta.get("kind") == "group":
            source = self._window_group(window, meta.get("group_id"))
            if source is None: return
            new_gid = uuid.uuid4().hex
            clone_group = deepcopy(source)
            clone_group["id"] = new_gid
            clone_group["link_id"] = uuid.uuid4().hex
            clone_group["name"] = self._unique_group_name(window, f"{source.get('name','그룹')} 복사")
            clone_group["collapsed"] = False
            groups = list(window.get("material_groups") or [])
            pos = groups.index(source) + 1
            groups.insert(pos, clone_group); window["material_groups"] = groups
            copied = []
            for material in list(window.get("materials") or []):
                if str(material.get("group_id") or "") == str(source.get("id") or ""):
                    m = deepcopy(material); m["group_id"] = new_gid; m["group"] = clone_group["name"]; copied.append(m)
            window["materials"].extend(copied)
        elif meta.get("kind") == "material":
            idx = int(meta.get("material_index", -1)); materials = list(window.get("materials") or [])
            if 0 <= idx < len(materials):
                materials.insert(idx + 1, deepcopy(materials[idx])); window["materials"] = materials
        else:
            self._insert_material_above(self.material_table.rowCount()); return
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _unique_group_name(self, window: dict, base: str) -> str:
        base = str(base or "그룹").strip() or "그룹"
        existing = {str(g.get("name") or "") for g in window.get("material_groups", [])}
        if base not in existing:
            return base
        n = 2
        while f"{base}{n}" in existing: n += 1
        return f"{base}{n}"

    def _add_material_group(self):
        window = self._current_window()
        if window is None:
            QMessageBox.information(self, "그룹 추가", "창호를 먼저 선택해 주세요."); return
        default = self._unique_group_name(window, "그룹")
        name, ok = QInputDialog.getText(self, "그룹 추가", "그룹명", text=default)
        name = str(name or "").strip()
        if not ok or not name: return
        if any(str(g.get("name") or "") == name for g in window.get("material_groups", [])):
            QMessageBox.information(self, "그룹 추가", "같은 이름의 그룹이 이미 있습니다."); return
        self._ensure_material_group(window, name)
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _on_material_cell_double_clicked(self, row: int, _col: int):
        meta = self._material_meta_at_row(row)
        if meta.get("kind") != "group": return
        window = self._current_window(); group = self._window_group(window, meta.get("group_id")) if window else None
        if group is None: return
        group["collapsed"] = not bool(group.get("collapsed"))
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _open_material_context_menu(self, pos):
        window = self._current_window()
        if window is None: return
        row = self.material_table.rowAt(pos.y())
        meta = self._material_meta_at_row(row) if row >= 0 else {}
        menu = QMenu(self.material_table)
        if not meta:
            add_action = menu.addAction("그룹 추가")
            action = menu.exec(self.material_table.viewport().mapToGlobal(pos))
            if action == add_action: self._add_material_group()
            return
        if meta.get("kind") != "group":
            add_action = menu.addAction("그룹 추가")
            action = menu.exec(self.material_table.viewport().mapToGlobal(pos))
            if action == add_action: self._add_material_group()
            return
        gid = str(meta.get("group_id") or "")
        rename_action = menu.addAction("이름변경")
        delete_action = menu.addAction("삭제")
        color_action = menu.addAction("색상")
        menu.addSeparator()
        formula_action = menu.addAction("산식 그룹 반영")
        break_action = menu.addAction("그룹 깨기")
        action = menu.exec(self.material_table.viewport().mapToGlobal(pos))
        if action == rename_action: self._rename_material_group(gid)
        elif action == delete_action: self._delete_material_group(gid)
        elif action == color_action: self._color_material_group(gid)
        elif action == formula_action: self._apply_group_formulas(gid)
        elif action == break_action: self._break_material_group(gid)

    def _rename_material_group(self, gid: str):
        window = self._current_window(); group = self._window_group(window, gid) if window else None
        if group is None: return
        old = str(group.get("name") or "그룹")
        name, ok = QInputDialog.getText(self, "그룹 이름변경", "그룹명", text=old)
        name = str(name or "").strip()
        if not ok or not name or name == old: return
        if any(g is not group and str(g.get("name") or "") == name for g in window.get("material_groups", [])):
            QMessageBox.information(self, "그룹 이름변경", "같은 이름의 그룹이 이미 있습니다."); return
        group["name"] = name
        for m in window.get("materials", []):
            if str(m.get("group_id") or "") == str(gid): m["group"] = name
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _delete_material_group(self, gid: str):
        window = self._current_window(); group = self._window_group(window, gid) if window else None
        if group is None: return
        if QMessageBox.question(self, "그룹 삭제", f"'{group.get('name','그룹')}' 그룹과 포함 재료를 삭제할까요?", QMessageBox.Yes|QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        window["material_groups"] = [g for g in window.get("material_groups", []) if str(g.get("id")) != str(gid)]
        window["materials"] = [m for m in window.get("materials", []) if str(m.get("group_id")) != str(gid)]
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _color_material_group(self, gid: str):
        window = self._current_window(); group = self._window_group(window, gid) if window else None
        if group is None: return
        initial = QColor(str(group.get("color") or "#E8F5E9"))
        color = QColorDialog.getColor(initial, self, "그룹 색상")
        if not color.isValid(): return
        group["color"] = color.name()
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def _break_material_group(self, gid: str):
        window = self._current_window(); group = self._window_group(window, gid) if window else None
        if group is None: return
        group["link_id"] = uuid.uuid4().hex
        self.data_changed.emit()
        QMessageBox.information(self, "그룹 깨기", "현재 창호의 그룹은 유지하고 다른 창호와의 그룹 연결만 해제했습니다.")

    def _apply_group_formulas(self, gid: str):
        source_window = self._current_window(); source_group = self._window_group(source_window, gid) if source_window else None
        if source_group is None: return
        link_id = str(source_group.get("link_id") or "")
        source_rows = [m for m in source_window.get("materials", []) if str(m.get("group_id") or "") == str(gid)]
        formulas = {self._material_identity(m): str(m.get("formula") or "") for m in source_rows}
        box = QMessageBox(self)
        box.setWindowTitle("그룹산식 적용")
        box.setText(f"'{source_group.get('name','그룹')}' 연결그룹의 산식을 다른 창호에 어떻게 적용할까요?")
        box.setInformativeText("전체적용: 기존 산식도 덮어씀\n빈칸적용: 산식이 비어 있는 재료에만 입력\n적용안함: 변경하지 않음")
        overwrite_btn = box.addButton("전체적용", QMessageBox.AcceptRole)
        blank_btn = box.addButton("빈칸적용", QMessageBox.ActionRole)
        cancel_btn = box.addButton("적용안함", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn or (clicked != overwrite_btn and clicked != blank_btn):
            return
        blank_only = clicked == blank_btn
        changed = 0
        for window in self.windows:
            for group in window.get("material_groups", []):
                if window is source_window and str(group.get("id")) == str(gid): continue
                if str(group.get("link_id") or "") != link_id: continue
                target_gid = str(group.get("id") or "")
                for material in window.get("materials", []):
                    if str(material.get("group_id") or "") != target_gid: continue
                    key = self._material_identity(material)
                    if key not in formulas:
                        continue
                    current = str(material.get("formula") or "")
                    if blank_only and current.strip():
                        continue
                    if current != formulas[key]:
                        material["formula"] = formulas[key]; changed += 1
        if changed:
            self._loading = True; self._refresh_material_table(); self._loading = False
            self.data_changed.emit()
        QMessageBox.information(self, "그룹산식 적용", f"연결된 동일 그룹에 산식을 반영했습니다.\n변경 재료: {changed:,}개")

    def apply_selected_group_formulas(self):
        meta = self._material_meta_at_row(self.material_table.currentRow())
        gid = str(meta.get("group_id") or "")
        if not gid:
            QMessageBox.information(self, "그룹산식 적용", "적용할 그룹 또는 그룹 내 재료를 먼저 선택해 주세요."); return
        self._apply_group_formulas(gid)

    def open_material_picker(self):
        window = self._current_window()
        if window is None:
            QMessageBox.information(self, "창호 산출", "창호를 먼저 선택해 주세요.")
            return
        dialog = MaterialPickerDialog(MATERIAL_FINISH_PATH, material_trades=self.material_trades, material_units=self.material_units, parent=self)
        dialog.library.material_units_changed.connect(self.material_units_changed.emit)
        dialog.library.materials_changed.connect(self.materials_changed.emit)
        if dialog.exec() != QDialog.Accepted:
            return
        meta = self._material_meta_at_row(self.material_table.currentRow())
        group = self._window_group(window, meta.get("group_id")) if meta else None
        if group is None:
            group = self._ensure_material_group(window, "그룹1")
        existing_ids = {str(row.get("id") or "") for row in window.get("materials", [])}
        for source in dialog.selected_materials:
            material_id = str(source.get("id") or "")
            if material_id and material_id in existing_ids:
                continue
            window.setdefault("materials", []).append({
                "group": str(group.get("name") or "그룹1"), "group_id": str(group.get("id") or ""),
                "id": material_id, "name": str(source.get("name") or ""),
                "spec": str(source.get("spec") or ""), "unit": normalize_material_unit(source.get("unit", "")),
                "formula": str(source.get("method") or "").upper(),
            })
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def delete_selected_materials(self):
        window = self._current_window()
        if window is None: return
        indexes = set()
        for index in self.material_table.selectionModel().selectedRows():
            meta = self._material_meta_at_row(index.row())
            if meta.get("kind") == "material": indexes.add(int(meta.get("material_index", -1)))
        if not indexes:
            meta = self._material_meta_at_row(self.material_table.currentRow())
            if meta.get("kind") == "material": indexes.add(int(meta.get("material_index", -1)))
        materials = list(window.get("materials") or [])
        for idx in sorted((i for i in indexes if 0 <= i < len(materials)), reverse=True):
            materials.pop(idx)
        window["materials"] = materials
        self._loading = True; self._refresh_material_table(); self._loading = False
        self.data_changed.emit()

    def open_group_assign_dialog(self):
        target_ids = self._selected_window_ids()
        if not target_ids:
            QMessageBox.information(self, "그룹지정", "창호 리스트에서 적용할 창호를 하나 이상 선택해 주세요.")
            return
        sources = []
        for window in self.windows:
            code = str(window.get("code") or "(미지정)")
            for group in window.get("material_groups", []):
                sources.append((code, str(group.get("name") or "그룹")))
        if not sources:
            QMessageBox.information(self, "그룹지정", "먼저 어느 한 창호에 산출 그룹을 만들어 주세요.")
            return
        dialog = MaterialGroupAssignDialog(sources, self)
        if dialog.exec() != QDialog.Accepted or not dialog.selected_source:
            return
        source_code, group_name = dialog.selected_source
        source_window = next((row for row in self.windows if str(row.get("code") or "") == source_code), None)
        source_group = next((g for g in (source_window or {}).get("material_groups", []) if str(g.get("name") or "") == group_name), None)
        if source_window is None or source_group is None: return
        source_gid = str(source_group.get("id") or "")
        source_rows = [deepcopy(row) for row in source_window.get("materials", []) if str(row.get("group_id") or "") == source_gid]
        changed = False
        for window_id in target_ids:
            target = self._window_by_id(window_id)
            if target is None: continue
            existing_groups = [g for g in target.get("material_groups", []) if str(g.get("name") or "") == group_name]
            if dialog.action == "delete":
                gids = {str(g.get("id") or "") for g in existing_groups}
                if gids:
                    target["material_groups"] = [g for g in target.get("material_groups", []) if str(g.get("id") or "") not in gids]
                    target["materials"] = [m for m in target.get("materials", []) if str(m.get("group_id") or "") not in gids]
                    changed = True
            else:
                gids = {str(g.get("id") or "") for g in existing_groups}
                target["material_groups"] = [g for g in target.get("material_groups", []) if str(g.get("id") or "") not in gids]
                target["materials"] = [m for m in target.get("materials", []) if str(m.get("group_id") or "") not in gids]
                new_gid = uuid.uuid4().hex
                new_group = deepcopy(source_group); new_group["id"] = new_gid; new_group["collapsed"] = False
                target.setdefault("material_groups", []).append(new_group)
                for row in source_rows:
                    row["group_id"] = new_gid; row["group"] = group_name
                    target.setdefault("materials", []).append(row)
                changed = True
        if changed:
            self._loading = True; self._refresh_material_table(); self._loading = False
            self.data_changed.emit()

    @staticmethod
    def _expand_floor_label(value: str) -> list[str]:
        text = str(value or "").strip().upper().replace(" ", "")
        if not text: return []
        # 4~20F / 4F~20F / 4~20
        m = re.fullmatch(r"(\d+)F?~(\d+)F?", text)
        if m:
            a, b = int(m.group(1)), int(m.group(2)); step = 1 if b >= a else -1
            return [f"{n}F" for n in range(a, b + step, step)]
        m = re.fullmatch(r"B(\d+)F?~B?(\d+)F?", text)
        if m:
            a, b = int(m.group(1)), int(m.group(2)); step = -1 if b <= a else 1
            return [f"B{n}F" for n in range(a, b + step, step)]
        return [text]

    def _floor_names(self) -> list[str]:
        floors = set()
        try:
            overview = dict(self.overview_provider() or {})
        except Exception:
            overview = {}
        ground = max(0, int(_number(overview.get("ground_floors"))))
        basement = max(0, int(_number(overview.get("basement_floors"))))
        rooftop = max(0, int(_number(overview.get("rooftop_floors"))))
        floors.update(f"B{n}F" for n in range(basement, 0, -1))
        floors.update(f"{n}F" for n in range(1, ground + 1))
        floors.update(f"PH{n}" for n in range(1, rooftop + 1))
        # 평면에서 개요에 없는 M1/중간층/범위층 등을 보강한다.
        for room in list(self.rooms_provider() or []):
            scope = str(getattr(room, "floor_scope", "") or getattr(room, "floor", "") or "")
            for floor in self._expand_floor_label(scope):
                floors.add(floor)
        for values in self.floor_counts.values():
            for key in values:
                floors.update(self._expand_floor_label(str(key)))
        return sorted((f for f in floors if f), key=_floor_key)

    def open_floor_schedule_dialog(self):
        if self._floor_dialog is None:
            self._floor_dialog = FloorWindowScheduleDialog(self)
            self._floor_dialog.finished.connect(lambda _r: setattr(self, "_floor_dialog", None))
        self._floor_dialog.refresh()
        self._floor_dialog.show(); self._floor_dialog.raise_(); self._floor_dialog.activateWindow()

    def _populate_floor_table(self, table: QTableWidget):
        floors = self._floor_names()
        self._loading = True
        try:
            table.setColumnCount(4 + len(floors))
            table.setHorizontalHeaderLabels(["구분", "창호명", "규격", "합계", *floors])
            table.setRowCount(0)
            for window in sorted(self.windows, key=lambda row: str(row.get("code") or "").casefold()):
                code = str(window.get("code") or "").upper()
                if not code: continue
                row = table.rowCount(); table.insertRow(row)
                counts = self.floor_counts.setdefault(code, {})
                total = sum(max(0, int(_number(counts.get(floor)))) for floor in floors)
                fixed = [window.get("category", ""), code, f"{_fmt(window.get('width'))}×{_fmt(window.get('height'))}", str(total)]
                for col, value in enumerate(fixed):
                    item = QTableWidgetItem(str(value)); item.setFlags(item.flags() & ~Qt.ItemIsEditable); item.setData(Qt.UserRole, code); table.setItem(row, col, item)
                for offset, floor in enumerate(floors):
                    count = max(0, int(_number(counts.get(floor))))
                    item = QTableWidgetItem(str(count) if count else "")
                    item.setData(Qt.UserRole, [code, floor]); item.setTextAlignment(Qt.AlignCenter); table.setItem(row, 4 + offset, item)
            header = table.horizontalHeader()
            for col in range(min(4, table.columnCount())): header.resizeSection(col, [110,100,145,65][col])
            for col in range(4, table.columnCount()): header.resizeSection(col, 48)
        finally:
            self._loading = False

    def refresh_floor_schedule(self):
        if self._floor_dialog is not None:
            self._floor_dialog.refresh()

    def _on_floor_item_changed(self, item):
        if self._loading or item.column() < 4: return
        data = item.data(Qt.UserRole)
        if not isinstance(data, (list, tuple)) or len(data) < 2: return
        code, floor = str(data[0]).upper(), str(data[1])
        count = max(0, int(_number(item.text())))
        self.floor_counts.setdefault(code, {})[floor] = count
        keep_row, keep_col = item.row(), item.column()
        if self._floor_dialog is not None:
            self._floor_dialog.refresh()
            if 0 <= keep_row < self._floor_dialog.table.rowCount() and 0 <= keep_col < self._floor_dialog.table.columnCount():
                self._floor_dialog.table.setCurrentCell(keep_row, keep_col)
        self.data_changed.emit()

    def _floors_for_opening_floor(self, raw_floor: str) -> list[str]:
        raw = str(raw_floor or "").strip().upper().replace(" ", "")
        direct = self._expand_floor_label(raw)
        if len(direct) > 1:
            return direct
        expanded = []
        for room in list(self.rooms_provider() or []):
            base = str(getattr(room, "floor", "") or "").strip().upper().replace(" ", "")
            if base != raw:
                continue
            scope = str(getattr(room, "floor_scope", "") or base)
            for floor in self._expand_floor_label(scope):
                if floor not in expanded:
                    expanded.append(floor)
        return expanded or direct or ([raw] if raw else [])

    def read_plan_opening_counts(self):
        try:
            opening_results = dict(self.opening_provider() or {})
        except Exception:
            opening_results = {}
        if not opening_results:
            QMessageBox.information(self._floor_dialog or self, "평면 창호 읽기", "평면에서 적용부호가 지정된 창호가 없습니다.")
            return
        read_counts: dict[str, dict[str, int]] = {}
        for raw_floor, markers in opening_results.items():
            floors = self._floors_for_opening_floor(str(raw_floor or "")) or [str(raw_floor or "")]
            per_code: dict[str, int] = {}
            for marker in list(markers or []):
                code = str((marker or {}).get("applied_code") or "").strip().upper()
                if code: per_code[code] = per_code.get(code, 0) + 1
            for floor in floors:
                for code, count in per_code.items():
                    read_counts.setdefault(code, {})[floor] = read_counts.setdefault(code, {}).get(floor, 0) + count
        if not any(read_counts.values()):
            QMessageBox.information(self._floor_dialog or self, "평면 창호 읽기", "적용부호가 지정된 창호가 없습니다.")
            return
        box = QMessageBox(self._floor_dialog or self)
        box.setWindowTitle("평면 창호 읽기")
        box.setText("평면의 적용부호 개수를 층별 창호리스트에 어떻게 반영할까요?")
        all_btn = box.addButton("전체 덮어쓰기", QMessageBox.AcceptRole)
        existing_btn = box.addButton("내용 있는 부호만 덮어쓰기", QMessageBox.ActionRole)
        cancel_btn = box.addButton("취소", QMessageBox.RejectRole)
        box.exec(); clicked = box.clickedButton()
        if clicked == cancel_btn: return
        floors = self._floor_names()
        codes = [str(w.get("code") or "").upper() for w in self.windows if str(w.get("code") or "").strip()]
        if clicked == all_btn:
            for code in codes:
                self.floor_counts.setdefault(code, {})
                for floor in floors:
                    self.floor_counts[code][floor] = int(read_counts.get(code, {}).get(floor, 0))
        else:
            for code, values in read_counts.items():
                if code not in codes: continue
                for floor, count in values.items():
                    if int(count) > 0:
                        self.floor_counts.setdefault(code, {})[floor] = int(count)
        self.refresh_floor_schedule(); self.data_changed.emit()
    def _commit_current_tables(self):
        # itemChanged에서 즉시 모델에 반영하므로 별도 작업이 필요하지 않는다.
        pass

    def _save_master_layout(self):
        if self.recent_settings is None or self._loading:
            return
        try:
            header = self.master_table.horizontalHeader()
            order = [header.logicalIndex(visual) for visual in range(header.count())]
            widths = [self.master_table.columnWidth(col) for col in range(self.master_table.columnCount())]
            self.recent_settings.set("WindowTab", "MasterColumnOrder", ",".join(map(str, order)))
            self.recent_settings.set("WindowTab", "MasterColumnWidths", ",".join(map(str, widths)))
            self.recent_settings.save()
        except Exception:
            pass

    def _restore_master_layout(self):
        defaults = [105, 100, 80, 80, 80, 90, 90, 60, 180, 90, 65]
        header = self.master_table.horizontalHeader()
        header.blockSignals(True)
        try:
            for col, width in enumerate(defaults):
                self.master_table.setColumnWidth(col, width)
            if self.recent_settings is None:
                return
            raw_widths = self.recent_settings.get("WindowTab", "MasterColumnWidths", "")
            widths = [int(part) for part in raw_widths.split(",") if part.strip()]
            for col, width in enumerate(widths[:self.master_table.columnCount()]):
                self.master_table.setColumnWidth(col, max(35, width))
            raw_order = self.recent_settings.get("WindowTab", "MasterColumnOrder", "")
            order = [int(part) for part in raw_order.split(",") if part.strip()]
            if sorted(order) == list(range(self.master_table.columnCount())):
                for visual, logical in enumerate(order):
                    current = header.visualIndex(logical)
                    if current != visual:
                        header.moveSection(current, visual)
        except Exception:
            pass
        finally:
            header.blockSignals(False)

