from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from ezdxf.colors import aci2rgb


def aci_qcolor(index: int) -> QColor:
    try:
        index = max(1, min(255, int(index)))
        rgb = aci2rgb(index)
        return QColor(int(rgb.r), int(rgb.g), int(rgb.b))
    except Exception:
        return QColor(255, 255, 255)


class AciColorDialog(QDialog):
    """CAD의 색상 색인(ACI) 방식과 동일한 1~255 색상 선택창.

    화면 배치는 CAD의 '색상 색인(ACI)' 팔레트처럼 10~249 색상을
    24개 색상군 x 10개 명암단계로 보여주고, 기본색 1~9와 250~255를
    아래에 별도로 배치한다. 저장값은 RGB가 아니라 ACI 번호다.
    """

    def __init__(self, current_aci: int = 7, parent=None, title: str = "색상 선택"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(500, 410)
        self._selected_aci = max(1, min(255, int(current_aci or 7)))
        self._buttons: dict[int, QPushButton] = {}

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(10, 10, 10, 10)
        page_layout.setSpacing(6)
        page_layout.addWidget(QLabel("색상 색인 (ACI):"))

        palette_frame = QFrame()
        palette_layout = QGridLayout(palette_frame)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.setHorizontalSpacing(1)
        palette_layout.setVerticalSpacing(1)

        # ACI 10~249 = 24개 색상군 x 10개 명암 단계.
        for hue_group in range(24):
            base = 10 + hue_group * 10
            for shade in range(10):
                aci = base + shade
                row = shade + (1 if shade >= 5 else 0)  # CAD 팔레트처럼 중간에 시각적 간격
                button = self._make_color_button(aci)
                palette_layout.addWidget(button, row, hue_group)
        page_layout.addWidget(palette_frame)
        page_layout.addSpacing(8)

        special = QGridLayout()
        special.setHorizontalSpacing(2)
        special.setVerticalSpacing(4)
        # 기본 색상 1~9
        for col, aci in enumerate(range(1, 10)):
            special.addWidget(self._make_color_button(aci, 24, 20), 0, col)
        # 회색 계열 250~255
        for col, aci in enumerate(range(250, 256)):
            special.addWidget(self._make_color_button(aci, 24, 20), 1, col)
        bylayer = QPushButton("도면층별(L)")
        byblock = QPushButton("블록별(K)")
        # 그룹색은 실제 ACI 1~255가 필요하므로 BYLAYER/BYBLOCK은 CAD 창처럼 보이되 선택은 막는다.
        bylayer.setEnabled(False)
        byblock.setEnabled(False)
        special.addWidget(bylayer, 0, 11, 1, 4)
        special.addWidget(byblock, 0, 15, 1, 4)
        special.setColumnStretch(20, 1)
        page_layout.addLayout(special)

        value_row = QHBoxLayout()
        value_row.addWidget(QLabel("색상(C):"))
        self.value_edit = QLineEdit(str(self._selected_aci))
        self.value_edit.setMaximumWidth(250)
        self.value_edit.selectAll()
        self.value_edit.textChanged.connect(self._on_text_changed)
        value_row.addWidget(self.value_edit)
        value_row.addStretch(1)
        self.preview = QLabel()
        self.preview.setFixedSize(88, 42)
        value_row.addWidget(self.preview)
        page_layout.addLayout(value_row)

        tabs.addTab(page, "색인 색상")
        root.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("확인")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._accept_value)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_selection()

    def _make_color_button(self, aci: int, width: int = 18, height: int = 13) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(width, height)
        button.setCursor(Qt.PointingHandCursor)
        color = aci_qcolor(aci)
        button.setStyleSheet(
            "QPushButton {"
            f"background-color: {color.name()}; border: 1px solid #444; padding: 0px;"
            "} QPushButton:hover { border: 2px solid #ffffff; }"
        )
        button.setToolTip(f"ACI {aci}")
        button.clicked.connect(lambda _checked=False, value=aci: self.set_selected_aci(value))
        self._buttons[aci] = button
        return button

    def set_selected_aci(self, value: int) -> None:
        value = max(1, min(255, int(value)))
        self._selected_aci = value
        self.value_edit.blockSignals(True)
        self.value_edit.setText(str(value))
        self.value_edit.blockSignals(False)
        self._refresh_selection()

    def _on_text_changed(self, text: str) -> None:
        try:
            value = int(str(text or "").strip())
        except ValueError:
            return
        if 1 <= value <= 255:
            self._selected_aci = value
            self._refresh_selection()

    def _refresh_selection(self) -> None:
        color = aci_qcolor(self._selected_aci)
        self.preview.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid #777;"
        )
        self.preview.setToolTip(f"ACI {self._selected_aci} / {color.name().upper()}")
        for aci, button in self._buttons.items():
            border = "3px solid #000000" if aci == self._selected_aci else "1px solid #444444"
            button.setStyleSheet(
                "QPushButton {"
                f"background-color: {aci_qcolor(aci).name()}; border: {border}; padding: 0px;"
                "} QPushButton:hover { border: 2px solid #ffffff; }"
            )

    def _accept_value(self) -> None:
        try:
            value = int(str(self.value_edit.text() or "").strip())
        except ValueError:
            value = 0
        if not 1 <= value <= 255:
            QMessageBox.warning(self, "색상 선택", "ACI 색상번호는 1~255 사이로 입력해 주세요.")
            return
        self._selected_aci = value
        self.accept()

    def selected_aci(self) -> int:
        return int(self._selected_aci)


def get_aci_color(current_aci: int = 7, parent=None, title: str = "색상 선택") -> int | None:
    dialog = AciColorDialog(current_aci, parent, title)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.selected_aci()
