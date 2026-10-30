from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor
from app.rooms.background_style import DEFAULT_ACI_WIDTHS, DEFAULT_WIDTH, parse_width_map, serialize_width_map
from app.common.layer_defaults import LAYER_DEFINITIONS, LAYER_GROUPS, default_layer_name, default_layer_color
from app.common.room_categories import ROOM_CATEGORIES, normalize_room_category_rules
from app.common.finish_calculations import (
    canonical_finish_category,
    normalize_finish_categories,
    normalize_formula_codes,
    normalize_formula_text,
)
from app.common.material_trades import normalize_material_trades
from app.common.material_units import normalize_material_unit, normalize_material_units
from app.common.window_categories import normalize_window_categories
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSlider,
    QDoubleSpinBox, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QAbstractItemView, QMessageBox, QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem, QInputDialog,
)


class CommonListTable(QTableWidget):
    """수기 리스트 공통 키: F9 위삽입, F3 복제, F4 확인삭제, 마지막행 ↓ 1회 추가, Enter 셀 유지."""
    insert_above_requested = Signal(int)
    duplicate_row_requested = Signal(int)
    append_row_requested = Signal()
    delete_requested = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._down_append_guard = False
        self.itemChanged.connect(lambda _item: setattr(self, "_down_append_guard", False))

    def _last_row_blank(self):
        if self.rowCount() <= 0: return True
        row = self.rowCount() - 1
        return not any(str(self.item(row,c).text() if self.item(row,c) else "").strip() for c in range(self.columnCount()))

    def _confirm_f4_delete(self):
        rows = {i.row() for i in self.selectionModel().selectedRows()} or ({self.currentRow()} if self.currentRow() >= 0 else set())
        count = max(1, len(rows)); text = "현재 행을 삭제하시겠습니까?" if count == 1 else f"선택한 {count}개 행을 삭제하시겠습니까?"
        return QMessageBox.question(self, "목록 삭제", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes

    def keyPressEvent(self, event):
        row, col = self.currentRow(), self.currentColumn()
        if event.key() == Qt.Key_F9 and row >= 0:
            self.insert_above_requested.emit(row); return
        if event.key() == Qt.Key_F3 and row >= 0:
            self.duplicate_row_requested.emit(row); return
        if event.key() == Qt.Key_F4 and row >= 0:
            if self._confirm_f4_delete(): self._down_append_guard = False; self.delete_requested.emit()
            return
        if event.key() == Qt.Key_Down and row >= 0 and row == self.rowCount() - 1:
            if self._last_row_blank(): return
            self.append_row_requested.emit(); self._down_append_guard = self._last_row_blank(); return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            super().keyPressEvent(event)
            if row >= 0 and col >= 0:
                QTimer.singleShot(0, lambda r=row,c=col: self.setCurrentCell(min(r,self.rowCount()-1), min(c,self.columnCount()-1)) if self.rowCount() and self.columnCount() else None)
            return
        super().keyPressEvent(event)


class ColorButton(QPushButton):
    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.clicked.connect(self.choose_color)
        self._refresh()

    def choose_color(self):
        color = QColorDialog.getColor(self._color, self, "색상 선택")
        if color.isValid():
            self._color = color
            self._refresh()

    def _refresh(self):
        self.setText(self._color.name().upper())
        text_color = "white" if self._color.lightness() < 128 else "black"
        self.setStyleSheet(f"QPushButton {{ background:{self._color.name()}; color:{text_color}; }}")

    def set_color(self, color: str) -> None:
        value = QColor(str(color or ""))
        if value.isValid():
            self._color = value
            self._refresh()

    def color_name(self) -> str:
        return self._color.name().upper()


class LayerPatternEditor(QDialog):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("배경 제외 레이어 리스트 편집")
        self.resize(560, 440)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("배경 생성 및 변경 비교에서 제외할 레이어 패턴을 한 줄에 1개씩 입력하십시오."))
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        self.editor.setPlaceholderText("■*\nTEMP_*\n작업용_*")
        root.addWidget(self.editor, 1)
        note = QLabel("※ 한 줄에 1개씩 입력 · 와일드카드(*) 가능")
        note.setStyleSheet("color:#555;")
        root.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def text(self) -> str:
        lines = [line.strip() for line in self.editor.toPlainText().splitlines() if line.strip()]
        return "\n".join(lines)


