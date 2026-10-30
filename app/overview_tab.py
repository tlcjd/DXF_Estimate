from __future__ import annotations

import json
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from app.common.room_categories import ROOM_CATEGORIES

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHeaderView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class _InfoCard(QGroupBox):
    """개요 탭 안에서 항목/값/단위를 표시하는 작은 카드."""

    def __init__(self, title: str, rows: list[tuple[str, str, str]] | None = None, columns=None, parent=None):
        super().__init__(title, parent)
        self._fields: dict[str, QLineEdit] = {}
        self._pyeong_labels: dict[str, QLabel] = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 9, 8, 8)
        outer.setSpacing(12)
        column_rows = list(columns or [list(rows or [])])
        for entries in column_rows:
            panel = QWidget()
            grid = QGridLayout(panel)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(5)
            grid.setVerticalSpacing(0)
            grid.setColumnStretch(1, 1)
            self._populate_grid(grid, list(entries or []))
            outer.addWidget(panel, 1)

        self.setStyleSheet(
            "QGroupBox { font-weight:600; color:#2456a6; border:1px solid #c9d3e1; margin-top:9px; padding-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 3px; }"
            "QLineEdit { background:transparent; border:1px solid transparent; border-bottom:1px solid #eceff3; padding:1px 4px; color:#202020; }"
            "QLineEdit[inputMode=\"true\"] { background:#ffffff; border:1px solid #6f8fb8; border-radius:2px; padding:1px 4px; }"
            "QLineEdit[inputMode=\"true\"]:focus { background:#fffdf5; border:2px solid #e48b17; padding:0px 3px; }"
            "QLabel { color:#242424; }"
            "QLabel#pyeongHint { color:#2f8a46; font-size:10px; font-weight:500; padding-left:4px; }"
        )

    def _populate_grid(self, layout: QGridLayout, rows: list[tuple[str, str, str]]):
        # M2 항목은 값 줄과 평 환산 줄을 명확히 분리한다.
        # 항목명이 두 줄 높이의 가운데에 걸쳐 보이지 않도록 첫 줄에만 배치한다.
        grid_row = 0
        for key, label_text, unit in rows:
            label = QLabel(label_text)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            edit = QLineEdit()
            edit.setFrame(False)
            edit.setClearButtonEnabled(False)
            edit.setMinimumHeight(24)
            if str(unit or '').strip():
                # 단위가 있는 수치 입력은 숫자표처럼 우측 정렬한다.
                edit.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            unit_label = QLabel(unit)
            unit_label.setMinimumWidth(26 if unit else 4)
            unit_label.setAlignment(Qt.AlignCenter)

            layout.addWidget(label, grid_row, 0)
            layout.addWidget(edit, grid_row, 1)
            layout.addWidget(unit_label, grid_row, 2)

            if str(unit or '').upper() == 'M2':
                pyeong = QLabel('')
                pyeong.setObjectName('pyeongHint')
                pyeong.setMinimumHeight(13)
                pyeong.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                # 평 환산값은 입력값 바로 아래 줄에만 표시한다.
                layout.addWidget(pyeong, grid_row + 1, 1)
                edit.textChanged.connect(lambda value, target=pyeong: self._update_pyeong_hint(target, value))
                self._pyeong_labels[key] = pyeong
                grid_row += 2
            else:
                grid_row += 1
            self._fields[key] = edit

    @staticmethod
    def _update_pyeong_hint(label: QLabel, value: str):
        raw = str(value or '').replace(',', '').strip()
        match = re.search(r'[-+]?\d+(?:\.\d+)?', raw)
        if not match:
            label.setText('')
            return
        try:
            m2 = float(match.group(0))
        except (TypeError, ValueError):
            label.setText('')
            return
        pyeong = m2 / 3.305785
        label.setText(f'{pyeong:,.2f}평')

    @property
    def fields(self) -> dict[str, QLineEdit]:
        return self._fields


class _AreaInfoCard(_InfoCard):
    """용도는 전폭 1줄, 면적은 좌/우 2열로 정렬하는 전용 카드."""

    def __init__(self, title: str, use_row, left_rows, right_rows, parent=None):
        QGroupBox.__init__(self, title, parent)
        self._fields: dict[str, QLineEdit] = {}
        self._pyeong_labels: dict[str, QLabel] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 9, 8, 8)
        outer.setSpacing(4)

        # 용도는 좌/우 열로 나누지 않고 카드 가로폭 전체를 사용한다.
        use_grid = QGridLayout()
        use_grid.setContentsMargins(0, 0, 0, 0)
        use_grid.setHorizontalSpacing(5)
        use_grid.setVerticalSpacing(0)
        use_grid.setColumnStretch(1, 1)
        self._populate_grid(use_grid, [use_row])
        outer.addLayout(use_grid)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(12)

        left_units = sum(2 if str(unit or '').upper() == 'M2' else 1 for _key, _label, unit in left_rows)
        right_units = sum(2 if str(unit or '').upper() == 'M2' else 1 for _key, _label, unit in right_rows)
        max_units = max(left_units, right_units)

        for entries, used_units in ((left_rows, left_units), (right_rows, right_units)):
            panel = QWidget()
            grid = QGridLayout(panel)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(5)
            grid.setVerticalSpacing(0)
            grid.setColumnStretch(1, 1)
            self._populate_grid(grid, list(entries or []))

            # 좌측 4개 면적항목과 우측 3개 연면적항목의 줄높이를 맞춘다.
            # 우측 마지막 한 항목 자리는 비워 두어 카드 하단이 좌우 동일선에 오게 한다.
            for row in range(used_units, max_units):
                grid.setRowMinimumHeight(row, 13 if row % 2 else 24)
            columns.addWidget(panel, 1)

        outer.addLayout(columns)

        self.setStyleSheet(
            "QGroupBox { font-weight:600; color:#2456a6; border:1px solid #c9d3e1; margin-top:9px; padding-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 3px; }"
            "QLineEdit { background:transparent; border:1px solid transparent; border-bottom:1px solid #eceff3; padding:1px 4px; color:#202020; }"
            "QLineEdit[inputMode=\"true\"] { background:#ffffff; border:1px solid #6f8fb8; border-radius:2px; padding:1px 4px; }"
            "QLineEdit[inputMode=\"true\"]:focus { background:#fffdf5; border:2px solid #e48b17; padding:0px 3px; }"
            "QLabel { color:#242424; }"
            "QLabel#pyeongHint { color:#2f8a46; font-size:10px; font-weight:500; padding-left:4px; }"
        )


