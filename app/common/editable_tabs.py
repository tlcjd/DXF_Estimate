from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, Signal
from PySide6.QtWidgets import QInputDialog, QTabWidget


class EditableTabWidget(QTabWidget):
    """더블클릭으로 탭 표시명을 변경할 수 있는 QTabWidget."""

    tabNameEdited = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tabBarDoubleClicked.connect(self._edit_tab_name)

    def _edit_tab_name(self, index: int) -> None:
        if index < 0:
            return
        current = self.tabText(index)
        # 개수 표기가 붙은 경우 편집창에는 기본 이름만 제시한다.
        base_name = current.rsplit(" (", 1)[0].strip()
        name, ok = QInputDialog.getText(
            self,
            "탭 이름 변경",
            "새 탭 이름:",
            text=base_name,
        )
        name = name.strip()
        if not ok or not name:
            return
        self.tabNameEdited.emit(index, name)
