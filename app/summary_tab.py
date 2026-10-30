from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class SummaryTab(QWidget):
    """전층 재료마감 집계: 공종별 / 전체 보기."""

    markup_changed = Signal(str, float)
    apply_material_markups_requested = Signal(dict)
    HEADERS = ["코드", "품명", "규격", "단위", "수량", "할증수량", "할증값"]

    def __init__(self, material_trades=None, parent=None):
        super().__init__(parent)
        self.material_trades = list(material_trades or [])
        self.rows: list[dict] = []
        self.markups: dict[str, float] = {}
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        title_row = QHBoxLayout()
        title = QLabel("재료마감 집계")
        f = title.font(); f.setBold(True); title.setFont(f)
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.options_btn = QPushButton("옵션")
        self.options_btn.clicked.connect(self._open_options)
        title_row.addWidget(self.options_btn)
        root.addLayout(title_row)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        trade_page = QWidget()
        trade_layout = QHBoxLayout(trade_page)
        trade_layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        self.trade_list = QListWidget()
        self.trade_list.setMinimumWidth(150)
        self.trade_list.setMaximumWidth(260)
        self.trade_table = self._new_table()
        splitter.addWidget(self.trade_list)
        splitter.addWidget(self.trade_table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([190, 1000])
        trade_layout.addWidget(splitter)
        self.tabs.addTab(trade_page, "공종별")

        self.all_table = self._new_table()
        self.tabs.addTab(self.all_table, "전체")

        self.trade_list.currentItemChanged.connect(self._refresh_trade_table)
        self.trade_table.itemChanged.connect(self._on_item_changed)
        self.all_table.itemChanged.connect(self._on_item_changed)

    def _new_table(self) -> QTableWidget:
        table = QTableWidget(0, len(self.HEADERS))
        table.setHorizontalHeaderLabels(self.HEADERS)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        widths = [145, 220, 220, 75, 110, 110, 85]
        for col, width in enumerate(widths):
            table.setColumnWidth(col, width)
        header.setStretchLastSection(False)
        return table

    @staticmethod
    def _row_key(row: dict) -> str:
        return str(row.get("key") or row.get("id") or row.get("code") or row.get("name") or "미지정")

    @staticmethod
    def _trade_name(row: dict) -> str:
        return str(row.get("trade") or row.get("trade_name") or "").strip() or "미구분"

    def _trade_sort_key(self, name: str):
        name = str(name or "")
        if name == "미구분":
            return (9, 999999, name)
        for index, trade in enumerate(self.material_trades):
            trade_name = str(trade.get("name", "") or "").strip()
            if trade_name == name:
                return (0, index, name)
        return (1, 0, name.casefold())

    def set_material_trades(self, material_trades):
        self.material_trades = list(material_trades or [])
        self._refresh_all()

    def set_data(self, rows, markups=None):
        self.rows = [dict(row or {}) for row in list(rows or [])]
        if markups is not None:
            self.markups = {
                str(key): float(value or 0.0)
                for key, value in dict(markups or {}).items()
                if str(key)
            }
        self._refresh_all()

    def _refresh_all(self):
        current_trade = self.trade_list.currentItem().data(Qt.UserRole) if self.trade_list.currentItem() else None
        trades = sorted({self._trade_name(row) for row in self.rows}, key=self._trade_sort_key)
        self.trade_list.blockSignals(True)
        try:
            self.trade_list.clear()
            for name in trades:
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, name)
                self.trade_list.addItem(item)
            if self.trade_list.count():
                target = 0
                if current_trade:
                    for i in range(self.trade_list.count()):
                        if self.trade_list.item(i).data(Qt.UserRole) == current_trade:
                            target = i; break
                self.trade_list.setCurrentRow(target)
        finally:
            self.trade_list.blockSignals(False)
        self._refresh_trade_table()
        self._fill_all_table()

    def _refresh_trade_table(self, *_args):
        item = self.trade_list.currentItem()
        trade = item.data(Qt.UserRole) if item else None
        rows = [row for row in self.rows if trade is not None and self._trade_name(row) == trade]
        self._fill_material_rows(self.trade_table, rows)

    def _fill_all_table(self):
        grouped = defaultdict(list)
        for row in self.rows:
            grouped[self._trade_name(row)].append(row)
        ordered_trades = sorted(grouped, key=self._trade_sort_key)
        table = self.all_table
        self._loading = True
        table.blockSignals(True)
        try:
            table.clearSpans()
            table.setRowCount(0)
            for trade in ordered_trades:
                header_row = table.rowCount()
                table.insertRow(header_row)
                item = QTableWidgetItem(f"■ {trade}")
                font = item.font(); font.setBold(True); item.setFont(font)
                item.setBackground(QBrush(QColor("#D9E6F2")))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(header_row, 0, item)
                table.setSpan(header_row, 0, 1, len(self.HEADERS))
                for row in sorted(grouped[trade], key=lambda value: (str(value.get("code", "")), str(value.get("name", "")))):
                    self._append_material_row(table, row)
        finally:
            table.blockSignals(False)
            self._loading = False

    def _fill_material_rows(self, table: QTableWidget, rows: list[dict]):
        self._loading = True
        table.blockSignals(True)
        try:
            table.clearSpans()
            table.setRowCount(0)
            for row in sorted(rows, key=lambda value: (str(value.get("code", "")), str(value.get("name", "")))):
                self._append_material_row(table, row)
        finally:
            table.blockSignals(False)
            self._loading = False

    def _append_material_row(self, table: QTableWidget, row_data: dict):
        r = table.rowCount(); table.insertRow(r)
        key = self._row_key(row_data)
        quantity = float(row_data.get("quantity", 0.0) or 0.0)
        if key in self.markups:
            markup = float(self.markups.get(key, 0.0) or 0.0)
        else:
            try:
                markup = float(str(row_data.get("markup", "") or "").replace("%", "").strip() or 0.0)
            except ValueError:
                markup = 0.0
        adjusted = quantity * (1.0 + markup / 100.0)
        values = [
            str(row_data.get("code", "") or ""),
            str(row_data.get("name", "") or ""),
            str(row_data.get("spec", "") or ""),
            str(row_data.get("unit", "") or ""),
            f"{quantity:,.3f}",
            f"{adjusted:,.3f}",
            f"{markup:g}",
        ]
        for c, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole, key)
            if c != 6:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if c in (4, 5, 6):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(r, c, item)

    def _open_options(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("집계 옵션")
        dialog.resize(420, 150)
        layout = QVBoxLayout(dialog)
        guide = QLabel("집계표의 '할증값'에 직접 입력한 값을 기본 재료의 할증 열로 복사합니다.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        apply_btn = QPushButton("재료 할증값 기본재료에 반영")
        layout.addWidget(apply_btn)
        close_row = QHBoxLayout(); close_row.addStretch(1)
        close_btn = QPushButton("닫기"); close_row.addWidget(close_btn); layout.addLayout(close_row)

        def apply_values():
            if not self.markups:
                QMessageBox.information(dialog, "할증값 반영", "집계에서 직접 입력한 할증값이 없습니다.")
                return
            self.apply_material_markups_requested.emit(dict(self.markups))

        apply_btn.clicked.connect(apply_values)
        close_btn.clicked.connect(dialog.accept)
        dialog.exec()

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 6:
            return
        table = item.tableWidget()
        key = str(item.data(Qt.UserRole) or "")
        if not key:
            return
        try:
            value = float(item.text().replace("%", "").replace(",", "").strip() or 0.0)
        except ValueError:
            value = float(self.markups.get(key, 0.0) or 0.0)
        value = max(-100.0, value)
        self.markups[key] = value
        # 같은 재료가 다른 보기에도 있으므로 전체를 다시 계산한다.
        self.markup_changed.emit(key, value)
        self._refresh_all()
