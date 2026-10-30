from __future__ import annotations

import uuid

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.common.material_units import normalize_material_unit, normalize_material_units


class _UnitDelegate(QStyledItemDelegate):
    def __init__(self, owner: "OtherWorksTab", parent=None):
        super().__init__(parent)
        self.owner = owner

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.addItems(self.owner.material_units)
        combo.setMaxVisibleItems(18)
        return combo

    def setEditorData(self, editor, index):
        value = normalize_material_unit(index.data(Qt.EditRole) or "")
        if isinstance(editor, QComboBox):
            found = editor.findText(value, Qt.MatchFixedString)
            if found >= 0:
                editor.setCurrentIndex(found)
            else:
                editor.setEditText(value)
            if editor.lineEdit() is not None:
                editor.lineEdit().selectAll()

    def setModelData(self, editor, model, index):
        old = normalize_material_unit(index.data(Qt.EditRole) or "")
        raw = editor.currentText() if isinstance(editor, QComboBox) else ""
        unit = normalize_material_unit(raw)
        if not unit:
            model.setData(index, "", Qt.EditRole)
            return
        known = {value.casefold() for value in self.owner.material_units}
        if unit.casefold() not in known:
            answer = QMessageBox.question(
                self.owner,
                "단위 추가",
                f"단위 리스트에 없는 '{unit}'입니다.\n단위 리스트에 추가할까요?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                model.setData(index, old, Qt.EditRole)
                return
            self.owner.add_material_unit(unit)
        model.setData(index, unit, Qt.EditRole)


class OtherWorksTable(QTableWidget):
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
        return not any(str(self.item(row, c).text() if self.item(row, c) else "").strip() for c in range(self.columnCount()))

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


class OtherWorksTab(QWidget):
    """도면 수량산출로 잡기 어려운 기타공사 직접입력 탭."""

    data_changed = Signal()
    material_units_changed = Signal(object)

    HEADERS = ["공종", "코드", "품명", "규격", "단위", "수량"]

    def __init__(self, material_units=None, parent=None):
        super().__init__(parent)
        self.material_units = normalize_material_units(material_units)
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self.table = OtherWorksTable(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        widths = [170, 145, 220, 220, 80, 115]
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(False)
        self.table.setItemDelegateForColumn(4, _UnitDelegate(self, self.table))
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("추가")
        self.delete_btn = QPushButton("삭제")
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.add_btn.clicked.connect(self.add_row)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.table.insert_above_requested.connect(self.insert_blank_above)
        self.table.duplicate_row_requested.connect(self.duplicate_row)
        self.table.append_row_requested.connect(lambda: self.insert_blank_above(self.table.rowCount()))
        self.table.delete_requested.connect(self.delete_selected)

    def set_material_units(self, units):
        self.material_units = normalize_material_units(units)

    def add_material_unit(self, unit: str):
        unit = normalize_material_unit(unit)
        if not unit:
            return
        values = list(self.material_units)
        if unit.casefold() not in {value.casefold() for value in values}:
            values.append(unit)
            self.material_units = normalize_material_units(values)
            self.material_units_changed.emit(list(self.material_units))

    def add_row(self, values: dict | None = None, insert_row: int | None = None):
        values = dict(values or {})
        row = self.table.rowCount() if insert_row is None else max(0, min(int(insert_row), self.table.rowCount()))
        self.table.insertRow(row)
        row_id = str(values.get("id", "") or uuid.uuid4().hex)
        texts = [
            values.get("trade", values.get("trade_name", "")),
            values.get("code", ""),
            values.get("name", ""),
            values.get("spec", ""),
            normalize_material_unit(values.get("unit", "")),
            values.get("quantity", ""),
        ]
        for column, text in enumerate(texts):
            item = QTableWidgetItem(str(text if text is not None else ""))
            if column == 0:
                item.setData(Qt.UserRole, row_id)
            if column == 5:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, column, item)
        if not self._loading:
            self.table.setCurrentCell(row, 2)
            self.table.editItem(self.table.item(row, 2))
            self.data_changed.emit()

    def insert_blank_above(self, row: int):
        self.add_row({}, insert_row=row)

    def duplicate_row(self, row: int):
        if not (0 <= int(row) < self.table.rowCount()):
            return
        values = [self.table.item(int(row), col).text() if self.table.item(int(row), col) else "" for col in range(self.table.columnCount())]
        payload = {
            "trade": values[0], "code": values[1], "name": values[2], "spec": values[3],
            "unit": values[4], "quantity": values[5],
        }
        self.add_row(payload, insert_row=int(row) + 1)

    def delete_selected(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        if not rows:
            return
        for row in rows:
            self.table.removeRow(row)
        self.data_changed.emit()

    @staticmethod
    def _to_float(value) -> float:
        try:
            return float(str(value or "").replace(",", "").strip() or 0.0)
        except ValueError:
            return 0.0

    def data(self) -> list[dict]:
        rows: list[dict] = []
        for row in range(self.table.rowCount()):
            first = self.table.item(row, 0)
            row_id = str(first.data(Qt.UserRole) or "") if first else ""
            values = [self.table.item(row, col).text().strip() if self.table.item(row, col) else "" for col in range(6)]
            trade, code, name, spec, unit, quantity_text = values
            # 완전 공란 행은 저장/집계에서 제외한다.
            if not any((trade, code, name, spec, unit, quantity_text)):
                continue
            rows.append({
                "id": row_id or uuid.uuid4().hex,
                "trade": trade,
                "code": code,
                "name": name,
                "spec": spec,
                "unit": normalize_material_unit(unit),
                "quantity": self._to_float(quantity_text),
            })
        return rows

    def set_data(self, rows):
        self._loading = True
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for values in list(rows or []):
                if isinstance(values, dict):
                    self.add_row(values)
        finally:
            self.table.blockSignals(False)
            self._loading = False

    def summary_rows(self) -> list[dict]:
        result = []
        for row in self.data():
            result.append({
                "key": f"other:{row['id']}",
                "id": row["id"],
                "code": row.get("code", ""),
                "name": row.get("name", "") or "미지정",
                "spec": row.get("spec", ""),
                "unit": row.get("unit", ""),
                "quantity": float(row.get("quantity", 0.0) or 0.0),
                "trade": row.get("trade", ""),
                "trade_code": "",
                "source": "other",
            })
        return result

    def _on_item_changed(self, _item):
        if not self._loading:
            self.data_changed.emit()