class OverviewListTable(QTableWidget):
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
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is not None and (item.flags() & Qt.ItemIsEditable) and str(item.text() or "").strip():
                return False
        return True

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


class FloorOverviewDialog(QDialog):
    """층별개요: 도서 직접입력 + 내부 실목록 연동 집계를 한 창에서 확인한다."""

    DEFAULT_USES = ["공용", "기전실", "주차장", "근생", "세대"]

    def __init__(self, saved=None, rooms=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("층별개요")
        self.resize(1180, 680)
        self._loading = False
        self._rooms = list(rooms or [])
        source = dict(saved or {})
        self.uses = [str(v).strip() for v in list(source.get("uses") or []) if str(v).strip()] or list(self.DEFAULT_USES)
        rows = [dict(v or {}) for v in list(source.get("rows") or [])]

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # 도서: 사용자가 도면을 보고 직접 입력하는 별도 자료.
        self.document_tab = QWidget()
        doc_layout = QVBoxLayout(self.document_tab)
        doc_layout.setContentsMargins(6, 6, 6, 6)
        doc_layout.setSpacing(6)

        tools = QHBoxLayout()
        self.add_use_btn = QPushButton("용도열 추가")
        self.remove_use_btn = QPushButton("용도열 제거")
        self.add_row_btn = QPushButton("행 추가")
        self.remove_row_btn = QPushButton("행 삭제")
        tools.addWidget(self.add_use_btn)
        tools.addWidget(self.remove_use_btn)
        tools.addSpacing(12)
        tools.addWidget(self.add_row_btn)
        tools.addWidget(self.remove_row_btn)
        tools.addStretch(1)
        doc_layout.addLayout(tools)

        self.table = OverviewListTable()
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        doc_layout.addWidget(self.table, 1)
        self.tabs.addTab(self.document_tab, "도서")

        # 내부: 내부 탭의 현재 실목록을 그대로 집계하는 읽기 전용 자료.
        self.internal_tab = QWidget()
        internal_layout = QVBoxLayout(self.internal_tab)
        internal_layout.setContentsMargins(6, 6, 6, 6)
        internal_layout.setSpacing(6)
        info = QLabel("내부 실목록의 층 / 적용층 / 구분 / 면적을 집계한 읽기 전용 표입니다.")
        info.setStyleSheet("color:#666666;")
        internal_layout.addWidget(info)
        self.internal_table = QTableWidget()
        self.internal_table.verticalHeader().setVisible(False)
        self.internal_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.internal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.internal_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.internal_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        internal_layout.addWidget(self.internal_table, 1)
        self.tabs.addTab(self.internal_tab, "내부")

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        ok_btn = QPushButton("확인")
        cancel_btn = QPushButton("취소")
        bottom.addWidget(ok_btn)
        bottom.addWidget(cancel_btn)
        root.addLayout(bottom)

        self.add_use_btn.clicked.connect(self._add_use)
        self.remove_use_btn.clicked.connect(self._remove_use)
        self.add_row_btn.clicked.connect(self._add_row)
        self.remove_row_btn.clicked.connect(self._remove_row)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.insert_above_requested.connect(self._insert_row_above)
        self.table.duplicate_row_requested.connect(self._duplicate_row_at)
        self.table.append_row_requested.connect(lambda: self._insert_row_above(self.table.rowCount()))
        self.table.delete_requested.connect(self._remove_row)

        self._rebuild(rows)
        self._rebuild_internal(self._rooms)

    @staticmethod
    def _floor_sort_key(value: str):
        text = str(value or "").strip().upper().replace(" ", "")
        # 범위는 시작층을 기준으로 정렬한다. 예: 4F~20F, B2F~B1F
        if "~" in text:
            text = text.split("~", 1)[0]
        m = re.fullmatch(r"B(\d+)F?", text)
        if m:
            return (0, -int(m.group(1)), text)
        m = re.fullmatch(r"(\d+)F?", text)
        if m:
            return (1, int(m.group(1)), text)
        if text in {"RF", "R"}:
            return (2, 0, text)
        m = re.fullmatch(r"PH(\d*)", text)
        if m:
            return (3, int(m.group(1) or 0), text)
        return (4, 0, text)

    def _headers(self):
        return ["구분", "층", "층고", *self.uses, "면적계"]

    def _rebuild(self, rows=None):
        rows = list(rows if rows is not None else self.data().get("rows", []))
        self._loading = True
        try:
            self.table.clear()
            headers = self._headers()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(rows))
            widths = [90, 100, 80] + [105] * len(self.uses) + [110]
            for col, width in enumerate(widths):
                self.table.setColumnWidth(col, width)
            total_col = len(headers) - 1
            for r, record in enumerate(rows):
                areas = dict(record.get("areas") or {})
                values = [record.get("section", ""), record.get("floor", ""), record.get("height", "")]
                values += [areas.get(use, "") for use in self.uses]
                values.append("")
                for c, value in enumerate(values):
                    if isinstance(value, (int, float)) and abs(float(value)) > 1e-12:
                        text = f"{float(value):,.3f}"
                    else:
                        text = str(value or "")
                    item = QTableWidgetItem(text)
                    if c == total_col:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if c >= 2:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(r, c, item)
                self._update_total(r)
        finally:
            self._loading = False

    @staticmethod
    def _number(text) -> float:
        try:
            return float(str(text or "").replace(",", "").strip() or 0.0)
        except Exception:
            return 0.0

    def _update_total(self, row: int):
        if not (0 <= row < self.table.rowCount()):
            return
        first = 3
        last = first + len(self.uses)
        total = sum(self._number(self.table.item(row, c).text() if self.table.item(row, c) else "") for c in range(first, last))
        item = self.table.item(row, last)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, last, item)
        item.setText(f"{total:,.3f}")
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def _on_item_changed(self, item):
        if self._loading:
            return
        if 3 <= item.column() < 3 + len(self.uses):
            self._loading = True
            try:
                self._update_total(item.row())
            finally:
                self._loading = False

    def _add_use(self):
        text, ok = QInputDialog.getText(self, "용도열 추가", "용도명:")
        name = str(text or "").strip()
        if not ok or not name:
            return
        if name in self.uses:
            QMessageBox.information(self, "용도열 추가", "이미 같은 용도열이 있습니다.")
            return
        data = self.data()
        self.uses.append(name)
        self._rebuild(data.get("rows", []))
        self.table.setCurrentCell(0 if self.table.rowCount() else -1, 3 + len(self.uses) - 1)

    def _remove_use(self):
        col = self.table.currentColumn()
        use_index = col - 3
        if not (0 <= use_index < len(self.uses)):
            QMessageBox.information(self, "용도열 제거", "제거할 용도 열을 먼저 선택해 주세요.")
            return
        name = self.uses[use_index]
        answer = QMessageBox.question(self, "용도열 제거", f"'{name}' 용도 열을 제거할까요?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        data = self.data()
        self.uses.pop(use_index)
        for record in data.get("rows", []):
            areas = record.get("areas")
            if isinstance(areas, dict):
                areas.pop(name, None)
        self._rebuild(data.get("rows", []))

    def _insert_row_above(self, row: int):
        row = max(0, min(int(row), self.table.rowCount()))
        self.table.insertRow(row)
        total_col = 3 + len(self.uses)
        for c in range(self.table.columnCount()):
            item = QTableWidgetItem("")
            if c == total_col:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable); item.setText("0.000")
            self.table.setItem(row, c, item)
        self.table.setCurrentCell(row, 1)

    def _duplicate_row_at(self, row: int):
        row = int(row)
        if not (0 <= row < self.table.rowCount()): return
        values = [self.table.item(row,c).text() if self.table.item(row,c) else "" for c in range(self.table.columnCount())]
        insert = row + 1
        self.table.insertRow(insert)
        total_col = 3 + len(self.uses)
        for c, value in enumerate(values):
            item = QTableWidgetItem(value)
            if c == total_col: item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(insert,c,item)
        self.table.setCurrentCell(insert, 1)

    def _add_row(self):
        row = self.table.currentRow()
        row = self.table.rowCount() if row < 0 else row + 1
        self.table.insertRow(row)
        total_col = 3 + len(self.uses)
        for c in range(self.table.columnCount()):
            item = QTableWidgetItem("")
            if c == total_col:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setText("0.000")
            self.table.setItem(row, c, item)
        self.table.setCurrentCell(row, 1)
        self.table.editItem(self.table.item(row, 1))

    def _remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    @staticmethod
    def _room_scope_label(room) -> str:
        return str(getattr(room, "floor_scope", "") or getattr(room, "floor", "") or "").strip()

    @classmethod
    def _internal_summary(cls, rooms) -> tuple[list[str], list[dict]]:
        rooms = list(rooms or [])
        present_categories: list[str] = []
        seen_categories = set()
        for room in rooms:
            category = str(getattr(room, "room_category", "") or "").strip() or "미구분"
            if category not in seen_categories:
                seen_categories.add(category)
                present_categories.append(category)

        category_order = [category for category in ROOM_CATEGORIES if category in seen_categories]
        if "미구분" in seen_categories:
            category_order.append("미구분")
        for category in present_categories:
            if category not in category_order:
                category_order.append(category)

        grouped: dict[str, dict] = {}
        for room in rooms:
            base_floor = str(getattr(room, "floor", "") or "").strip()
            scope = cls._room_scope_label(room)
            if not scope:
                continue
            row = grouped.setdefault(scope, {
                "section": "지하층" if base_floor.upper().startswith("B") else "지상층",
                "floor": scope,
                "sort_floor": base_floor or scope,
                "heights": set(),
                "areas": {category: 0.0 for category in category_order},
            })
            height = getattr(room, "floor_height_m", None)
            if height is not None:
                try:
                    row["heights"].add(round(float(height), 9))
                except Exception:
                    pass
            category = str(getattr(room, "room_category", "") or "").strip() or "미구분"
            if category not in row["areas"]:
                row["areas"][category] = 0.0
            try:
                # 층별개요는 1개 층(또는 동일 적용층 범위)의 면적을 보여준다.
                # 반복개소를 곱한 quantity_area_m2가 아니라 내부 목록의 면적값을 합산한다.
                row["areas"][category] += float(getattr(room, "area_m2", 0.0) or 0.0)
            except Exception:
                pass

        result = []
        for scope, row in sorted(grouped.items(), key=lambda kv: (cls._floor_sort_key(kv[1].get("sort_floor", "")), kv[0])):
            heights = row.pop("heights", set())
            row.pop("sort_floor", None)
            if len(heights) == 1:
                value = next(iter(heights))
                row["height"] = f"{value:.3f}".rstrip("0").rstrip(".")
            else:
                row["height"] = ""
            result.append(row)
        return category_order, result

    def _rebuild_internal(self, rooms):
        categories, rows = self._internal_summary(rooms)
        headers = ["구분", "층", "층고", *categories, "면적계"]
        self.internal_table.clear()
        self.internal_table.setColumnCount(len(headers))
        self.internal_table.setHorizontalHeaderLabels(headers)
        self.internal_table.setRowCount(len(rows))
        widths = [90, 120, 80] + [105] * len(categories) + [110]
        for col, width in enumerate(widths):
            self.internal_table.setColumnWidth(col, width)

        total_col = len(headers) - 1
        for r, record in enumerate(rows):
            areas = dict(record.get("areas") or {})
            total = sum(float(areas.get(category, 0.0) or 0.0) for category in categories)
            values = [record.get("section", ""), record.get("floor", ""), record.get("height", "")]
            values += [areas.get(category, 0.0) for category in categories]
            values.append(total)
            for c, value in enumerate(values):
                if c >= 3:
                    try:
                        number = float(value or 0.0)
                        text = f"{number:,.3f}" if abs(number) > 1e-12 else ""
                    except Exception:
                        text = str(value or "")
                else:
                    text = str(value or "")
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.internal_table.setItem(r, c, item)
        if total_col >= 0:
            self.internal_table.horizontalHeader().setStretchLastSection(False)

    def data(self) -> dict:
        # 내부 탭은 현재 실목록의 파생자료이므로 저장하지 않는다.
        # 도서 탭의 직접입력 자료만 프로젝트에 저장한다.
        rows = []
        for r in range(self.table.rowCount()):
            areas = {}
            for idx, use in enumerate(self.uses):
                item = self.table.item(r, 3 + idx)
                text = item.text().strip() if item else ""
                if text:
                    areas[use] = self._number(text)
            rows.append({
                "section": self.table.item(r, 0).text().strip() if self.table.item(r, 0) else "",
                "floor": self.table.item(r, 1).text().strip() if self.table.item(r, 1) else "",
                "height": self.table.item(r, 2).text().strip() if self.table.item(r, 2) else "",
                "areas": areas,
            })
        return {"uses": list(self.uses), "rows": rows}