class RoomCategoryRulesDialog(QDialog):
    """실명에 포함된 문자열로 최초 실 구분을 판단하는 기준 편집창."""

    def __init__(self, rules: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실 구분 기준")
        self.resize(720, 760)
        self.setMinimumSize(620, 600)

        root = QVBoxLayout(self)
        guide = QLabel(
            "실명이 처음 입력될 때 아래 단어를 포함하면 해당 구분을 자동 입력합니다. "
            "한 줄 또는 쉼표로 구분해 입력할 수 있습니다."
        )
        guide.setWordWrap(True)
        root.addWidget(guide)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        normalized = normalize_room_category_rules(rules)
        self.editors: dict[str, QPlainTextEdit] = {}
        for category in ROOM_CATEGORIES:
            box = QGroupBox(category)
            box_layout = QVBoxLayout(box)
            editor = QPlainTextEdit()
            editor.setMinimumHeight(70)
            editor.setPlainText("\n".join(normalized[category]))
            editor.setPlaceholderText("포함 문자열을 한 줄에 1개씩 입력")
            self.editors[category] = editor
            box_layout.addWidget(editor)
            if category == "기타":
                note = QLabel("※ 다른 기준에 해당하지 않는 실이 자동 분류됩니다. 연면적에 합산하지 않을 대상을 관리하는 구분입니다.")
                note.setWordWrap(True)
                note.setStyleSheet("color:#555;")
                box_layout.addWidget(note)
            content_layout.addWidget(box)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> dict[str, list[str]]:
        return normalize_room_category_rules({
            category: editor.toPlainText()
            for category, editor in self.editors.items()
        })


class FormulaCodeDialog(QDialog):
    """부위별 산식 코드 목록 편집창."""

    def __init__(self, rows=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("산식 코드")
        self.resize(820, 520)
        self.setMinimumSize(680, 420)

        root = QVBoxLayout(self)
        guide = QLabel("재료마감 산출식에 사용할 코드를 관리합니다. 코드는 중복될 수 없습니다.")
        guide.setWordWrap(True)
        root.addWidget(guide)

        self.table = CommonListTable(0, 4)
        self.table.setHorizontalHeaderLabels(["부위", "코드", "산식", "설명"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 360)
        root.addWidget(self.table, 1)

        for row in normalize_formula_codes(rows):
            self._append_row(row)

        actions = QHBoxLayout()
        add_btn = QPushButton("추가")
        remove_btn = QPushButton("제거")
        duplicate_btn = QPushButton("복제")
        add_btn.clicked.connect(self.add_row)
        remove_btn.clicked.connect(self.remove_row)
        duplicate_btn.clicked.connect(self.duplicate_row)
        self.table.insert_above_requested.connect(lambda row: self._append_row({}, row))
        self.table.duplicate_row_requested.connect(lambda _row: self.duplicate_row())
        self.table.append_row_requested.connect(lambda: self._append_row({}))
        self.table.delete_requested.connect(self.remove_row)
        actions.addWidget(add_btn)
        actions.addWidget(remove_btn)
        actions.addWidget(duplicate_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _append_row(self, values: dict | None = None, index: int | None = None):
        values = dict(values or {})
        row = self.table.rowCount() if index is None else max(0, min(index, self.table.rowCount()))
        self.table.insertRow(row)
        for col, key in enumerate(("part", "code", "formula", "description")):
            self.table.setItem(row, col, QTableWidgetItem(str(values.get(key, "") or "")))
        self.table.setCurrentCell(row, 0)
        return row

    def add_row(self):
        row = self._append_row()
        self.table.editItem(self.table.item(row, 0))

    def remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def duplicate_row(self):
        row = self.table.currentRow()
        if row < 0:
            return
        values = [self.table.item(row, col).text().strip() if self.table.item(row, col) else "" for col in range(4)]
        existing = {
            (self.table.item(r, 1).text().strip() if self.table.item(r, 1) else "").casefold()
            for r in range(self.table.rowCount())
        }
        base = values[1] or "CODE"
        candidate = f"{base}_COPY"
        number = 2
        while candidate.casefold() in existing:
            candidate = f"{base}_COPY{number}"
            number += 1
        values[1] = candidate
        new_row = self._append_row(dict(zip(("part", "code", "formula", "description"), values)), row + 1)
        self.table.setCurrentCell(new_row, 1)

    def values(self) -> list[dict[str, str]]:
        rows = []
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, col).text().strip() if self.table.item(row, col) else "" for col in range(4)]
            if not any(values):
                continue
            rows.append({
                "part": values[0], "code": values[1].upper(),
                "formula": values[2], "description": values[3],
            })
        return rows

    def accept(self):
        rows = self.values()
        seen = set()
        for index, row in enumerate(rows, 1):
            if not row["code"]:
                QMessageBox.warning(self, "산식 코드", f"{index}행의 코드를 입력해 주세요.")
                return
            marker = row["code"].casefold()
            if marker in seen:
                QMessageBox.warning(self, "산식 코드", f"중복 코드가 있습니다: {row['code']}")
                return
            seen.add(marker)
        super().accept()


class FinishCategoryDialog(QDialog):
    """재료마감 구분과 기본 산식 코드 연결 목록 편집창."""

    def __init__(self, categories=None, formula_codes=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("마감 구분 리스트")
        self.resize(620, 500)
        self.setMinimumSize(520, 400)
        self._formula_codes = normalize_formula_codes(formula_codes)

        root = QVBoxLayout(self)
        guide = QLabel(
            "구분에 산식 코드를 연결하면 재료마감 목록에서 해당 구분을 선택할 때 "
            "코드의 산식을 적용합니다. 산식 칸에 직접 입력한 내용이 있으면 직접 산식을 우선 사용합니다."
        )
        guide.setWordWrap(True)
        root.addWidget(guide)

        self.table = CommonListTable(0, 3)
        self.table.setHorizontalHeaderLabels(["구분", "산식코드", "산식"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 105)
        self.table.setColumnWidth(2, 360)
        root.addWidget(self.table, 1)

        for row in normalize_finish_categories(categories):
            self._append_row(row)

        actions = QHBoxLayout()
        add_btn = QPushButton("추가")
        remove_btn = QPushButton("제거")
        duplicate_btn = QPushButton("복제")
        edit_formula_btn = QPushButton("산출식 편집")
        add_btn.clicked.connect(self.add_row)
        remove_btn.clicked.connect(self.remove_row)
        duplicate_btn.clicked.connect(self.duplicate_row)
        edit_formula_btn.clicked.connect(self.edit_formulas)
        self.table.insert_above_requested.connect(lambda row: self._append_row({}, row))
        self.table.duplicate_row_requested.connect(lambda _row: self.duplicate_row())
        self.table.append_row_requested.connect(lambda: self._append_row({}))
        self.table.delete_requested.connect(self.remove_row)
        actions.addWidget(add_btn)
        actions.addWidget(remove_btn)
        actions.addWidget(duplicate_btn)
        actions.addWidget(edit_formula_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _code_values(self) -> list[str]:
        return [row["code"] for row in self._formula_codes]

    def _formula_for_code(self, code: str) -> str:
        marker = str(code or "").strip().casefold()
        for row in self._formula_codes:
            if row["code"].casefold() == marker:
                return row["formula"]
        return ""

    def _make_code_combo(self, current: str = "") -> QComboBox:
        combo = QComboBox()
        combo.addItem("")
        combo.addItems(self._code_values())
        current = str(current or "").strip().upper()
        index = combo.findText(current, Qt.MatchFixedString)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.setProperty("previous_code", current)
        combo.currentTextChanged.connect(
            lambda code, current_combo=combo: self._on_code_changed(current_combo, code)
        )
        return combo

    def _on_code_changed(self, combo: QComboBox, code: str):
        row = next(
            (index for index in range(self.table.rowCount()) if self.table.cellWidget(index, 1) is combo),
            -1,
        )
        if row < 0:
            return
        formula_item = self.table.item(row, 2)
        if formula_item is None:
            formula_item = QTableWidgetItem("")
            self.table.setItem(row, 2, formula_item)
        previous_code = str(combo.property("previous_code") or "")
        previous_formula = self._formula_for_code(previous_code)
        current_formula = normalize_formula_text(formula_item.text())
        # 직접 입력한 산식은 유지하고, 공란 또는 이전 코드에서 자동 표시된 산식만 새 코드 산식으로 바꾼다.
        if not current_formula or current_formula == previous_formula:
            formula_item.setText(self._formula_for_code(code))
        combo.setProperty("previous_code", str(code or "").strip().upper())

    def _append_row(self, values: dict | None = None, index: int | None = None):
        values = dict(values or {})
        row = self.table.rowCount() if index is None else max(0, min(index, self.table.rowCount()))
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(values.get("category", "") or "")))
        formula_code = str(values.get("formula_code", "") or "").strip().upper()
        direct_formula = normalize_formula_text(values.get("formula", ""))
        self.table.setCellWidget(row, 1, self._make_code_combo(formula_code))
        self.table.setItem(row, 2, QTableWidgetItem(direct_formula or self._formula_for_code(formula_code)))
        self.table.setCurrentCell(row, 0)
        return row

    def add_row(self):
        row = self._append_row()
        self.table.editItem(self.table.item(row, 0))

    def remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def duplicate_row(self):
        row = self.table.currentRow()
        if row < 0:
            return
        category = self.table.item(row, 0).text().strip() if self.table.item(row, 0) else ""
        combo = self.table.cellWidget(row, 1)
        formula_code = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
        formula_text = normalize_formula_text(self.table.item(row, 2).text()) if self.table.item(row, 2) else ""
        direct_formula = "" if formula_text == self._formula_for_code(formula_code) else formula_text
        base = category or "새 구분"
        existing = {
            (self.table.item(r, 0).text().strip() if self.table.item(r, 0) else "").casefold()
            for r in range(self.table.rowCount())
        }
        candidate = f"{base} 복사"
        number = 2
        while candidate.casefold() in existing:
            candidate = f"{base} 복사 {number}"
            number += 1
        self._append_row({"category": candidate, "formula_code": formula_code, "formula": direct_formula}, row + 1)

    def edit_formulas(self):
        dialog = FormulaCodeDialog(self._formula_codes, self)
        if not dialog.exec():
            return
        previous_rows = []
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            code = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
            formula_item = self.table.item(row, 2)
            formula_text = normalize_formula_text(formula_item.text()) if formula_item else ""
            direct_formula = "" if formula_text == self._formula_for_code(code) else formula_text
            previous_rows.append((code, direct_formula))
        self._formula_codes = normalize_formula_codes(dialog.values())
        valid_codes = {row["code"].casefold() for row in self._formula_codes}
        for row, (previous_code, direct_formula) in enumerate(previous_rows):
            code = previous_code if previous_code.casefold() in valid_codes else ""
            self.table.setCellWidget(row, 1, self._make_code_combo(code))
            formula_item = self.table.item(row, 2)
            if formula_item is None:
                formula_item = QTableWidgetItem("")
                self.table.setItem(row, 2, formula_item)
            formula_item.setText(direct_formula or self._formula_for_code(code))

    def values(self, validate: bool = True) -> list[dict[str, str]]:
        rows = []
        seen = set()
        for row in range(self.table.rowCount()):
            category = self.table.item(row, 0).text().strip() if self.table.item(row, 0) else ""
            combo = self.table.cellWidget(row, 1)
            formula_code = combo.currentText().strip().upper() if isinstance(combo, QComboBox) else ""
            formula_text = normalize_formula_text(self.table.item(row, 2).text()) if self.table.item(row, 2) else ""
            direct_formula = "" if formula_text == self._formula_for_code(formula_code) else formula_text
            if not category and not formula_code and not direct_formula:
                continue
            if validate:
                if not category:
                    raise ValueError(f"{row + 1}행의 구분을 입력해 주세요.")
                marker = canonical_finish_category(category).casefold()
                if marker in seen:
                    raise ValueError(f"중복 구분이 있습니다: {category}")
                seen.add(marker)
            rows.append({"category": category, "formula_code": formula_code, "formula": direct_formula})
        return rows

    def formula_values(self) -> list[dict[str, str]]:
        return normalize_formula_codes(self._formula_codes)

    def accept(self):
        try:
            rows = self.values(validate=True)
        except ValueError as exc:
            QMessageBox.warning(self, "마감 구분 리스트", str(exc))
            return
        if not rows:
            QMessageBox.warning(self, "마감 구분 리스트", "마감 구분을 1개 이상 등록해 주세요.")
            return
        super().accept()


class MaterialTradeEntryDialog(QDialog):
    def __init__(self, title: str, code: str = "", name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.code_edit = QLineEdit(code)
        self.name_edit = QLineEdit(name)
        form.addRow("코드", self.code_edit)
        form.addRow("공종", self.name_edit)
        root.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        if not self.code_edit.text().strip() or not self.name_edit.text().strip():
            QMessageBox.warning(self, "공종 입력", "코드와 공종을 모두 입력해 주세요.")
            return
        super().accept()

    def values(self) -> tuple[str, str]:
        return self.code_edit.text().strip(), self.name_edit.text().strip()


class MaterialTradeDialog(QDialog):
    """대분류와 하부 항목으로 구성된 재료 공종 목록 편집창."""

    def __init__(self, trades=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("공종 리스트")
        self.resize(620, 620)
        self.setMinimumSize(520, 460)
        root = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["코드", "공종"])
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAlternatingRowColors(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.add_major_btn = QPushButton("대분류 추가")
        self.add_child_btn = QPushButton("하부 항목 추가")
        self.edit_btn = QPushButton("수정")
        self.delete_btn = QPushButton("삭제")
        actions.addWidget(self.add_major_btn)
        actions.addWidget(self.add_child_btn)
        actions.addWidget(self.edit_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.add_major_btn.clicked.connect(self.add_major)
        self.add_child_btn.clicked.connect(self.add_child)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.tree.itemDoubleClicked.connect(lambda *_args: self.edit_selected())
        self._load(trades)

    @staticmethod
    def _set_regular_font(item: QTreeWidgetItem):
        for column in range(2):
            font = item.font(column)
            font.setBold(False)
            item.setFont(column, font)

    def _load(self, trades):
        rows = normalize_material_trades(trades)
        majors: dict[str, QTreeWidgetItem] = {}
        for row in rows:
            if row["parent_code"]:
                continue
            item = QTreeWidgetItem([row["code"], row["name"]])
            self._set_regular_font(item)
            self.tree.addTopLevelItem(item)
            majors[row["code"]] = item
        for row in rows:
            parent_code = row["parent_code"]
            if not parent_code:
                continue
            parent_item = majors.get(parent_code)
            if parent_item is None:
                parent_item = QTreeWidgetItem([parent_code, parent_code])
                self._set_regular_font(parent_item)
                self.tree.addTopLevelItem(parent_item)
                majors[parent_code] = parent_item
            item = QTreeWidgetItem([row["code"], row["name"]])
            self._set_regular_font(item)
            parent_item.addChild(item)
        self.tree.expandAll()

    def _codes(self, excluding: QTreeWidgetItem | None = None) -> set[str]:
        result = set()
        root = self.tree.invisibleRootItem()
        stack = [root.child(index) for index in range(root.childCount())]
        while stack:
            item = stack.pop()
            if item is not excluding:
                result.add(item.text(0).strip().casefold())
            stack.extend(item.child(index) for index in range(item.childCount()))
        return result

    def _edit_values(self, title: str, code: str = "", name: str = "", excluding=None):
        dialog = MaterialTradeEntryDialog(title, code, name, self)
        if dialog.exec() != QDialog.Accepted:
            return None
        values = dialog.values()
        if values[0].casefold() in self._codes(excluding):
            QMessageBox.warning(self, "공종 리스트", f"중복 코드가 있습니다: {values[0]}")
            return None
        return values

    def add_major(self):
        values = self._edit_values("대분류 추가")
        if values is None:
            return
        item = QTreeWidgetItem(list(values))
        self._set_regular_font(item)
        self.tree.addTopLevelItem(item)
        self.tree.setCurrentItem(item)

    def _selected_major(self) -> QTreeWidgetItem | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item if item.parent() is None else item.parent()

    def add_child(self):
        parent_item = self._selected_major()
        if parent_item is None:
            QMessageBox.information(self, "하부 항목 추가", "먼저 대분류 또는 그 하부 항목을 선택해 주세요.")
            return
        values = self._edit_values("하부 항목 추가")
        if values is None:
            return
        item = QTreeWidgetItem(list(values))
        self._set_regular_font(item)
        parent_item.addChild(item)
        parent_item.setExpanded(True)
        self.tree.setCurrentItem(item)

    def edit_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        values = self._edit_values("공종 수정", item.text(0), item.text(1), excluding=item)
        if values is None:
            return
        old_code = item.text(0)
        item.setText(0, values[0])
        item.setText(1, values[1])
        if item.parent() is None and old_code != values[0]:
            # 하부 항목은 tree 구조로 부모를 판별하므로 별도 데이터 갱신이 필요 없다.
            item.setExpanded(True)
        self._set_regular_font(item)

    def delete_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        child_note = "\n하부 항목도 함께 삭제됩니다." if item.childCount() else ""
        answer = QMessageBox.question(
            self,
            "공종 삭제",
            f"'{item.text(0)} {item.text(1)}'을(를) 삭제할까요?{child_note}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        parent_item = item.parent()
        if parent_item is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        else:
            parent_item.takeChild(parent_item.indexOfChild(item))

    def values(self) -> list[dict[str, str]]:
        rows = []
        for major_index in range(self.tree.topLevelItemCount()):
            major = self.tree.topLevelItem(major_index)
            major_code = major.text(0).strip()
            major_name = major.text(1).strip()
            if major_code and major_name:
                rows.append({"code": major_code, "name": major_name, "parent_code": ""})
            for child_index in range(major.childCount()):
                child = major.child(child_index)
                code = child.text(0).strip()
                name = child.text(1).strip()
                if code and name:
                    rows.append({"code": code, "name": name, "parent_code": major_code})
        return normalize_material_trades(rows)



class MaterialUnitDialog(QDialog):
    """재료/내역에서 공통으로 사용하는 단위 마스터 목록 편집창."""

    def __init__(self, units=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("단위 리스트")
        self.resize(430, 520)
        self.setMinimumSize(360, 420)

        root = QVBoxLayout(self)
        guide = QLabel("재료와 내역의 단위 입력은 이 목록을 사용합니다. 영문 단위는 자동으로 대문자 처리됩니다.")
        guide.setWordWrap(True)
        root.addWidget(guide)

        self.table = CommonListTable(0, 1)
        self.table.setHorizontalHeaderLabels(["단위"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        for unit in normalize_material_units(units):
            self._append(unit)

        actions = QHBoxLayout()
        add_btn = QPushButton("추가")
        remove_btn = QPushButton("제거")
        up_btn = QPushButton("위로")
        down_btn = QPushButton("아래로")
        add_btn.clicked.connect(self.add_row)
        remove_btn.clicked.connect(self.remove_row)
        up_btn.clicked.connect(lambda: self.move_row(-1))
        down_btn.clicked.connect(lambda: self.move_row(1))
        self.table.insert_above_requested.connect(lambda row: self._append("", row))
        self.table.duplicate_row_requested.connect(self._duplicate_current_row)
        self.table.append_row_requested.connect(lambda: self._append(""))
        self.table.delete_requested.connect(self.remove_row)
        actions.addWidget(add_btn)
        actions.addWidget(remove_btn)
        actions.addWidget(up_btn)
        actions.addWidget(down_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        note = QLabel("※ 목록에 없는 단위를 재료표에서 직접 입력하면 단위 리스트에 추가할지 확인합니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#555;")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._accept_checked)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _append(self, value: str = "", index: int | None = None):
        row = self.table.rowCount() if index is None else max(0, min(index, self.table.rowCount()))
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(normalize_material_unit(value)))
        self.table.setCurrentCell(row, 0)
        return row

    def add_row(self):
        row = self._append("")
        self.table.editItem(self.table.item(row, 0))

    def _duplicate_current_row(self, row: int = -1):
        row = int(row if row >= 0 else self.table.currentRow())
        if 0 <= row < self.table.rowCount():
            value = self.table.item(row, 0).text() if self.table.item(row, 0) else ""
            self._append(value, row + 1)

    def remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def move_row(self, offset: int):
        row = self.table.currentRow()
        target = row + int(offset)
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        value = self.table.item(row, 0).text() if self.table.item(row, 0) else ""
        self.table.removeRow(row)
        self._append(value, target)

    def values(self) -> list[str]:
        return normalize_material_units([
            self.table.item(row, 0).text() if self.table.item(row, 0) else ""
            for row in range(self.table.rowCount())
        ])

    def _accept_checked(self):
        raw = [
            normalize_material_unit(self.table.item(row, 0).text() if self.table.item(row, 0) else "")
            for row in range(self.table.rowCount())
        ]
        nonempty = [value for value in raw if value]
        if len({value.casefold() for value in nonempty}) != len(nonempty):
            QMessageBox.warning(self, "단위 리스트", "중복된 단위가 있습니다.")
            return
        self.accept()


class DisplayLineSettingsDialog(QDialog):
    """도곽/창호 표시선의 색상과 두께만 compact하게 편집한다."""

    def __init__(self, values: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("표시선 두께 / 색상 설정")
        self.resize(430, 300)
        self.setMinimumSize(400, 280)

        root = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("표시선"), 0, 0)
        grid.addWidget(QLabel("두께"), 0, 1)
        grid.addWidget(QLabel("색상"), 0, 2)

        self.controls = {}
        specs = [
            ("boundary", "실구분 기본선", values.get("boundary_width", 9), values.get("boundary_color", "#0066FF")),
            ("deduction", "공제선", values.get("deduction_width", 1), values.get("deduction_color", "#FF00FF")),
            ("selection", "선택 실 표시선", values.get("selection_width", 18), values.get("selection_color", "#FF0000")),
            ("wall_selection", "벽 지정 선택선", values.get("wall_selection_width", 2), values.get("wall_selection_color", "#FF00FF")),
            ("opening", "창호선", values.get("opening_line_width", 4), values.get("opening_line_color", "#8ED4E3")),
        ]
        for row, (key, label, width_value, color_value) in enumerate(specs, start=1):
            width = QSpinBox()
            width.setRange(1, 100)
            width.setValue(int(width_value))
            width.setSuffix(" px")
            width.setFixedWidth(90)
            color = ColorButton(str(color_value))
            color.setMinimumWidth(120)
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(width, row, 1)
            grid.addWidget(color, row, 2)
            self.controls[key] = (width, color)

        grid.setColumnStretch(0, 1)
        root.addLayout(grid)
        root.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> dict:
        result = {}
        for key, (width, color) in self.controls.items():
            result[f"{key}_width"] = int(width.value())
            result[f"{key}_color"] = color.color_name()
        return result


class DisplaySettingsDialog(QDialog):
    """도면 표시 관련 설정을 한 곳에서 관리한다.

    V24.10부터 모델리스 창으로 사용한다. 창을 띄운 상태에서도 메인 도면을
    Pan/Zoom할 수 있고, 적용 버튼을 눌렀을 때만 변경값을 실제 화면에 반영한다.
    """

    apply_requested = Signal()

    def __init__(self, values: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("표시 설정")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(650, 720)
        self.setMinimumSize(600, 650)
        root = QVBoxLayout(self)
        tabs = QTabWidget()

        # 기존 메인 설정창의 '실 표시 설정'을 그대로 이동한다.
        display = QWidget()
        display_form = QFormLayout(display)
        self.background_pixels_spin = QSpinBox(); self.background_pixels_spin.setRange(400, 5000); self.background_pixels_spin.setSingleStep(100); self.background_pixels_spin.setSuffix(" px"); self.background_pixels_spin.setValue(max(400, min(5000, int(values["background_max_pixels"]))))
        self.background_render_mode = QComboBox(); self.background_render_mode.addItem("PNG 캐시", "png"); self.background_render_mode.addItem("벡터 캐시", "vector"); self.background_render_mode.setCurrentIndex(max(0, self.background_render_mode.findData(values.get("background_render_mode", "png"))))
        self.background_render_mode.setToolTip("PNG: 기존 고정 이미지 캐시 / 벡터: 도곽 내부 선·문자를 벡터 캐시에서 직접 표시")
        self.background_style = QComboBox(); self.background_style.addItem("CAD 원래 색상", "cad"); self.background_style.addItem("회색톤", "gray"); self.background_style.setCurrentIndex(max(0, self.background_style.findData(values["background_style"])))
        self.opacity_slider = QSlider(Qt.Horizontal); self.opacity_slider.setRange(0, 100); self.opacity_slider.setValue(int(values["background_opacity"]))
        self.opacity_label = QLabel(f"{self.opacity_slider.value()}%"); self.opacity_slider.valueChanged.connect(lambda value: self.opacity_label.setText(f"{value}%"))
        opacity_row = QWidget(); opacity_layout = QHBoxLayout(opacity_row); opacity_layout.setContentsMargins(0,0,0,0); opacity_layout.addWidget(self.opacity_slider); opacity_layout.addWidget(self.opacity_label)
        # 선택실 보기 전용 치수 옵션은 도곽 우하단의 "치수 표시" 상자에서 바로 조절한다.
        # 표시설정 창에서는 중복 UI를 없애되 기존 저장값은 그대로 통과시켜 호환성을 유지한다.
        self._selected_room_passthrough = {
            "edge_length_font_size": int(values.get("edge_length_font_size", 45)),
            "edge_length_width_ratio": int(values.get("edge_length_width_ratio", 80)),
            "edge_length_offset": int(values.get("edge_length_offset", 150)),
            "edge_length_show_unit_m": bool(values.get("edge_length_show_unit_m", True)),
            "label_selected_view_weight": float(values.get("label_selected_view_weight", 1.00)),
        }
        self.room_label_scale_mode = QComboBox()
        self.room_label_scale_mode.addItem("실면적 대비 문자크기", "room_area")
        self.room_label_scale_mode.addItem("전체 표시 대비 문자크기", "full_width")
        self.room_label_scale_mode.setCurrentIndex(max(0, self.room_label_scale_mode.findData(values.get("room_label_scale_mode", "full_width"))))
        self.room_label_scale_spin = QDoubleSpinBox(); self.room_label_scale_spin.setRange(0.10,10.00); self.room_label_scale_spin.setSingleStep(0.10); self.room_label_scale_spin.setDecimals(2); self.room_label_scale_spin.setValue(float(values.get("room_label_area_scale",1.0)))
        self.label_full_weight_spin = QDoubleSpinBox(); self.label_full_weight_spin.setRange(0.10,3.00); self.label_full_weight_spin.setSingleStep(0.05); self.label_full_weight_spin.setDecimals(2); self.label_full_weight_spin.setValue(float(values.get("label_full_view_weight",0.70)))
        self.boundary_color = ColorButton(values["boundary_color"])
        self.boundary_width = QSpinBox(); self.boundary_width.setRange(1,100); self.boundary_width.setValue(int(values["boundary_width"])); self.boundary_width.setSuffix(" px")
        self.deduction_color = ColorButton(values.get("deduction_color", "#FF00FF"))
        self.deduction_width = QSpinBox(); self.deduction_width.setRange(1,100); self.deduction_width.setValue(int(values.get("deduction_width",1))); self.deduction_width.setSuffix(" px")
        self.render_quality = QComboBox(); self.render_quality.addItem("일반","normal"); self.render_quality.addItem("안티앨리어싱","antialias"); self.render_quality.addItem("고품질 안티앨리어싱","high"); self.render_quality.setCurrentIndex(max(0,self.render_quality.findData(values["render_quality"])))
        self.selection_color = ColorButton(values["selection_color"])
        self.selection_width = QSpinBox(); self.selection_width.setRange(1,100); self.selection_width.setValue(int(values["selection_width"])); self.selection_width.setSuffix(" px")
        self.wall_selection_color = ColorButton(values.get("wall_selection_color", "#FF00FF"))
        self.wall_selection_width = QSpinBox(); self.wall_selection_width.setRange(1,100); self.wall_selection_width.setValue(int(values.get("wall_selection_width",2))); self.wall_selection_width.setSuffix(" px")
        self.opening_line_color = ColorButton(values.get("opening_line_color", "#8ED4E3"))
        self.opening_line_width = QSpinBox(); self.opening_line_width.setRange(1,100); self.opening_line_width.setValue(int(values.get("opening_line_width",4))); self.opening_line_width.setSuffix(" px")
        self.line_style_btn = QPushButton("표시선 두께 / 색상 설정")
        self.line_style_btn.clicked.connect(self._open_line_style_dialog)
        self.wall_selection_zoom_scale = QDoubleSpinBox(); self.wall_selection_zoom_scale.setRange(0.0,1.0); self.wall_selection_zoom_scale.setSingleStep(0.1); self.wall_selection_zoom_scale.setDecimals(2); self.wall_selection_zoom_scale.setValue(float(values.get("wall_selection_zoom_scale",0.35)))
        self.wall_selection_zoom_scale.setToolTip("실구분선/선택실선/공제선/그룹선/벽지정선 공통 · 0.0=화면 픽셀 고정, 0.5=줌 절반 비례, 1.0=줌 완전 비례")
        self.group_overlay_opacity = QSpinBox(); self.group_overlay_opacity.setRange(0,100); self.group_overlay_opacity.setValue(int(values.get("group_overlay_opacity",35))); self.group_overlay_opacity.setSuffix(" %")
        for label, widget in [
            ("CAD 배경 방식",self.background_render_mode),("CAD 배경 이미지 최대 크기",self.background_pixels_spin),("CAD 배경 표시",self.background_style),("CAD 배경 투명도",opacity_row),
            ("실명 문자크기 기준",self.room_label_scale_mode),("실명 문자 배율",self.room_label_scale_spin),("층 전체보기 문자 가중치",self.label_full_weight_spin),
            ("표시선 설정",self.line_style_btn),("벡터 선 렌더링 품질",self.render_quality),
            ("선 굵기 확대/축소 비율",self.wall_selection_zoom_scale),
            ("마감지정 선 투명도",self.group_overlay_opacity)]:
            display_form.addRow(label,widget)
        tabs.addTab(display, "실 표시 설정")

        # V15.2의 CAD 색상별 선두께 설정을 같은 창의 두 번째 탭으로 묶는다.
        cad_bg = QWidget()
        cad_root = QVBoxLayout(cad_bg)
        form = QFormLayout()
        self.default_width = QDoubleSpinBox()
        self.default_width.setRange(0.0, 20.0); self.default_width.setDecimals(3); self.default_width.setSingleStep(0.01); self.default_width.setSuffix(" px")
        self.default_width.setValue(float(values.get("background_default_line_width", DEFAULT_WIDTH)))
        form.addRow("기본 / 미지정 선두께", self.default_width)
        self.zoomout_width_weight = QDoubleSpinBox()
        self.zoomout_width_weight.setRange(0.0, 1.0); self.zoomout_width_weight.setDecimals(2); self.zoomout_width_weight.setSingleStep(0.05)
        self.zoomout_width_weight.setValue(float(values.get("background_zoomout_width_weight", 0.25)))
        self.zoomout_width_weight.setToolTip("전체보기보다 축소할 때 벡터선이 너무 굵어 보이는 정도를 줄입니다. 0.00=기존 화면픽셀 고정, 1.00=축소배율에 강하게 비례. 확대 시에는 적용하지 않습니다.")
        form.addRow("축소시 선두께 가중치", self.zoomout_width_weight)
        self.keep_red = QCheckBox("회색톤에서도 빨간색(ACI 1)은 원래 색상으로 표시")
        self.keep_red.setChecked(bool(values.get("background_keep_red", False)))
        form.addRow("색상 표시", self.keep_red)
        cad_root.addLayout(form)

        cad_root.addWidget(QLabel("CAD 기본 색상번호별 선두께 가중치 (1px 기준, 비례 적용) (0~9, 255)"))
        widths = parse_width_map(values.get("background_aci_widths", ""))
        self.basic_spins = {}
        grid = QFormLayout()
        for aci in [0,1,2,3,4,5,6,7,8,9]:
            spin = QDoubleSpinBox(); spin.setRange(0.0,20.0); spin.setDecimals(3); spin.setSingleStep(0.01); spin.setSuffix(" px")
            spin.setValue(float(widths.get(aci, DEFAULT_ACI_WIDTHS.get(aci, DEFAULT_WIDTH))))
            self.basic_spins[aci] = spin
            label = "255 / 0" if aci == 0 else str(aci)
            grid.addRow(f"색상 {label}", spin)
        cad_root.addLayout(grid)

        cad_root.addWidget(QLabel("추가 특정 색상번호"))
        self.custom_table = QTableWidget(0,2)
        self.custom_table.setHorizontalHeaderLabels(["색상번호", "가중치(1px 기준)"])
        self.custom_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        basic = set(self.basic_spins) | {255}
        for aci, width in sorted(widths.items()):
            if aci in basic:
                continue
            row=self.custom_table.rowCount(); self.custom_table.insertRow(row)
            self.custom_table.setItem(row,0,QTableWidgetItem(str(aci))); self.custom_table.setItem(row,1,QTableWidgetItem(f"{width:g}"))
        cad_root.addWidget(self.custom_table,1)
        row = QHBoxLayout()
        add_btn=QPushButton("추가"); del_btn=QPushButton("선택 삭제")
        add_btn.clicked.connect(self._add_custom); del_btn.clicked.connect(self._delete_custom)
        row.addWidget(add_btn); row.addWidget(del_btn); row.addStretch(1); cad_root.addLayout(row)
        tabs.addTab(cad_bg, "CAD 배경선 설정")

        root.addWidget(tabs, 1)

        # V24.10: 확인=적용 후 닫기 / 적용=창 유지 / 닫기=미적용 상태로 닫기.
        # QDialog.exec()를 사용하지 않는 모델리스 창이므로 메인 도면도 계속 조작할 수 있다.
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.ok_button = QPushButton("확인")
        self.apply_button = QPushButton("적용")
        self.close_button = QPushButton("닫기")
        self.ok_button.clicked.connect(self._confirm_and_close)
        self.apply_button.clicked.connect(self.apply_requested.emit)
        self.close_button.clicked.connect(self.close)
        button_row.addWidget(self.ok_button)
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.close_button)
        root.addLayout(button_row)

    def _confirm_and_close(self):
        self.apply_requested.emit()
        self.close()

    def _open_line_style_dialog(self):
        dialog = DisplayLineSettingsDialog({
            "boundary_color": self.boundary_color.color_name(),
            "boundary_width": self.boundary_width.value(),
            "deduction_color": self.deduction_color.color_name(),
            "deduction_width": self.deduction_width.value(),
            "selection_color": self.selection_color.color_name(),
            "selection_width": self.selection_width.value(),
            "wall_selection_color": self.wall_selection_color.color_name(),
            "wall_selection_width": self.wall_selection_width.value(),
            "opening_line_color": self.opening_line_color.color_name(),
            "opening_line_width": self.opening_line_width.value(),
        }, self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        for key, color_button, width_spin in [
            ("boundary", self.boundary_color, self.boundary_width),
            ("deduction", self.deduction_color, self.deduction_width),
            ("selection", self.selection_color, self.selection_width),
            ("wall_selection", self.wall_selection_color, self.wall_selection_width),
            ("opening", self.opening_line_color, self.opening_line_width),
        ]:
            color_button.set_color(values.get(f"{key}_color", color_button.color_name()))
            width_spin.setValue(int(values.get(f"{key}_width", width_spin.value())))

    def _add_custom(self):
        row=self.custom_table.rowCount(); self.custom_table.insertRow(row)
        self.custom_table.setItem(row,0,QTableWidgetItem("10")); self.custom_table.setItem(row,1,QTableWidgetItem("0.0"))
        self.custom_table.setCurrentCell(row,0); self.custom_table.editItem(self.custom_table.item(row,0))

    def _delete_custom(self):
        rows=sorted({i.row() for i in self.custom_table.selectedIndexes()}, reverse=True)
        for row in rows: self.custom_table.removeRow(row)

    def values(self):
        widths={aci: spin.value() for aci,spin in self.basic_spins.items()}
        widths[255] = widths.get(0, 0.03)
        for row in range(self.custom_table.rowCount()):
            try:
                aci=int(self.custom_table.item(row,0).text().strip()); width=float(self.custom_table.item(row,1).text().strip())
            except Exception:
                continue
            if 0 <= aci <= 255 and aci not in self.basic_spins and 0.0 <= width <= 20.0:
                widths[aci]=width
        return {
            "background_render_mode": self.background_render_mode.currentData(),
            "background_max_pixels": self.background_pixels_spin.value(),
            "edge_length_font_size": self._selected_room_passthrough["edge_length_font_size"],
            "room_label_scale_mode": self.room_label_scale_mode.currentData(),
            "room_label_area_scale": self.room_label_scale_spin.value(),
            "edge_length_width_ratio": self._selected_room_passthrough["edge_length_width_ratio"],
            "edge_length_offset": self._selected_room_passthrough["edge_length_offset"],
            "edge_length_show_unit_m": self._selected_room_passthrough["edge_length_show_unit_m"],
            "label_full_view_weight": self.label_full_weight_spin.value(),
            "label_selected_view_weight": self._selected_room_passthrough["label_selected_view_weight"],
            "background_style": self.background_style.currentData(),
            "background_opacity": self.opacity_slider.value(),
            "boundary_color": self.boundary_color.color_name(),
            "boundary_width": self.boundary_width.value(),
            "deduction_color": self.deduction_color.color_name(),
            "deduction_width": self.deduction_width.value(),
            "render_quality": self.render_quality.currentData(),
            "selection_color": self.selection_color.color_name(),
            "selection_width": self.selection_width.value(),
            "wall_selection_color": self.wall_selection_color.color_name(),
            "wall_selection_width": self.wall_selection_width.value(),
            "opening_line_color": self.opening_line_color.color_name(),
            "opening_line_width": self.opening_line_width.value(),
            "wall_selection_zoom_scale": self.wall_selection_zoom_scale.value(),
            "group_overlay_opacity": self.group_overlay_opacity.value(),
            "background_default_line_width": self.default_width.value(),
            "background_zoomout_width_weight": self.zoomout_width_weight.value(),
            "background_aci_widths": serialize_width_map(widths),
            "background_keep_red": self.keep_red.isChecked(),
        }

class LayerBlockListDialog(QDialog):
    """CAD 업무 레이어와 블록 규격을 한 곳에서 편집하는 창."""

    def __init__(self, values: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("레이어 / 블록 리스트")
        self.resize(920, 560)
        self.setMinimumSize(820, 500)

        root = QVBoxLayout(self)
        tabs = QTabWidget()

        def layer_row(name_value: str, color_value: str):
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(name_value)
            edit.setMinimumWidth(430)
            color = ColorButton(color_value)
            color.setMinimumWidth(105)
            color.setToolTip("없는 레이어를 새로 만들 때 적용할 초기 CAD 색상")
            layout.addWidget(edit, 1)
            layout.addWidget(QLabel("초기색상"))
            layout.addWidget(color)
            return row, edit, color

        # 레이어 설정은 탭을 넘기는 방식이 아니라 한 페이지 안에서
        # 공통 / 구조 / 마감 / 외부 구역을 세로로 나눠 모두 보여준다.
        # 항목이 늘어나 창 높이를 넘으면 레이어 페이지 자체를 스크롤한다.
        layer_tab = QWidget()
        layer_tab_root = QVBoxLayout(layer_tab)
        layer_tab_root.setContentsMargins(0, 0, 0, 0)

        layer_scroll = QScrollArea()
        layer_scroll.setWidgetResizable(True)
        layer_scroll.setFrameShape(QScrollArea.NoFrame)
        layer_scroll_content = QWidget()
        layer_scroll_layout = QVBoxLayout(layer_scroll_content)
        layer_scroll_layout.setContentsMargins(10, 10, 10, 10)
        layer_scroll_layout.setSpacing(10)

        for group_name in LAYER_GROUPS:
            group_box = QGroupBox(group_name)
            form = QFormLayout(group_box)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(8)
            for key, spec in LAYER_DEFINITIONS.items():
                if spec["group"] != group_name:
                    continue
                row, edit, color = layer_row(
                    values.get(key, default_layer_name(key)),
                    values.get(f"{key}_color", default_layer_color(key)),
                )
                setattr(self, key, edit)
                setattr(self, f"{key}_color", color)
                form.addRow(spec["label"], row)
            layer_scroll_layout.addWidget(group_box)

        note = QLabel("※ 레이어명은 설정값으로만 참조하며, 이 창에서 바꾸면 읽기·쓰기·레이어 생성에 같은 값이 사용됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#555;")
        layer_scroll_layout.addWidget(note)
        layer_scroll_layout.addStretch(1)

        layer_scroll.setWidget(layer_scroll_content)
        layer_tab_root.addWidget(layer_scroll)
        tabs.addTab(layer_tab, "레이어")

        block_tab = QWidget()
        block_root = QVBoxLayout(block_tab)
        room_group = QGroupBox("실명 블록")
        room_form = QFormLayout(room_group)
        self.block_name = QLineEdit(values["block_name"])
        self.floor_tag = QLineEdit(values["floor_tag"])
        self.room_name_tag = QLineEdit(values["room_name_tag"])
        self.room_number_tags = QLineEdit(values["room_number_tags"])
        room_form.addRow("블록명", self.block_name)
        room_form.addRow("층", self.floor_tag)
        room_form.addRow("실명", self.room_name_tag)
        room_form.addRow("실번호", self.room_number_tags)
        block_root.addWidget(room_group)

        status_group = QGroupBox("블록 라이브러리")
        status_layout = QVBoxLayout(status_group)
        self.library_status = QLabel(values.get("block_library_status", "아직 가져온 블록이 없습니다."))
        self.library_status.setWordWrap(True)
        status_layout.addWidget(self.library_status)
        status_layout.addWidget(QLabel("※ CAD 연동 창의 '블록 가져오기'로 현재 도면의 규격 블록을 저장합니다."))
        block_root.addWidget(status_group)
        block_root.addStretch(1)
        tabs.addTab(block_tab, "블록")

        root.addWidget(tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


class CommonListWidget(QListWidget):
    """간단 목록에도 공통 F9/F3/F4/마지막행 ↓ 입력 규칙을 제공한다."""
    insert_above_requested = Signal(int)
    duplicate_row_requested = Signal(int)
    append_row_requested = Signal()
    delete_requested = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._down_append_guard = False
        self.itemChanged.connect(lambda _item: setattr(self, "_down_append_guard", False))

    def keyPressEvent(self, event):
        row = self.currentRow()
        if event.key() == Qt.Key_F9 and row >= 0:
            self.insert_above_requested.emit(row); return
        if event.key() == Qt.Key_F3 and row >= 0:
            self.duplicate_row_requested.emit(row); return
        if event.key() == Qt.Key_F4 and row >= 0:
            if QMessageBox.question(self, "목록 삭제", "현재 항목을 삭제하시겠습니까?", QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
                self._down_append_guard = False; self.delete_requested.emit()
            return
        if event.key() == Qt.Key_Down and row >= 0 and row == self.count() - 1:
            item = self.item(row)
            blank = item is None or not str(item.text() or "").strip()
            if blank: return
            self.append_row_requested.emit()
            last = self.item(self.count() - 1) if self.count() else None
            self._down_append_guard = last is None or not str(last.text() or "").strip()
            return
        super().keyPressEvent(event)


class WindowCategoryDialog(QDialog):
    """창호 구분명만 간단히 추가/삭제/이름변경하는 설정창."""
    def __init__(self, categories=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("창호 구분")
        self.resize(380, 420)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("창호 탭의 구분 열에서 사용할 목록입니다."))
        self.list = CommonListWidget()
        self.list.addItems(normalize_window_categories(categories))
        self.list.itemDoubleClicked.connect(lambda _item: self.rename_selected())
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        add_btn = QPushButton("추가")
        delete_btn = QPushButton("삭제")
        rename_btn = QPushButton("이름변경")
        up_btn = QPushButton("위로")
        down_btn = QPushButton("아래로")
        row.addWidget(add_btn); row.addWidget(delete_btn); row.addWidget(rename_btn)
        row.addStretch(1); row.addWidget(up_btn); row.addWidget(down_btn)
        root.addLayout(row)
        add_btn.clicked.connect(self.add_item)
        delete_btn.clicked.connect(self.delete_selected)
        rename_btn.clicked.connect(self.rename_selected)
        up_btn.clicked.connect(lambda: self.move_selected(-1))
        down_btn.clicked.connect(lambda: self.move_selected(1))
        self.list.insert_above_requested.connect(self._insert_blank_at)
        self.list.duplicate_row_requested.connect(self._duplicate_at)
        self.list.append_row_requested.connect(lambda: self._insert_blank_at(self.list.count()))
        self.list.delete_requested.connect(self.delete_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _insert_blank_at(self, row: int):
        row = max(0, min(int(row), self.list.count()))
        item = QListWidgetItem("")
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.list.insertItem(row, item)
        self.list.setCurrentItem(item)
        self.list.editItem(item)

    def _duplicate_at(self, row: int):
        if not (0 <= int(row) < self.list.count()):
            return
        source = self.list.item(int(row))
        item = QListWidgetItem(source.text() if source is not None else "")
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.list.insertItem(int(row) + 1, item)
        self.list.setCurrentItem(item)
        self.list.editItem(item)

    def _names(self):
        return [self.list.item(i).text().strip() for i in range(self.list.count()) if self.list.item(i).text().strip()]

    def add_item(self):
        name, ok = QInputDialog.getText(self, "창호 구분 추가", "구분명")
        name = str(name or "").strip()
        if ok and name:
            if name in self._names():
                QMessageBox.information(self, "창호 구분", "이미 등록된 이름입니다.")
                return
            self.list.addItem(name)
            self.list.setCurrentRow(self.list.count()-1)

    def delete_selected(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def rename_selected(self):
        item = self.list.currentItem()
        if item is None:
            return
        name, ok = QInputDialog.getText(self, "창호 구분 이름변경", "구분명", text=item.text())
        name = str(name or "").strip()
        if ok and name:
            if name != item.text() and name in self._names():
                QMessageBox.information(self, "창호 구분", "이미 등록된 이름입니다.")
                return
            item.setText(name)

    def move_selected(self, delta):
        row = self.list.currentRow()
        target = row + int(delta)
        if row < 0 or not (0 <= target < self.list.count()):
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)

    def values(self):
        return normalize_window_categories(self._names())


class SettingsDialog(QDialog):
    create_layers_requested = Signal()

    def __init__(self, values: dict, has_current_dxf: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.setModal(True)
        self.resize(900, 720)
        self.setMinimumSize(820, 640)
        self._background_exclude_patterns = values.get("background_exclude_patterns", "■*")
        self._room_category_rules = normalize_room_category_rules(values.get("room_category_rules"))
        self._finish_formula_codes = normalize_formula_codes(values.get("finish_formula_codes"))
        self._finish_categories = normalize_finish_categories(values.get("finish_categories"))
        self._material_trades = normalize_material_trades(values.get("material_trades"))
        self._material_units = normalize_material_units(values.get("material_units"))
        self._window_categories = normalize_window_categories(values.get("window_categories"))
        self._layer_block_dialog = LayerBlockListDialog(values, self)

        # 기존 호출부와 values()가 같은 위젯 이름을 계속 사용하도록 연결한다.
        for key in LAYER_DEFINITIONS:
            setattr(self, key, getattr(self._layer_block_dialog, key))
            setattr(self, f"{key}_color", getattr(self._layer_block_dialog, f"{key}_color"))
        self.block_name = self._layer_block_dialog.block_name
        self.floor_tag = self._layer_block_dialog.floor_tag
        self.room_name_tag = self._layer_block_dialog.room_name_tag
        self.room_number_tags = self._layer_block_dialog.room_number_tags

        root = QVBoxLayout(self)
        tabs = QTabWidget()

        common_tab = QWidget()
        common_root = QVBoxLayout(common_tab)
        backup_group = QGroupBox("프로젝트 자동 백업")
        backup_form = QFormLayout(backup_group)
        self.auto_backup_minutes = QSpinBox()
        self.auto_backup_minutes.setRange(1, 1440)
        self.auto_backup_minutes.setSuffix(" 분")
        self.auto_backup_minutes.setValue(max(1, min(1440, int(values.get("auto_backup_minutes", 30) or 30))))
        backup_form.addRow("백업 간격", self.auto_backup_minutes)
        backup_note = QLabel("활성 프로젝트의 재료·내부·캐드배경 캐시를 프로젝트/캐쉬/자동저장에 백그라운드로 저장합니다.")
        backup_note.setWordWrap(True)
        backup_note.setStyleSheet("color:#555;")
        backup_form.addRow("", backup_note)
        common_root.addWidget(backup_group)
        common_root.addStretch(1)
        tabs.addTab(common_tab, "공통")

        cad = QWidget(); cad_root = QVBoxLayout(cad)
        basic = QGroupBox("기본"); basic_form = QFormLayout(basic)
        self.layer_block_btn = QPushButton("레이어 / 블록 리스트")
        self.layer_block_btn.clicked.connect(self.open_layer_block_list)
        basic_form.addRow("CAD 업무 설정", self.layer_block_btn)
        self.layer_block_summary = QLabel(); self._refresh_layer_block_summary()
        self.layer_block_summary.setWordWrap(True)
        basic_form.addRow("현재 설정", self.layer_block_summary)

        exclude_row = QWidget(); exclude_layout = QHBoxLayout(exclude_row); exclude_layout.setContentsMargins(0,0,0,0)
        self.exclude_summary = QLabel(); self._refresh_exclude_summary(); self.edit_exclude_btn = QPushButton("편집하기"); self.edit_exclude_btn.clicked.connect(self.edit_exclude_patterns)
        exclude_layout.addWidget(self.exclude_summary,1); exclude_layout.addWidget(self.edit_exclude_btn)
        basic_form.addRow("배경 제외 레이어", exclude_row)
        cad_root.addWidget(basic, 1)

        bottom = QHBoxLayout()
        dxf_group = QGroupBox("CAD·DXF"); dxf_layout = QVBoxLayout(dxf_group)
        self.create_layers_btn = QPushButton("현재 DXF에 누락 레이어 생성"); self.create_layers_btn.setEnabled(has_current_dxf); self.create_layers_btn.clicked.connect(self.create_layers_requested.emit)
        dxf_layout.addWidget(self.create_layers_btn); dxf_layout.addWidget(QLabel("※ 원본은 보존하고 '_필수레이어추가.dxf' 파일로 저장합니다.")); dxf_layout.addStretch(1)
        link_group = QGroupBox("CAD 연동"); link_layout = QVBoxLayout(link_group)
        link_layout.addWidget(QLabel("연동 레이어 생성, 블록 가져오기와\n가져오기·내보내기는 메인 화면의 CAD 연동 창에서 실행합니다.")); link_layout.addStretch(1)
        bottom.addWidget(dxf_group,1); bottom.addWidget(link_group,1); cad_root.addLayout(bottom,1)
        tabs.addTab(cad, "CAD 설정")

        interior = QWidget()
        interior_root = QVBoxLayout(interior)
        category_group = QGroupBox("실 구분")
        category_layout = QVBoxLayout(category_group)
        category_row = QHBoxLayout()
        self.room_category_rules_btn = QPushButton("실 구분 기준")
        self.room_category_rules_btn.clicked.connect(self.edit_room_category_rules)
        self.room_category_summary = QLabel()
        self._refresh_room_category_summary()
        category_row.addWidget(self.room_category_rules_btn)
        category_row.addWidget(self.room_category_summary, 1)
        category_layout.addLayout(category_row)
        category_note = QLabel("※ 자동판정은 실 구분이 비어 있는 상태에서 실명이 처음 입력될 때만 실행됩니다.")
        category_note.setWordWrap(True)
        category_note.setStyleSheet("color:#555;")
        category_layout.addWidget(category_note)
        interior_root.addWidget(category_group)

        self.interior_reserved_group_1 = QGroupBox("재료마감")
        finish_layout = QVBoxLayout(self.interior_reserved_group_1)
        finish_formula_row = QHBoxLayout()
        self.finish_formula_codes_btn = QPushButton("산식 코드")
        self.finish_formula_codes_btn.clicked.connect(self.edit_finish_formula_codes)
        self.finish_formula_summary = QLabel()
        finish_formula_row.addWidget(self.finish_formula_codes_btn)
        finish_formula_row.addWidget(self.finish_formula_summary, 1)
        finish_layout.addLayout(finish_formula_row)
        finish_category_row = QHBoxLayout()
        self.finish_categories_btn = QPushButton("구분 리스트")
        self.finish_categories_btn.clicked.connect(self.edit_finish_categories)
        self.finish_category_summary = QLabel()
        finish_category_row.addWidget(self.finish_categories_btn)
        finish_category_row.addWidget(self.finish_category_summary, 1)
        finish_layout.addLayout(finish_category_row)
        finish_note = QLabel("※ 구분에 연결된 코드의 산식은 재료마감 목록에서 구분을 선택할 때 적용합니다.")
        finish_note.setWordWrap(True)
        finish_note.setStyleSheet("color:#555;")
        finish_layout.addWidget(finish_note)
        finish_layout.addStretch(1)
        self._refresh_finish_summaries()
        self.interior_reserved_group_1.setMinimumHeight(150)
        self.interior_reserved_group_2 = QGroupBox("")
        self.interior_reserved_group_2.setMinimumHeight(150)
        interior_root.addWidget(self.interior_reserved_group_1, 1)
        interior_root.addWidget(self.interior_reserved_group_2, 1)
        tabs.addTab(interior, "내부")

        material_tab = QWidget()
        material_root = QVBoxLayout(material_tab)
        trade_group = QGroupBox("공종")
        trade_layout = QVBoxLayout(trade_group)
        trade_row = QHBoxLayout()
        self.material_trades_btn = QPushButton("공종 리스트")
        self.material_trades_btn.clicked.connect(self.edit_material_trades)
        self.material_trades_summary = QLabel()
        trade_row.addWidget(self.material_trades_btn)
        trade_row.addWidget(self.material_trades_summary, 1)
        trade_layout.addLayout(trade_row)
        trade_note = QLabel("※ 재료마감 목록의 공종 열은 이 목록에서 선택합니다.")
        trade_note.setStyleSheet("color:#555;")
        trade_layout.addWidget(trade_note)
        material_root.addWidget(trade_group)

        unit_group = QGroupBox("단위")
        unit_layout = QVBoxLayout(unit_group)
        unit_row = QHBoxLayout()
        self.material_units_btn = QPushButton("단위 리스트")
        self.material_units_btn.clicked.connect(self.edit_material_units)
        self.material_units_summary = QLabel()
        unit_row.addWidget(self.material_units_btn)
        unit_row.addWidget(self.material_units_summary, 1)
        unit_layout.addLayout(unit_row)
        unit_note = QLabel("※ 재료/내역의 단위 입력은 이 목록에서 선택하며, 미등록 단위를 입력하면 추가 여부를 확인합니다.")
        unit_note.setWordWrap(True)
        unit_note.setStyleSheet("color:#555;")
        unit_layout.addWidget(unit_note)
        material_root.addWidget(unit_group)

        window_group = QGroupBox("창호")
        window_layout = QVBoxLayout(window_group)
        window_row = QHBoxLayout()
        self.window_categories_btn = QPushButton("창호 구분")
        self.window_categories_btn.clicked.connect(self.edit_window_categories)
        self.window_categories_summary = QLabel()
        window_row.addWidget(self.window_categories_btn)
        window_row.addWidget(self.window_categories_summary, 1)
        window_layout.addLayout(window_row)
        window_note = QLabel("※ 창호 탭의 구분 열에서 사용하는 목록입니다.")
        window_note.setStyleSheet("color:#555;")
        window_layout.addWidget(window_note)
        material_root.addWidget(window_group)
        material_root.addStretch(1)
        self._refresh_material_trade_summary()
        self._refresh_material_unit_summary()
        self._refresh_window_category_summary()
        tabs.addTab(material_tab, "재료")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        root.addWidget(tabs); root.addWidget(buttons)

    def open_layer_block_list(self):
        if self._layer_block_dialog.exec():
            self._refresh_layer_block_summary()

    def _refresh_layer_block_summary(self):
        self.layer_block_summary.setText(
            f"레이어 {len(LAYER_DEFINITIONS)}개 · 실명 블록 {self.block_name.text().strip() or '(미지정)'} · "
            f"속성 태그 3개"
        )

    def _refresh_exclude_summary(self):
        count = len([x for x in self._background_exclude_patterns.splitlines() if x.strip()])
        self.exclude_summary.setText(f"{count}개 패턴 등록됨")

    def edit_exclude_patterns(self):
        dialog = LayerPatternEditor(self._background_exclude_patterns, self)
        if dialog.exec():
            self._background_exclude_patterns = dialog.text()
            self._refresh_exclude_summary()

    def _refresh_room_category_summary(self):
        count = sum(len(values) for values in self._room_category_rules.values())
        self.room_category_summary.setText(f"8개 구분 · {count}개 판단 문자열")

    def edit_room_category_rules(self):
        dialog = RoomCategoryRulesDialog(self._room_category_rules, self)
        if dialog.exec():
            self._room_category_rules = dialog.values()
            self._refresh_room_category_summary()

    def _refresh_finish_summaries(self):
        self.finish_formula_summary.setText(f"{len(self._finish_formula_codes)}개 코드 등록됨")
        linked = sum(1 for row in self._finish_categories if row.get("formula_code") or row.get("formula"))
        self.finish_category_summary.setText(f"{len(self._finish_categories)}개 구분 · {linked}개 산식 설정")

    def edit_finish_formula_codes(self):
        dialog = FormulaCodeDialog(self._finish_formula_codes, self)
        if dialog.exec():
            self._finish_formula_codes = normalize_formula_codes(dialog.values())
            valid_codes = {row["code"].casefold() for row in self._finish_formula_codes}
            for row in self._finish_categories:
                if row.get("formula_code", "").casefold() not in valid_codes:
                    row["formula_code"] = ""
            self._refresh_finish_summaries()

    def edit_finish_categories(self):
        dialog = FinishCategoryDialog(self._finish_categories, self._finish_formula_codes, self)
        if dialog.exec():
            self._finish_categories = normalize_finish_categories(dialog.values())
            self._finish_formula_codes = normalize_formula_codes(dialog.formula_values())
            self._refresh_finish_summaries()

    def _refresh_material_trade_summary(self):
        major_count = sum(1 for row in self._material_trades if not row.get("parent_code"))
        child_count = len(self._material_trades) - major_count
        self.material_trades_summary.setText(f"대분류 {major_count}개 · 하부 항목 {child_count}개")

    def edit_material_trades(self):
        dialog = MaterialTradeDialog(self._material_trades, self)
        if dialog.exec():
            self._material_trades = normalize_material_trades(dialog.values())
            self._refresh_material_trade_summary()

    def _refresh_material_unit_summary(self):
        self.material_units_summary.setText(f"{len(self._material_units)}개 단위 등록됨")

    def _refresh_window_category_summary(self):
        self.window_categories_summary.setText(f"{len(self._window_categories)}개 구분 · " + ", ".join(self._window_categories[:5]))

    def edit_window_categories(self):
        dialog = WindowCategoryDialog(self._window_categories, self)
        if dialog.exec():
            self._window_categories = normalize_window_categories(dialog.values())
            self._refresh_window_category_summary()

    def edit_material_units(self):
        dialog = MaterialUnitDialog(self._material_units, self)
        if dialog.exec():
            self._material_units = normalize_material_units(dialog.values())
            self._refresh_material_unit_summary()

    def values(self) -> dict:
        return {
            **{key: getattr(self, key).text().strip() for key in LAYER_DEFINITIONS},
            **{f"{key}_color": getattr(self, f"{key}_color").color_name() for key in LAYER_DEFINITIONS},
            "block_name": self.block_name.text().strip(),
            "floor_tag": self.floor_tag.text().strip().upper(),
            "room_name_tag": self.room_name_tag.text().strip().upper(),
            "room_number_tags": self.room_number_tags.text().strip().upper(),
            "background_exclude_patterns": self._background_exclude_patterns,
            "room_category_rules": normalize_room_category_rules(self._room_category_rules),
            "finish_formula_codes": normalize_formula_codes(self._finish_formula_codes),
            "finish_categories": normalize_finish_categories(self._finish_categories),
            "material_trades": normalize_material_trades(self._material_trades),
            "material_units": normalize_material_units(self._material_units),
            "window_categories": normalize_window_categories(self._window_categories),
            "auto_backup_minutes": self.auto_backup_minutes.value(),
        }
