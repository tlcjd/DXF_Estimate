from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout, QLabel, QComboBox, QLineEdit, QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from app.common.editable_tabs import EditableTabWidget
from app.common.models import ResultRow


class ResultTable(QTableWidget):
    headers = ["정규화 문자", "원문 예시", "재료명", "두께(mm)", "출현횟수", "레이어", "객체형식", "핸들"]

    def __init__(self):
        super().__init__(0, len(self.headers))
        self.setHorizontalHeaderLabels(self.headers)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        widths = [360, 360, 160, 90, 90, 180, 100, 200]
        for i, width in enumerate(widths):
            self.setColumnWidth(i, width)

    def set_rows(self, rows: list[ResultRow]):
        self.setSortingEnabled(False)
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row.normalized,
                row.raw_example,
                row.material,
                row.thickness,
                row.count,
                row.layers,
                row.entity_types,
                row.handles,
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem()
                if c == 4:
                    item.setData(Qt.DisplayRole, int(value))
                    item.setTextAlignment(Qt.AlignCenter)
                else:
                    item.setText(str(value))
                self.setItem(r, c, item)
        self.setSortingEnabled(True)


class MaterialTextTab(QWidget):
    """상위 재료문자 탭 안에서 후보/보류/제외를 하위 탭으로 관리한다."""

    reread_requested = Signal()
    categories = ["재료 후보", "보류", "제외"]

    def __init__(
        self,
        display_names: dict[str, str] | None = None,
        name_changed: Callable[[str, str], None] | None = None,
    ):
        super().__init__()
        self.display_names = {
            category: (display_names or {}).get(category, category)
            for category in self.categories
        }
        self.row_counts = {category: 0 for category in self.categories}
        self.name_changed = name_changed

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QHBoxLayout()
        controls.addStretch(1)
        controls.addWidget(QLabel("정렬:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["가나다순", "출현횟수 많은 순"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("현재 재료 탭 검색")
        controls.addWidget(self.sort_combo)
        controls.addWidget(self.search)
        self.reread_button = QPushButton("재료 다시읽기")
        self.reread_button.setToolTip("현재 연결된 CAD의 도곽 구분과 관계없이 전체 문자와 블록 속성을 다시 읽습니다.")
        controls.addWidget(self.reread_button)
        layout.addLayout(controls)

        self.tabs = EditableTabWidget()
        self.tables: dict[str, ResultTable] = {}
        for name in self.categories:
            table = ResultTable()
            self.tables[name] = table
            self.tabs.addTab(table, self.display_names[name])
        self.tabs.tabNameEdited.connect(self._tab_name_edited)
        self.reread_button.clicked.connect(self.reread_requested.emit)
        layout.addWidget(self.tabs)

    def current_table(self) -> ResultTable:
        return self.tabs.currentWidget()

    def _tab_name_edited(self, index: int, new_name: str) -> None:
        if not 0 <= index < len(self.categories):
            return
        category = self.categories[index]
        self.display_names[category] = new_name
        self._refresh_tab_text(category)
        if self.name_changed:
            self.name_changed(category, new_name)

    def _refresh_tab_text(self, category: str) -> None:
        index = self.categories.index(category)
        count = self.row_counts[category]
        text = self.display_names[category]
        if count:
            text = f"{text} ({count:,})"
        self.tabs.setTabText(index, text)

    def set_category_rows(self, name: str, rows: list[ResultRow]):
        table = self.tables[name]
        table.set_rows(rows)
        self.row_counts[name] = len(rows)
        self._refresh_tab_text(name)