class OverviewTab(QWidget):
    """프로젝트 개요/주요조건을 박스형으로 입력·확인하는 화면."""

    overview_changed = Signal(str)
    fields_changed = Signal(dict)

    FIELD_DEFS = {
        "common": [
            ("project_name", "공사명", ""),
            ("location", "대지위치", ""),
            ("construction_period", "공사기간", ""),
        ],
        "area_use": ("use_type", "용도", ""),
        "area_left": [
            ("site_area", "대지면적", "M2"),
            ("building_area", "건축면적", "M2"),
            ("foundation_area", "기초면적", "M2"),
            ("landscape_area", "조경면적", "M2"),
        ],
        "area_right": [
            ("ground_gross_area", "지상연면적", "M2"),
            ("basement_gross_area", "지하연면적", "M2"),
            ("gross_area", "전체연면적", "M2"),
        ],
        "height_left": [
            ("ground_floors", "지상층수", "층"),
            ("basement_floors", "지하층수", "층"),
            ("rooftop_floors", "옥탑층수", "층"),
            ("total_floors", "전체층수", "층"),
        ],
        "height_right": [
            ("height", "최고높이", "m"),
            ("typical_floor", "기준층수", "개층"),
            ("typical_floor_height", "기준층고", "m"),
            ("room_count", "세대 / 객실수", "실"),
        ],
        "parking": [
            ("parking_ground", "지상주차", "대"),
            ("parking_basement", "지하주차", "대"),
            ("parking_mechanical", "기계주차(지하)", "대"),
            ("parking_total", "총 주차대수", "대"),
        ],
        "exterior": [
            ("exterior_finish", "외장마감", ""),
            ("window_type", "창호형식", ""),
            ("glass_spec", "유리사양", ""),
        ],
        "finish": [
            ("insulation", "단열구분", ""),
            ("floor_finish", "주요 바닥마감", ""),
            ("wall_finish", "주요 벽마감", ""),
            ("ceiling_finish", "주요 천정마감", ""),
            ("architectural_notes", "특이사항", ""),
        ],
        "civil_size": [
            ("basement_wall_perimeter", "지하외벽 둘레", "m"),
            ("excavation_area", "터파기 면적", "M2"),
            ("excavation_depth", "굴착깊이", "m"),
        ],
        "civil_method": [
            ("weathered_soil", "풍화토", "M"),
            ("weathered_rock", "풍화암", "M"),
            ("soft_rock", "연암", "M"),
            ("hard_rock", "경암", "M"),
            ("earth_retaining_1", "흙막이공법1", ""),
            ("earth_retaining_2", "흙막이공법2", ""),
            ("water_cutoff", "차수공법", ""),
            ("waler_levels", "띠장 단수", "단"),
        ],
        "earthwork": [
            ("earthwork_total", "전체 토량", "M3"),
            ("rock_total", "암 수량", "M3"),
            ("rock_ratio", "암 비율", "%"),
            ("pile_method", "지정 / 파일", ""),
        ],
        "structure_general": [
            ("structure_type", "구조형식", ""),
            ("basement_structure", "지하 구조", ""),
            ("ground_structure", "지상 구조", ""),
            ("foundation_type", "기초형식", ""),
            ("foundation_thickness", "기초두께", "m"),
            ("concrete_strength", "콘크리트 강도", ""),
            ("rebar_strength", "철근강도", ""),
        ],
        "mechanical": [
            ("elevator", "승강기", ""),
            ("water_tank", "저수조", ""),
            ("septic_tank", "정화조", ""),
            ("automatic_control", "자동제어", ""),
            ("solar", "태양광", ""),
        ],
        "electrical": [
            ("parking_control", "주차관제", ""),
            ("fire_protection", "소방 관련", ""),
            ("electrical_notes", "전기 특이사항", ""),
            ("mechanical_notes", "설비 특이사항", ""),
        ],
    }

    STRUCTURE_TOTAL_METRICS = [
        ("concrete_total", "레미콘", "M3"),
        ("rebar_total", "철근", "TON"),
        ("form_horizontal_total", "거푸집(수평)", "M2"),
        ("form_vertical_total", "거푸집(수직)", "M2"),
        ("form_total", "거푸집(전체)", "M2"),
    ]
    STRUCTURE_RATIO_METRICS = [
        ("concrete_per_py", "평당 레미콘", "M3/평"),
        ("rebar_ratio", "철근비", "M3/TON"),
        ("form_horizontal_per_py", "평당 거푸집(수평)", "M2/평"),
        ("form_vertical_per_py", "평당 거푸집(수직)", "M2/평"),
        ("form_total_per_py", "평당 거푸집(전체)", "M2/평"),
    ]
    STRUCTURE_METRICS = STRUCTURE_TOTAL_METRICS + STRUCTURE_RATIO_METRICS
    STRUCTURE_AUTO_METRICS = {"form_total", "form_total_per_py"}

    AUTO_SUM_FIELDS = {"gross_area", "total_floors", "parking_total"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._has_project = False
        self._field_edits: dict[str, QLineEdit] = {}
        self._structure_metric_edits: dict[str, QLineEdit] = {}
        self._input_order: list[QLineEdit] = []
        self._floor_overview_data: dict = {}
        self._rooms_provider = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._emit_fields_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addStretch(1)
        self.floor_overview_button = QPushButton("층별개요")
        self.floor_overview_button.setEnabled(False)
        self.floor_overview_button.setToolTip("층고 및 용도별 층면적 표를 엽니다.")
        self.floor_overview_button.clicked.connect(self._open_floor_overview)
        mode_row.addWidget(self.floor_overview_button)
        self.input_mode_button = QPushButton("입력모드")
        self.input_mode_button.setCheckable(True)
        self.input_mode_button.setEnabled(False)
        self.input_mode_button.setToolTip("개요 값 입력모드 켜기/끄기 (F2) · Enter를 누르면 다음 항목으로 이동")
        self.input_mode_button.setStyleSheet(
            "QPushButton { min-width:88px; padding:4px 12px; border:1px solid #9aa7b7; border-radius:3px; background:#f7f8fa; }"
            "QPushButton:hover { background:#eef3f9; }"
            "QPushButton:checked { color:white; background:#2f67ad; border-color:#245791; font-weight:600; }"
        )
        self.input_mode_button.toggled.connect(self._set_input_mode)
        mode_row.addWidget(self.input_mode_button)
        self.input_mode_shortcut = QShortcut(QKeySequence("F2"), self)
        self.input_mode_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.input_mode_shortcut.setAutoRepeat(False)
        self.input_mode_shortcut.activated.connect(self._toggle_input_mode_shortcut)
        root.addLayout(mode_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(self.scroll, 1)

        content = QWidget()
        content.setMinimumWidth(1520)
        row = QHBoxLayout(content)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(10)
        row.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(content)

        # 큰 세로 구역은 각각 독립 폭을 갖게 한다.
        # 자식 카드의 최소폭/제목 길이에 의해 옆 구역을 침범하지 않도록
        # 상위 컬럼을 고정폭으로 두고 가로 스크롤로 전체를 본다.
        row.addWidget(self._build_column("1. 공사 공통내용", [
            ("개요", self.FIELD_DEFS["common"]),
            ("용도 / 면적", self.FIELD_DEFS["area_use"], self.FIELD_DEFS["area_left"], self.FIELD_DEFS["area_right"]),
            ("높이 / 층수", self.FIELD_DEFS["height_left"], self.FIELD_DEFS["height_right"]),
            ("주차", self.FIELD_DEFS["parking"]),
        ], 400))
        row.addWidget(self._build_column("2. 건축 관련", [
            ("외장 / 창호", self.FIELD_DEFS["exterior"]),
            ("단열 / 마감", self.FIELD_DEFS["finish"]),
        ], 240))
        row.addWidget(self._build_column("3. 토목 관련", [
            ("면적 / 깊이", self.FIELD_DEFS["civil_size"]),
            ("지반 / 공법", self.FIELD_DEFS["civil_method"]),
            ("토량 / 암", self.FIELD_DEFS["earthwork"]),
        ], 240))

        structure_col = self._build_column("4. 구조 관련", [
            ("일반사항", self.FIELD_DEFS["structure_general"]),
        ], 300)
        structure_col.layout().addWidget(self._build_structure_metrics())
        structure_col.layout().addStretch(1)
        row.addWidget(structure_col)

        mep_col = self._build_column("5. 설비 / 전기 관련", [
            ("설비", self.FIELD_DEFS["mechanical"]),
            ("전기 / 기타", self.FIELD_DEFS["electrical"]),
        ], 280)
        memo_box = QGroupBox("작성 방향 메모")
        memo_layout = QVBoxLayout(memo_box)
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setPlaceholderText("특이사항, 작성 방향, 검토 메모")
        self.memo_edit.setMinimumHeight(130)
        memo_layout.addWidget(self.memo_edit)
        memo_box.setStyleSheet(
            "QGroupBox { font-weight:600; color:#2456a6; border:1px solid #c9d3e1; margin-top:9px; padding-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 3px; }"
        )
        mep_col.layout().addWidget(memo_box)
        mep_col.layout().addStretch(1)
        row.addWidget(mep_col)
        row.addStretch(1)

        self._field_edits["project_name"].setToolTip("입력모드에서 공사명을 수정하면 프로젝트명에도 함께 반영됩니다.")
        for edit in self._field_edits.values():
            edit.textChanged.connect(self._schedule_save)
            edit.returnPressed.connect(lambda current=edit: self._focus_next_input(current))

        for key in ("ground_gross_area", "basement_gross_area", "basement_floors", "ground_floors", "rooftop_floors", "parking_ground", "parking_basement", "parking_mechanical"):
            self._field_edits[key].textChanged.connect(lambda _value, source=key: self._refresh_auto_fields())
        self._field_edits["gross_area"].setToolTip("지상연면적 + 지하연면적 자동 합계")
        self._field_edits["total_floors"].setToolTip("지하층수 + 지상층수 + 옥탑층수 자동 합계")
        self._field_edits["parking_total"].setToolTip("지상주차 + 지하주차 + 기계주차 자동 합계")

        self.memo_edit.textChanged.connect(self._schedule_save)
        self._apply_input_mode(False)

    def _build_column(self, title: str, cards, minimum_width: int) -> QFrame:
        # 큰 구역에 QGroupBox를 중첩하면 제목/테두리 스타일이 자식 카드까지
        # 전파되어 경계가 겹쳐 보일 수 있다. 큰 구역은 독립 QFrame으로 만들고
        # 제목은 QLabel로 분리한다. 각 컬럼은 고정폭이라 옆 컬럼을 침범하지 않는다.
        box = QFrame()
        box.setObjectName("overviewColumn")
        box.setFixedWidth(minimum_width)
        box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("overviewColumnTitle")
        title_label.setMinimumHeight(24)
        layout.addWidget(title_label)

        for card_spec in cards:
            card_title = card_spec[0]
            if card_title == "용도 / 면적" and len(card_spec) >= 4:
                card = _AreaInfoCard(card_title, card_spec[1], card_spec[2], card_spec[3])
            elif len(card_spec) >= 3:
                card = _InfoCard(card_title, columns=[card_spec[1], card_spec[2]])
            else:
                card = _InfoCard(card_title, card_spec[1])
            if card_title == "주차":
                card.setFixedWidth(max(185, int((minimum_width - 16) * 0.52)))
                card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            elif title == "1. 공사 공통내용" and card_title == "개요":
                # 공사 공통내용의 개요 카드는 컬럼 폭 전체를 사용한다.
                # 한 열짜리 내용의 sizeHint 때문에 카드가 절반 폭으로 줄어드는 현상을 방지한다.
                card.setFixedWidth(max(120, minimum_width - 16))
                card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            else:
                card.setMaximumWidth(max(120, minimum_width - 16))
            self._field_edits.update(card.fields)
            self._input_order.extend(card.fields.values())
            layout.addWidget(card, 0, Qt.AlignLeft)
        layout.addStretch(1)

        box.setStyleSheet(
            "QFrame#overviewColumn { border:1px solid #d1d7df; background:transparent; }"
            "QLabel#overviewColumnTitle { border:none; color:#174b99; font-weight:700; padding:0 2px 2px 2px; }"
        )
        return box

    def _build_structure_metrics(self) -> QGroupBox:
        box = QGroupBox("평당수량 / 구조지표")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 9, 8, 8)
        outer.setSpacing(5)

        def add_section(title: str, metrics):
            title_label = QLabel(title)
            title_label.setObjectName("structureMetricTitle")
            outer.addWidget(title_label)
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(1)
            grid.setColumnStretch(1, 1)
            for row, (key, label_text, unit) in enumerate(metrics):
                label = QLabel(label_text)
                label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                edit = QLineEdit()
                edit.setFrame(False)
                edit.setClearButtonEnabled(False)
                edit.setMinimumHeight(22)
                edit.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                edit.setReadOnly(True)
                unit_label = QLabel(unit)
                unit_label.setMinimumWidth(52)
                unit_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                grid.addWidget(label, row, 0)
                grid.addWidget(edit, row, 1)
                grid.addWidget(unit_label, row, 2)
                self._structure_metric_edits[key] = edit
                self._input_order.append(edit)
                edit.textChanged.connect(lambda _value, metric_key=key: self._on_structure_metric_text_changed(metric_key))
            outer.addLayout(grid)

        add_section("전체 수량", self.STRUCTURE_TOTAL_METRICS)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        outer.addWidget(line)
        add_section("평당 / 구조지표", self.STRUCTURE_RATIO_METRICS)

        box.setStyleSheet(
            "QGroupBox { font-weight:600; color:#2456a6; border:1px solid #c9d3e1; margin-top:9px; padding-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 3px; }"
            "QLabel { color:#242424; }"
            "QLabel#structureMetricTitle { color:#4d6480; font-size:10px; font-weight:600; padding:1px 0; }"
            "QLineEdit { background:transparent; border:1px solid transparent; border-bottom:1px solid #eceff3; padding:1px 4px; color:#202020; }"
            "QLineEdit[inputMode=\"true\"] { background:#ffffff; border:1px solid #6f8fb8; border-radius:2px; padding:1px 4px; }"
            "QLineEdit[inputMode=\"true\"]:focus { background:#fffdf5; border:2px solid #e48b17; padding:0px 3px; }"
        )
        return box

    @staticmethod
    def _repolish(widget):
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _apply_input_mode(self, enabled: bool):
        enabled = bool(enabled and self._has_project)
        for key, edit in self._field_edits.items():
            editable = enabled and key not in self.AUTO_SUM_FIELDS
            edit.setReadOnly(not editable)
            edit.setProperty("inputMode", bool(editable))
            self._repolish(edit)

        for key, edit in self._structure_metric_edits.items():
            editable = enabled and key not in self.STRUCTURE_AUTO_METRICS
            edit.setReadOnly(not editable)
            edit.setProperty("inputMode", bool(editable))
            self._repolish(edit)

        self.memo_edit.setReadOnly(not enabled)
        if enabled:
            self.memo_edit.setStyleSheet(
                "QPlainTextEdit { background:#ffffff; border:1px solid #6f8fb8; border-radius:2px; padding:4px; }"
                "QPlainTextEdit:focus { border:2px solid #e48b17; padding:3px; }"
            )
        else:
            self.memo_edit.setStyleSheet(
                "QPlainTextEdit { background:#fafafa; border:1px solid #d8dde5; border-radius:2px; padding:4px; }"
            )

        self.input_mode_button.setText("입력모드 종료" if enabled else "입력모드")

    def _toggle_input_mode_shortcut(self):
        if not self.input_mode_button.isEnabled():
            return
        self.input_mode_button.toggle()

    def _set_input_mode(self, checked: bool):
        if checked and not self._has_project:
            self.input_mode_button.blockSignals(True)
            self.input_mode_button.setChecked(False)
            self.input_mode_button.blockSignals(False)
            self._apply_input_mode(False)
            return

        self._apply_input_mode(bool(checked))
        if checked:
            for edit in self._input_order:
                if edit.isEnabled() and not edit.isReadOnly():
                    edit.setFocus(Qt.TabFocusReason)
                    edit.selectAll()
                    break

    def _focus_next_input(self, current: QLineEdit):
        if not self.input_mode_button.isChecked() or not self._input_order:
            return
        try:
            start = self._input_order.index(current)
        except ValueError:
            start = -1

        for offset in range(1, len(self._input_order) + 1):
            candidate = self._input_order[(start + offset) % len(self._input_order)]
            if candidate.isEnabled() and candidate.isVisible() and not candidate.isReadOnly():
                candidate.setFocus(Qt.TabFocusReason)
                candidate.selectAll()
                return

    @staticmethod
    def _fmt_number(value, decimals=2) -> str:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return ""
        if not number:
            return ""
        return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")

    @staticmethod
    def _floor_seed(record) -> tuple[str, str, str]:
        groups = list(getattr(record, "building_groups", []) or [])
        if not groups:
            return "", "", ""
        ground = max((int(group.get("ground", 0) or 0) for group in groups), default=0)
        basement = max((int(group.get("basement", 0) or 0) for group in groups), default=0)
        total = ground + basement
        return (str(ground) if ground else "", str(basement) if basement else "", str(total) if total else "")

    def _seed_from_record(self, record) -> dict[str, str]:
        ground_floors, basement_floors, total_floors = self._floor_seed(record)
        construction_months = int(getattr(record, "construction_months", 0) or 0)
        values = {
            "project_name": str(getattr(record, "name", "") or ""),
            "location": str(getattr(record, "location", "") or ""),
            "construction_period": f"{construction_months}개월" if construction_months else "",
            "use_type": str(getattr(record, "construction_type", "") or ""),
            "building_area": self._fmt_number(getattr(record, "building_area_m2", 0.0)),
            "gross_area": self._fmt_number(getattr(record, "gross_area_m2", 0.0)),
            "height": self._fmt_number(getattr(record, "height_m", 0.0)),
            "excavation_depth": self._fmt_number(getattr(record, "depth_m", 0.0)),
            "exterior_finish": str(getattr(record, "exterior_material", "") or ""),
            "structure_type": str(getattr(record, "building_structure", "") or ""),
            "insulation": str(getattr(record, "insulation_type", "") or ""),
            "ground_floors": ground_floors,
            "basement_floors": basement_floors,
            "total_floors": total_floors,
        }
        saved = dict(getattr(record, "overview_fields", {}) or {})
        # 프로젝트 정보에서 실제 값이 있는 항목은 처음 표시할 때 우선 사용한다.
        # 그 외 항목은 개요 탭에서 저장한 값을 유지한다.
        for key, value in saved.items():
            if key not in values or not values.get(key):
                values[key] = str(value or "")
        # 세부 입력값이 이미 저장되어 있으면 기존 프로젝트 기본값보다 우선한다.
        for key, value in saved.items():
            if str(value or "").strip():
                values[key] = str(value or "")
        return values

    @staticmethod
    def _parse_numeric_text(value):
        raw = str(value or "").replace(",", "").strip()
        if not raw:
            return None
        match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
        if not match:
            return None
        try:
            return float(match.group(0))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_auto_number(value: float, decimals: int = 2) -> str:
        if decimals <= 0:
            return f"{int(round(value)):,}"
        return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")

    def _set_auto_sum(self, target_key: str, source_keys: tuple[str, ...], decimals: int, preserve_when_blank: bool = False):
        source_texts = [self._field_edits[key].text().strip() for key in source_keys]
        numbers = [self._parse_numeric_text(value) for value in source_texts]
        if not any(source_texts):
            if preserve_when_blank:
                return
            value_text = ""
        else:
            value_text = self._format_auto_number(sum(value or 0.0 for value in numbers), decimals)
        target = self._field_edits[target_key]
        if target.text() != value_text:
            target.setText(value_text)

    def _refresh_auto_fields(self, preserve_when_blank: bool = False):
        self._set_auto_sum("gross_area", ("ground_gross_area", "basement_gross_area"), 2, preserve_when_blank)
        self._set_auto_sum("total_floors", ("basement_floors", "ground_floors", "rooftop_floors"), 0, preserve_when_blank)
        self._set_auto_sum("parking_total", ("parking_ground", "parking_basement", "parking_mechanical"), 0, preserve_when_blank)

    def _on_structure_metric_text_changed(self, key: str):
        if self._loading:
            return
        if key in {"form_horizontal_total", "form_vertical_total"}:
            self._refresh_structure_total("form_horizontal_total", "form_vertical_total", "form_total")
        elif key in {"form_horizontal_per_py", "form_vertical_per_py"}:
            self._refresh_structure_total("form_horizontal_per_py", "form_vertical_per_py", "form_total_per_py")
        self._schedule_save()

    def _refresh_structure_total(self, left_key: str, right_key: str, target_key: str, preserve_when_blank: bool = False):
        left = self._structure_metric_edits.get(left_key)
        right = self._structure_metric_edits.get(right_key)
        target = self._structure_metric_edits.get(target_key)
        if left is None or right is None or target is None:
            return
        left_text = left.text().strip()
        right_text = right.text().strip()
        if not left_text and not right_text:
            if preserve_when_blank:
                return
            value_text = ""
        else:
            left_value = self._parse_numeric_text(left_text) or 0.0
            right_value = self._parse_numeric_text(right_text) or 0.0
            value_text = self._format_auto_number(left_value + right_value, 2)
        if target.text() != value_text:
            target.blockSignals(True)
            try:
                target.setText(value_text)
            finally:
                target.blockSignals(False)
            self._schedule_save()

    def _refresh_structure_totals(self, preserve_when_blank: bool = False):
        self._refresh_structure_total("form_horizontal_total", "form_vertical_total", "form_total", preserve_when_blank)
        self._refresh_structure_total("form_horizontal_per_py", "form_vertical_per_py", "form_total_per_py", preserve_when_blank)

    def _schedule_save(self):
        if not self._loading:
            self._save_timer.start()

    def _collect_fields(self) -> dict[str, str]:
        result = {key: edit.text().strip() for key, edit in self._field_edits.items()}
        for key, edit in self._structure_metric_edits.items():
            result[f"metric:{key}:quantity"] = edit.text().strip()
        if self._floor_overview_data:
            result["__floor_overview_json__"] = json.dumps(self._floor_overview_data, ensure_ascii=False, separators=(",", ":"))
        return result

    def _emit_fields_changed(self):
        if self._loading:
            return
        self.fields_changed.emit(self._collect_fields())
        self.overview_changed.emit(self.memo_edit.toPlainText())

    def set_rooms_provider(self, provider):
        self._rooms_provider = provider if callable(provider) else None

    def _open_floor_overview(self):
        if not self._has_project:
            return
        rooms = []
        if self._rooms_provider is not None:
            try:
                rooms = list(self._rooms_provider() or [])
            except Exception:
                rooms = []
        dialog = FloorOverviewDialog(self._floor_overview_data, rooms, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._floor_overview_data = dialog.data()
        self._schedule_save()

    def set_project(self, record=None):
        self._save_timer.stop()
        self._loading = True
        self._has_project = record is not None
        try:
            self.input_mode_button.blockSignals(True)
            self.input_mode_button.setChecked(False)
            self.input_mode_button.blockSignals(False)
            self.input_mode_button.setEnabled(self._has_project)
            self.floor_overview_button.setEnabled(self._has_project)

            values = self._seed_from_record(record) if record is not None else {}
            for key, edit in self._field_edits.items():
                edit.setText(str(values.get(key, "") or ""))
                edit.setEnabled(self._has_project)

            saved = dict(getattr(record, "overview_fields", {}) or {}) if record is not None else {}
            self._floor_overview_data = {}
            raw_floor_overview = str(saved.get("__floor_overview_json__", "") or "").strip()
            if raw_floor_overview:
                try:
                    parsed = json.loads(raw_floor_overview)
                    if isinstance(parsed, dict):
                        self._floor_overview_data = parsed
                except Exception:
                    self._floor_overview_data = {}
            legacy_key_fallbacks = {
                "concrete_per_py": ("metric:concrete_per_py:total", "metric:concrete_per_py:ground", "metric:avg_concrete_per_py:total"),
                "form_total_per_py": ("metric:form_per_py:total", "metric:form_per_py:ground", "metric:avg_form_per_py:total"),
            }
            for key, edit in self._structure_metric_edits.items():
                value = str(saved.get(f"metric:{key}:quantity", "") or "")
                if not value:
                    for legacy_key in legacy_key_fallbacks.get(key, ()):
                        value = str(saved.get(legacy_key, "") or "")
                        if value:
                            break
                edit.setText(value)
                edit.setEnabled(self._has_project)

            self._refresh_structure_totals(preserve_when_blank=True)
            self._refresh_auto_fields(preserve_when_blank=True)
            self.memo_edit.setPlainText(str(getattr(record, "overview", "") or "") if record is not None else "")
            self.memo_edit.setEnabled(self._has_project)
            self._apply_input_mode(False)
        finally:
            self._loading = False


class PlaceholderTab(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        label = QLabel(f"{title}\n\n내용 구성 보류")
        label.setStyleSheet("color:#777777; font-size:16px;")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        layout.addStretch(1)
