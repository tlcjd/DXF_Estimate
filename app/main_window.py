from __future__ import annotations

import sys
import os
import json
import hashlib
import fnmatch
import subprocess
import traceback
import uuid
from copy import deepcopy
import ezdxf
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PySide6.QtCore import Qt, QPointF, QRect, QRectF, QTimer, QSize, QObject, QEvent, QItemSelectionModel, QSignalBlocker, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication,QFileDialog,QHBoxLayout,QGridLayout,QLabel,QMainWindow,QMessageBox,QPushButton,QTabWidget,QVBoxLayout,QWidget,QProgressBar,QDialog,QDialogButtonBox,QTableWidget,QTableWidgetItem,QHeaderView,QCheckBox,QTextEdit,QInputDialog,QAbstractItemView,QTabBar,QStylePainter,QStyleOptionTab,QStyle,QComboBox,QSpinBox,QLineEdit,QStyledItemDelegate,QStyleOptionViewItem)
from app.core.settings import AppSettings
from app.common.settings_dialog import SettingsDialog, DisplaySettingsDialog
from app.common.project_dialog import ProjectListDialog
from app.common.project_manager import ProjectManager, ProjectRecord
from app.common.layer_defaults import LAYER_DEFINITIONS, LAYER_KEYS, default_layer_name, default_layer_color, migrate_legacy_layer_settings
from app.common.room_categories import (
    DEFAULT_ROOM_CATEGORY_KEYWORDS,
    ROOM_CATEGORIES,
    ROOM_CATEGORY_INI_KEYS,
    normalize_room_category_rules,
)
from app.common.finish_calculations import (
    normalize_finish_categories,
    normalize_formula_codes,
    serialize_finish_categories,
    serialize_formula_codes,
)
from app.common.material_trades import normalize_material_trades, serialize_material_trades
from app.common.material_units import normalize_material_units, serialize_material_units
from app.common.window_categories import normalize_window_categories, serialize_window_categories
from app.common.cad_sync_dialog import CadSyncDialog
from app.common.project_cache import (
    cache_exists as project_cache_exists,
    load_project_cache,
    load_room_category_snapshots,
    restore_room_categories,
    save_project_cache,
)
from app.common.project_snapshot import (
    SECTION_LABELS, AUTOSAVE_DIR_NAME, existing_sections, load_project_snapshot, save_project_snapshot,
)
from app.common.project_autosave import ProjectAutosaveThread
from app.cad.bridge import CadBridge, CadConnectionError
from app.common.editable_tabs import EditableTabWidget
from app.material_text.core import MaterialClassifier, aggregate, iter_text_records
from app.material_text.widget import MaterialTextTab, ResultTable
from app.rooms.core import (
    validate_room_numbers, read_rooms, read_cad_background, read_drawing_divisions, build_room_import_diagnostics,
    normalize_drawing_floor_input, drawing_input_from_title, drawing_title_from_input, next_drawing_floor, _classify_drawing_title,
)
from app.rooms.widget import RoomsTab
from app.rooms.background_cache import load_background_cache, build_background_cache, cache_dir_for_source
from app.rooms.background_source_cache import (
    save_background_source_cache, load_background_source_cache, cache_file_for_source, source_path_from_cache_file,
    floor_frame_rects, plan_frame_rects, build_opening_candidates_by_floor, detect_opening_candidates_dxf,
)
from app.rooms.background_program_cache import (
    build_filter_catalog, default_selection, build_program_cache, build_raw_preview_floors,
    filter_program_floors, ensure_program_edit_ids, program_layer_names, move_program_edit_items,
    load_background_program_cache, save_background_program_cache, clear_background_program_cache,
    program_cache_file,
)
from app.finish_materials import FinishMaterialsPanel
from app.overview_tab import OverviewTab, PlaceholderTab
from app.summary_tab import SummaryTab
from app.other_tab import OtherWorksTab
from app.window_tab import WindowTab
from app.worker import BackgroundScanWorker, CadBackgroundWorker, ScanWorker
from app.services.block_service import BlockService
from app.services.layer_service import LayerService
from app.core.paths import (
    BLOCK_LIBRARY_DIR,
    CAD_BACKGROUND_CACHE_DIR,
    DRAWINGS_DIR,
    MATERIAL_DICTIONARY_PATH,
    MATERIAL_FINISH_PATH,
    TEMP_CAD_DXF_PATH,
    TEMP_PROJECT_CACHE_DIR,
    TEMP_PROJECT_DIR,
    PROJECTS_DIR,
    PROJECT_BACKGROUND_DXF_PATH,
    ensure_user_file_structure,
)

APP_DIR=Path(__file__).resolve().parent.parent
DICT_PATH=MATERIAL_DICTIONARY_PATH
SETTINGS_PATH=APP_DIR/'settings.ini'
RECENT_SETTINGS_PATH=APP_DIR/'settings2.ini'
BACKGROUND_CACHE_ROOT = CAD_BACKGROUND_CACHE_DIR


class _SortableTableItem(QTableWidgetItem):
    """표시 문자열과 별개로 숫자/정렬 키를 안정적으로 비교한다."""

    SORT_ROLE = Qt.UserRole + 50

    def __lt__(self, other):
        try:
            left = self.data(self.SORT_ROLE)
            right = other.data(self.SORT_ROLE) if isinstance(other, QTableWidgetItem) else None
            if left is not None and right is not None:
                return left < right
        except Exception:
            pass
        return super().__lt__(other)


class _CheckSortableItem(QTableWidgetItem):
    """체크 상태 자체를 정렬키로 사용하는 표시열 항목."""

    def __lt__(self, other):
        try:
            left = 1 if self.checkState() == Qt.Checked else 0
            right = 1 if isinstance(other, QTableWidgetItem) and other.checkState() == Qt.Checked else 0
            if left != right:
                return left < right
            # 체크 상태가 같으면 항목명이 있으면 이름으로 안정적으로 묶는다.
            left_name = str(self.data(Qt.UserRole + 1) or "").casefold()
            right_name = str(other.data(Qt.UserRole + 1) or "").casefold() if isinstance(other, QTableWidgetItem) else ""
            return left_name < right_name
        except Exception:
            return super().__lt__(other)


class _BlackCheckDelegate(QStyledItemDelegate):
    """표시열 체크박스를 OS 스타일과 무관하게 검은색으로 직접 그린다."""

    def paint(self, painter, option, index):
        state = index.data(Qt.CheckStateRole)
        if state not in (Qt.Checked, Qt.Unchecked, Qt.PartiallyChecked):
            super().paint(painter, option, index)
            return

        # 체크 가능한 표시열은 super().paint()를 호출하지 않는다.
        # Windows/Qt 기본 회색 체크 인디케이터가 섞이지 않도록 셀 배경부터
        # 체크박스까지 전부 직접 그린다.
        painter.save()
        try:
            selected = bool(option.state & QStyle.State_Selected)
            bg = option.palette.highlight().color() if selected else option.palette.base().color()
            painter.fillRect(option.rect, bg)

            style = option.widget.style() if option.widget is not None else QApplication.style()
            w = max(14, int(style.pixelMetric(QStyle.PM_IndicatorWidth, None, option.widget)))
            h = max(14, int(style.pixelMetric(QStyle.PM_IndicatorHeight, None, option.widget)))
            size = min(w, h, max(14, option.rect.height() - 6))
            box = QRect(0, 0, size, size)
            box.moveCenter(option.rect.center())

            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(box, QColor(255, 255, 255))
            border = QPen(QColor(0, 0, 0))
            border.setWidthF(1.2)
            painter.setPen(border)
            painter.drawRect(box.adjusted(0, 0, -1, -1))

            if state == Qt.Checked:
                pen = QPen(QColor(0, 0, 0))
                pen.setWidthF(max(2.2, size * 0.15))
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                x = box.left(); y = box.top(); bw = box.width(); bh = box.height()
                path = QPainterPath()
                path.moveTo(x + bw * 0.18, y + bh * 0.52)
                path.lineTo(x + bw * 0.40, y + bh * 0.74)
                path.lineTo(x + bw * 0.84, y + bh * 0.22)
                painter.drawPath(path)
            elif state == Qt.PartiallyChecked:
                pen = QPen(QColor(0, 0, 0))
                pen.setWidthF(max(2.2, size * 0.15))
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(box.left() + box.width() * 0.22, box.center().y()),
                    QPointF(box.right() - box.width() * 0.22, box.center().y()),
                )
        finally:
            painter.restore()


class _PercentSpinDelegate(QStyledItemDelegate):
    """평소에는 값만 표시하고 편집할 때만 스핀박스를 띄운다."""

    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(-100, 100)
        editor.setSingleStep(5)
        editor.setSuffix(" %")
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor, index):
        try:
            editor.setValue(int(index.data(Qt.EditRole) or 0))
        except Exception:
            editor.setValue(0)

    def setModelData(self, editor, model, index):
        model.setData(index, int(editor.value()), Qt.EditRole)

    def displayText(self, value, locale):
        try:
            number = int(value)
            return "0 %" if number == 0 else f"{number:+d} %"
        except Exception:
            return "0 %"


class _SpaceToggleTableWidget(QTableWidget):
    """도면정리 목록에서 어느 열에 포커스가 있어도 Space로 표시 체크를 토글한다."""

    check_toggle_finished = Signal()
    enter_edit_requested = Signal(int, int)

    def __init__(self, *args, check_column: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._check_column = int(check_column)
        # 목록 열 헤더로 정렬하면 현재 선택/포커스 항목이 화면 맨 위에 오도록 한다.
        # QTableWidget은 정렬 후에도 currentItem 객체를 유지하므로 다음 이벤트 루프에서
        # PositionAtTop으로 스크롤하면 정렬 결과를 즉시 확인할 수 있다.
        self.horizontalHeader().sortIndicatorChanged.connect(self._scroll_current_to_top_after_sort)

    def _scroll_current_to_top_after_sort(self, *_args):
        QTimer.singleShot(0, self._scroll_current_to_top)

    def _scroll_current_to_top(self):
        item = self.currentItem()
        if item is not None:
            self.scrollToItem(item, QAbstractItemView.PositionAtTop)
        elif self.rowCount() > 0:
            first = self.item(0, max(0, self.currentColumn())) or self.item(0, 0)
            if first is not None:
                self.scrollToItem(first, QAbstractItemView.PositionAtTop)

    @staticmethod
    def _plain_key_event(event) -> bool:
        return not bool(event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))

    def _toggle_selected_checks(self) -> bool:
        # SelectRows 모드뿐 아니라 현재 셀만 포커스된 경우까지 모두 처리한다.
        rows = {index.row() for index in self.selectedIndexes()}
        if self.selectionModel() is not None:
            rows.update(index.row() for index in self.selectionModel().selectedRows())
        if self.currentRow() >= 0:
            rows.add(self.currentRow())
        rows = sorted(rows)
        toggle_items = []
        for row in rows:
            item = self.item(row, self._check_column)
            if item is not None and bool(item.flags() & Qt.ItemIsUserCheckable) and bool(item.flags() & Qt.ItemIsEnabled):
                toggle_items.append(item)
        if not toggle_items:
            return False
        current = self.item(self.currentRow(), self._check_column) if self.currentRow() >= 0 else toggle_items[0]
        target = Qt.Unchecked if (current is not None and current.checkState() == Qt.Checked) else Qt.Checked
        previous = self.blockSignals(True)
        try:
            for item in toggle_items:
                item.setCheckState(target)
        finally:
            self.blockSignals(previous)
        self.viewport().update()
        self.check_toggle_finished.emit()
        return True

    def event(self, event):
        # 일부 Qt/IME 조합에서는 Space가 keyPressEvent까지 내려오지 않는 경우가 있어
        # event 단계에서 키코드와 실제 입력문자 둘 다 검사한다.
        if event.type() == QEvent.KeyPress and self._plain_key_event(event):
            key = event.key()
            text = event.text() or ""
            if key == Qt.Key_Space or text == " ":
                if self._toggle_selected_checks():
                    event.accept()
                    return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                row, column = self.currentRow(), self.currentColumn()
                item = self.item(row, column) if row >= 0 and column >= 0 else None
                if item is not None and bool(item.flags() & Qt.ItemIsEditable):
                    self.editItem(item)
                    self.enter_edit_requested.emit(row, column)
                    event.accept()
                    return True
        return super().event(event)

    def keyPressEvent(self, event):
        # event()에서 처리되지 않은 키만 기본 QTableWidget 동작으로 넘긴다.
        super().keyPressEvent(event)


class VectorDirectSelectStatusDialog(QDialog):
    """도면 직접선택 작업 중 화면 상단에 유지되는 작은 상태창."""
    off_requested = Signal()
    save_requested = Signal()
    undo_requested = Signal()
    close_requested = Signal()
    live_apply_changed = Signal(bool)
    all_layers_view_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("도면 직접선택")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.Tool, True)
        self.setModal(False)
        self._all_layers_view = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(5)

        self.info_label = QLabel("선택 0개")
        self.info_label.setMinimumWidth(130)
        root.addWidget(self.info_label)

        # 조작버튼은 작은 상태창 안에서 2 x 2로 묶는다.
        controls = QGridLayout()
        controls.setHorizontalSpacing(6)
        controls.setVerticalSpacing(4)
        self.off_btn = QPushButton("레이어 OFF")
        self.undo_btn = QPushButton("되돌리기")
        self.save_btn = QPushButton("저장")
        self.close_btn = QPushButton("닫기")
        for button in (self.off_btn, self.undo_btn, self.save_btn, self.close_btn):
            button.setMinimumWidth(92)
        self.off_btn.clicked.connect(self.off_requested.emit)
        self.undo_btn.clicked.connect(self.undo_requested.emit)
        self.save_btn.clicked.connect(self.save_requested.emit)
        self.close_btn.clicked.connect(self.close_requested.emit)
        controls.addWidget(self.off_btn, 0, 0)
        controls.addWidget(self.undo_btn, 0, 1)
        controls.addWidget(self.save_btn, 1, 0)
        controls.addWidget(self.close_btn, 1, 1)
        root.addLayout(controls)

        # 순서 고정: 실시간 적용이 위, 모든 레이어 보기가 바로 아래.
        options = QVBoxLayout()
        options.setSpacing(2)
        self.live_apply_check = QCheckBox("실시간 적용")
        self.live_apply_check.setChecked(False)
        self.live_apply_check.setToolTip(
            "일반 모드에서는 선택한 레이어/블록을 즉시 OFF합니다. "
            "모든 레이어 보기에서는 선택한 레이어/블록을 ON 대상에 즉시 저장합니다."
        )
        self.live_apply_check.toggled.connect(self.live_apply_changed.emit)
        options.addWidget(self.live_apply_check)
        self.all_layers_view_check = QCheckBox("모든 레이어 보기")
        self.all_layers_view_check.setChecked(False)
        self.all_layers_view_check.setToolTip(
            "현재 숨겨진 레이어/블록까지 임시로 모두 표시합니다. 이 상태에서 파란색으로 선택 후 [레이어 ON]을 누르면 ON 대상으로 저장되고, 체크를 끌 때 실제 표시상태에 반영됩니다."
        )
        self.all_layers_view_check.toggled.connect(self.all_layers_view_changed.emit)
        options.addWidget(self.all_layers_view_check)
        root.addLayout(options)

        self.help_label = QLabel(
            "클릭/드래그: 선택 추가 · Ctrl+클릭/드래그: 선택 해제 · Space: 레이어 OFF/ON\n"
            "Ctrl+Z: 되돌리기 · 모든 레이어 보기: 숨김 객체 표시/ON 대상 선택 · 저장: 현재 상태 확정"
        )
        self.help_label.setStyleSheet("color: #555;")
        self.help_label.setWordWrap(True)
        root.addWidget(self.help_label)
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.activated.connect(self.undo_requested.emit)
        self._allow_close = False
        self.resize(480, self.sizeHint().height())

    def set_all_layers_view(self, enabled: bool):
        self._all_layers_view = bool(enabled)
        self.off_btn.setText("레이어 ON" if self._all_layers_view else "레이어 OFF")
        self.off_btn.setToolTip(
            "현재 파란색 선택 레이어/블록을 ON 대상에 저장합니다. 모든 레이어 보기 체크를 끌 때 실제로 반영됩니다."
            if self._all_layers_view else
            "현재 선택한 레이어/블록을 OFF합니다."
        )
        self.set_selection(set())

    def set_selection(self, values):
        values = set(values or set())
        layers = sum(1 for kind, _name in values if kind == "layer")
        blocks = sum(1 for kind, _name in values if kind == "block")
        pending = ""
        if self._all_layers_view:
            pending = " · ON대상 선택"
        self.info_label.setText(f"선택 {len(values)}개 · 레이어 {layers} / 블록 {blocks}{pending}")
        self.off_btn.setEnabled(bool(values))

    def closeEvent(self, event):
        if bool(getattr(self, "_allow_close", False)):
            event.accept()
            return
        event.ignore()
        self.close_requested.emit()


class LayerSelectDialog(QDialog):
    """그룹 편집에서 이동할 기존/새 레이어를 고르는 작은 목록창."""
    def __init__(self, layers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("레이어 선택")
        self.resize(360, 430)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("이동할 레이어"))
        self.list = QTableWidget(0, 1)
        self.list.setHorizontalHeaderLabels(["레이어"])
        self.list.horizontalHeader().setStretchLastSection(True)
        self.list.verticalHeader().setVisible(False)
        self.list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        for name in sorted({str(v) for v in layers or [] if str(v).strip()}, key=str.casefold):
            row = self.list.rowCount(); self.list.insertRow(row)
            self.list.setItem(row, 0, QTableWidgetItem(name))
        root.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        new_btn = QPushButton("새 레이어")
        ok_btn = QPushButton("선택")
        cancel_btn = QPushButton("취소")
        new_btn.clicked.connect(self._new_layer)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(new_btn); buttons.addStretch(1); buttons.addWidget(ok_btn); buttons.addWidget(cancel_btn)
        root.addLayout(buttons)
        self._new_name = ""
        self.list.doubleClicked.connect(lambda _idx: self.accept())

    def _new_layer(self):
        text, ok = QInputDialog.getText(self, "새 레이어", "새 레이어명")
        name = str(text or "").strip()
        if ok and name:
            self._new_name = name
            self.accept()

    def selected_layer(self):
        if self._new_name:
            return self._new_name
        row = self.list.currentRow()
        item = self.list.item(row, 0) if row >= 0 else None
        return str(item.text() or "").strip() if item is not None else ""


class VectorGroupEditStatusDialog(QDialog):
    transition_requested = Signal()
    undo_requested = Signal()
    save_requested = Signal()
    close_requested = Signal()
    live_apply_changed = Signal(bool)
    target_group_changed = Signal(str)
    color_mode_requested = Signal()
    specified_mode_requested = Signal()

    def __init__(self, source_group: str, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("최적화 그룹 편집")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.Tool, True)
        self.setModal(False)
        self.source_group = str(source_group or "")
        self._mode = "color"
        self._specified_layer = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(5)
        self.info_label = QLabel(f"편집 그룹: {self.source_group} · 선택 0개")
        root.addWidget(self.info_label)
        self.live_check = QCheckBox("실시간 적용")
        self.live_check.setChecked(False)
        self.live_check.toggled.connect(self.live_apply_changed.emit)
        root.addWidget(self.live_check)

        mode_row = QHBoxLayout()
        self.color_btn = QPushButton("색상레이어로 보내기")
        self.color_btn.setCheckable(True); self.color_btn.setChecked(True)
        self.layer_btn = QPushButton("지정레이어로 보내기")
        self.layer_btn.setCheckable(True)
        self.color_btn.clicked.connect(self._choose_color)
        self.layer_btn.clicked.connect(self._choose_specified)
        mode_row.addWidget(self.color_btn); mode_row.addWidget(self.layer_btn)
        root.addLayout(mode_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("이동 그룹"))
        self.group_combo = QComboBox()
        for key, label in groups:
            if str(key) == self.source_group:
                continue
            self.group_combo.addItem(str(label), str(key))
        self.group_combo.currentIndexChanged.connect(lambda _i: self.target_group_changed.emit(self.target_group()))
        target_row.addWidget(self.group_combo, 1)
        root.addLayout(target_row)
        self.layer_label = QLabel("대상 레이어: 객체 색상")
        root.addWidget(self.layer_label)

        buttons = QGridLayout()
        self.transition_btn = QPushButton("전환")
        self.transition_btn.setEnabled(False)
        self.undo_btn = QPushButton("되돌리기")
        self.save_btn = QPushButton("저장")
        self.close_btn = QPushButton("닫기")
        self.transition_btn.clicked.connect(self.transition_requested.emit)
        self.undo_btn.clicked.connect(self.undo_requested.emit)
        self.save_btn.clicked.connect(self.save_requested.emit)
        self.close_btn.clicked.connect(self.close_requested.emit)
        buttons.addWidget(self.transition_btn, 0, 0); buttons.addWidget(self.undo_btn, 0, 1)
        buttons.addWidget(self.save_btn, 1, 0); buttons.addWidget(self.close_btn, 1, 1)
        root.addLayout(buttons)
        help_label = QLabel(
            "클릭/드래그: 객체 선택 · Ctrl+클릭/드래그: 선택 해제 · Space: 전환\n"
            "전환은 대상 그룹 + 레이어 모드가 지정된 경우만 실행 · Ctrl+Z: 되돌리기"
        )
        help_label.setWordWrap(True); help_label.setStyleSheet("color:#555;")
        root.addWidget(help_label)
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.activated.connect(self.undo_requested.emit)
        self._allow_close = False
        self.resize(470, self.sizeHint().height())

    def _choose_color(self):
        self._mode = "color"
        self.color_btn.setChecked(True); self.layer_btn.setChecked(False)
        self.layer_label.setText("대상 레이어: 객체 색상")
        self.color_mode_requested.emit()

    def _choose_specified(self):
        self._mode = "specified"
        self.color_btn.setChecked(False); self.layer_btn.setChecked(True)
        self.specified_mode_requested.emit()

    def set_specified_layer(self, name: str):
        self._specified_layer = str(name or "").strip()
        if self._specified_layer:
            self._mode = "specified"
            self.color_btn.setChecked(False); self.layer_btn.setChecked(True)
            self.layer_label.setText(f"대상 레이어: {self._specified_layer}")
        else:
            self._choose_color()

    def mode(self): return self._mode
    def specified_layer(self): return self._specified_layer
    def target_group(self): return str(self.group_combo.currentData() or "")
    def set_selection_count(self, count: int):
        self.info_label.setText(f"편집 그룹: {self.source_group} · 선택 {int(count)}개")
        self.transition_btn.setEnabled(int(count) > 0 and bool(self.target_group()) and (self._mode == "color" or bool(self._specified_layer)))

    def closeEvent(self, event):
        if self._allow_close:
            event.accept(); return
        event.ignore(); self.close_requested.emit()


class VectorFilterDialog(QDialog):
    """2단 원본 정리 캐시와 3단 프로그램 최적화 캐시를 분리해서 관리한다."""

    RAW_CATEGORY_LABELS = {"layer": "레이어", "block": "블록"}
    GROUPS = (
        ("LINE", "선"),
        ("WINDOW", "창호"),
        ("BLOCK", "블록"),
        ("DIMENSION", "치수"),
        ("TEXT", "문자"),
        ("HATCH", "해치"),
    )

    def __init__(self, catalog: dict, selection: dict | None = None,
                 program_cache: dict | None = None, parent=None,
                 raw_apply_callback=None, build_callback=None, optimized_apply_callback=None,
                 selection_state_callback=None, layer_apply_callback=None, direct_select_callback=None, group_edit_callback=None):
        super().__init__(parent)
        self.setWindowTitle("도면 정리")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(560, 720)
        self.setMinimumSize(520, 560)
        self.catalog = dict(catalog or {})
        self._selection = dict(selection or {})
        self._program_cache = dict(program_cache or {})
        self._raw_apply_callback = raw_apply_callback if callable(raw_apply_callback) else None
        self._build_callback = build_callback if callable(build_callback) else None
        self._optimized_apply_callback = optimized_apply_callback if callable(optimized_apply_callback) else None
        self._selection_state_callback = selection_state_callback if callable(selection_state_callback) else None
        self._layer_apply_callback = layer_apply_callback if callable(layer_apply_callback) else None
        self._direct_select_callback = direct_select_callback if callable(direct_select_callback) else None
        self._group_edit_callback = group_edit_callback if callable(group_edit_callback) else None
        self._bulk_raw_update = False
        self._bulk_group_update = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self._build_raw_tab()
        self._build_optimized_tab()

        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        self.realtime_check = QCheckBox("실시간 반영")
        self.realtime_check.setChecked(False)
        self.realtime_check.setToolTip(
            "기본 OFF · 켜면 현재 도면정리 탭의 표시 체크/투명도 변경을 즉시 도면에 반영합니다. "
            "레이어 탭과 최적화 도면 탭이 이 설정을 공통으로 사용합니다."
        )
        self.apply_btn = QPushButton("적용")
        self.apply_btn.setToolTip("현재 보고 있는 도면정리 탭의 설정을 도면에 한 번 적용합니다.")
        self.apply_btn.clicked.connect(self._apply_current_tab)
        button_height = max(int(getattr(self, "_raw_bulk_button_height", 0) or 0), self.apply_btn.sizeHint().height(), 24)
        self.realtime_check.setFixedHeight(button_height)
        bottom.addWidget(self.realtime_check)
        bottom.addWidget(self.apply_btn)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.close)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

    def _build_raw_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        guide = QLabel(
            "CAD/XREF에서 XCLIP 처리를 끝낸 2단 원본 캐시입니다. "
            "레이어/블록 표시 체크는 원본/최적화 도면의 표시 필터와 다음 최적화 생성 대상에 함께 사용합니다. "
            "기본은 체크만 바꾸고 창 하단 [적용] 때 반영하며, [실시간 반영]을 켜면 즉시 반영합니다. "
            "[원도면 적용]은 필터/최적화를 취소한 것처럼 2단 원본 캐시 전체를 현재 도곽에 그대로 표시합니다."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)
        if not bool(self.catalog.get("metadata_ready", False)):
            warn = QLabel(
                "※ 현재 원본 캐시는 레이어/블록 메타정보가 부족한 이전 캐시입니다. "
                "CAD 배경 재생성을 한 번 실행하면 실제 레이어/블록 목록으로 다시 만들어집니다."
            )
            warn.setWordWrap(True)
            layout.addWidget(warn)

        title_row = QHBoxLayout()
        title = QLabel("레이어 설정")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(QLabel("항목명 검색"))
        self.raw_search = QLineEdit()
        self.raw_search.setPlaceholderText("WALL, *주차*, A-?DOOR")
        self.raw_search.setClearButtonEnabled(True)
        self.raw_search.textChanged.connect(self._apply_raw_filters)
        search_row.addWidget(self.raw_search, 3)
        search_row.addWidget(QLabel("색상 검색"))
        self.raw_color_search = QLineEdit()
        self.raw_color_search.setPlaceholderText("1,3,7 / 2*")
        self.raw_color_search.setMaximumWidth(125)
        self.raw_color_search.setClearButtonEnabled(True)
        self.raw_color_search.textChanged.connect(self._apply_raw_filters)
        search_row.addWidget(self.raw_color_search, 1)
        layout.addLayout(search_row)

        bulk = QHBoxLayout()
        bulk_buttons = []
        for category, label in (("layer", "레이어"), ("block", "블록")):
            on_btn = QPushButton(f"{label} 전체 선택")
            off_btn = QPushButton(f"{label} 전체 해제")
            on_btn.clicked.connect(lambda _=False, c=category: self._set_raw_category(c, True))
            off_btn.clicked.connect(lambda _=False, c=category: self._set_raw_category(c, False))
            bulk.addWidget(on_btn)
            bulk.addWidget(off_btn)
            bulk_buttons.extend((on_btn, off_btn))
        bulk.addStretch(1)
        layout.addLayout(bulk)
        self._raw_bulk_button_height = max([btn.sizeHint().height() for btn in bulk_buttons] or [24])

        rows = []
        details = dict(self.catalog.get("details") or {})
        for category in ("layer", "block"):
            values = dict(self.catalog.get(category) or {})
            category_details = dict(details.get(category) or {})
            for name in sorted(values, key=lambda v: str(v).casefold()):
                detail = dict(category_details.get(str(name)) or {})
                rows.append((category, str(name), int(values.get(name, 0) or 0), detail))

        self.raw_table = _SpaceToggleTableWidget(len(rows), 6, check_column=0)
        self.raw_table.setHorizontalHeaderLabels(["표시", "구분", "색상", "항목명", "최적화", "객체수"])
        self.raw_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.raw_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.raw_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.raw_table.verticalHeader().setVisible(False)
        self.raw_table.setItemDelegateForColumn(0, _BlackCheckDelegate(self.raw_table))
        header = self.raw_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        # 색상은 내용 폭에 맞추고, 항목명은 이전 폭의 절반 수준으로 시작한다.
        self.raw_table.setColumnWidth(3, 130)
        selected_map = {
            "layer": set(self._selection.get("layers") or []),
            "block": set(self._selection.get("blocks") or []),
        }
        has_saved = any(key in self._selection for key in ("layers", "blocks"))
        for row, (category, name, count, detail) in enumerate(rows):
            check = _CheckSortableItem("")
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if (not has_saved or name in selected_map[category]) else Qt.Unchecked)
            check.setData(Qt.UserRole, category)
            check.setData(Qt.UserRole + 1, name)
            check.setTextAlignment(Qt.AlignCenter)
            self.raw_table.setItem(row, 0, check)

            cat_item = QTableWidgetItem(self.RAW_CATEGORY_LABELS.get(category, category))
            cat_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.raw_table.setItem(row, 1, cat_item)

            color_item = self._make_color_item(detail)
            self.raw_table.setItem(row, 2, color_item)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.raw_table.setItem(row, 3, name_item)

            optimized_item = QTableWidgetItem("")
            optimized_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            optimized_item.setTextAlignment(Qt.AlignCenter)
            optimized_item.setData(Qt.UserRole, category)
            optimized_item.setData(Qt.UserRole + 1, name)
            self.raw_table.setItem(row, 4, optimized_item)

            count_item = _SortableTableItem(f"{count:,}")
            count_item.setData(_SortableTableItem.SORT_ROLE, int(count))
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            count_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.raw_table.setItem(row, 5, count_item)

        self._refresh_raw_optimized_marks()
        self.raw_table.itemChanged.connect(self._on_raw_item_changed)
        self.raw_table.check_toggle_finished.connect(self._on_raw_space_toggle)
        self.raw_table.setSortingEnabled(True)
        layout.addWidget(self.raw_table, 1)

        filter_bottom = QHBoxLayout()
        self.optimized_only_check = QCheckBox("최적화 도면")
        self.optimized_only_check.setChecked(False)
        self.optimized_only_check.setMinimumHeight(self._raw_bulk_button_height)
        self.optimized_only_check.setToolTip("현재 최적화 도면 생성에 실제 사용된 레이어/블록만 목록에 표시합니다.")
        self.optimized_only_check.toggled.connect(self._apply_raw_filters)
        filter_bottom.addWidget(self.optimized_only_check)
        filter_bottom.addStretch(1)
        layout.addLayout(filter_bottom)

        actions = QHBoxLayout()
        direct_btn = QPushButton("도면 직접선택")
        direct_btn.setToolTip("도면에서 객체를 직접 골라 해당 레이어/블록을 선택하고 표시를 끕니다.")
        direct_btn.clicked.connect(self._start_direct_select)
        actions.addWidget(direct_btn)
        actions.addStretch(1)
        build_btn = QPushButton("최적화 도면 생성")
        build_btn.setDefault(True)
        build_btn.setToolTip("현재 레이어/블록 표시 체크 상태로 3단 프로그램 최적화 캐시를 새로 생성합니다.")
        raw_apply = QPushButton("원도면 적용")
        raw_apply.setToolTip("필터와 최적화를 적용하지 않은 2단 원본 캐시 전체를 현재 도곽에 표시합니다. 기존 최적화 캐시는 삭제하지 않습니다.")
        build_btn.clicked.connect(self._build_optimized)
        raw_apply.clicked.connect(self._apply_original_view)
        actions.addWidget(build_btn)
        actions.addWidget(raw_apply)
        layout.addLayout(actions)
        self.tabs.addTab(tab, "레이어")

    def _make_color_item(self, detail: dict):
        detail = dict(detail or {})
        rgb = detail.get("rgb")
        try:
            aci = int(detail.get("aci", -1))
        except Exception:
            aci = -1
        # 표에는 "ACI"/"다색" 같은 설명을 붙이지 않고 CAD 색상번호만 표시한다.
        # 여러 색이 섞인 레이어/블록은 집계상 가장 많이 사용된 대표 색상번호를 사용한다.
        label = str(aci) if aci >= 0 else "-"
        item = _SortableTableItem(label)
        item.setData(Qt.UserRole, label)
        item.setData(_SortableTableItem.SORT_ROLE, aci if aci >= 0 else 9999)
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
            try:
                r, g, b = [max(0, min(255, int(v))) for v in rgb[:3]]
                color = QColor(r, g, b)
                item.setBackground(color)
                luminance = (r * 299 + g * 587 + b * 114) / 1000.0
                item.setForeground(QColor(0, 0, 0) if luminance >= 145 else QColor(255, 255, 255))
            except Exception:
                pass
        return item

    @staticmethod
    def _matches_text_filter(value: str, pattern: str) -> bool:
        value = str(value or "").casefold()
        pattern = str(pattern or "").strip().casefold()
        if not pattern:
            return True
        if any(ch in pattern for ch in "*?"):
            return fnmatch.fnmatchcase(value, pattern)
        return pattern in value

    @staticmethod
    def _matches_color_filter(value: str, pattern: str) -> bool:
        value = str(value or "").strip().casefold()
        pattern = str(pattern or "").strip().casefold()
        if not pattern:
            return True
        tokens = [token.strip() for token in pattern.split(",") if token.strip()]
        if not tokens:
            return True
        for token in tokens:
            if any(ch in token for ch in "*?"):
                if fnmatch.fnmatchcase(value, token):
                    return True
            elif value == token:
                return True
        return False

    def _apply_raw_filters(self, *_args):
        name_pattern = str(self.raw_search.text() if hasattr(self, "raw_search") else "")
        color_pattern = str(self.raw_color_search.text() if hasattr(self, "raw_color_search") else "")
        optimized_only = bool(self.optimized_only_check.isChecked()) if hasattr(self, "optimized_only_check") else False
        for row in range(self.raw_table.rowCount()):
            check = self.raw_table.item(row, 0)
            name = str(check.data(Qt.UserRole + 1) or "") if check is not None else ""
            category = str(check.data(Qt.UserRole) or "") if check is not None else ""
            color_item = self.raw_table.item(row, 2)
            color_value = str(color_item.data(Qt.UserRole) if color_item is not None else "")
            optimized_match = True
            if optimized_only:
                optimized_match, _used_count = self._optimized_item_used(category, name)
            visible = (
                self._matches_text_filter(name, name_pattern)
                and self._matches_color_filter(color_value, color_pattern)
                and optimized_match
            )
            self.raw_table.setRowHidden(row, not visible)

    def _optimized_item_used(self, category: str, name: str) -> tuple[bool, int | None]:
        stats = dict(self._program_cache.get("stats") or {})
        stat_key = "optimized_layer_counts" if category == "layer" else "optimized_block_counts"
        counts = dict(stats.get(stat_key) or {})
        if counts:
            count = int(counts.get(name, 0) or 0)
            return count > 0, count
        selection = dict(self._program_cache.get("selection") or {})
        key = "layers" if category == "layer" else "blocks"
        if key not in selection:
            return False, None
        return name in set(selection.get(key) or []), None

    def _refresh_raw_optimized_marks(self):
        if not hasattr(self, "raw_table"):
            return
        sorting = bool(self.raw_table.isSortingEnabled())
        if sorting:
            self.raw_table.setSortingEnabled(False)
        has_program = bool(self._program_cache.get("floors"))
        for row in range(self.raw_table.rowCount()):
            marker = self.raw_table.item(row, 4)
            check = self.raw_table.item(row, 0)
            if marker is None or check is None:
                continue
            category = str(check.data(Qt.UserRole) or "")
            name = str(check.data(Qt.UserRole + 1) or "")
            if not has_program:
                marker.setText("미생성")
                marker.setForeground(QColor(110, 110, 110))
                marker.setToolTip("아직 최적화 도면이 생성되지 않았습니다.")
                continue
            used, used_count = self._optimized_item_used(category, name)
            marker.setText("사용" if used else "미사용")
            marker.setForeground(QColor(0, 105, 50) if used else QColor(145, 60, 40))
            marker.setToolTip(
                (f"최적화 도면 생성에 사용됨 · 원본 객체 {used_count:,}개" if used_count is not None and used else
                 "최적화 도면 생성에 사용됨" if used else
                 "현재 최적화 도면에는 사용되지 않음")
            )
        if sorting:
            self.raw_table.setSortingEnabled(True)
        self._apply_raw_filters()

    def _apply_original_view(self):
        if self._raw_apply_callback is None:
            return
        try:
            # 레이어/블록 체크는 최적화 생성용 초안으로만 저장하고, 원도면 적용은 2단 원본 전체를 표시한다.
            self._save_selection_draft()
            self._raw_apply_callback()
            self.status_label.setText("2단 원도면 캐시 전체를 표시했습니다. 최적화 캐시는 삭제하지 않습니다.")
        except Exception as exc:
            self.status_label.setText(f"원도면 적용 실패: {type(exc).__name__}: {exc}")

    def _build_optimized_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        guide = QLabel(
            "프로그램용 최적화 캐시를 선 / 창호 / 블록 / 치수 / 문자 / 해치로 나눠 표시합니다. "
            "선 계열은 분기점에서 끊어 새 POLYLINE으로 재구성하고, 중복/겹침선을 정리합니다. "
            "표시 여부와 투명도 가중치는 기본적으로 [적용]을 눌렀을 때 반영됩니다. "
            "각 그룹은 먼저 불투명 레이어로 합성한 뒤 투명도를 한 번만 적용해 그룹 내부 교차부가 더 진해지는 현상을 줄입니다. "
            "가중치는 표시설정의 CAD 배경 투명도 값에 더합니다. 예: 전체 50% + 문자 +10% = 문자 60%."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)

        self.group_table = _SpaceToggleTableWidget(len(self.GROUPS), 4, check_column=0)
        self.group_table.setHorizontalHeaderLabels(["표시", "그룹", "투명도 가중치", "도형수"])
        self.group_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.group_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.group_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.group_table.verticalHeader().setVisible(False)
        self.group_table.setItemDelegateForColumn(0, _BlackCheckDelegate(self.group_table))
        self.group_table.setItemDelegateForColumn(2, _PercentSpinDelegate(self.group_table))
        group_header = self.group_table.horizontalHeader()
        for column in range(4):
            group_header.setSectionResizeMode(column, QHeaderView.Interactive)
        group_header.setStretchLastSection(False)
        self._group_opacity_items = {}
        self._populate_group_rows()

        # 전체 표시/숨김은 목록 위로 이동한다.
        group_top_actions = QHBoxLayout()
        all_on = QPushButton("그룹 전체 표시")
        all_off = QPushButton("그룹 전체 숨김")
        all_on.clicked.connect(lambda: self._set_all_groups(True))
        all_off.clicked.connect(lambda: self._set_all_groups(False))
        group_top_actions.addWidget(all_on)
        group_top_actions.addWidget(all_off)
        group_top_actions.addStretch(1)
        layout.addLayout(group_top_actions)
        layout.addWidget(self.group_table, 1)

        actions = QHBoxLayout()
        group_edit_btn = QPushButton("그룹 편집")
        group_edit_btn.setToolTip("편집할 최적화 그룹만 도면에 표시하고 객체 단위로 다른 그룹/레이어로 이동합니다.")
        group_edit_btn.clicked.connect(self._start_group_edit)
        actions.addWidget(group_edit_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.group_table.itemChanged.connect(self._on_group_item_changed)
        self.group_table.check_toggle_finished.connect(self._on_group_space_toggle)
        self.tabs.addTab(tab, "최적화 도면")
        # 숨겨진 탭 상태에서 계산한 폭은 실제 표시 폭과 다를 수 있으므로
        # 탭 전환 직후에도 현재 viewport 기준으로 다시 맞춘다.
        self.tabs.currentChanged.connect(lambda _index: QTimer.singleShot(0, self._resize_group_columns))
        QTimer.singleShot(0, self._resize_group_columns)

    def _start_group_edit(self):
        if self._group_edit_callback is None:
            return
        if not bool(self._program_cache.get("floors")):
            QMessageBox.information(self, "그룹 편집", "먼저 [최적화 도면 생성]을 실행해 주세요.")
            return
        try:
            self._group_edit_callback(self)
        except Exception as exc:
            QMessageBox.warning(self, "그룹 편집", f"그룹 편집 시작 실패: {type(exc).__name__}: {exc}")

    def _resize_group_columns(self):
        """현재 보이는 테이블 내부폭을 기준으로 10 / 50 / 20 / 20 비율을 정확히 맞춘다."""
        if not hasattr(self, "group_table"):
            return
        table = self.group_table
        viewport = table.viewport()
        if viewport is None:
            return
        # 실제 데이터 영역 폭을 사용하고, 프레임/반올림 때문에 수평 스크롤이
        # 생기지 않도록 2px 여유를 둔다. 숨겨진 탭에서 폭이 비정상적으로
        # 작게 잡히면 다음 tab/show/resize 이벤트에서 다시 계산한다.
        width = int(viewport.contentsRect().width()) - 2
        if width <= 0:
            return
        w0 = int(width * 0.10)
        w1 = int(width * 0.50)
        w2 = int(width * 0.20)
        w3 = max(0, width - w0 - w1 - w2)
        for column, section in enumerate((w0, w1, w2, w3)):
            table.setColumnWidth(column, section)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._resize_group_columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._resize_group_columns)

    def _populate_group_rows(self):
        settings = dict(self._program_cache.get("group_settings") or {})
        counts = dict((self._program_cache.get("stats") or {}).get("group_counts") or {})
        has_program = bool(self._program_cache.get("floors"))
        for row, (key, label) in enumerate(self.GROUPS):
            cfg = dict(settings.get(key) or {})
            check = _CheckSortableItem("")
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if bool(cfg.get("visible", True)) else Qt.Unchecked)
            check.setData(Qt.UserRole, key)
            check.setTextAlignment(Qt.AlignCenter)
            self.group_table.setItem(row, 0, check)
            name_item = QTableWidgetItem(label)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.group_table.setItem(row, 1, name_item)
            opacity_item = _SortableTableItem()
            opacity_item.setTextAlignment(Qt.AlignCenter)
            opacity_item.setData(Qt.EditRole, int(cfg.get("opacity_offset", 0) or 0))
            opacity_item.setData(_SortableTableItem.SORT_ROLE, int(cfg.get("opacity_offset", 0) or 0))
            opacity_item.setFlags((Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable) if has_program else Qt.ItemIsSelectable)
            self._group_opacity_items[key] = opacity_item
            self.group_table.setItem(row, 2, opacity_item)
            count_item = QTableWidgetItem(f"{int(counts.get(key, 0) or 0):,}" if has_program else "미생성")
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            count_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.group_table.setItem(row, 3, count_item)
            if not has_program:
                check.setFlags(Qt.ItemIsSelectable)

    def _refresh_program_state(self, program_cache: dict | None):
        self._program_cache = dict(program_cache or {})
        settings = dict(self._program_cache.get("group_settings") or {})
        counts = dict((self._program_cache.get("stats") or {}).get("group_counts") or {})
        has_program = bool(self._program_cache.get("floors"))
        for row, (key, _label) in enumerate(self.GROUPS):
            cfg = dict(settings.get(key) or {})
            check = self.group_table.item(row, 0)
            check.setFlags(
                (Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                if has_program else Qt.ItemIsSelectable
            )
            check.setCheckState(Qt.Checked if bool(cfg.get("visible", True)) else Qt.Unchecked)
            opacity_item = self._group_opacity_items.get(key)
            if opacity_item is not None:
                value = int(cfg.get("opacity_offset", 0) or 0)
                opacity_item.setFlags((Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable) if has_program else Qt.ItemIsSelectable)
                opacity_item.setData(Qt.EditRole, value)
                opacity_item.setData(_SortableTableItem.SORT_ROLE, value)
            count_item = self.group_table.item(row, 3)
            if count_item is not None:
                count_item.setText(f"{int(counts.get(key, 0) or 0):,}" if has_program else "미생성")
        self._refresh_raw_optimized_marks()
        if has_program:
            self.tabs.setCurrentIndex(1)

    def _set_raw_category(self, category: str, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        self._bulk_raw_update = True
        try:
            for row in range(self.raw_table.rowCount()):
                item = self.raw_table.item(row, 0)
                if item is not None and str(item.data(Qt.UserRole) or "") == category:
                    item.setCheckState(state)
        finally:
            self._bulk_raw_update = False
        self._save_selection_draft()
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_layer_view()

    def _on_raw_item_changed(self, item):
        if self._bulk_raw_update or item is None or item.column() != 0:
            return
        self._save_selection_draft()
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_layer_view()

    def _on_raw_space_toggle(self):
        self._save_selection_draft()
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_layer_view()

    def _apply_current_tab(self):
        """창 하단 공통 [적용] 버튼: 현재 선택된 도면정리 탭의 설정을 적용한다."""
        if hasattr(self, "tabs") and self.tabs.currentIndex() == 1:
            self._apply_optimized_view()
        else:
            self._apply_layer_view()

    def _apply_layer_view(self):
        if self._layer_apply_callback is None:
            return
        try:
            selection = self.selection_values()
            self._save_selection_draft()
            result = self._layer_apply_callback(selection)
            if isinstance(result, dict) and result.get("program_cache"):
                self._program_cache = dict(result.get("program_cache") or self._program_cache)
            mode = str((result or {}).get("mode", "") if isinstance(result, dict) else "")
            if mode == "optimized":
                self.status_label.setText("레이어 표시 적용 완료 · 현재 최적화 도면에 표시 필터만 적용했습니다.")
            elif mode == "raw":
                self.status_label.setText("레이어 표시 적용 완료 · 2단 원본 캐시 미리보기에 적용했습니다.")
            else:
                self.status_label.setText("레이어 표시 적용 완료")
        except Exception as exc:
            self.status_label.setText(f"레이어 적용 실패: {type(exc).__name__}: {exc}")

    def _set_all_groups(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        has_program = bool(self._program_cache.get("floors"))
        if not has_program:
            return
        self._bulk_group_update = True
        try:
            for row in range(self.group_table.rowCount()):
                item = self.group_table.item(row, 0)
                if item is not None:
                    # 그룹 사이에는 배타/최소 1개 유지 규칙이 없다. 5개 전부 숨김도 허용한다.
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    item.setCheckState(state)
        finally:
            self._bulk_group_update = False
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_optimized_view()

    def _on_group_item_changed(self, item):
        if self._bulk_group_update or item is None:
            return
        if item.column() == 2:
            try:
                item.setData(_SortableTableItem.SORT_ROLE, int(item.data(Qt.EditRole) or 0))
            except Exception:
                item.setData(Qt.EditRole, 0)
                item.setData(_SortableTableItem.SORT_ROLE, 0)
        elif item.column() != 0:
            return
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_optimized_view()

    def _on_group_space_toggle(self):
        if hasattr(self, "realtime_check") and self.realtime_check.isChecked():
            self._apply_optimized_view()

    def _start_direct_select(self):
        if self._direct_select_callback is None:
            return
        self._save_selection_draft()
        self._direct_select_callback(self.selection_values(), self, self.catalog)

    def selection_values(self) -> dict:
        result = {"layers": [], "blocks": []}
        key_map = {"layer": "layers", "block": "blocks"}
        for row in range(self.raw_table.rowCount()):
            item = self.raw_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            category = str(item.data(Qt.UserRole) or "")
            name = str(item.data(Qt.UserRole + 1) or "")
            if category in key_map:
                result[key_map[category]].append(name)
        return result

    def group_settings_values(self) -> dict:
        result = {}
        for row, (key, _label) in enumerate(self.GROUPS):
            item = self.group_table.item(row, 0)
            opacity_item = self._group_opacity_items.get(key)
            try:
                opacity_value = int(opacity_item.data(Qt.EditRole) if opacity_item is not None else 0)
            except Exception:
                opacity_value = 0
            result[key] = {
                "visible": bool(item is not None and item.checkState() == Qt.Checked),
                "opacity_offset": opacity_value,
            }
        return result

    def _save_selection_draft(self):
        if self._selection_state_callback is None:
            return
        try:
            self._selection_state_callback(self.selection_values())
        except Exception:
            pass

    def closeEvent(self, event):
        # 체크만 바꾸고 창을 닫아도 다음에 같은 상태로 다시 연다. 화면 재생성은 하지 않는다.
        self._save_selection_draft()
        super().closeEvent(event)

    def _build_optimized(self):
        if self._build_callback is None:
            return
        try:
            selection = self.selection_values()
            self._save_selection_draft()
            result = self._build_callback(selection)
            if result:
                self._refresh_program_state(result)
                self.status_label.setText("최적화 도면을 생성하고 도곽에 반영했습니다.")
        except Exception as exc:
            self.status_label.setText(f"최적화 도면 생성 실패: {type(exc).__name__}: {exc}")

    def _apply_optimized_view(self):
        if self._optimized_apply_callback is None:
            return
        try:
            settings = self.group_settings_values()
            result = self._optimized_apply_callback(settings)
            if result:
                self._program_cache = dict(result)
            visible_count = sum(1 for value in settings.values() if bool(value.get("visible", False)))
            self.status_label.setText(
                f"최적화 도면 적용 완료 · 표시 그룹 {visible_count}/{len(self.GROUPS)}"
                + (" · 모든 CAD 벡터 그룹 숨김" if visible_count == 0 else "")
            )
        except Exception as exc:
            self.status_label.setText(f"최적화 도면 적용 실패: {type(exc).__name__}: {exc}")


class DrawingDivisionAssignmentDialog(QDialog):
    """도곽 전체를 좌→우/상→하 순으로 보여주고 도서구분과 층/방향/번호를 지정한다."""

    TYPES = ["", "개요", "평면도", "입면도", "단면도", "창호도", "재료마감표"]

    def __init__(self, frames: list[dict], parent=None, highlight_callback=None, clear_highlight_callback=None, focus_callback=None, write_titles_callback=None):
        super().__init__(parent)
        self.setWindowTitle("도곽 도서구분 / 층 지정")
        self.resize(820, min(760, 210 + max(5, len(frames)) * 32))
        self.setMinimumSize(700, 420)
        self.frames = sorted([dict(frame) for frame in frames], key=self._frame_sort_key)
        self._updating = False
        self._highlight_callback = highlight_callback if callable(highlight_callback) else None
        self._clear_highlight_callback = clear_highlight_callback if callable(clear_highlight_callback) else None
        self._focus_callback = focus_callback if callable(focus_callback) else None
        self._write_titles_callback = write_titles_callback if callable(write_titles_callback) else None
        self._last_highlight_row = -1
        root = QVBoxLayout(self)
        hint = QLabel(
            "도곽 내부 기존 내용은 자동으로 채웁니다. 도서구분/도곽명 입력 편집을 확정하면 적용 도곽 명칭이 갱신됩니다.\n"
            "평면도·창호평면도 공란은 위 층 흐름에서 자동 추정합니다."
        )
        root.addWidget(hint)

        self.table = QTableWidget(len(self.frames), 4)
        self.table.setHorizontalHeaderLabels(["연번", "도서구분", "도곽명 입력", "적용 도곽 명칭"])
        self.table.setStyleSheet(
            "QTableWidget::item:selected { background:#BFEAFF; color:#000000; }"
            "QTableWidget::item:selected:active { background:#BFEAFF; color:#000000; }"
            "QLineEdit { selection-background-color:#BFEAFF; selection-color:#000000; }"
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 210)
        self.table.setColumnWidth(3, 330)

        for row, frame in enumerate(self.frames):
            no_item = QTableWidgetItem(str(row + 1))
            no_item.setFlags(no_item.flags() & ~Qt.ItemIsEditable)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, no_item)

            combo = QComboBox()
            # 평면탭 표 셀 콤보와 동일하게 셀 격자만 보이고 콤보 자체의 두꺼운 사각 테두리는 숨긴다.
            combo.setFrame(False)
            combo.setStyleSheet(
                "QComboBox { border:none; background:transparent; padding:0 16px 0 3px; }"
                "QComboBox::drop-down { border:none; background:transparent; width:16px; }"
                "QAbstractItemView { border:1px solid #A0A0A0; background:#FFFFFF; selection-background-color:#BFEAFF; selection-color:#000000; outline:0; }"
                "QAbstractItemView::item { padding:2px 4px; min-height:20px; }"
            )
            combo.addItems(self.TYPES)
            dtype = str(frame.get("drawing_type", "") or "").strip()
            if dtype == "미구분":
                dtype = ""
            if dtype and combo.findText(dtype) < 0:
                combo.addItem(dtype)
            combo.setCurrentText(dtype if combo.findText(dtype) >= 0 else "")
            combo.currentTextChanged.connect(self._refresh_outputs)
            self.table.setCellWidget(row, 1, combo)

            assigned_input = str(frame.get("assigned_input", "") or "").strip()
            user_input = assigned_input or drawing_input_from_title(dtype, frame.get("title", ""), frame.get("floors") or [])
            input_item = QTableWidgetItem(user_input)
            self.table.setItem(row, 2, input_item)

            output_item = QTableWidgetItem("")
            output_item.setFlags(output_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 3, output_item)

        self.table.itemChanged.connect(self._on_item_changed)
        # CAD 연동 중일 때만 행 이동/마우스 진입 시 해당 도곽 Handle을 짧게 강조한다.
        # 같은 행 안에서 마우스가 움직일 때는 COM을 다시 호출하지 않는다.
        self.table.currentCellChanged.connect(lambda row, _col, _pr, _pc: self._highlight_row(row))
        self.table.cellEntered.connect(lambda row, _col: self._highlight_row(row))
        root.addWidget(self.table, 1)

        help_label = QLabel(
            "평면도, 창호평면도 : 층수  /  입면도 : 방향  /  개요 : 미입력  /  단면도 : 번호"
        )
        help_label.setStyleSheet("color:#555555;")
        root.addWidget(help_label)

        action_row = QHBoxLayout()
        self.focus_frame_btn = QPushButton("도곽 위치 확인")
        self.focus_frame_btn.setEnabled(self._focus_callback is not None)
        self.focus_frame_btn.setToolTip("현재 행의 도곽 위치로 연동 CAD 화면을 이동합니다.")
        self.focus_frame_btn.clicked.connect(self._focus_current_frame)
        action_row.addWidget(self.focus_frame_btn)

        self.write_titles_btn = QPushButton("도곽명 기입")
        self.write_titles_btn.setEnabled(self._write_titles_callback is not None)
        self.write_titles_btn.setToolTip("적용 도곽 명칭이 있는 모든 행을 도면구분 레이어의 도곽 좌상단 안쪽에 기입합니다.")
        self.write_titles_btn.clicked.connect(self._write_all_titles)
        action_row.addWidget(self.write_titles_btn)

        self.bulk_type_btn = QPushButton("선택된 항목 일괄적용")
        self.bulk_type_btn.setToolTip("Ctrl/Shift로 여러 행을 선택한 뒤 도서구분을 한 번에 적용합니다.")
        self.bulk_type_btn.clicked.connect(self._apply_type_to_selected_rows)
        action_row.addWidget(self.bulk_type_btn)
        action_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("확인")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._accept_now)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)

        self._refresh_outputs()

    @staticmethod
    def _frame_sort_key(frame: dict):
        points = list(frame.get("points") or [])
        if not points:
            return (0.0, 0.0)
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        # 상단 행부터, 같은 높이에서는 좌→우.
        return (-max(ys), min(xs))

    def _highlight_row(self, row: int):
        if self._highlight_callback is None:
            return
        row = int(row)
        if row < 0 or row >= len(self.frames) or row == self._last_highlight_row:
            return
        self._last_highlight_row = row
        try:
            self._highlight_callback(dict(self.frames[row]))
        except Exception:
            # 도곽 강조 실패가 목록 편집을 막으면 안 된다.
            pass

    def _focus_current_frame(self):
        if self._focus_callback is None:
            return
        row = self.table.currentRow()
        if row < 0 or row >= len(self.frames):
            QMessageBox.information(self, "도곽 위치 확인", "먼저 확인할 도곽 행을 선택하십시오.")
            return
        try:
            self._focus_callback(dict(self.frames[row]))
        except Exception as exc:
            QMessageBox.warning(self, "도곽 위치 확인", f"CAD에서 도곽 위치를 확인하지 못했습니다.\n{exc}")

    def _selected_rows(self) -> list[int]:
        """현재 표에서 선택된 셀 기준으로 중복 없이 행 번호를 반환한다."""
        rows = sorted({int(index.row()) for index in self.table.selectedIndexes()})
        if not rows:
            current = self.table.currentRow()
            if 0 <= current < self.table.rowCount():
                rows = [int(current)]
        return rows

    def _apply_type_to_selected_rows(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "도서구분 일괄적용", "적용할 항목을 선택하십시오.")
            return

        choices = [value for value in self.TYPES if str(value).strip()]
        current_type = ""
        first_combo = self.table.cellWidget(rows[0], 1)
        if isinstance(first_combo, QComboBox):
            current_type = first_combo.currentText().strip()
        initial = choices.index(current_type) if current_type in choices else 0

        selected_type, ok = QInputDialog.getItem(
            self,
            "도서구분 일괄적용",
            f"선택한 {len(rows)}개 항목에 적용할 도서구분을 선택하십시오.",
            choices,
            initial,
            False,
        )
        if not ok or not str(selected_type).strip():
            return

        # 현재 도곽명 입력값은 유지하고 도서구분만 변경한다.
        self._updating = True
        try:
            for row in rows:
                combo = self.table.cellWidget(row, 1)
                if not isinstance(combo, QComboBox):
                    continue
                if combo.findText(selected_type) < 0:
                    combo.addItem(selected_type)
                combo.setCurrentText(selected_type)
        finally:
            self._updating = False
        # 선택된 도서구분 변경을 확정한 뒤 적용 도곽 명칭을 즉시 다시 계산한다.
        self._refresh_outputs()

    def _write_all_titles(self):
        if self._write_titles_callback is None:
            return
        # 현재 확정된 입력값으로 적용 도곽 명칭을 다시 계산해서 기입한다.
        self._refresh_outputs()
        rows = [frame for frame in self.resolved_frames() if str(frame.get("title", "") or "").strip()]
        if not rows:
            QMessageBox.information(self, "도곽명 기입", "기입할 적용 도곽 명칭이 없습니다.")
            return
        try:
            result = self._write_titles_callback(rows) or {}
        except Exception as exc:
            QMessageBox.warning(self, "도곽명 기입", f"도곽명을 기입하지 못했습니다.\n{exc}")
            return
        created = int(result.get("created", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        text = f"도곽명 기입 완료 · 신규 {created}개"
        if updated:
            text += f" · 갱신 {updated}개"
        if skipped:
            text += f" · 제외 {skipped}개"
        if failed:
            text += f" · 실패 {failed}개"
        QMessageBox.information(self, "도곽명 기입", text)

    def _clear_cad_highlight(self):
        self._last_highlight_row = -1
        if self._clear_highlight_callback is None:
            return
        try:
            self._clear_highlight_callback()
        except Exception:
            pass

    def done(self, result):
        self._clear_cad_highlight()
        super().done(result)

    def _on_item_changed(self, item):
        if self._updating or item.column() != 2:
            return
        # QTableWidget 편집이 확정되는 시점(Enter 또는 다른 셀로 이동)에만 갱신한다.
        self._refresh_outputs()

    @staticmethod
    def _advance_state(effective: str, previous: str, last_numeric: str):
        value = normalize_drawing_floor_input(effective) or str(effective or "").strip().upper()
        if not value:
            return previous, last_numeric
        if value == "PIT":
            return value, last_numeric
        if value in {"RF", "PHR"}:
            return value, last_numeric
        return value, value

    def _refresh_outputs(self):
        self._updating = True
        try:
            states = {
                "평면도": {"previous": "", "last_numeric": ""},
                "창호도": {"previous": "", "last_numeric": ""},
            }
            for row in range(self.table.rowCount()):
                combo = self.table.cellWidget(row, 1)
                dtype = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
                input_item = self.table.item(row, 2)
                raw = str(input_item.text() if input_item else "").strip()
                effective = raw
                title = ""

                if dtype in {"평면도", "창호도"}:
                    state = states[dtype]
                    normalized = normalize_drawing_floor_input(raw)
                    if normalized:
                        effective = normalized
                    else:
                        inferred, _last = next_drawing_floor(state["previous"], state["last_numeric"])
                        effective = inferred
                    title = drawing_title_from_input(dtype, effective)
                    state["previous"], state["last_numeric"] = self._advance_state(
                        effective, state["previous"], state["last_numeric"]
                    )
                elif dtype == "재료마감표":
                    title = "재료마감표"
                else:
                    title = drawing_title_from_input(dtype, raw)

                output = self.table.item(row, 3)
                if output is None:
                    output = QTableWidgetItem("")
                    output.setFlags(output.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(row, 3, output)
                output.setText(title)
                output.setData(Qt.UserRole, effective)
                if dtype and not title:
                    output.setBackground(QColor("#FFF2B2"))
                else:
                    output.setBackground(QColor("transparent"))
        finally:
            self._updating = False

    def _accept_now(self):
        self._refresh_outputs()
        self.accept()

    def resolved_frames(self) -> list[dict]:
        self._refresh_outputs()
        result = []
        for row, source in enumerate(self.frames):
            frame = dict(source)
            combo = self.table.cellWidget(row, 1)
            dtype = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
            output = self.table.item(row, 3)
            title = output.text().strip() if output else ""
            effective = str(output.data(Qt.UserRole) or "").strip() if output else ""
            if not dtype:
                dtype = "미구분"
            frame["drawing_type"] = dtype
            frame["title"] = title
            frame["assigned_input"] = effective
            if title:
                _kind, floors = _classify_drawing_title(title)
            else:
                floors = []
            frame["floors"] = list(floors or []) if dtype in {"평면도", "창호도"} else []
            frame["floor"] = frame["floors"][0] if frame["floors"] else ""
            result.append(frame)
        return result


class TableKeyboardEventFilter(QObject):
    """프로그램 전체 QTableWidget의 공통 키 조작을 보정한다.

    - Space: 비편집 상태에서 편집모드 진입 금지
    - Ctrl+Space: 현재 행/셀을 기존 선택에 추가하거나 해제
    - 편집 중 Ctrl+Up/Down: 편집을 확정/종료하고 같은 열의 위/아래 행으로 이동
    """

    @staticmethod
    def _find_table(obj):
        widget = obj if isinstance(obj, QWidget) else QApplication.focusWidget()
        seen = set()
        while widget is not None and id(widget) not in seen:
            seen.add(id(widget))
            if isinstance(widget, QTableWidget):
                return widget
            try:
                widget = widget.parentWidget()
            except Exception:
                break
        focus = QApplication.focusWidget()
        seen.clear()
        widget = focus
        while widget is not None and id(widget) not in seen:
            seen.add(id(widget))
            if isinstance(widget, QTableWidget):
                return widget
            try:
                widget = widget.parentWidget()
            except Exception:
                break
        return None

    @staticmethod
    def _toggle_current_selection(table: QTableWidget):
        index = table.currentIndex()
        if not index.isValid():
            return
        sm = table.selectionModel()
        if sm is None:
            return
        sm.setCurrentIndex(index, QItemSelectionModel.NoUpdate)
        command = QItemSelectionModel.Toggle
        behavior = table.selectionBehavior()
        if behavior == QAbstractItemView.SelectRows:
            command |= QItemSelectionModel.Rows
        elif behavior == QAbstractItemView.SelectColumns:
            command |= QItemSelectionModel.Columns
        sm.select(index, command)

    @staticmethod
    def _finish_edit_and_move(table: QTableWidget, direction: int):
        row, col = table.currentRow(), table.currentColumn()
        if row < 0 or col < 0 or table.rowCount() <= 0:
            return
        target_row = max(0, min(row + int(direction), table.rowCount() - 1))
        # 에디터에서 포커스를 빼면 delegate가 현재 입력값을 모델에 확정한다.
        # 그 다음 이벤트루프에서 같은 열의 위/아래 셀로 이동한다.
        table.setFocus(Qt.OtherFocusReason)

        def move():
            if table.rowCount() <= 0 or table.columnCount() <= 0:
                return
            r = max(0, min(target_row, table.rowCount() - 1))
            c = max(0, min(col, table.columnCount() - 1))
            table.setCurrentCell(r, c)
            table.scrollToItem(table.item(r, c), QAbstractItemView.EnsureVisible) if table.item(r, c) is not None else None
            table.setFocus(Qt.OtherFocusReason)

        QTimer.singleShot(0, move)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress:
            return False
        table = self._find_table(obj)
        if table is None:
            return False

        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)

        if key == Qt.Key_Space and ctrl and table.state() != QAbstractItemView.EditingState:
            self._toggle_current_selection(table)
            return True

        if key == Qt.Key_Space and not ctrl and table.state() != QAbstractItemView.EditingState:
            # 도면정리 전용 목록은 어느 열에 포커스가 있어도 Space로 표시 체크를 토글한다.
            # 공통 필터가 일반 셀의 Space를 먼저 먹어버리면 _SpaceToggleTableWidget.event()까지
            # 이벤트가 내려가지 않으므로, 여기서 최우선으로 직접 처리한다.
            if isinstance(table, _SpaceToggleTableWidget):
                if table._toggle_selected_checks():
                    event.accept()
                    return True
            # 그 밖의 일반 테이블은 기존 동작을 유지한다.
            item = table.currentItem()
            if item is not None and bool(item.flags() & Qt.ItemIsUserCheckable):
                return False
            return True

        if ctrl and key in (Qt.Key_Up, Qt.Key_Down) and table.state() == QAbstractItemView.EditingState:
            self._finish_edit_and_move(table, -1 if key == Qt.Key_Up else 1)
            return True

        return False


class SpacedTabBar(QTabBar):
    """특정 탭 앞에만 실제 빈 공간을 확보하는 탭바."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gap_before: dict[int, int] = {}

    def set_gap_before(self, index: int, pixels: int):
        if index >= 0:
            self._gap_before[int(index)] = max(0, int(pixels))
            self.updateGeometry()
            self.update()

    def tabSizeHint(self, index: int) -> QSize:
        size = super().tabSizeHint(index)
        gap = int(self._gap_before.get(int(index), 0) or 0)
        if gap:
            size.setWidth(size.width() + gap)
        return size

    def paintEvent(self, event):
        painter = QStylePainter(self)
        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            gap = int(self._gap_before.get(index, 0) or 0)
            if gap:
                option.rect.adjust(gap, 0, 0, 0)
            painter.drawControl(QStyle.CE_TabBarTab, option)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_user_file_structure()
        self.project_manager = ProjectManager()
        self.active_project: ProjectRecord | None = None
        self._project_snapshot_loaded = False
        self.settings = AppSettings(SETTINGS_PATH)
        migrate_legacy_layer_settings(self.settings)
        self.recent_settings = AppSettings(RECENT_SETTINGS_PATH)
        self._migrate_recent_settings_once()
        self._capture_launch_bat()
        self._base_window_title = "DXF 견적프로그램 V0.24.12 - 블록 숨김상태·문자 선명도 보정"
        self.setWindowTitle(self._base_window_title)
        self.resize(
            int(self.settings.get("Window", "Width", "1500")),
            int(self.settings.get("Window", "Height", "900")),
        )
        self._table_keyboard_filter = TableKeyboardEventFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._table_keyboard_filter)
        self.setMinimumSize(1000, 650)
        if self.settings.get("Window", "Maximized", "False").lower() == "true":
            self.showMaximized()

        self.classifier = MaterialClassifier(DICT_PATH)
        self.current_path = ""
        self.applied_dxf_path = ""
        self.dxf_link_active = False
        self._restored_project_source_geometry_cache = ""
        self._vector_filter_dialog = None
        self._restored_project_program_geometry_cache = ""
        self._restored_project_cache_source_path = ""
        self.current_result = {"재료 후보": [], "보류": [], "제외": []}
        self.current_rooms = []
        self.current_room_errors = []
        self.current_drawing_divisions = []
        self.background_max_pixels = max(400, min(5000, int(self.recent_settings.get("Preview", "BackgroundMaxPixels", self.settings.get("Preview", "BackgroundMaxPixels", "1800")))))
        self.edge_length_font_size = int(self.recent_settings.get("Preview", "EdgeLengthFontSize", self.settings.get("Preview", "EdgeLengthFontSize", "45")))
        self.room_label_font_size = int(self.recent_settings.get("Preview", "RoomLabelFontSize", self.settings.get("Preview", "RoomLabelFontSize", "100")))
        self.room_number_entry_settings = {
            "digits": self.recent_settings.get("RoomNumbering", "Digits", "3"),
            "floor_pattern": self.recent_settings.get("RoomNumbering", "FloorPattern", ""),
            "separator": self.recent_settings.get("RoomNumbering", "Separator", "-"),
            "suffix": self.recent_settings.get("RoomNumbering", "Suffix", ""),
        }
        self.room_label_area_scale = float(self.recent_settings.get("Preview", "RoomLabelAreaScale", self.settings.get("Preview", "RoomLabelAreaScale", "1.0")))
        self.room_label_scale_mode = self.recent_settings.get("Preview", "RoomLabelScaleMode", self.settings.get("Preview", "RoomLabelScaleMode", "full_width"))
        self.edge_length_offset = int(self.recent_settings.get("Preview", "EdgeLengthOffset", self.settings.get("Preview", "EdgeLengthOffset", "150")))
        self.edge_length_width_ratio = int(self.recent_settings.get("Preview", "EdgeLengthWidthRatio", self.settings.get("Preview", "EdgeLengthWidthRatio", "80")))
        self.edge_length_show_unit_m = self.recent_settings.get("Preview", "EdgeLengthShowUnitM", self.settings.get("Preview", "EdgeLengthShowUnitM", "True")).lower() == "true"
        self.edge_length_visible = self.recent_settings.get("Preview", "EdgeLengthVisible", "True").lower() == "true"
        self.label_full_view_weight = float(self.recent_settings.get("Preview", "LabelFullViewWeight", self.settings.get("Preview", "LabelFullViewWeight", "0.70")))
        self.label_selected_view_weight = float(self.recent_settings.get("Preview", "LabelSelectedViewWeight", self.settings.get("Preview", "LabelSelectedViewWeight", "1.00")))
        self.background_style = self.recent_settings.get("Preview", "BackgroundStyle", self.settings.get("Preview", "BackgroundStyle", "gray"))
        self.background_render_mode = self.recent_settings.get("Preview", "BackgroundRenderMode", self.settings.get("Preview", "BackgroundRenderMode", "png")).strip().lower()
        if self.background_render_mode not in {"png", "vector"}:
            self.background_render_mode = "png"
        self.cad_background_mode = self.recent_settings.get("Preview", "CADBackgroundMode", "black")
        if self.cad_background_mode not in {"none", "black", "white"}:
            self.cad_background_mode = "black"
        self.background_line_width = float(self.recent_settings.get("Preview", "BackgroundDefaultLineWidth", self.settings.get("Preview", "BackgroundDefaultLineWidth", "1.5")))
        self.background_zoomout_width_weight = float(self.recent_settings.get("Preview", "BackgroundZoomoutWidthWeight", self.settings.get("Preview", "BackgroundZoomoutWidthWeight", "0.25")))
        self.background_zoomout_width_weight = max(0.0, min(1.0, self.background_zoomout_width_weight))
        self.background_aci_widths = self.recent_settings.get("Preview", "BackgroundACIWidths", self.settings.get("Preview", "BackgroundACIWidths", "0:0.03;1:0;2:0.13;3:0.13;4:0.09;5:0.03;6:0.03;7:0.09;8:0;9:0;255:0.03"))
        self.background_keep_red = self.recent_settings.get("Preview", "BackgroundKeepRed", self.settings.get("Preview", "BackgroundKeepRed", "False")).lower() == "true"
        self.background_opacity = int(self.recent_settings.get("Preview", "BackgroundOpacity", self.settings.get("Preview", "BackgroundOpacity", "100")))
        self.boundary_color = self.recent_settings.get("Preview", "BoundaryColor", self.settings.get("Preview", "BoundaryColor", "#0066FF"))
        self.boundary_width = int(self.recent_settings.get("Preview", "BoundaryWidth", self.settings.get("Preview", "BoundaryWidth", "9")))
        self.deduction_color = self.recent_settings.get("Preview", "DeductionColor", self.settings.get("Preview", "DeductionColor", "#FF00FF"))
        self.deduction_width = int(self.recent_settings.get("Preview", "DeductionWidth", self.settings.get("Preview", "DeductionWidth", "1")))
        self.render_quality = self.recent_settings.get("Preview", "RenderQuality", self.settings.get("Preview", "RenderQuality", "high"))
        self.selection_color = self.recent_settings.get("Preview", "SelectionColor", self.settings.get("Preview", "SelectionColor", "#FF0000"))
        self.selection_width = int(self.recent_settings.get("Preview", "SelectionWidth", self.settings.get("Preview", "SelectionWidth", "18")))
        self.wall_selection_color = self.recent_settings.get("Preview", "WallSelectionColor", self.settings.get("Preview", "WallSelectionColor", "#FF00FF"))
        self.wall_selection_width = int(self.recent_settings.get("Preview", "WallSelectionWidth", self.settings.get("Preview", "WallSelectionWidth", "2")))
        self.opening_line_color = self.recent_settings.get("Preview", "OpeningLineColor", self.settings.get("Preview", "OpeningLineColor", "#8ED4E3"))
        self.opening_line_width = int(self.recent_settings.get("Preview", "OpeningLineWidth", self.settings.get("Preview", "OpeningLineWidth", "4")))
        self.wall_selection_zoom_scale = float(self.recent_settings.get("Preview", "WallSelectionZoomScale", self.settings.get("Preview", "WallSelectionZoomScale", "0.35")))
        self.group_overlay_opacity = int(self.recent_settings.get("Preview", "GroupOverlayOpacity", self.settings.get("Preview", "GroupOverlayOpacity", "35")))
        self.room_rules = {
            **{
                key: self.settings.get("CAD", spec["ini_name"], default_layer_name(key))
                for key, spec in LAYER_DEFINITIONS.items()
            },
            **{
                f"{key}_color": self.settings.get("CAD", spec["ini_color"], default_layer_color(key))
                for key, spec in LAYER_DEFINITIONS.items()
            },
            "block_name": self.settings.get("CAD", "BlockName", "DXF_ROOM_TAG"),
            "floor_tag": self.settings.get("CAD", "FloorTag", "FLOOR"),
            "room_name_tag": self.settings.get("CAD", "RoomNameTag", "ROOM_NAME"),
            "room_number_tags": self.settings.get("CAD", "RoomNumberTags", "NUMBER,ROOM_NO"),
            "background_exclude_patterns": self.settings.get("CAD", "BackgroundExcludePatterns", "■*"),
        }
        self.room_category_rules = normalize_room_category_rules({
            category: self.settings.get(
                "RoomCategories",
                ROOM_CATEGORY_INI_KEYS[category],
                "\n".join(DEFAULT_ROOM_CATEGORY_KEYWORDS[category]),
            )
            for category in ROOM_CATEGORIES
        })
        self.finish_formula_codes = normalize_formula_codes(
            self.settings.get("FinishCalculation", "FormulaCodes", serialize_formula_codes())
        )
        self.finish_categories = normalize_finish_categories(
            self.settings.get("FinishCalculation", "FinishCategories", serialize_finish_categories())
        )
        self.material_trades = normalize_material_trades(
            self.settings.get("Materials", "Trades", serialize_material_trades())
        )
        self.material_units = normalize_material_units(
            self.settings.get("Materials", "Units", serialize_material_units())
        )
        self.window_categories = normalize_window_categories(
            self.settings.get("Windows", "Categories", serialize_window_categories())
        )
        # 재료마감 5개 타입의 내부키는 고정하고 화면 탭 이름만 settings2에 저장한다.
        self.finish_group_type_labels = {
            "마감": self.recent_settings.get("FinishGroupTabs", "Finish", "마감"),
            "단열": self.recent_settings.get("FinishGroupTabs", "Insulation", "단열"),
            "방수": self.recent_settings.get("FinishGroupTabs", "Waterproof", "방수"),
            "외부": self.recent_settings.get("FinishGroupTabs", "Exterior", "외부"),
            "기타": self.recent_settings.get("FinishGroupTabs", "Other", "기타"),
        }
        # 각 재료마감 탭 안의 사용자 구분과 도면표시 확장 옵션도 settings2에 저장한다.
        _division_defaults = {"마감": ["바탕", "마감"], "단열": ["단열"], "방수": ["방수"], "외부": ["외부"], "기타": ["기타"]}
        _division_keys = {"마감": "Finish", "단열": "Insulation", "방수": "Waterproof", "외부": "Exterior", "기타": "Other"}
        _divisions = {}
        _show_other = {}
        for _key, _ini_key in _division_keys.items():
            _raw = self.recent_settings.get(
                "FinishGroupDivisions", _ini_key,
                json.dumps(_division_defaults[_key], ensure_ascii=False),
            )
            try:
                _values = json.loads(_raw)
                if not isinstance(_values, list):
                    raise ValueError
                _values = [str(value).strip() for value in _values if str(value).strip()]
            except Exception:
                _values = list(_division_defaults[_key])
            _divisions[_key] = _values or list(_division_defaults[_key])
            _show_other[_key] = self.recent_settings.get(
                "FinishGroupDisplay", f"{_ini_key}ShowOther", "False"
            ).lower() == "true"
        self.finish_group_tab_settings = {
            "divisions": _divisions,
            "show_other_filters": _show_other,
        }
        try:
            self.auto_backup_minutes = max(1, min(1440, int(self.settings.get("Common", "AutoBackupMinutes", "30"))))
        except (TypeError, ValueError):
            self.auto_backup_minutes = 30
        self.worker = None
        self.cad_bridge = CadBridge()
        self.block_service = BlockService(self.cad_bridge, BLOCK_LIBRARY_DIR)
        self.layer_service = LayerService(self.cad_bridge)
        self.background_workers = []
        self._reuse_background_cache_once = False
        self.project_cache_save_timer = QTimer(self)
        self.project_cache_save_timer.setSingleShot(True)
        self.project_cache_save_timer.setInterval(500)
        self.project_cache_save_timer.timeout.connect(self._save_current_cad_project_cache)
        self.project_autosave_timer = QTimer(self)
        self.project_autosave_timer.setSingleShot(False)
        self.project_autosave_timer.timeout.connect(self._start_project_autosave)
        self.project_autosave_worker: ProjectAutosaveThread | None = None
        self._restart_project_autosave_timer()

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
        self.open_btn = QPushButton("DXF 파일 선택")
        self.export_btn = QPushButton("Excel 저장")
        self.cad_sync_btn = QPushButton("CAD 연동")
        self.settings_btn = QPushButton("설정")
        self.project_save_btn = QPushButton("저장", root)
        self.restart_btn = QPushButton("재실행", root)
        self.project_save_btn.setFixedSize(self.settings_btn.sizeHint())
        self.restart_btn.setFixedSize(self.settings_btn.sizeHint())
        self.project_save_btn.raise_()
        self.restart_btn.raise_()
        self.export_btn.setEnabled(False)
        self.path_label = QLabel("선택된 파일 없음")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.open_btn)
        top.addWidget(self.cad_sync_btn)
        top.addWidget(self.path_label, 1)
        top.addWidget(self.export_btn)
        top.addWidget(self.settings_btn)
        layout.addLayout(top)

        # V23.17: 메인 업무 흐름을 고정 탭으로 재구성한다.
        # 기존 재료문자 탭은 메인에서 빼고 재료마감 우상단의 [재료] 버튼으로 연다.
        self.main_tab_names = {
            "overview": "개요", "rooms": "평면", "external": "입면",
            "structure": "구조", "other": "기타", "window": "창호",
            "finish": "재료마감", "summary": "집계",
        }
        material_display_names = {
            "재료 후보": self.settings.get("TabNames", "MaterialCandidate", "재료 후보"),
            "보류": self.settings.get("TabNames", "MaterialHold", "보류"),
            "제외": self.settings.get("TabNames", "MaterialExcluded", "제외"),
        }

        self.tabs = QTabWidget()
        self.main_tab_bar = SpacedTabBar(self.tabs)
        self.tabs.setTabBar(self.main_tab_bar)
        self.material_tab = MaterialTextTab(material_display_names, self.on_material_tab_name_edited)
        self.tables = self.material_tab.tables
        self.search = self.material_tab.search
        self.sort_combo = self.material_tab.sort_combo

        room_column_widths = self.recent_settings.get("RoomList", "ColumnWidths", "82,85,150,95,130,82,82,85")
        room_column_order_version = self.recent_settings.get("RoomList", "ColumnOrderVersion", "").strip()
        if room_column_order_version == "V20":
            room_column_widths = RoomsTab._normalize_room_column_widths(room_column_widths)
            self.recent_settings.set("RoomList", "ColumnWidths", ",".join(str(value) for value in room_column_widths))
            self.recent_settings.set("RoomList", "ColumnOrderVersion", "V20.4")
            self.recent_settings.save()
        elif room_column_order_version != "V20.4":
            room_column_widths = RoomsTab._migrate_legacy_room_column_widths(room_column_widths)
            self.recent_settings.set("RoomList", "ColumnWidths", ",".join(str(value) for value in room_column_widths))
            self.recent_settings.set("RoomList", "ColumnOrderVersion", "V20.4")
            self.recent_settings.save()
        else:
            room_column_widths = RoomsTab._normalize_room_column_widths(room_column_widths)
        finish_summary_column_order = self.recent_settings.get(
            "RoomList", "FinishSummaryColumnOrder", "0,1,2,3"
        )
        self.rooms_tab = RoomsTab(
            self.background_max_pixels, self.edge_length_font_size, self.edge_length_offset,
            self.edge_length_width_ratio, self.room_label_font_size,
            self.room_number_entry_settings, room_column_widths, self.room_category_rules,
            self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units,
            finish_summary_column_order,
        )
        self.rooms_tab.set_finish_group_type_labels(self.finish_group_type_labels)
        self.rooms_tab.finish_group_type_labels_changed.connect(self._save_finish_group_type_labels)
        self.rooms_tab.set_finish_group_tab_settings(self.finish_group_tab_settings)
        self.rooms_tab.finish_group_tab_settings_changed.connect(self._save_finish_group_tab_settings)
        self.rooms_tab.set_room_rules(self.room_rules)
        self.rooms_tab.set_original_text_visible(self.recent_settings.get("Preview", "ShowOriginalText", "True").lower() == "true")
        self.rooms_tab.set_cad_background_mode(self.cad_background_mode)
        self.rooms_tab.cad_background_mode_changed.connect(self._save_cad_background_mode)
        self.rooms_tab.set_group_by_floor(self.recent_settings.get("RoomList", "GroupByFloor", "True").lower() == "true")
        self.rooms_tab.original_text_toggled.connect(self._save_original_text_visible)
        self.rooms_tab.group_by_floor_toggled.connect(self._save_room_group_by_floor)
        self.rooms_tab.room_column_widths_changed.connect(self._save_room_column_widths)
        self.rooms_tab.finish_summary_column_order_changed.connect(self._save_finish_summary_column_order)
        self.rooms_tab.number_entry_settings_changed.connect(self._save_room_number_entry_settings)
        self.rooms_tab.apply_preview_settings(self.background_max_pixels, self.edge_length_font_size, self.edge_length_offset, self.edge_length_width_ratio, self.background_style, self.background_opacity, self.boundary_color, self.boundary_width, self.render_quality, self.selection_color, self.selection_width, self.background_line_width, self.background_aci_widths, self.background_keep_red, self.edge_length_show_unit_m, self.label_full_view_weight, self.label_selected_view_weight, self.room_label_scale_mode, self.deduction_color, self.deduction_width, self.wall_selection_color, self.wall_selection_width, self.group_overlay_opacity, self.wall_selection_zoom_scale, self.edge_length_visible, self.opening_line_color, self.opening_line_width, self.background_zoomout_width_weight)
        self.rooms_tab.set_background_render_mode(self.background_render_mode)
        self.rooms_tab.preview.set_label_area_scale(self.room_label_area_scale)

        self.overview_tab = OverviewTab()
        self.overview_tab.set_rooms_provider(lambda: list(getattr(self.rooms_tab, "rooms", []) or []))
        self.overview_tab.set_project(None)
        self.external_tab = PlaceholderTab("입면")
        self.structure_tab = PlaceholderTab("구조")
        self.other_tab = OtherWorksTab(self.material_units)
        self.window_tab = WindowTab(
            self.window_categories, self.material_trades, self.material_units,
            recent_settings=self.recent_settings,
            rooms_provider=lambda: list(getattr(self.rooms_tab, "rooms", []) or []),
            overview_provider=lambda: self.overview_tab._collect_fields(),
            opening_provider=lambda: dict(getattr(self.rooms_tab, "opening_read_results", {}) or {}),
        )
        self.summary_tab = SummaryTab(self.material_trades)
        self.rooms_tab.set_window_catalog_provider(self.window_tab.window_catalog)

        self.tabs.addTab(self.overview_tab, self.main_tab_names["overview"])
        self.tabs.addTab(self.rooms_tab, self.main_tab_names["rooms"])
        self.tabs.addTab(self.external_tab, self.main_tab_names["external"])
        self.tabs.addTab(self.structure_tab, self.main_tab_names["structure"])
        self.tabs.addTab(self.other_tab, self.main_tab_names["other"])
        self.tabs.addTab(self.window_tab, self.main_tab_names["window"])
        self.rooms_tab.background_regenerate_requested.connect(self.request_background_regeneration)
        self.rooms_tab.background_filter_requested.connect(self.open_vector_filter_dialog)
        self.rooms_tab.cad_create_blocks_requested.connect(self.create_room_blocks_in_cad)
        self.rooms_tab.cad_refresh_blocks_requested.connect(self.refresh_room_blocks_in_cad)
        self.rooms_tab.room_wall_link_cad_requested.connect(self.handle_room_wall_link_cad)
        self.rooms_tab.opening_read_requested.connect(self.handle_opening_read)
        self.rooms_tab.background_settings_requested.connect(self.open_cad_background_settings)
        self.rooms_tab.drawing_library_requested.connect(self.open_drawing_library)
        self.rooms_tab.dimension_settings_changed.connect(self.on_dimension_settings_changed)
        self.rooms_tab.label_size_changed.connect(self.on_room_label_size_changed)
        self.rooms_tab.data_changed.connect(self._schedule_current_cad_project_cache_save)

        materials_path = MATERIAL_FINISH_PATH
        self.finish_materials_tab = FinishMaterialsPanel(
            materials_path, assignment_enabled=False,
            formula_codes=self.finish_formula_codes, finish_categories=self.finish_categories,
            material_trades=self.material_trades, material_units=self.material_units,
        )
        self.tabs.addTab(self.finish_materials_tab, self.main_tab_names["finish"])
        summary_index = self.tabs.addTab(self.summary_tab, self.main_tab_names["summary"])
        # 집계는 입력/편집 탭과 시각적으로 분리한다.
        self.main_tab_bar.set_gap_before(summary_index, 20)
        self.finish_materials_tab.materials_changed.connect(self.rooms_tab.finish_panel.reload)
        self.window_tab.materials_changed.connect(self.finish_materials_tab.reload)
        self.window_tab.materials_changed.connect(self.rooms_tab.finish_panel.reload)
        self.finish_materials_tab.materials_changed.connect(self._refresh_summary_tab)
        self.finish_materials_tab.material_units_changed.connect(self._on_material_units_changed)
        self.rooms_tab.finish_panel.material_units_changed.connect(self._on_material_units_changed)
        self.rooms_tab.finish_panel.materials_changed.connect(self.finish_materials_tab.reload)
        self.rooms_tab.finish_panel.materials_changed.connect(self._refresh_summary_tab)
        self.finish_materials_tab.material_delete_requested.connect(self.rooms_tab.handle_material_deleted)
        self.rooms_tab.data_changed.connect(self._refresh_summary_tab)
        self.other_tab.data_changed.connect(self._refresh_summary_tab)
        self.other_tab.material_units_changed.connect(self._on_material_units_changed)
        self.window_tab.data_changed.connect(self._schedule_current_cad_project_cache_save)
        self.window_tab.catalog_changed.connect(self.rooms_tab.refresh_opening_catalog_assignments)
        self.window_tab.catalog_changed.connect(self._refresh_window_tab_title)
        self.window_tab.material_units_changed.connect(self._on_material_units_changed)
        self.rooms_tab.data_changed.connect(self.window_tab.refresh_floor_schedule)
        self.summary_tab.markup_changed.connect(self.rooms_tab.set_summary_markup)
        self.summary_tab.apply_material_markups_requested.connect(self._apply_summary_markups_to_materials)
        self.overview_tab.overview_changed.connect(self._save_project_overview)
        self.overview_tab.fields_changed.connect(self._save_project_overview_fields)
        self.overview_tab.fields_changed.connect(lambda _values: self.window_tab.refresh_floor_schedule())
        self._refresh_window_tab_title()
        # 초기 UI 구성/프로젝트 복원 직후 다른 탭 제목 갱신이 창호명을 덮지 않도록 한 번 더 동기화한다.
        QTimer.singleShot(0, self._refresh_window_tab_title)

        # 기존 '재료' 메인탭은 재료마감 우상단 버튼으로 이동한다.
        self.material_dialog = QDialog(self)
        self.material_dialog.setWindowTitle("재료")
        self.material_dialog.resize(1200, 760)
        material_dialog_layout = QVBoxLayout(self.material_dialog)
        material_dialog_layout.setContentsMargins(6, 6, 6, 6)
        material_dialog_layout.addWidget(self.material_tab)
        self.material_open_btn = QPushButton("재료")
        self.material_open_btn.setToolTip("기존 재료 문자 추출/분류 화면을 엽니다.")
        self.material_open_btn.clicked.connect(self.open_material_dialog)
        if hasattr(self.finish_materials_tab, "search_widget") and self.finish_materials_tab.search_widget.layout():
            self.finish_materials_tab.search_widget.layout().addWidget(self.material_open_btn)

        layout.addWidget(self.tabs)

        bottom = QHBoxLayout()
        self.status_label = QLabel("준비")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.progress)
        # V24.22: 현재 벡터 표시 단계는 메인 창 맨 아래 상태/알림 라인의 우측 끝에 둔다.
        bottom.addWidget(self.rooms_tab.vector_stage_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(bottom)

        self.open_btn.clicked.connect(self.open_file)
        self.export_btn.clicked.connect(self.export_excel)
        self.cad_sync_btn.clicked.connect(self.open_cad_sync)
        self.settings_btn.clicked.connect(self.open_settings)
        self.project_save_btn.clicked.connect(self.open_project_cache_save)
        # Ctrl+S는 메인 상단 [저장] 버튼과 완전히 동일한 수동 프로젝트 캐시 저장창을 연다.
        self.project_save_shortcut = QShortcut(QKeySequence.Save, self)
        self.project_save_shortcut.setContext(Qt.WindowShortcut)
        self.project_save_shortcut.activated.connect(self.open_project_cache_save)
        self.restart_btn.clicked.connect(self.restart_application)
        self.search.textChanged.connect(self.apply_filter)
        self.tabs.currentChanged.connect(self.apply_filter)
        self.material_tab.tabs.currentChanged.connect(self.apply_filter)
        self.sort_combo.currentIndexChanged.connect(self.apply_sort)
        self.material_tab.reread_requested.connect(self.reread_materials_from_cad)

        project_action = QAction("프로젝트 목록", self)
        project_action.triggered.connect(lambda: self.open_project_list(False))
        quit_action = QAction("종료", self)
        quit_action.triggered.connect(self.close)
        file_menu = self.menuBar().addMenu("파일")
        file_menu.addAction(project_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)
        QTimer.singleShot(0, self._position_restart_button)
        QTimer.singleShot(100, self.show_startup_project_dialog)

    def show_startup_project_dialog(self):
        self.open_project_list(True)

    def open_project_list(self, startup: bool = False):
        last_id = self.recent_settings.get("Project", "LastProjectId", "").strip()
        dialog = ProjectListDialog(self.project_manager, last_id, self, startup=startup)
        if dialog.exec() != QDialog.Accepted or dialog.selected_project is None:
            return
        self.set_active_project(dialog.selected_project)

    def _project_relative_text(self, path: str | Path | None = None, fallback_folder: str = "") -> str:
        """사용자에게는 앱 절대경로 대신 프로젝트/<프로젝트명>/... 형태로 보여준다."""
        if self.active_project is None:
            return str(path or "")
        candidate = Path(path) if path else (self.active_project.path / fallback_folder if fallback_folder else self.active_project.path)
        try:
            rel = candidate.resolve().relative_to(PROJECTS_DIR.resolve())
            return str(Path("프로젝트") / rel)
        except Exception:
            try:
                rel = candidate.relative_to(self.active_project.path)
                return str(Path("프로젝트") / self.active_project.name / rel)
            except Exception:
                return str(candidate)

    def _set_project_path_label(self, source_path: str = ""):
        if self.active_project is None:
            return
        cache_text = self._project_relative_text(fallback_folder="캐쉬")
        if source_path:
            drawing_text = self._project_relative_text(source_path)
            self.path_label.setText(f"프로젝트 캐시: {cache_text} · 도면: {drawing_text}")
        else:
            self.path_label.setText(f"프로젝트 캐시: {cache_text} · 도면 없음")

    def _update_window_title(self):
        parts = [self._base_window_title]
        if self.active_project is not None:
            parts.append(self.active_project.name)
        link_states = []
        try:
            if self.cad_bridge.connected:
                link_states.append("CAD 연동")
        except Exception:
            pass
        dxf_path = str(getattr(self, "applied_dxf_path", "") or "").strip()
        if bool(getattr(self, "dxf_link_active", False)) and dxf_path:
            try:
                if Path(dxf_path).is_file() and Path(dxf_path).suffix.lower() == ".dxf":
                    link_states.append("DXF 연동")
            except Exception:
                pass
        if link_states:
            parts.append(" · ".join(link_states))
        self.setWindowTitle(" · ".join(parts))

    def set_active_project(self, record: ProjectRecord):
        # V24.08: 프로젝트 열기는 캐시 복원만 한다. 이전 프로젝트에서 CAD가 연결되어 있어도
        # 새 프로젝트를 여는 순간 연결을 끊고, 사용자가 [CAD 연동] 버튼을 눌렀을 때만 다시 연결한다.
        try:
            if self.cad_bridge.connected:
                self.cad_bridge.disconnect()
        except Exception:
            pass
        self.rooms_tab.set_cad_connected_mode(False)
        self._clear_loaded_source("프로젝트 캐시 확인 중")
        self.active_project = self.project_manager.get(record.project_id)
        if hasattr(self, "overview_tab"):
            self.overview_tab.set_project(self.active_project)
        self.recent_settings.set("Project", "LastProjectId", self.active_project.project_id)
        self.recent_settings.save()
        drawing = self.active_project.base_drawing_path
        self.applied_dxf_path = str(drawing) if drawing else ""
        self.dxf_link_active = bool(
            drawing and Path(drawing).is_file() and Path(drawing).suffix.lower() == ".dxf"
        )
        self._update_window_title()
        self.recent_settings.set("BackgroundDrawing", "ProjectName", self.active_project.name)
        self.recent_settings.set("BackgroundDrawing", "LibraryPath", str(drawing) if drawing else "")
        self.recent_settings.save()
        snapshot_loaded = self._restore_active_project_snapshot()
        if not snapshot_loaded:
            self._set_project_path_label(str(drawing) if drawing else "")
            self.status_label.setText(f"프로젝트 열림: {self.active_project.name} · 저장된 캐시 없음")
        self._restore_applied_project_dxf(drawing, snapshot_loaded=snapshot_loaded)
        # V24.12.2: 프로젝트 열기 직후 캐시는 복원됐지만 Preview가 캐시 연결 전 상태로
        # 한 번 그려져 층을 이동해야 도곽이 나타나는 경우가 있었다. 현재 이벤트 처리가
        # 끝난 뒤 캐시가 준비된 현재 층을 딱 한 번 다시 그린다. CAD 연동/재읽기는 하지 않는다.
        QTimer.singleShot(0, self._refresh_restored_project_background_once)
        self._restart_project_autosave_timer()

    def _refresh_restored_project_background_once(self):
        try:
            preview = self.rooms_tab.preview
            has_cache = bool(
                preview.floor_background_files
                or preview.floor_vector_cache
                or preview.cad_background
            )
            if not has_cache:
                return
            self.rooms_tab.refresh_preview()
            preview.viewport().update()
        except Exception:
            # 프로젝트 열기 자체를 방해하지 않는 보조 새로고침이다.
            pass

    def _restore_applied_project_dxf(self, drawing, snapshot_loaded: bool = False):
        """프로젝트에 이미 적용된 DXF는 별도 '도면 적용' 없이 자동 연동하고 기존 캐시를 우선 복원한다."""
        if not drawing:
            self.dxf_link_active = False
            self._update_window_title()
            return
        try:
            dxf_path = Path(drawing)
            if not dxf_path.is_file() or dxf_path.suffix.lower() != ".dxf":
                self.dxf_link_active = False
                self._update_window_title()
                return
        except Exception:
            self.dxf_link_active = False
            self._update_window_title()
            return

        self.applied_dxf_path = str(dxf_path)
        self.dxf_link_active = True
        if not self.current_path or Path(str(self.current_path)).suffix.lower() != ".dwg":
            self.current_path = str(dxf_path)
            self.rooms_tab.set_source_path(self.current_path)
        try:
            self.rooms_tab.set_base_dxf_path(str(dxf_path))
        except Exception:
            pass

        # 프로젝트 저장본에서 도곽/층 지정값이 복원되어 있으면 현재 작업값에도 즉시 연결한다.
        if not self.current_drawing_divisions:
            self.current_drawing_divisions = list(self.cad_bridge.last_selected_divisions or [])

        # V24.09: 배경 캐시 키는 적용 DXF와 다를 수 있다. 특히 CAD 연동 상태에서 생성한
        # 캐시는 DWG 경로를 source_path로 사용하므로 프로젝트를 다시 열 때 CAD가 꺼져 있어도
        # 저장된 DWG 캐시 키 / 프로젝트의 기존 연동 CAD 경로를 먼저 확인한다.
        restored_geometry = str(getattr(self, "_restored_project_source_geometry_cache", "") or "")
        restored_program = str(getattr(self, "_restored_project_program_geometry_cache", "") or "")
        restored_key = str(getattr(self, "_restored_project_cache_source_path", "") or "").strip()
        if not restored_key and restored_geometry:
            restored_key = source_path_from_cache_file(restored_geometry)
            if restored_key:
                self._restored_project_cache_source_path = restored_key

        linked_key = ""
        try:
            if self.active_project is not None:
                linked_key = str(self.active_project.cad_link_document or "").strip()
        except Exception:
            linked_key = ""

        # 프로젝트에 백업된 원본/프로그램 캐시는 원래 source_path 키 위치로 복원한다.
        if restored_key:
            try:
                import shutil
                expected_geometry = cache_file_for_source(BACKGROUND_CACHE_ROOT, restored_key)
                if restored_geometry and Path(restored_geometry).is_file():
                    expected_geometry.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(restored_geometry, expected_geometry)
                expected_program = program_cache_file(BACKGROUND_CACHE_ROOT, restored_key)
                if restored_program and Path(restored_program).is_file():
                    expected_program.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(restored_program, expected_program)
            except Exception:
                pass

        candidates = []
        for candidate in (restored_key, linked_key, str(self.current_path or ""), str(dxf_path)):
            candidate = str(candidate or "").strip()
            if candidate and candidate.casefold() not in {v.casefold() for v in candidates}:
                candidates.append(candidate)

        cache_files = {}
        vector_cache = None
        cache_key = ""
        for candidate in candidates:
            try:
                trial_files = load_background_cache(
                    candidate, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style,
                    self._background_filter_signature(), self.background_line_width, self.background_aci_widths,
                    self.background_keep_red,
                )
            except Exception:
                trial_files = {}
            try:
                base_for_signature = str(dxf_path) if Path(candidate).suffix.lower() == ".dxf" else None
                trial_vector = self._load_vector_background_cache(candidate, base_for_signature)
            except Exception:
                trial_vector = None
            if trial_files or trial_vector:
                cache_key = candidate
                cache_files = trial_files
                vector_cache = trial_vector
                break

        if cache_key:
            self.current_path = cache_key
            self.rooms_tab.set_source_path(cache_key)
        if cache_files:
            self.rooms_tab.set_background_cache_files(cache_files)
        has_cached_background = bool(cache_files or vector_cache)
        if has_cached_background:
            self.rooms_tab.set_background_ready(True)
            self.status_label.setText(
                f"프로젝트 캐시 복원 · 기존 배경 캐시 사용 · CAD 미연동 · {dxf_path.name}"
            )
        elif snapshot_loaded:
            # 프로젝트 스냅샷 안의 렌더 캐시가 이미 복원된 경우는 그대로 유지한다.
            preview = self.rooms_tab.preview
            if preview.floor_background_files or preview.floor_vector_cache:
                self.rooms_tab.set_background_ready(True)
                self.status_label.setText(f"프로젝트 캐시 복원 · 프로젝트 배경 캐시 사용 · CAD 미연동 · {dxf_path.name}")
            else:
                self.status_label.setText(f"프로젝트 열림 · 배경 캐시 없음 · CAD 미연동 · {dxf_path.name}")
        else:
            self.status_label.setText(f"프로젝트 열림 · 배경 캐시 없음 · CAD 미연동 · {dxf_path.name}")
        self._set_project_path_label(str(dxf_path))
        self._update_window_title()

    def _restore_active_project_snapshot(self) -> bool:
        if self.active_project is None:
            return False
        try:
            snapshot = load_project_snapshot(self.active_project.path)
        except Exception:
            snapshot = None
        if not snapshot:
            self._project_snapshot_loaded = False
            self._restored_project_source_geometry_cache = ""
            self._restored_project_program_geometry_cache = ""
            self._restored_project_cache_source_path = ""
            return False

        self._restored_project_source_geometry_cache = str(snapshot.get("source_geometry_path", "") or "")
        self._restored_project_program_geometry_cache = str(snapshot.get("program_geometry_path", "") or "")
        self._restored_project_cache_source_path = str(snapshot.get("background_source_path", "") or "").strip()
        # V24.09: V24.08 이전 프로젝트에는 background_source_path가 없을 수 있다.
        # 그 경우 프로젝트에 백업된 원본도형 캐시 내부 source_path를 직접 읽어 정확한 캐시 키를 복원한다.
        if not self._restored_project_cache_source_path and self._restored_project_source_geometry_cache:
            self._restored_project_cache_source_path = source_path_from_cache_file(
                self._restored_project_source_geometry_cache
            )
        finish_material_list = snapshot.get("finish_material_list")
        if isinstance(finish_material_list, dict):
            try:
                MATERIAL_FINISH_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary = MATERIAL_FINISH_PATH.with_suffix(MATERIAL_FINISH_PATH.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(finish_material_list, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary.replace(MATERIAL_FINISH_PATH)
            except Exception:
                pass

        if snapshot.get("formula_codes") is not None:
            self.finish_formula_codes = normalize_formula_codes(snapshot.get("formula_codes"))
        if snapshot.get("finish_categories") is not None:
            self.finish_categories = normalize_finish_categories(snapshot.get("finish_categories"))
        if snapshot.get("material_trades") is not None:
            self.material_trades = normalize_material_trades(snapshot.get("material_trades"))
        if snapshot.get("material_units") is not None:
            self.material_units = normalize_material_units(snapshot.get("material_units"))
        self.finish_materials_tab.set_calculation_settings(
            self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units
        )
        self.rooms_tab.finish_panel.set_calculation_settings(
            self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units
        )
        self.other_tab.set_material_units(self.material_units)
        self.window_tab.set_calculation_settings(self.window_categories, self.material_trades, self.material_units)

        self.current_result = snapshot.get("result") or {"재료 후보": [], "보류": [], "제외": []}
        for category in ("재료 후보", "보류", "제외"):
            self.current_result.setdefault(category, [])
            self.material_tab.set_category_rows(category, self.current_result[category])
        self.current_rooms = list(snapshot.get("rooms") or [])
        self.current_room_errors = list(snapshot.get("errors") or [])
        self.cad_bridge.last_selected_divisions = list(snapshot.get("divisions") or [])
        self.current_drawing_divisions = list(snapshot.get("divisions") or [])

        base_drawing = self.active_project.base_drawing_path
        cached_source = str(snapshot.get("source_path", "") or "").strip()
        # 배경 캐시는 적용 DXF와 다른 CAD(DWG) 경로를 키로 생성될 수 있다.
        # 프로젝트 열기에서는 CAD에 연결하지 않아도 저장 당시의 배경 캐시 키를 최우선으로 유지한다.
        source_path = (
            self._restored_project_cache_source_path
            or cached_source
            or (str(base_drawing) if base_drawing else "")
        )
        self.current_path = source_path
        self.rooms_tab.set_source_path(source_path)
        self.rooms_tab.set_data(self.current_rooms, self.current_room_errors)
        self.rooms_tab.restore_finish_assignments(
            snapshot.get("wall_finish_assignments"), snapshot.get("room_finish_assignments"),
            snapshot.get("finish_groups"), snapshot.get("summary_markups"),
            snapshot.get("opening_read_results"),
        )
        self.other_tab.set_data(snapshot.get("other_items") or [])
        self.window_tab.set_data(snapshot.get("window_data") or {})
        self._refresh_window_tab_title()
        self.rooms_tab.refresh_opening_catalog_assignments()
        background_files = dict(snapshot.get("background_files") or {})
        if background_files:
            self.rooms_tab.set_background_cache_files(background_files)
            self.rooms_tab.set_background_ready(True)
        self.rooms_tab.set_cad_connected_mode(False)
        self.export_btn.setEnabled(bool(self.current_rooms or any(self.current_result.values())))
        self._set_project_path_label(source_path)
        self.refresh_main_tab_titles()
        self._project_snapshot_loaded = True
        sources = snapshot.get("sources") or {}
        used = []
        for section in ("material", "rooms", "background"):
            source = str(sources.get(section, "") or "")
            if source:
                used.append(f"{SECTION_LABELS[section]}:{source}")
        self.status_label.setText(f"프로젝트 캐시 자동복원 완료 · {' / '.join(used) or '복원 항목 있음'}")
        return True

    def _restart_project_autosave_timer(self):
        if not hasattr(self, "project_autosave_timer"):
            return
        interval_ms = max(1, int(self.auto_backup_minutes)) * 60 * 1000
        self.project_autosave_timer.start(interval_ms)

    def _project_autosave_state(self):
        if self.active_project is None:
            return None
        try:
            project = self.project_manager.get(self.active_project.project_id)
        except Exception:
            return None

        finish_material_list = []
        if MATERIAL_FINISH_PATH.is_file():
            try:
                finish_material_list = json.loads(MATERIAL_FINISH_PATH.read_text(encoding="utf-8"))
            except Exception:
                finish_material_list = []

        material_available = bool(
            any(self.current_result.get(name) for name in self.current_result)
            or self.rooms_tab.wall_finish_assignments
            or self.rooms_tab.room_finish_assignments
            or self.rooms_tab.finish_groups
            or self.rooms_tab.summary_markups
            or self.rooms_tab.opening_read_results
            or self.other_tab.data()
            or self.window_tab.data().get("windows")
            or finish_material_list
        )
        room_available = bool(self.current_rooms)
        rendered_background = None
        source_geometry = None
        if self.current_path:
            candidate = cache_dir_for_source(BACKGROUND_CACHE_ROOT, self.current_path)
            if candidate.is_dir() and any(candidate.iterdir()):
                rendered_background = candidate
            geometry_candidate = cache_file_for_source(BACKGROUND_CACHE_ROOT, self.current_path)
            if geometry_candidate.is_file():
                source_geometry = geometry_candidate
                candidate.mkdir(parents=True, exist_ok=True)
                rendered_background = candidate

        sections = []
        if material_available:
            sections.append("material")
        if room_available:
            sections.append("rooms")
        if rendered_background is not None:
            sections.append("background")
        if not sections:
            return None

        material_payload = {
            "result": deepcopy(self.current_result),
            "finish_material_list": deepcopy(finish_material_list),
            "wall_finish_assignments": deepcopy(self.rooms_tab.wall_finish_assignments),
            "room_finish_assignments": deepcopy(self.rooms_tab.room_finish_assignments),
            "finish_groups": deepcopy(self.rooms_tab.finish_groups),
            "summary_markups": deepcopy(self.rooms_tab.summary_markups),
            "opening_read_results": deepcopy(self.rooms_tab.opening_read_results),
            "other_items": deepcopy(self.other_tab.data()),
            "window_data": deepcopy(self.window_tab.data()),
            "formula_codes": deepcopy(self.finish_formula_codes),
            "finish_categories": deepcopy(self.finish_categories),
            "material_trades": deepcopy(self.material_trades),
            "material_units": deepcopy(self.material_units),
        }
        room_payload = {
            "rooms": deepcopy(self.current_rooms),
            "errors": deepcopy(self.current_room_errors),
            "divisions": deepcopy(list(self.cad_bridge.last_selected_divisions or [])),
        }
        return {
            "project": project,
            "sections": sections,
            "source_path": self.current_path,
            "material_payload": material_payload,
            "room_payload": room_payload,
            "rendered_background": rendered_background,
            "source_geometry": source_geometry,
        }

    def _start_project_autosave(self):
        if self.project_autosave_worker is not None and self.project_autosave_worker.isRunning():
            return
        self._autosave_linked_cad()
        state = self._project_autosave_state()
        if state is None:
            return
        worker = ProjectAutosaveThread(
            state["project"].path,
            state["sections"],
            source_path=state["source_path"],
            material_payload=state["material_payload"],
            room_payload=state["room_payload"],
            rendered_background_cache=state["rendered_background"],
            source_geometry_cache=state["source_geometry"],
            parent=self,
        )
        self.project_autosave_worker = worker
        worker.completed.connect(self._on_project_autosave_completed)
        worker.failed.connect(self._on_project_autosave_failed)
        worker.finished.connect(self._on_project_autosave_finished)
        self.status_label.setText(f"프로젝트 자동저장 중: {state['project'].name}")
        worker.start()

    def _on_project_autosave_completed(self, report: dict):
        saved_names = ", ".join(SECTION_LABELS.get(section, section) for section in report.get("saved", []))
        saved_at = str(report.get("saved_at", "") or "")
        self.status_label.setText(f"프로젝트 자동저장 완료: {saved_names} · {saved_at}")

    def _on_project_autosave_failed(self, error_text: str):
        summary = str(error_text or "").strip().splitlines()
        detail = summary[-1] if summary else "알 수 없는 오류"
        self.status_label.setText(f"프로젝트 자동저장 실패: {detail}")

    def _on_project_autosave_finished(self):
        worker = self.project_autosave_worker
        self.project_autosave_worker = None
        if worker is not None:
            worker.deleteLater()

    def _cad_link_layer(self) -> str:
        return str(
            self.room_rules.get(
                "drawing_division_layer",
                default_layer_name("drawing_division_layer"),
            ) or default_layer_name("drawing_division_layer")
        ).strip()

    def _register_active_cad_project_link(self) -> str:
        """현재 CAD에 식별 마커를 저장하고 Handle을 프로젝트 정보에 기록한다."""
        if self.active_project is None or not self.cad_bridge.connected:
            return ""
        record = self.project_manager.get(self.active_project.project_id)
        token = record.cad_link_token or uuid.uuid4().hex.upper()
        marker = self.cad_bridge.ensure_project_link_marker(
            record.project_id,
            token,
            self._cad_link_layer(),
            existing_handle=record.cad_link_handle,
            save_document=True,
        )
        document = marker.get("document") or (
            self.cad_bridge.info.document_path if self.cad_bridge.info else ""
        )
        self.active_project = self.project_manager.update_cad_link(
            record.project_id,
            marker["handle"],
            token,
            document,
        )
        detail = f"CAD 연동표시 저장: Handle {marker['handle']}"
        self.status_label.setText(detail)
        return detail

    def register_connected_cad_project(self, dialog=None):
        """CAD 연동창의 등록 버튼으로 현재 도면에 식별 마커를 즉시 만든다."""
        if not self.cad_bridge.connected:
            QMessageBox.information(dialog or self, "CAD 연동 등록", "먼저 현재 실행 중인 CAD에 연결해 주세요.")
            return
        if self.active_project is None:
            QMessageBox.information(dialog or self, "CAD 연동 등록", "먼저 프로젝트 목록에서 프로젝트를 열어 주세요.")
            return
        try:
            detail = self._register_active_cad_project_link()
            result = self._check_connected_cad_project(dialog, show_warning=False)
            if dialog is not None and self.active_project is not None:
                dialog.set_existing_link(str(self.active_project.cad_link_document or ""))
            if result.get("status") != "same":
                raise RuntimeError(result.get("text") or "등록 후 CAD 식별 마커를 확인하지 못했습니다.")
            QMessageBox.information(
                dialog or self,
                "CAD 연동 등록 완료",
                f"현재 CAD를 프로젝트 도면으로 등록했습니다.\n\n"
                f"레이어: {self._cad_link_layer()}\n{detail}\n\n"
                "CAD 도면과 프로젝트 연동정보를 함께 저장했습니다.",
            )
        except Exception as exc:
            QMessageBox.critical(dialog or self, "CAD 연동 등록 오류", str(exc))

    def _autosave_linked_cad(self):
        """이미 연결표시가 있는 프로젝트만 자동저장 주기에 CAD와 메타정보를 동기화한다."""
        if self.active_project is None or not self.cad_bridge.connected:
            return
        try:
            record = self.project_manager.get(self.active_project.project_id)
            if not record.cad_link_handle or not record.cad_link_token:
                return
            status = self.cad_bridge.project_link_status(
                record.cad_link_handle,
                record.project_id,
                record.cad_link_token,
                self._cad_link_layer(),
            )
            if status.get("status") != "same":
                return
            self.cad_bridge.save_active_document()
            document = self.cad_bridge.info.document_path if self.cad_bridge.info else record.cad_link_document
            self.active_project = self.project_manager.update_cad_link(
                record.project_id,
                record.cad_link_handle,
                record.cad_link_token,
                document,
            )
        except Exception:
            # 캐시 자동저장은 CAD 저장 실패와 관계없이 계속 진행한다.
            return

    def open_project_cache_save(self):
        """현재 작업 중 선택한 항목만 활성 프로젝트의 수동 캐시에 저장한다."""
        if self.active_project is None:
            QMessageBox.information(self, "프로젝트 캐시 저장", "먼저 프로젝트 목록에서 프로젝트를 열어 주세요.")
            return
        try:
            self.active_project = self.project_manager.get(self.active_project.project_id)
        except Exception:
            QMessageBox.warning(self, "프로젝트 캐시 저장", "현재 프로젝트 정보를 다시 불러올 수 없습니다.")
            return

        material_available = bool(
            any(self.current_result.get(name) for name in self.current_result)
            or self.rooms_tab.wall_finish_assignments
            or self.rooms_tab.room_finish_assignments
            or self.rooms_tab.finish_groups
            or self.rooms_tab.opening_read_results
            or self.other_tab.data()
            or MATERIAL_FINISH_PATH.is_file()
        )
        room_available = bool(self.current_rooms)
        rendered_background = None
        source_geometry = None
        if self.current_path:
            candidate = cache_dir_for_source(BACKGROUND_CACHE_ROOT, self.current_path)
            if candidate.is_dir() and any(candidate.iterdir()):
                rendered_background = candidate
            geometry_candidate = cache_file_for_source(BACKGROUND_CACHE_ROOT, self.current_path)
            if geometry_candidate.is_file():
                source_geometry = geometry_candidate
                # 벡터 전용 모드에는 PNG가 없어도 원본도형 캐시를 프로젝트 저장본에 넣는다.
                candidate.mkdir(parents=True, exist_ok=True)
                rendered_background = candidate
        background_available = rendered_background is not None or source_geometry is not None

        dialog = QDialog(self)
        dialog.setWindowTitle("프로젝트 캐시 저장")
        dialog.setModal(True)
        dialog.setFixedWidth(390)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"프로젝트: {self.active_project.name}"))
        source_text = self.current_path or "(현재 도면 없음)"
        source_label = QLabel(f"현재 도면: {source_text}")
        source_label.setWordWrap(True)
        source_label.setStyleSheet("color:#666666;")
        layout.addWidget(source_label)

        material_check = QCheckBox("재료")
        room_check = QCheckBox("내부")
        background_check = QCheckBox("캐드배경")
        checks = {
            "material": material_check,
            "rooms": room_check,
            "background": background_check,
        }
        availability = {
            "material": material_available,
            "rooms": room_available,
            "background": background_available,
        }
        for section, check in checks.items():
            check.setEnabled(availability[section])
            check.setChecked(availability[section])
            if not availability[section]:
                check.setToolTip("현재 저장할 내용이 없습니다.")
            layout.addWidget(check)

        guide = QLabel(
            "선택한 항목만 프로젝트의 캐쉬/수동저장 폴더에 저장합니다. "
            "CAD가 연결되어 있으면 선택 항목이 없어도 현재 CAD 연동표시를 저장할 수 있습니다."
        )
        guide.setStyleSheet("color:#666666;")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Ok)
        save_button.setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def update_save_enabled(_checked=None):
            cache_selected = any(check.isEnabled() and check.isChecked() for check in checks.values())
            save_button.setEnabled(cache_selected or self.cad_bridge.connected)

        for check in checks.values():
            check.toggled.connect(update_save_enabled)
        update_save_enabled()
        if dialog.exec() != QDialog.Accepted:
            return

        selected = [section for section, check in checks.items() if check.isEnabled() and check.isChecked()]
        if not selected and not self.cad_bridge.connected:
            QMessageBox.information(self, "프로젝트 캐시 저장", "저장할 항목을 하나 이상 선택해 주세요.")
            return

        existing = existing_sections(self.active_project.path, selected)
        if existing:
            names = "\n".join(f"- {SECTION_LABELS[section]}" for section in existing)
            answer = QMessageBox.question(
                self,
                "기존 프로젝트 캐시 덮어쓰기",
                f"다음 프로젝트 캐시가 이미 있습니다.\n\n{names}\n\n선택한 캐시를 덮어쓸까요?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        finish_material_list = []
        if "material" in selected and MATERIAL_FINISH_PATH.is_file():
            try:
                finish_material_list = json.loads(MATERIAL_FINISH_PATH.read_text(encoding="utf-8"))
            except Exception:
                finish_material_list = []
        material_payload = {
            "result": self.current_result,
            "finish_material_list": finish_material_list,
            "wall_finish_assignments": self.rooms_tab.wall_finish_assignments,
            "room_finish_assignments": self.rooms_tab.room_finish_assignments,
            "finish_groups": self.rooms_tab.finish_groups,
            "summary_markups": self.rooms_tab.summary_markups,
            "opening_read_results": self.rooms_tab.opening_read_results,
            "other_items": self.other_tab.data(),
            "window_data": self.window_tab.data(),
            "formula_codes": self.finish_formula_codes,
            "finish_categories": self.finish_categories,
            "material_trades": self.material_trades,
            "material_units": self.material_units,
        }
        room_payload = {
            "rooms": self.current_rooms,
            "errors": self.current_room_errors,
            "divisions": list(self.cad_bridge.last_selected_divisions or []),
        }
        try:
            report = None
            if selected:
                report = save_project_snapshot(
                    self.active_project.path,
                    selected,
                    source_path=self.current_path,
                    material_payload=material_payload,
                    room_payload=room_payload,
                    rendered_background_cache=rendered_background,
                    source_geometry_cache=source_geometry,
                )
            link_detail = ""
            link_error = ""
            if self.cad_bridge.connected:
                try:
                    link_detail = self._register_active_cad_project_link()
                except Exception as exc:
                    link_error = str(exc)
            saved_names = ", ".join(SECTION_LABELS[section] for section in report["saved"]) if report else "없음 (CAD 연동표시만)"
            status_text = f"프로젝트 저장 완료: {saved_names}"
            if link_detail:
                status_text += f" · {link_detail}"
            elif link_error:
                status_text += " · CAD 연동표시 저장 실패"
            self.status_label.setText(status_text)
            message = f"저장 항목: {saved_names}"
            if report:
                message += f"\n\n저장 위치:\n{report['root']}"
            if link_detail:
                message += f"\n\n{link_detail}\n현재 CAD 도면도 함께 저장했습니다."
            if link_error:
                message += f"\n\nCAD 연동표시는 저장하지 못했습니다.\n{link_error}"
                QMessageBox.warning(self, "프로젝트 캐시 저장 완료 / CAD 연동표시 실패", message)
            else:
                QMessageBox.information(self, "프로젝트 캐시 저장 완료", message)
        except Exception:
            QMessageBox.critical(self, "프로젝트 캐시 저장 오류", traceback.format_exc())

    def _position_restart_button(self):
        if not hasattr(self, "restart_btn") or not hasattr(self, "project_save_btn") or not hasattr(self, "settings_btn"):
            return
        pos = self.settings_btn.mapTo(self.centralWidget(), self.settings_btn.rect().bottomLeft())
        x = pos.x()
        y = pos.y() + 3
        self.restart_btn.move(x, y)
        self.restart_btn.resize(self.settings_btn.width(), self.settings_btn.height())
        self.project_save_btn.resize(self.settings_btn.width(), self.settings_btn.height())
        self.project_save_btn.move(max(0, x - self.project_save_btn.width() - 5), y)
        self.project_save_btn.raise_()
        self.restart_btn.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._position_restart_button)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._position_restart_button)

    def _clear_loaded_source(self, message: str = "선택된 파일 없음"):
        self.project_cache_save_timer.stop()
        self._project_snapshot_loaded = False
        self.current_path = ""
        self.dxf_link_active = False
        self.current_result = {"재료 후보": [], "보류": [], "제외": []}
        self.current_rooms = []
        self.current_room_errors = []
        self.path_label.setText(message)
        self.export_btn.setEnabled(False)
        for name in self.current_result:
            self.material_tab.set_category_rows(name, [])
        self.rooms_tab.set_source_path("")
        self.rooms_tab.set_data([], [])
        if hasattr(self, "other_tab"):
            self.other_tab.set_data([])
        self.rooms_tab.clear_background_cache_files()
        self.rooms_tab.set_background_ready(False)
        self.rooms_tab.save_dxf_btn.setEnabled(False)
        self.refresh_main_tab_titles()

    def _switch_to_dxf_mode(self):
        if self.cad_bridge.connected:
            self.cad_bridge.disconnect()
        self._update_window_title()
        self.status_label.setText("CAD 연결 해제 · DXF 모드")

    def _switch_to_cad_mode(self, document_text: str):
        if self._project_snapshot_loaded:
            self.path_label.setText(f"CAD 연결: {document_text} · 프로젝트 캐시 유지")
            self.status_label.setText(f"CAD 연결됨 · 프로젝트 캐시 유지: {document_text}")
            return
        self._clear_loaded_source(f"CAD 연결: {document_text}")
        self.status_label.setText(f"CAD 연결됨: {document_text}")

    def _cad_sync_values(self):
        defaults = {
            "import_background": False,
            "import_material_list": True,
            "import_boundaries": True,
            "import_room_tags": True,
            "import_wall_finishes": True,
            "import_floor_finishes": False,
            "import_ceiling_finishes": False,
            "import_openings": False,
            "import_selected_only": False,
            "export_boundaries": True,
            "export_room_tags": True,
            "export_wall_finishes": True,
            "export_floor_finishes": False,
            "export_ceiling_finishes": False,
            "export_new_objects": True,
            "export_deleted_objects": True,
            "export_regen": True,
        }
        result = {}
        for key, default in defaults.items():
            # CAD 도서 읽기 체크 상태는 배포 ZIP에 없는 settings2.ini에 보관한다.
            source = self.recent_settings if key.startswith("import_") else self.settings
            section = "CADImport" if key.startswith("import_") else "CADSync"
            result[key] = source.get(section, key, str(default)).lower() == "true"
        result.update({
            "drawing_overview": self.recent_settings.get("CADBookRead", "drawing_overview", "False").lower() == "true",
            "drawing_plan": self.recent_settings.get("CADBookRead", "drawing_plan", "True").lower() == "true",
            "drawing_elevation": self.recent_settings.get("CADBookRead", "drawing_elevation", "False").lower() == "true",
            "drawing_section": self.recent_settings.get("CADBookRead", "drawing_section", "False").lower() == "true",
            "drawing_finish_schedule": self.recent_settings.get("CADBookRead", "drawing_finish_schedule", "False").lower() == "true",
        })
        return result

    def _save_cad_sync_values(self, values: dict):
        recent_changed = False
        normal_changed = False
        for key, value in values.items():
            if key.startswith("import_"):
                self.recent_settings.set("CADImport", key, value)
                recent_changed = True
            elif key.startswith("drawing_"):
                self.recent_settings.set("CADBookRead", key, value)
                recent_changed = True
            elif key.startswith("export_"):
                self.settings.set("CADSync", key, value)
                normal_changed = True
        if recent_changed:
            self.recent_settings.save()
        if normal_changed:
            self.settings.save()

    def _saved_cad_import_values(self) -> dict:
        values = self._cad_sync_values()
        # 현재 구현된 CAD 도서 읽기는 평면도만 지원한다.
        values.update({
            "drawing_overview": self.recent_settings.get("CADBookRead", "drawing_overview", "False").lower() == "true",
            "drawing_plan": self.recent_settings.get("CADBookRead", "drawing_plan", "True").lower() == "true",
            "drawing_elevation": self.recent_settings.get("CADBookRead", "drawing_elevation", "False").lower() == "true",
            "drawing_section": self.recent_settings.get("CADBookRead", "drawing_section", "False").lower() == "true",
            "drawing_finish_schedule": self.recent_settings.get("CADBookRead", "drawing_finish_schedule", "False").lower() == "true",
        })
        # 평면도가 꺼진 과거 설정이 있어도 현재 버전에서는 읽을 수 있는 유일한 도서이므로 켠다.
        values["drawing_plan"] = True
        return values

    def _prompt_cad_book_read(self, parent=None):
        """연동 경로와 무관하게 동일한 CAD 도서 읽기 흐름을 사용한다.

        시작창 연동과 CAD 연동창의 연결 버튼 모두 Yes 이후 동일한
        '가져올 도면' 선택창을 거친다. 실제 도면 스캔은 이 단계 전에는 하지 않는다.
        """
        parent = parent or self
        answer = QMessageBox.question(
            parent,
            "CAD 도서 읽기",
            "연결한 CAD에서 CAD 도서 읽기를 실행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        if isinstance(parent, CadSyncDialog):
            # CAD 연동창에서 연결한 경우 현재 체크 상태를 그대로 사용한다.
            parent._show_drawing_import_dialog()
            return

        # 시작창에서 연결한 경우에도 CAD 연동창과 같은 체크값/도면선택 로직을 사용한다.
        # 큰 CAD 연동창 자체는 띄우지 않고 동일한 '가져올 도면' 선택창만 호출한다.
        picker_host = CadSyncDialog(self._cad_sync_values(), self)
        if self.cad_bridge.connected and self.cad_bridge.info:
            info = self.cad_bridge.info
            cad_text = f"{info.application_name} {info.application_version} · {info.document_name}"
            picker_host.set_connection_info(True, info.foreground_title, cad_text)
        picker_host.import_requested.connect(lambda values: self.handle_cad_import(self, values))
        picker_host._show_drawing_import_dialog()
        picker_host.deleteLater()

    def _schedule_current_cad_project_cache_save(self):
        if self.current_path:
            self.project_cache_save_timer.start()

    def _save_current_cad_project_cache(self):
        self.project_cache_save_timer.stop()
        if not self.current_path:
            return
        try:
            save_project_cache(
                self.current_path, self.current_result, self.current_rooms, self.current_room_errors,
                list(self.cad_bridge.last_selected_divisions or [])
            )
        except Exception:
            pass

    def _save_background_project_snapshot_now(self, source_path: str | None = None) -> bool:
        """성공한 배경 캐시를 활성 프로젝트 자동저장 영역에 즉시 백업한다.

        벡터 배경은 PNG가 없어도 원본도형 캐시만으로 복원할 수 있다. 따라서
        source 전용 렌더캐시 폴더가 아직 없으면 빈 폴더를 만든 뒤 원본도형 캐시와
        program_vector.pkl(있으면)을 함께 프로젝트 쪽에 복사한다. CAD 저장은 하지 않는다.
        """
        if self.active_project is None:
            return False
        key = str(source_path or self.current_path or "").strip()
        if not key:
            return False
        geometry = cache_file_for_source(BACKGROUND_CACHE_ROOT, key)
        if not geometry.is_file():
            return False
        rendered = cache_dir_for_source(BACKGROUND_CACHE_ROOT, key)
        try:
            rendered.mkdir(parents=True, exist_ok=True)
            project = self.project_manager.get(self.active_project.project_id)
            save_project_snapshot(
                project.path, ["background"],
                source_path=key,
                rendered_background_cache=rendered,
                source_geometry_cache=geometry,
                directory_name=AUTOSAVE_DIR_NAME,
            )
            self.active_project = project
            return True
        except Exception:
            return False

    def _save_original_text_visible(self, enabled: bool):
        self.recent_settings.set("Preview", "ShowOriginalText", bool(enabled))
        self.recent_settings.save()

    def _load_cad_project_cache(self, source_path: str, use_data: bool, use_background: bool) -> bool:
        data_loaded = False
        if use_data:
            cached = load_project_cache(source_path)
            if cached:
                self.current_path = source_path
                self.current_result = cached["result"]
                self.current_rooms = cached["rooms"]
                self.current_room_errors = cached["errors"]
                self.cad_bridge.last_selected_divisions = list(cached.get("divisions") or [])
                self.path_label.setText(f"CAD 연결(캐시): {source_path}")
                for name, rows in self.current_result.items():
                    self.material_tab.set_category_rows(name, rows)
                self.rooms_tab.set_source_path(source_path)
                self.rooms_tab.set_data(self.current_rooms, self.current_room_errors)
                self.rooms_tab.set_cad_connected_mode(True)
                self.export_btn.setEnabled(True)
                self.refresh_main_tab_titles()
                data_loaded = True
        if use_background:
            cache_files = load_background_cache(
                source_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style,
                self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
            )
            vector_cache = self._load_vector_background_cache(source_path)
            if cache_files or vector_cache:
                if cache_files:
                    self.rooms_tab.set_background_cache_files(cache_files)
                self.rooms_tab.set_background_ready(True)
                self._reuse_background_cache_once = True
        if data_loaded:
            self.status_label.setText(f"기존 CAD 연동 캐시 사용 · 실 {len(self.current_rooms):,}개")
        return data_loaded

    def _active_cad_has_unsaved_changes(self) -> bool:
        """현재 CAD 문서가 디스크 저장본과 다른지 가볍게 확인한다.

        열린 DWG를 수정만 하고 저장하지 않은 경우 파일 mtime은 바뀌지 않으므로
        디스크 시각만으로 기존 캐시의 신선도를 판단하면 오래된 층 목록을 다시 불러올 수 있다.
        """
        try:
            doc = self.cad_bridge.doc
            if doc is None:
                return False
            saved = getattr(doc, "Saved", None)
            if saved is not None:
                return not bool(saved)
        except Exception:
            pass
        return False

    @staticmethod
    def _format_cad_import_diagnostics(bridge_diag: dict, room_diag: dict, cache_mode: str = "새로 읽음") -> str:
        lines = []
        lines.append(f"캐시: {cache_mode}")
        lines.append(f"도면구분 레이어: {bridge_diag.get('drawing_layer', '')}")
        lines.append(f"도곽 폴리선 후보: {bridge_diag.get('frame_sources', 0)}개")
        lines.append(f"판정된 도곽: {bridge_diag.get('detected_divisions', 0)}개")
        lines.append(f"도면구분 제목문자: {bridge_diag.get('drawing_title_texts', 0)}개")
        if bridge_diag.get('frame_selection_mode'):
            lines.append(f"도곽 객체수집 방식: {bridge_diag.get('frame_selection_mode')}")
        if 'critical_layer_candidates' in bridge_diag:
            lines.append(
                "핵심레이어 보강: "
                f"전체후보 {bridge_diag.get('critical_layer_candidates', 0)}개 / "
                f"도곽내 {bridge_diag.get('critical_frame_matches', 0)}개 / "
                f"Crossing 누락보강 {bridge_diag.get('critical_merge_added', 0)}개"
            )
        if bridge_diag.get('critical_merge_error'):
            lines.append(f"핵심레이어 보강 오류: {bridge_diag.get('critical_merge_error')}")
        poly_types = bridge_diag.get('source_polyline_types') or {}
        if poly_types:
            lines.append("폴리선 원본형식: " + " / ".join(f"{k} {v}개" for k, v in sorted(poly_types.items())))
        selected_types = bridge_diag.get('selected_types') or []
        lines.append("선택 도서: " + (", ".join(selected_types) if selected_types else "없음"))
        details = bridge_diag.get('detected_frame_details') or []
        if details:
            lines.append("")
            lines.append("[도곽 판정]")
            for idx, frame in enumerate(details, 1):
                floors = ",".join(frame.get('floors') or []) or "미구분"
                title = frame.get('title') or "(제목 없음)"
                lines.append(f"{idx}. {frame.get('drawing_type') or '미구분'} / {floors} / {title} / H={frame.get('handle','')}")
        lines.append("")
        lines.append(f"실구분선 총계: {room_diag.get('boundary_total', 0)}개")
        lines.append(f"실명블록 총계: {room_diag.get('tag_total', 0)}개")
        frame_rows = room_diag.get('frames') or []
        if frame_rows:
            lines.append("[선택 도곽별 인식]")
            for frame in frame_rows:
                floors = ",".join(frame.get('floors') or []) or "미구분"
                lines.append(
                    f"{floors}: 실구분선 {frame.get('boundary_count',0)}개 / 실명블록 {frame.get('tag_count',0)}개 / {frame.get('title') or '(제목 없음)'}"
                )
        floor_counts = room_diag.get('room_floor_counts') or {}
        lines.append("")
        lines.append("[최종 실 목록]")
        if floor_counts:
            for floor, count in floor_counts.items():
                lines.append(f"{floor}: {count}개")
        else:
            lines.append("없음")
        lines.append(f"최종 RoomRecord: {room_diag.get('room_total', 0)}개")
        lines.append(f"수집 원본 객체: {bridge_diag.get('selected_source_count', 0)}개 / 중복제외 {bridge_diag.get('duplicate_source_count', 0)}개")
        return "\n".join(lines)

    def _after_cad_connected(self, parent=None):
        parent = parent or self
        if not self.cad_bridge.connected or not self.cad_bridge.info:
            return
        source_path = self.cad_bridge.info.document_path or self.cad_bridge.info.document_name
        cad_dirty = self._active_cad_has_unsaved_changes()
        # 저장되지 않은 CAD 변경은 파일 mtime에 반영되지 않는다. 이 상태에서 기존 캐시를
        # 재사용하면 방금 추가한 B1/B2 도곽이나 실구분선이 통째로 사라져 보일 수 있으므로 재사용 금지.
        has_data = False if cad_dirty else project_cache_exists(source_path)
        has_background = False
        if not cad_dirty:
            try:
                has_background = bool(load_background_cache(
                    source_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style,
                    self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
                ))
            except Exception:
                has_background = False

        if cad_dirty:
            self.status_label.setText("CAD에 저장되지 않은 변경 감지 · 기존 캐시 미사용")

        if not has_data and not has_background:
            self._prompt_cad_book_read(parent)
            return

        dialog = QDialog(parent)
        dialog.setWindowTitle("기존 CAD 캐시")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("이 CAD 도면의 기존 읽기 결과가 있습니다. 재사용할 항목을 선택하십시오."))
        data_check = QCheckBox("연동 데이터 캐시 사용 (실 목록/재료문자/도곽 판정)")
        data_check.setChecked(has_data); data_check.setEnabled(has_data)
        background_check = QCheckBox("CAD 배경 캐시 사용")
        background_check.setChecked(has_background); background_check.setEnabled(has_background)
        layout.addWidget(data_check); layout.addWidget(background_check)
        buttons = QDialogButtonBox()
        use_btn = buttons.addButton("기존 캐시 사용", QDialogButtonBox.AcceptRole)
        fresh_btn = buttons.addButton("새로 읽기", QDialogButtonBox.RejectRole)
        layout.addWidget(buttons)
        use_btn.clicked.connect(dialog.accept); fresh_btn.clicked.connect(dialog.reject)
        if dialog.exec() == QDialog.Accepted:
            data_loaded = self._load_cad_project_cache(source_path, data_check.isChecked(), background_check.isChecked())
            if data_loaded:
                return
        self._prompt_cad_book_read(parent)

    def open_cad_sync(self):
        dialog = CadSyncDialog(self._cad_sync_values(), self)
        foreground = self.cad_bridge.foreground_window_title()
        existing_document = ""
        try:
            if self.active_project is not None:
                self.active_project = self.project_manager.get(self.active_project.project_id)
                existing_document = str(self.active_project.cad_link_document or "").strip()
        except Exception:
            existing_document = ""
        dialog.set_existing_link(existing_document)
        if self.cad_bridge.connected and self.cad_bridge.info:
            info = self.cad_bridge.info
            cad_text = f"{info.application_name} {info.application_version} · {info.document_name} · {info.document_path}"
            dialog.set_connection_info(True, foreground, cad_text)
            self._check_connected_cad_project(dialog, show_warning=False)
        else:
            dialog.set_connection_info(False, foreground, "")
        dialog.connection_requested.connect(lambda: self.connect_active_cad(dialog))
        dialog.existing_link_open_requested.connect(lambda: self.open_existing_linked_cad(dialog))
        dialog.disconnection_requested.connect(lambda: self.disconnect_cad(dialog))
        dialog.registration_requested.connect(lambda: self.register_connected_cad_project(dialog))
        dialog.import_requested.connect(lambda values: self.handle_cad_import(dialog, values))
        dialog.export_requested.connect(lambda values: self.handle_cad_export(dialog, values))
        dialog.create_layers_requested.connect(lambda: self.create_cad_sync_layers(dialog))
        dialog.import_blocks_requested.connect(lambda: self.import_cad_blocks(dialog))
        dialog.apply_library_blocks_requested.connect(lambda: self.apply_library_blocks_to_cad(dialog))
        dialog.finished.connect(lambda _result: self._save_cad_sync_values(dialog.values()))
        dialog.exec()

    def _connect_active_cad_common(self, dialog=None):
        """ActiveDocument 연결 후 저장된 식별 Handle 객체 하나만 확인한다.

        도곽·실·재료·배경 내용은 가져오지 않으며 사용자가 CAD 도서 읽기를 눌렀을 때만 읽는다.
        """
        self._save_current_cad_project_cache()
        info = self.cad_bridge.connect_to_active(require_foreground=False)
        current = info.document_path or info.document_name
        self._switch_to_cad_mode(current)
        self._save_recent_cad(info)
        self.rooms_tab.set_cad_connected_mode(True)
        self.status_label.setText(f"CAD 연결됨: {current}")
        self._update_window_title()
        if dialog is not None:
            document_path = info.document_path or "(저장 전 도면 또는 경로 확인 불가)"
            cad_text = f"{info.application_name} {info.application_version} · {info.document_name} · {document_path}"
            dialog.set_connection_info(True, info.foreground_title, cad_text)
        self._check_connected_cad_project(dialog, show_warning=True)
        return info

    def _check_connected_cad_project(self, dialog=None, show_warning: bool = True) -> dict:
        if not self.cad_bridge.connected:
            result = {"status": "unknown", "text": "CAD가 연결되지 않았습니다."}
            if dialog is not None:
                dialog.set_project_match_info(result["status"], result["text"])
            return result
        if self.active_project is None:
            result = {"status": "unknown", "text": "열린 프로젝트가 없습니다."}
            if dialog is not None:
                dialog.set_project_match_info(result["status"], result["text"])
            self.status_label.setText("CAD 연결됨 · 열린 프로젝트 없음")
            return result
        try:
            self.active_project = self.project_manager.get(self.active_project.project_id)
            result = self.cad_bridge.project_link_status(
                self.active_project.cad_link_handle,
                self.active_project.project_id,
                self.active_project.cad_link_token,
                self._cad_link_layer(),
            )
            status = result.get("status", "unknown")
            detail = result.get("text", "CAD 식별 결과 없음")
            if dialog is not None:
                dialog.set_project_match_info(status, detail)
            if status == "same":
                self.status_label.setText(f"CAD 연결됨 · 기존 프로젝트 도면 확인: {detail}")
            elif status == "unlinked":
                self.status_label.setText("CAD 연결됨 · 미등록 도면 (저장 버튼으로 연동표시 등록)")
            else:
                self.status_label.setText(f"CAD 도면 연동표시 불일치: {detail}")
                if show_warning:
                    QMessageBox.warning(
                        dialog or self,
                        "프로젝트 도면 확인",
                        "현재 CAD 도면에서 이 프로젝트의 연동표시를 확인하지 못했습니다.\n\n"
                        f"{detail}\n\n저장 버튼을 누르면 현재 CAD를 이 프로젝트 도면으로 다시 등록합니다.",
                    )
            return result
        except Exception as exc:
            detail = f"도면 비교 실패: {exc}"
            if dialog is not None:
                dialog.set_project_match_info("unknown", detail)
            self.status_label.setText(detail)
            return {"status": "unknown", "text": detail}

    def _refresh_connected_active_document(self):
        """현재 연결 앱의 ActiveDocument만 재확인하여 브리지 참조를 최신화한다.

        도면 내용을 스캔하지 않으며 시작창/수동 연동 경로 차이를 없애기 위한 가벼운 재바인딩이다.
        """
        if not self.cad_bridge.app:
            return self._connect_active_cad_common()
        try:
            doc = self.cad_bridge.app.ActiveDocument
            if doc is None:
                raise CadConnectionError("ZWCAD에 열린 활성 도면이 없습니다.")
            name = str(getattr(doc, "Name", "") or "")
            full_name = str(getattr(doc, "FullName", "") or "")
            path = full_name or str(Path(str(getattr(doc, "Path", "") or "")) / name)
            old = self.cad_bridge.info
            info_type = type(old) if old is not None else None
            if info_type is None:
                return self._connect_active_cad_common()
            self.cad_bridge.doc = doc
            self.cad_bridge.info = info_type(
                old.application_name, old.application_version, name, path, old.foreground_title
            )
            current = path or name
            self._save_recent_cad(self.cad_bridge.info)
            self.rooms_tab.set_cad_connected_mode(True)
            self.status_label.setText(f"CAD 연결됨: {current}")
            self._update_window_title()
            return self.cad_bridge.info
        except CadConnectionError:
            raise
        except Exception as exc:
            raise CadConnectionError(f"활성 CAD 도면 재확인 실패: {exc}") from exc

    def open_existing_linked_cad(self, dialog=None):
        """현재 프로젝트에 저장된 cad_link_document를 열고 바로 연결한다.

        프로젝트 열기 자체에서는 CAD를 자동 실행하지 않는다. 사용자가 CAD 연동 창의
        [기존 연동된 CAD 열기]를 눌렀을 때만 이 경로가 실행된다.
        """
        if self.active_project is None:
            QMessageBox.information(dialog or self, "기존 연동된 CAD 열기", "먼저 프로젝트를 열어 주세요.")
            return
        try:
            self.active_project = self.project_manager.get(self.active_project.project_id)
            document_path = str(self.active_project.cad_link_document or "").strip()
        except Exception as exc:
            QMessageBox.warning(dialog or self, "기존 연동된 CAD 열기", f"프로젝트 연동정보를 읽지 못했습니다.\n\n{exc}")
            return
        if not document_path:
            QMessageBox.information(dialog or self, "기존 연동된 CAD 열기", "이 프로젝트에 저장된 CAD 연동 경로가 없습니다.")
            return
        if not Path(document_path).is_file():
            QMessageBox.warning(
                dialog or self, "기존 연동된 CAD 열기",
                f"저장된 CAD 파일을 찾지 못했습니다.\n\n{document_path}",
            )
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.status_label.setText(f"기존 연동 CAD 여는 중: {document_path}")
            QApplication.processEvents()
            info = self.cad_bridge.open_document(document_path)
            current = info.document_path or info.document_name
            self._switch_to_cad_mode(current)
            self._save_recent_cad(info)
            self.rooms_tab.set_cad_connected_mode(True)
            self._update_window_title()
            if dialog is not None:
                cad_text = f"{info.application_name} {info.application_version} · {info.document_name} · {current}"
                dialog.set_connection_info(True, info.foreground_title, cad_text)
                dialog.set_existing_link(document_path)
            result = self._check_connected_cad_project(dialog, show_warning=False)
            if result.get("status") == "same":
                self.status_label.setText(f"기존 연동 CAD 열기 완료 · 프로젝트 도면 확인 · {current}")
            else:
                self.status_label.setText(f"기존 연동 CAD 열기 완료 · 연동표시 확인 필요 · {current}")
        except CadConnectionError as exc:
            self.cad_bridge.disconnect()
            self.rooms_tab.set_cad_connected_mode(False)
            if dialog is not None:
                dialog.set_connection_info(False, self.cad_bridge.foreground_window_title(), "")
            self._update_window_title()
            QMessageBox.warning(dialog or self, "기존 연동된 CAD 열기", str(exc))
        except Exception:
            self.cad_bridge.disconnect()
            self.rooms_tab.set_cad_connected_mode(False)
            if dialog is not None:
                dialog.set_connection_info(False, self.cad_bridge.foreground_window_title(), "")
            self._update_window_title()
            QMessageBox.critical(dialog or self, "기존 연동된 CAD 열기 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()

    def connect_active_cad(self, dialog):
        try:
            # 시작창 연동과 동일한 공통 연결 루틴을 사용한다. 별도 연결 확인창은 띄우지 않는다.
            self._connect_active_cad_common(dialog)
        except CadConnectionError as exc:
            self.cad_bridge.disconnect()
            dialog.set_connection_info(False, self.cad_bridge.foreground_window_title(), "")
            QMessageBox.warning(dialog, "CAD 연결", str(exc))
        except Exception:
            self.cad_bridge.disconnect()
            dialog.set_connection_info(False, self.cad_bridge.foreground_window_title(), "")
            QMessageBox.critical(dialog, "CAD 연결 오류", traceback.format_exc())

    def disconnect_cad(self, dialog):
        self.cad_bridge.disconnect()
        self.rooms_tab.set_cad_connected_mode(False)
        dialog.set_connection_info(False, self.cad_bridge.foreground_window_title(), "")
        self._update_window_title()
        self.status_label.setText("CAD 연결 해제")

    def _background_dxf_source_path(self) -> str:
        """벡터/PNG 배경의 DXF 원본은 실제 적용된 DXF를 최우선으로 사용한다."""
        candidates = []
        # 1) 사용자가 도면 적용으로 선택한 실제 DXF
        applied = str(getattr(self, "applied_dxf_path", "") or "").strip()
        if applied:
            candidates.append(Path(applied))

        # 2) 현재 작업 원본 자체가 DXF인 경우
        if self.current_path:
            candidates.append(Path(self.current_path))

        # 3) 현재 프로젝트에 등록된 기본도면
        try:
            if self.active_project is not None:
                refreshed = self.project_manager.get(self.active_project.project_id)
                self.active_project = refreshed
                if refreshed.base_drawing_path:
                    candidates.append(Path(refreshed.base_drawing_path))
        except Exception:
            pass

        # 4) 이전 버전 호환용 임시 DXF는 마지막 수단으로만 사용한다.
        candidates.append(PROJECT_BACKGROUND_DXF_PATH)

        seen = set()
        for candidate in candidates:
            try:
                key = str(candidate.resolve()).casefold()
            except Exception:
                key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_file() and candidate.suffix.lower() == ".dxf":
                    return str(candidate)
            except Exception:
                continue
        return ""

    def _load_vector_background_cache(self, cache_key: str | None = None, base_dxf_path: str | None = None):
        """현재 프로젝트의 도곽 단위 벡터 캐시를 평면 Preview에 연결한다.

        CAD 연동(DWG)으로 만든 캐시는 DWG 자체를 원본 서명으로 저장한다.
        프로젝트 폴더에 우연히 존재하는 DXF를 서명 원본으로 대입하면 정상 캐시도
        오래된 것으로 판정되어 즉시 버려질 수 있으므로, DXF 직접 작업일 때만
        base DXF 서명 검사를 사용한다.
        """
        key = str(cache_key or self.current_path or "")
        if not key:
            self.rooms_tab.set_vector_background_cache({})
            self.rooms_tab.set_vector_display_stage("none")
            return None

        key_suffix = Path(key).suffix.lower()
        base = str(base_dxf_path or "")
        if not base and key_suffix == ".dxf":
            base = str(self._background_dxf_source_path() or key)

        # CAD 연동(DWG 등)에서는 DXF를 베이스도면으로 잘못 연결하지 않는다.
        try:
            self.rooms_tab.set_base_dxf_path(base if key_suffix == ".dxf" else "")
        except Exception:
            pass

        signature_source = base if (key_suffix == ".dxf" and base) else None
        cached = load_background_source_cache(
            BACKGROUND_CACHE_ROOT, key, signature_source_path=signature_source
        )
        if cached and cached.get("floors"):
            # V24.08: 원본 참조 캐시는 그대로 보존하고, 사용자가 [도면 정리]에서
            # 만든 프로그램용 재생성 캐시가 있으면 그쪽을 화면에 우선 표시한다.
            program_cached = load_background_program_cache(BACKGROUND_CACHE_ROOT, key)
            if program_cached and program_cached.get("floors"):
                display_selection = dict(program_cached.get("display_selection") or {})
                program_floors = (
                    filter_program_floors(program_cached, display_selection, cached)
                    if display_selection else (program_cached.get("floors") or {})
                )
                self.rooms_tab.set_vector_background_cache(program_floors)
                self.rooms_tab.set_vector_display_stage("optimized")
                return {**cached, "program_cache": program_cached, "floors": program_floors}
            self.rooms_tab.set_vector_background_cache(cached.get("floors") or {})
            self.rooms_tab.set_vector_display_stage("raw")
            return cached
        self.rooms_tab.set_vector_background_cache({})
        self.rooms_tab.set_vector_display_stage("none")
        return None

    def _ensure_base_dxf_vector_cache(self, base_dxf_path: str):
        """베이스 DXF의 도곽 내부 벡터/창호 후보 캐시가 없거나 오래됐으면 1회 생성한다."""
        base = str(base_dxf_path or "")
        if not base or not Path(base).is_file() or Path(base).suffix.lower() != ".dxf" or not self.current_path:
            return None
        cached = load_background_source_cache(
            BACKGROUND_CACHE_ROOT, self.current_path, signature_source_path=base
        )
        if cached and cached.get("floors"):
            self.rooms_tab.set_base_dxf_path(base)
            self.rooms_tab.set_vector_background_cache(cached.get("floors") or {})
            self.rooms_tab.set_vector_display_stage("raw")
            return cached
        self.status_label.setText("베이스 DXF 도곽 벡터 캐시 생성 중...")
        QApplication.processEvents()
        doc = ezdxf.readfile(base)
        raw_divisions = read_drawing_divisions(doc.modelspace(), self.room_rules)
        # V24.07.2: 자동 베이스 캐시 경로도 현재 저장된 도곽/층 지정만 단독으로 쓰지 않는다.
        # 실제 DXF에서 검출한 평면도 도곽 전체를 유지하고, 일치하는 도곽에만 저장 지정값을 덮는다.
        saved_by_key = {self._drawing_frame_key(v): dict(v) for v in (self.current_drawing_divisions or []) if self._drawing_frame_key(v)}
        divisions = []
        used_keys = set()
        for raw in raw_divisions:
            frame = dict(raw)
            key = self._drawing_frame_key(frame)
            saved = saved_by_key.get(key)
            if saved:
                for name in ("drawing_type", "assigned_input", "title", "floors", "floor"):
                    value = saved.get(name)
                    if value not in (None, "", []):
                        frame[name] = list(value) if name == "floors" else value
                used_keys.add(key)
            divisions.append(frame)
        for saved in (self.current_drawing_divisions or []):
            key = self._drawing_frame_key(saved)
            if key and key not in used_keys and not any(self._drawing_frame_key(v) == key for v in divisions):
                divisions.append(dict(saved))
        rects = plan_frame_rects(divisions)
        if not rects:
            raise RuntimeError("베이스 DXF에서 평면 도곽을 찾지 못했습니다. 도곽 밖 전체 도면은 읽지 않습니다.")
        background, stats = read_cad_background(
            doc, self._background_exclude_patterns(), [], crop_rects=rects
        )
        texts = stats.get("texts", [])
        annotations = stats.get("annotation_segments", [])
        fills = stats.get("fills", [])
        openings = build_opening_candidates_by_floor(doc, divisions, self.current_rooms, (4, 5))
        save_background_source_cache(
            BACKGROUND_CACHE_ROOT, self.current_path, background, divisions, texts, annotations,
            rooms=self.current_rooms, opening_candidates=openings, signature_source_path=base, fills=fills
        )
        cached = load_background_source_cache(
            BACKGROUND_CACHE_ROOT, self.current_path, signature_source_path=base
        )
        self.rooms_tab.set_base_dxf_path(base)
        self.rooms_tab.set_vector_background_cache((cached or {}).get("floors") or {})
        self.rooms_tab.set_vector_display_stage("raw")
        return cached

    @staticmethod
    def _drawing_frame_key(frame: dict) -> str:
        handle = str(frame.get("handle", "") or "").strip().upper()
        if handle:
            return f"H:{handle}"
        points = list(frame.get("points") or [])
        if not points:
            return ""
        xs = [float(p[0]) for p in points]; ys = [float(p[1]) for p in points]
        return f"B:{min(xs):.3f},{min(ys):.3f},{max(xs):.3f},{max(ys):.3f}"

    def _drawing_assignment_path(self, source_path: str) -> Path:
        key = str(source_path or "현재도면").strip()
        try:
            key = str(Path(key).resolve())
        except Exception:
            pass
        digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:12]
        root = TEMP_PROJECT_CACHE_DIR / "도곽도서지정"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{digest}.json"

    def _load_drawing_assignments(self, source_path: str) -> dict:
        path = self._drawing_assignment_path(source_path)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload.get("frames") or {})
        except Exception:
            return {}

    def _save_drawing_assignments(self, source_path: str, frames: list[dict]):
        path = self._drawing_assignment_path(source_path)
        data = {}
        for frame in frames or []:
            key = self._drawing_frame_key(frame)
            if not key:
                continue
            data[key] = {
                "drawing_type": str(frame.get("drawing_type", "") or ""),
                "assigned_input": str(frame.get("assigned_input", "") or ""),
                "title": str(frame.get("title", "") or ""),
                "floors": list(frame.get("floors") or []),
                "floor": str(frame.get("floor", "") or ""),
            }
        payload = {"source": str(source_path or ""), "frames": data}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _drawing_frame_needs_assignment(frame: dict) -> bool:
        dtype = str(frame.get("drawing_type", "") or "").strip()
        if dtype in {"", "미구분"}:
            return True
        if dtype in {"평면도", "창호도"}:
            return not bool(frame.get("floors") or drawing_input_from_title(dtype, frame.get("title", ""), []))
        if dtype == "입면도":
            return not bool(drawing_input_from_title(dtype, frame.get("title", ""), []))
        if dtype == "단면도":
            return not bool(drawing_input_from_title(dtype, frame.get("title", ""), []))
        return False

    def _highlight_drawing_frame_in_cad(self, frame: dict):
        """도곽 지정 목록의 현재 행을 CAD에서 강조한다. CAD 연동 중일 때만 호출한다."""
        if not self.cad_bridge.connected:
            return
        handle = str(frame.get("handle", "") or "").strip().upper()
        if not handle:
            return
        self.cad_bridge.highlight_handle(handle)

    def _clear_drawing_frame_highlight(self):
        if not self.cad_bridge.connected:
            return
        self.cad_bridge.clear_highlight()

    def _focus_drawing_frame_in_cad(self, frame: dict):
        """도곽 지정 목록의 현재 행을 실제 CAD 화면에서 찾을 수 있도록 줌한다."""
        if not self.cad_bridge.connected:
            raise CadConnectionError("CAD가 연동되어 있지 않습니다.")
        self.cad_bridge.focus_frame(frame)

    def _write_drawing_frame_titles_to_cad(self, frames: list[dict]) -> dict:
        """적용 도곽 명칭이 있는 행을 도곽 좌상단 안쪽에 같은 도면구분 레이어 TEXT로 기입한다."""
        if not self.cad_bridge.connected:
            raise CadConnectionError("CAD가 연동되어 있지 않습니다.")
        layer = str(self.room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer")) or "").strip()
        if not layer:
            raise CadConnectionError("도면구분 레이어가 지정되지 않았습니다.")
        return self.cad_bridge.write_drawing_frame_titles(frames, layer)

    def _resolve_drawing_divisions(self, frames: list[dict]):
        """도곽에 구분/층명이 빠졌을 때 전체 도곽 목록을 띄워 프로그램 도곽 객체를 완성한다."""
        source = ""
        try:
            if self.cad_bridge.connected and self.cad_bridge.info is not None:
                source = self.cad_bridge.info.document_path or self.cad_bridge.info.document_name
        except Exception:
            source = self.current_path
        source = source or self.current_path or "현재도면"

        saved = self._load_drawing_assignments(source)
        merged = []
        raw_missing = False
        for original in frames or []:
            frame = dict(original)
            needs = self._drawing_frame_needs_assignment(frame)
            raw_missing = raw_missing or needs
            if needs:
                row = saved.get(self._drawing_frame_key(frame)) or {}
                if row:
                    frame["drawing_type"] = str(row.get("drawing_type", frame.get("drawing_type", "")) or "")
                    frame["assigned_input"] = str(row.get("assigned_input", "") or "")
                    frame["title"] = str(row.get("title", frame.get("title", "")) or "")
                    frame["floors"] = list(row.get("floors") or [])
                    frame["floor"] = str(row.get("floor", "") or "")
            merged.append(frame)

        # 원본에 누락이 있더라도 이미 프로그램 도곽지정이 저장되어 완성되어 있으면 다시 묻지 않는다.
        unresolved = any(self._drawing_frame_needs_assignment(frame) for frame in merged)
        if not raw_missing and not unresolved:
            return merged
        if raw_missing and not unresolved:
            return merged

        # 도서읽기 전체는 WaitCursor 상태지만, 이 창은 사용자의 입력을 기다리는 UI이므로
        # 표시 중에는 일반 커서로 되돌린다. 확인 후 실제 도서읽기가 재개될 때만 WaitCursor를 복원한다.
        override = QApplication.overrideCursor()
        had_wait_cursor = bool(override is not None and override.shape() == Qt.WaitCursor)
        if had_wait_cursor:
            QApplication.restoreOverrideCursor()
        try:
            cad_highlight = self._highlight_drawing_frame_in_cad if self.cad_bridge.connected else None
            cad_clear = self._clear_drawing_frame_highlight if self.cad_bridge.connected else None
            cad_focus = self._focus_drawing_frame_in_cad if self.cad_bridge.connected else None
            cad_write_titles = self._write_drawing_frame_titles_to_cad if self.cad_bridge.connected else None
            assignment_dialog = DrawingDivisionAssignmentDialog(
                merged, self,
                highlight_callback=cad_highlight,
                clear_highlight_callback=cad_clear,
                focus_callback=cad_focus,
                write_titles_callback=cad_write_titles,
            )
            if assignment_dialog.exec() != QDialog.Accepted:
                return None
            resolved = assignment_dialog.resolved_frames()
            self._save_drawing_assignments(source, resolved)
            return resolved
        finally:
            self._clear_drawing_frame_highlight()
            if had_wait_cursor:
                QApplication.setOverrideCursor(Qt.WaitCursor)

    def handle_cad_import(self, dialog, values: dict):
        self._save_cad_sync_values(values)
        if values.get("drawing_plan") is False:
            QMessageBox.information(dialog, "CAD 도서 읽기", "현재 버전에서는 평면도만 읽을 수 있습니다.")
            return
        if not self.cad_bridge.connected or not self.cad_bridge.info:
            QMessageBox.information(dialog, "CAD → 프로그램", "먼저 현재 활성 CAD를 확인하고 연결하십시오.")
            return
        # 목록에서 사용자가 바꾼 구분을 새 CAD 읽기 전에 먼저 캐시에 확정한다.
        self._save_current_cad_project_cache()
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            # 시작창 연동/수동 연동 어느 경로든 도서읽기 직전에 현재 ActiveDocument를 재확인한다.
            self._refresh_connected_active_document()
            info = self.cad_bridge.info
            category_source_path = info.document_path or info.document_name
            category_snapshots = load_room_category_snapshots(category_source_path)
            # V18.8: CAD 연동에서는 실/도면구분 등 업무 데이터만 읽는다.
            # 전체 배경 객체는 COM으로 수집하지 않고 프로젝트 폴더의 원본CAD.dxf에서 읽는다.
            cad_read_values = dict(values)
            cad_read_values["import_background"] = False
            doc, counts, skipped = self.cad_bridge.build_ezdxf_document(
                cad_read_values, self.room_rules, division_resolver=self._resolve_drawing_divisions
            )
            self.current_drawing_divisions = list(self.cad_bridge.last_selected_divisions or [])
            if values.get("import_material_list", True):
                records = list(iter_text_records(doc))
                result = aggregate(records, self.classifier)
            else:
                records = []
                result = aggregate([], self.classifier)
            rooms, errors = read_rooms(doc, self.room_rules, self.cad_bridge.last_selected_divisions)
            restored_category_count = restore_room_categories(rooms, category_snapshots)
            errors.extend(validate_room_numbers(rooms))
            room_diag = build_room_import_diagnostics(
                doc.modelspace(), self.room_rules, self.cad_bridge.last_selected_divisions, rooms
            )
            bridge_diag = dict(self.cad_bridge.last_import_diagnostics or {})
            diagnostic_text = self._format_cad_import_diagnostics(bridge_diag, room_diag, "새로 읽음")
            try:
                TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                (TEMP_PROJECT_CACHE_DIR / "CAD도서읽기_진단.txt").write_text(diagnostic_text, encoding="utf-8")
            except Exception:
                pass

            self.current_path = info.document_path or info.document_name
            self._save_recent_cad(info)
            self.current_result = result
            self.current_rooms = rooms
            self.current_room_errors = errors
            self.path_label.setText(f"CAD 연결: {self.current_path}")
            for name, rows in result.items():
                self.material_tab.set_category_rows(name, rows)
            self.rooms_tab.set_source_path(self.current_path)
            self.rooms_tab.set_data(rooms, errors)
            # DWG 연결 상태에서는 기존 '수정내용 DXF 저장' 대신 CAD 내보내기를 사용한다.
            self.rooms_tab.set_cad_connected_mode(True)
            self.export_btn.setEnabled(True)
            self.refresh_main_tab_titles()

            background_text = "배경 제외"
            background_dxf = self._background_dxf_source_path()
            self.rooms_tab.set_base_dxf_path(background_dxf)

            # V24.38: 사용자가 CAD 도서를 "새로 읽음"으로 다시 읽은 경우에는
            # 이전 3단 최적화 캐시/그룹 표시상태/최적화 선택상태를 재사용하지 않는다.
            # 원본(2단) 캐시는 그대로 두고 파생된 프로그램 최적화 캐시만 폐기하여
            # 재읽기 직후에는 항상 원본 정리 도면부터 표시한다.
            try:
                clear_background_program_cache(BACKGROUND_CACHE_ROOT, self.current_path)
            except Exception:
                pass
            try:
                self.rooms_tab.set_vector_display_stage("raw")
            except Exception:
                pass

            self._load_vector_background_cache(self.current_path, background_dxf)
            if values.get("import_background"):
                # V18.8: 배경 캐시는 CAD 연결 도면 경로를 키로 계속 재사용하되,
                # 실제 배경 원본 읽기는 프로젝트 폴더의 원본CAD.dxf만 사용한다.
                cache_files = load_background_cache(
                    self.current_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style,
                    self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
                )
                if cache_files:
                    self.rooms_tab.set_background_cache_files(cache_files)
                    self.rooms_tab.set_background_ready(True)
                    background_text = "기존 배경 캐시 사용"
                elif self.background_render_mode == "vector" and self.cad_bridge.connected:
                    # V24.04.1: CAD 연동 상태의 벡터 배경은 적용 DXF보다 현재 연결 CAD를 우선한다.
                    # 그래야 CAD가 실제로 로드해 표시 중인 XREF/블록 정의까지 직접 읽을 수 있다.
                    self.start_cad_background_scan(
                        values, rooms=rooms, preserve_existing=False, refresh_source=True
                    )
                    background_text = "연결 CAD 벡터 배경 백그라운드 생성 중"
                elif self.background_render_mode == "vector" and background_dxf:
                    self.start_background_scan(
                        background_dxf, cache_source_path=self.current_path, rooms=rooms,
                        preserve_existing=False, refresh_source=True,
                    )
                    background_text = "적용 DXF 벡터 배경 백그라운드 생성 중"
                elif self.cad_bridge.connected:
                    self.start_cad_background_scan(
                        values, rooms=rooms, preserve_existing=False, refresh_source=True
                    )
                    background_text = "연결 CAD 배경 백그라운드 생성 중"
                elif background_dxf:
                    self.start_background_scan(
                        background_dxf, cache_source_path=self.current_path, rooms=rooms,
                        preserve_existing=False, refresh_source=True,
                    )
                    background_text = "프로젝트 DXF 배경 백그라운드 생성 중"
                else:
                    background_text = "배경 원본 없음"
                    self.rooms_tab.set_background_ready(False)
                self._reuse_background_cache_once = False
            elif not values.get("import_background"):
                cache_files = load_background_cache(
                    self.current_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style,
                    self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
                ) if self.current_path else None
                if cache_files:
                    self.rooms_tab.set_background_cache_files(cache_files)
                    self.rooms_tab.set_background_ready(True)
                    background_text = "기존 배경 캐시 사용"

            try:
                save_project_cache(
                    self.current_path, self.current_result, self.current_rooms, self.current_room_errors,
                    list(self.cad_bridge.last_selected_divisions or divisions if 'divisions' in locals() else [])
                )
            except Exception:
                pass

            summary = ", ".join(f"{key} {value:,}" for key, value in sorted(counts.items())) or "지원 객체 없음"
            if hasattr(dialog, "connection_label"):
                dialog.connection_label.setText(f"연결 상태: 가져오기 완료 · 실 {len(rooms):,}개")
            category_text = f" · 구분 캐시 {restored_category_count:,}개 복원" if restored_category_count else ""
            self.status_label.setText(f"CAD 가져오기 완료: 실 {len(rooms):,}개 / 문자 {len(records):,}개 · {background_text}{category_text}")
            floor_counts = room_diag.get("room_floor_counts") or {}
            floor_summary = " / ".join(f"{floor} {count}개" for floor, count in floor_counts.items()) or "층별 실 없음"
            summary_text = (
                f"현재 활성 도면에서 가져왔습니다.\n"
                f"{floor_summary}\n"
                f"{summary}\n"
                f"구분 캐시 복원 {restored_category_count:,}개\n"
                f"건너뜀 {skipped:,}개 / 실 오류 {len(errors):,}개"
            )
            self._show_cad_import_result_dialog(dialog, summary_text, diagnostic_text)
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "CAD 가져오기", str(exc))
        except Exception:
            QMessageBox.critical(dialog, "CAD 가져오기 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()

    def reread_materials_from_cad(self):
        """연결 CAD의 도곽 구분과 관계없이 전체 문자·블록 속성만 다시 읽는다."""
        if not self.cad_bridge.connected or not self.cad_bridge.info:
            QMessageBox.information(self, "재료 다시읽기", "먼저 현재 활성 CAD를 연결하십시오.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            info = self._refresh_connected_active_document()
            doc, counts, skipped = self.cad_bridge.build_ezdxf_document(
                {
                    "import_material_list": True,
                    "_material_only": True,
                    "import_background": False,
                    "import_selected_only": False,
                },
                self.room_rules,
            )
            records = list(iter_text_records(doc))
            result = aggregate(records, self.classifier)
            self.current_result = result
            self.current_path = info.document_path or info.document_name
            self.path_label.setText(f"CAD 연결: {self.current_path}")
            for name, rows in result.items():
                self.material_tab.set_category_rows(name, rows)
            self.apply_sort()
            self.refresh_main_tab_titles()
            self._save_current_cad_project_cache()
            summary = ", ".join(f"{key} {value:,}" for key, value in sorted(counts.items())) or "문자 없음"
            self.status_label.setText(
                f"재료 다시읽기 완료: 전체 문자·속성 {len(records):,}개 / {summary} / 건너뜀 {skipped:,}개"
            )
        except CadConnectionError as exc:
            QMessageBox.warning(self, "재료 다시읽기", str(exc))
        except Exception:
            QMessageBox.critical(self, "재료 다시읽기 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()


    def _show_copyable_error(self, parent, title: str, summary: str, detail: str, log_path=None):
        """오류 내용을 마우스 선택/Ctrl+C 및 전체복사할 수 있는 공통 오류창으로 표시한다."""
        dialog = QDialog(parent or self)
        dialog.setWindowTitle(title)
        dialog.setModal(True)
        dialog.resize(820, 560)
        dialog.setMinimumSize(640, 380)

        root = QVBoxLayout(dialog)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        summary_label = QLabel(str(summary or title))
        summary_label.setWordWrap(True)
        summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(summary_label)

        if log_path:
            path_label = QLabel(f"오류 로그: {log_path}")
            path_label.setWordWrap(True)
            path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(path_label)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.NoWrap)
        text.setPlainText(str(detail or "오류 상세가 없습니다."))
        root.addWidget(text, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_btn = QPushButton("전체 복사")
        close_btn = QPushButton("확인")
        close_btn.setDefault(True)
        buttons.addWidget(copy_btn)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

        def copy_all():
            QApplication.clipboard().setText(text.toPlainText())
            # 팝업을 즉시 닫아도 삭제된 QPushButton을 QTimer가 다시 건드리지 않도록
            # 복사 완료 문구는 이 팝업이 닫힐 때까지 그대로 유지한다.
            copy_btn.setText("복사됨")

        copy_btn.clicked.connect(copy_all)
        close_btn.clicked.connect(dialog.accept)
        dialog.exec()

    def _show_cad_import_result_dialog(self, parent, summary_text: str, diagnostic_text: str):
        """CAD 도서읽기 결과를 기본부터 넓게 표시하고, 상세 내용은 필요한 높이만 사용한다."""
        box = QDialog(parent)
        box.setWindowTitle("CAD → 프로그램")
        box.setModal(True)
        box.setMinimumWidth(640)
        box.resize(680, 210)

        root = QVBoxLayout(box)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        top = QHBoxLayout()
        icon = QLabel("ⓘ")
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setFixedWidth(42)
        icon.setStyleSheet("font-size: 34px; color: #1683d8; font-weight: bold;")
        top.addWidget(icon)

        summary_label = QLabel(summary_text)
        summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_label.setWordWrap(False)
        summary_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        top.addWidget(summary_label, 1)
        root.addLayout(top)

        hint = QLabel("층 누락이 있으면 [자세히 보기]에서 도곽별 실구분선 개수를 확인할 수 있습니다.")
        hint.setWordWrap(True)
        root.addWidget(hint)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setPlainText(diagnostic_text or "진단 내용 없음")
        details.setLineWrapMode(QTextEdit.NoWrap)
        line_count = max(1, (diagnostic_text or "").count("\n") + 1)
        detail_height = min(300, max(130, line_count * 18 + 18))
        details.setFixedHeight(detail_height)
        details.hide()
        root.addWidget(details)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_btn = QPushButton("내용 복사")
        detail_btn = QPushButton("자세히 보기")
        ok_btn = QPushButton("확인")
        ok_btn.setDefault(True)
        buttons.addWidget(copy_btn)
        buttons.addWidget(detail_btn)
        buttons.addWidget(ok_btn)
        root.addLayout(buttons)

        def copy_all():
            text = summary_text
            if diagnostic_text:
                text += "\n\n" + diagnostic_text
            QApplication.clipboard().setText(text)
            # 닫힌 팝업의 삭제된 버튼을 지연 타이머가 다시 접근하지 않게 한다.
            copy_btn.setText("복사됨")

        def toggle_details():
            showing = details.isVisible()
            details.setVisible(not showing)
            detail_btn.setText("자세히 숨기기" if not showing else "자세히 보기")
            if not showing:
                box.resize(max(box.width(), 760), box.sizeHint().height())
            else:
                box.resize(max(640, box.width()), box.minimumSizeHint().height())

        copy_btn.clicked.connect(copy_all)
        detail_btn.clicked.connect(toggle_details)
        ok_btn.clicked.connect(box.accept)
        box.exec()

    def create_cad_sync_layers(self, dialog):
        if not self.cad_bridge.connected:
            QMessageBox.information(dialog, "연동 레이어 생성", "먼저 현재 활성 CAD를 확인하고 연결하십시오.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            report = self.layer_service.ensure_configured_layers(self.room_rules)
            created = ", ".join(report["created"]) or "없음"
            existing = ", ".join(report["existing"]) or "없음"
            lines = [f"생성: {created}", f"중복 건너뜀: {existing}"]
            if report["failed"]:
                lines.append("실패: " + " / ".join(report["failed"]))
            dialog.connection_label.setText("연결 상태: 연동 레이어 생성 완료")
            self.status_label.setText("연동 레이어 생성 완료")
            QMessageBox.information(dialog, "연동 레이어 생성", "\n".join(lines))
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "연동 레이어 생성", str(exc))
        except Exception:
            QMessageBox.critical(dialog, "연동 레이어 생성 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()


    @staticmethod
    def _make_block_preview_pixmap(geometry, width=220, height=125):
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor("#202830"))
        geometry = geometry or []

        bounds = []
        for shape in geometry:
            kind = shape.get("type")
            if kind in ("line", "polyline", "solid"):
                bounds.extend(shape.get("points", []))
            elif kind in ("circle", "arc"):
                cx, cy = shape.get("center", [0, 0])
                radius = float(shape.get("radius", 0) or 0)
                bounds.extend([[cx - radius, cy - radius], [cx + radius, cy + radius]])
            elif kind == "text":
                x, y = shape.get("point", [0, 0])
                h = float(shape.get("height", 1) or 1)
                text = str(shape.get("text", "") or "")
                wf = float(shape.get("width_factor", 1) or 1)
                approx_w = max(h * 0.55 * max(len(text), 1) * wf, h * 0.6)
                bounds.extend([[x - approx_w / 2, y - h / 2], [x + approx_w / 2, y + h / 2]])

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        if not bounds:
            painter.setPen(QColor("#A0A0A0"))
            painter.drawText(pixmap.rect(), Qt.AlignCenter, "미리보기 없음")
            painter.end()
            return pixmap

        xs = [float(p[0]) for p in bounds]
        ys = [float(p[1]) for p in bounds]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 1e-6)
        span_y = max(max_y - min_y, 1e-6)
        margin = 9.0
        scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
        off_x = (width - span_x * scale) / 2 - min_x * scale
        off_y = (height - span_y * scale) / 2 + max_y * scale

        def pt(value):
            return QPointF(float(value[0]) * scale + off_x, -float(value[1]) * scale + off_y)

        # 채움 도형을 먼저 그려 선과 문자가 가려지지 않게 한다.
        for shape in geometry:
            if shape.get("type") != "solid":
                continue
            polygon = QPolygonF([pt(p) for p in shape.get("points", [])])
            if polygon.count() >= 3:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#C7C7C7"))
                painter.drawPolygon(polygon)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#E7E7E7"), 1.25))
        for shape in geometry:
            kind = shape.get("type")
            if kind in ("line", "polyline"):
                pts = shape.get("points", [])
                for i in range(len(pts) - 1):
                    painter.drawLine(pt(pts[i]), pt(pts[i + 1]))
                if kind == "polyline" and shape.get("closed") and len(pts) > 2:
                    painter.drawLine(pt(pts[-1]), pt(pts[0]))
            elif kind == "circle":
                center = pt(shape.get("center", [0, 0]))
                radius = max(0.5, float(shape.get("radius", 0) or 0) * scale)
                painter.drawEllipse(center, radius, radius)
            elif kind == "arc":
                center = pt(shape.get("center", [0, 0]))
                radius = max(0.5, float(shape.get("radius", 0) or 0) * scale)
                rect = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
                start = -float(shape.get("start_angle", 0) or 0) * 180.0 / 3.141592653589793
                end = -float(shape.get("end_angle", 0) or 0) * 180.0 / 3.141592653589793
                span = end - start
                painter.drawArc(rect, int(start * 16), int(span * 16))

        for shape in geometry:
            if shape.get("type") != "text":
                continue
            text = str(shape.get("text", "") or "").strip()
            if not text:
                continue
            anchor = pt(shape.get("point", [0, 0]))
            pixel_height = max(7.0, min(42.0, float(shape.get("height", 1) or 1) * scale))
            width_factor = float(shape.get("width_factor", 1) or 1)
            font = QFont("Malgun Gothic")
            font.setPixelSize(max(7, round(pixel_height)))
            font.setBold(bool(shape.get("is_attribute")))
            painter.save()
            painter.translate(anchor)
            painter.rotate(-float(shape.get("rotation", 0) or 0) * 180.0 / 3.141592653589793)
            painter.scale(width_factor, 1.0)
            metrics = QFontMetricsF(font)
            rect = metrics.boundingRect(text)
            alignment = int(shape.get("alignment", 0) or 0)
            if alignment == 0:  # 좌측 기준
                x = 0.0
            elif alignment in (1, 4, 7, 10):  # 중앙 계열
                x = -rect.width() / 2
            else:  # 우측 및 기타 정렬
                x = -rect.width()
            y = rect.height() * 0.35
            painter.setFont(font)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QPointF(x, y), text)
            painter.restore()

        painter.end()
        return pixmap

    def import_cad_blocks(self, dialog):
        if not self.cad_bridge.connected:
            QMessageBox.information(dialog, "블록 가져오기", "먼저 현재 활성 CAD를 확인하고 연결하십시오.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            blocks = self.block_service.list_importable_blocks(self.room_rules)
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "블록 가져오기", str(exc))
            return
        except Exception:
            QMessageBox.critical(dialog, "블록 가져오기 오류", traceback.format_exc())
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not blocks:
            QMessageBox.information(dialog, "블록 가져오기", "설정에 등록된 블록이 없습니다.")
            return

        picker = QDialog(dialog)
        picker.setWindowTitle("설정 블록 가져오기")
        picker.resize(920, 620)
        layout = QVBoxLayout(picker)
        layout.addWidget(QLabel("설정에 등록된 블록만 표시합니다. 미리보기로 형상을 확인한 뒤 가져오십시오."))
        table = QTableWidget(len(blocks), 6, picker)
        table.setHorizontalHeaderLabels(["선택", "블록 미리보기", "블록명", "CAD 발견", "현재 보유", "구성 객체"] )
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        table.setColumnWidth(1, 235)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        for row, item in enumerate(blocks):
            found = bool(item.get("found"))
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if found else Qt.Unchecked)
            if not found:
                check.setFlags(Qt.ItemIsSelectable)
            table.setItem(row, 0, check)
            preview_item = QTableWidgetItem()
            preview_item.setData(Qt.DecorationRole, self._make_block_preview_pixmap(item.get("preview", [])))
            preview_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 1, preview_item)
            table.setRowHeight(row, 135)
            table.setItem(row, 2, QTableWidgetItem(item["name"]))
            cad_found = QTableWidgetItem("O" if found else "없음")
            cad_found.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 3, cad_found)
            owned = QTableWidgetItem("O" if item.get("exists") else "")
            owned.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 4, owned)
            count = QTableWidgetItem(str(item.get("entity_count", 0)))
            count.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 5, count)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=picker)
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        layout.addWidget(buttons)
        if picker.exec() != QDialog.Accepted:
            return

        selected = []
        existing = []
        for row, item in enumerate(blocks):
            if item.get("found") and table.item(row, 0).checkState() == Qt.Checked:
                selected.append(item["name"])
                if item.get("exists"):
                    existing.append(item["name"])
        if not selected:
            QMessageBox.information(dialog, "블록 가져오기", "가져올 블록을 하나 이상 선택하십시오.")
            return

        lines = ["선택한 블록을 프로그램 블록 라이브러리로 가져오시겠습니까?", "", *selected]
        if existing:
            lines.extend(["", "다음 블록은 현재 보유 중이므로 덮어씁니다.", *existing])
        answer = QMessageBox.question(
            dialog, "블록 가져오기 확인", "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            reports = self.block_service.import_blocks(selected, self.room_rules)
            imported = [r for r in reports if r.success]
            dialog.connection_label.setText("연결 상태: 블록 가져오기 완료")
            self.status_label.setText(f"블록 가져오기 완료: {len(imported)}개")
            detail_lines = []
            for result in imported:
                detail_lines.append(
                    f"{result.block_name} · 내부 객체 {result.entity_count}개 · "
                    f"레이어 변경 {result.layer_changed}개 · 적용 레이어 {result.target_layer}"
                    + (f" · 변경 실패 {result.layer_failed}개" if result.layer_failed else "")
                )
            QMessageBox.information(
                dialog, "블록 가져오기 완료",
                f"가져오기 완료: {len(imported)}개\n\n" + "\n".join(detail_lines)
            )
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "블록 가져오기", str(exc))
        except Exception:
            QMessageBox.critical(dialog, "블록 가져오기 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()


    def apply_library_blocks_to_cad(self, dialog):
        if not self.cad_bridge.connected:
            QMessageBox.information(dialog, "블록 라이브러리 적용", "먼저 현재 활성 CAD를 확인하고 연결하십시오.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            blocks = self.block_service.list_library_blocks()
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "블록 라이브러리 적용", str(exc))
            return
        except Exception:
            QMessageBox.critical(dialog, "블록 라이브러리 적용 오류", traceback.format_exc())
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not blocks:
            QMessageBox.information(dialog, "블록 라이브러리 적용", "프로그램 블록 라이브러리에 DWG 블록이 없습니다.")
            return

        picker = QDialog(dialog)
        picker.setWindowTitle("블록 라이브러리 적용")
        picker.resize(920, 620)
        layout = QVBoxLayout(picker)
        guide = QLabel(
            "프로그램 블록 라이브러리 목록입니다. 현재 연결된 CAD 도면의 보유 여부를 확인한 뒤 적용할 블록을 선택하십시오."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)

        table = QTableWidget(len(blocks), 5, picker)
        table.setHorizontalHeaderLabels(["선택", "블록 미리보기", "블록명", "현재 CAD", "구성 객체"])
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        table.setColumnWidth(1, 235)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)

        for row, item in enumerate(blocks):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked)
            table.setItem(row, 0, check)
            preview_item = QTableWidgetItem()
            preview_item.setData(Qt.DecorationRole, self._make_block_preview_pixmap(item.get("preview", [])))
            preview_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 1, preview_item)
            table.setRowHeight(row, 135)
            table.setItem(row, 2, QTableWidgetItem(item["name"]))
            cad_item = QTableWidgetItem("있음" if item.get("in_cad") else "없음")
            cad_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 3, cad_item)
            count_item = QTableWidgetItem(str(item.get("entity_count", 0)))
            count_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 4, count_item)
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=picker)
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        layout.addWidget(buttons)
        if picker.exec() != QDialog.Accepted:
            return

        selected = [
            item["name"] for row, item in enumerate(blocks)
            if table.item(row, 0).checkState() == Qt.Checked
        ]
        if not selected:
            QMessageBox.information(dialog, "블록 라이브러리 적용", "적용할 블록을 하나 이상 선택하십시오.")
            return

        selected_info = {item["name"]: item for item in blocks if item["name"] in selected}
        existing = [name for name in selected if selected_info[name].get("in_cad")]
        overwrite = False
        if existing:
            answer = QMessageBox.question(
                dialog,
                "기존 블록 덮어쓰기",
                "현재 CAD에 같은 이름의 블록이 있습니다.\n\n"
                + "\n".join(existing)
                + "\n\n기존 블록 정의를 라이브러리 내용으로 덮어쓰시겠습니까?\n"
                  "[예] 기존 블록 갱신 + 없는 블록 추가\n"
                  "[아니오] 기존 블록은 건너뛰고 없는 블록만 추가",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return
            overwrite = answer == QMessageBox.Yes

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            report = self.block_service.apply_library_blocks(selected, overwrite=overwrite)
            dialog.connection_label.setText("연결 상태: 블록 라이브러리 적용 완료")
            self.status_label.setText(
                f"블록 적용 완료: 추가 {len(report['added'])}, 갱신 {len(report['updated'])}, 건너뜀 {len(report['skipped'])}"
            )
            message = (
                f"신규 추가: {len(report['added'])}개\n"
                f"기존 갱신: {len(report['updated'])}개\n"
                f"건너뜀: {len(report['skipped'])}개\n"
                f"실패: {len(report['failed'])}개"
            )
            if report["failed"]:
                message += "\n\n실패 상세\n" + "\n".join(report["failed"])
            QMessageBox.information(dialog, "블록 라이브러리 적용 결과", message)
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "블록 라이브러리 적용", str(exc))
        except Exception:
            QMessageBox.critical(dialog, "블록 라이브러리 적용 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()


    def create_room_blocks_in_cad(self, targets):
        if not self.cad_bridge.connected:
            QMessageBox.information(self, "실명블록 생성", "현재 CAD가 연결되어 있지 않습니다.")
            return
        # CAD에서 읽어 온 실 데이터의 직접 작업 모드가 다른 UI 갱신으로 풀리지 않도록
        # 실행 전후에 연동 상태를 명시적으로 유지한다.
        self.rooms_tab.set_cad_connected_mode(True)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            report = self.cad_bridge.create_room_tags(targets, self.room_rules, BLOCK_LIBRARY_DIR)
            self.rooms_tab.rebuild_views()
            self.rooms_tab.data_changed.emit()
            self.status_label.setText(f"CAD 실명블록 생성 완료: {report['created']}개")
            summary = (
                f"연결된 CAD 도면에 생성: {report['created']}/{report['requested']}개\n"
                f"기존 블록 있음: {report['already_exists']}개\n"
                f"경계 누락: {report['boundary_missing']}개\n"
                f"삽입점 오류: {report.get('invalid_insert_point', 0)}개\n"
                f"CAD 삽입 실패: {report['failed']}개\n"
                f"속성 경고: {report.get('attribute_warning', 0)}개\n\n"
                f"블록: {report.get('block_name', '')}\n"
                f"블록 소스: {report.get('block_source_type', '')}"
            )
            details = []
            for item in report.get("errors", []):
                point = item.get("point")
                point_text = f" / 삽입점 {point[0]:.3f}, {point[1]:.3f}" if point else ""
                details.append(
                    f"[실패-{item.get('stage', '알 수 없음')}] {item.get('room', '')}{point_text}\n"
                    f"{item.get('message', '')}"
                )
            for item in report.get("warnings", []):
                details.append(
                    f"[경고-{item.get('stage', '알 수 없음')}] {item.get('room', '')}\n"
                    f"{item.get('message', '')}"
                )
            if details:
                summary += "\n\n상세 진단\n" + "\n\n".join(details[:20])
                if len(details) > 20:
                    summary += f"\n\n외 {len(details) - 20}건"
            message_type = QMessageBox.warning if report['failed'] or report['boundary_missing'] else QMessageBox.information
            message_type(self, "실명블록 생성 결과", summary)
        except CadConnectionError as exc:
            QMessageBox.warning(self, "실명블록 생성", str(exc))
        except Exception:
            QMessageBox.critical(self, "실명블록 생성 오류", traceback.format_exc())
        finally:
            self.rooms_tab.set_cad_connected_mode(self.cad_bridge.connected)
            QApplication.restoreOverrideCursor()

    def handle_opening_read(self, request):
        """연동 CAD 또는 베이스 DXF 벡터 캐시에서 현재층/전층 창호 후보를 읽는다."""
        payload = dict(request or {})
        source = str(payload.get("source", "cad") or "cad").strip().lower()
        requests = list(payload.get("requests", []) or [])
        if not requests:
            return
        results = {}
        errors = []

        if source == "cad":
            if not self.cad_bridge.connected:
                QMessageBox.information(self, "창호 읽기", "현재 CAD가 연결되어 있지 않습니다.")
                return
            try:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                for index, item in enumerate(requests, start=1):
                    floor = str(dict(item or {}).get("floor", "") or "")
                    rect = dict(item or {}).get("rect")
                    if self.rooms_tab._opening_read_dialog is not None:
                        self.rooms_tab._opening_read_dialog.status_label.setText(
                            f"연동 CAD 읽는 중... {index}/{len(requests)} · {floor}"
                        )
                        QApplication.processEvents()
                    try:
                        results[floor] = self.cad_bridge.detect_opening_candidates(
                            rect, payload.get("window_acis", [4, 5])
                        )
                    except Exception as exc:
                        errors.append(f"{floor}: {type(exc).__name__}: {exc}")
            finally:
                QApplication.restoreOverrideCursor()
        else:
            base_dxf = str(payload.get("base_dxf_path", "") or self._background_dxf_source_path() or "")
            if not base_dxf or not Path(base_dxf).is_file():
                QMessageBox.information(self, "창호 읽기", "베이스 DXF를 찾을 수 없습니다.")
                return
            try:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                cached = self._ensure_base_dxf_vector_cache(base_dxf)
                floors_cache = dict((cached or {}).get("floors") or {})
                # 캐시에 없는 특수층/도곽만 동일 DXF에서 해당 범위만 보조 판정한다.
                doc = None
                for index, item in enumerate(requests, start=1):
                    floor = str(dict(item or {}).get("floor", "") or "")
                    rect = dict(item or {}).get("rect")
                    if self.rooms_tab._opening_read_dialog is not None:
                        self.rooms_tab._opening_read_dialog.status_label.setText(
                            f"베이스 DXF 캐시 읽는 중... {index}/{len(requests)} · {floor}"
                        )
                        QApplication.processEvents()
                    floor_cache = dict(floors_cache.get(floor) or {})
                    candidates = list(floor_cache.get("opening_candidates") or [])
                    if floor in floors_cache:
                        results[floor] = {
                            "rect": tuple(floor_cache.get("crop_rect") or rect or (0,0,0,0)),
                            "scanned": len(floor_cache.get("segments") or []),
                            "candidates": candidates,
                            "door_count": sum(1 for v in candidates if v.get("kind") == "door"),
                            "window_count": sum(1 for v in candidates if v.get("kind") == "window"),
                        }
                    else:
                        try:
                            if doc is None:
                                doc = ezdxf.readfile(base_dxf)
                            results[floor] = detect_opening_candidates_dxf(
                                doc, rect, payload.get("window_acis", [4, 5])
                            )
                        except Exception as exc:
                            errors.append(f"{floor}: {type(exc).__name__}: {exc}")
            except Exception as exc:
                errors.append(f"베이스 DXF 캐시: {type(exc).__name__}: {exc}")
            finally:
                QApplication.restoreOverrideCursor()

        self.rooms_tab.set_opening_read_results(
            results,
            str(payload.get("scope", "current") or "current"),
            errors,
            payload.get("missing_floors", []),
        )
        if errors and not results:
            QMessageBox.warning(self, "창호 읽기", "\n".join(errors[:20]))

    def handle_opening_test(self, request):
        """구버전 호출 호환."""
        payload = dict(request or {})
        floor = str(payload.get("floor", "") or "")
        self.handle_opening_read({
            "scope": "current",
            "requests": [{"floor": floor, "rect": payload.get("rect")}],
            "window_acis": payload.get("window_acis", [4, 5]),
        })

    def handle_room_wall_link_cad(self, request):
        """내부탭의 실/벽 연동 요청을 현재 연결 CAD에 적용/읽기/검증한다."""
        if not self.cad_bridge.connected:
            QMessageBox.information(self, "실/벽 연동", "현재 CAD가 연결되어 있지 않습니다.")
            return
        payload = dict(request or {})
        action = str(payload.get("action", "") or "")
        self.rooms_tab.set_cad_connected_mode(True)
        result_payload = None
        background_refresh = False
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            if action == "export":
                result_payload = self.cad_bridge.write_room_wall_link_geometry(payload.get("items", []) or [], payload.get("layers"))
                background_refresh = bool(result_payload.get("created", 0))
            elif action in {"read", "validate"}:
                result_payload = self.cad_bridge.read_room_wall_link_geometry(payload.get("layers"))
            else:
                raise CadConnectionError(f"알 수 없는 연동 요청입니다: {action}")
        except CadConnectionError as exc:
            QMessageBox.warning(self, "실/벽 연동", str(exc))
            return
        except Exception:
            QMessageBox.critical(self, "실/벽 연동 오류", traceback.format_exc())
            return
        finally:
            self.rooms_tab.set_cad_connected_mode(self.cad_bridge.connected)
            QApplication.restoreOverrideCursor()

        # CAD COM 읽기/쓰기가 끝난 뒤에 매핑창/결과창을 띄워 대기 커서가 입력창에 남지 않게 한다.
        self.rooms_tab.handle_room_wall_link_cad_result(action, result_payload)
        if background_refresh:
            try:
                self.request_background_regeneration(force_new=True)
            except Exception:
                pass

    def refresh_room_blocks_in_cad(self, request):
        """프로그램의 선택 항목을 연결 CAD에 반영한다.

        V15.1부터 적용 항목을 실명/마감으로 분리한다. 마감은 현재 벽체 구간선만
        지원하며 바닥·천장 객체는 같은 요청 구조에 후속 확장한다.
        """
        if not self.cad_bridge.connected:
            QMessageBox.information(self, "수정사항 CAD에 적용", "현재 CAD가 연결되어 있지 않습니다.")
            return

        if isinstance(request, dict):
            targets = list(request.get('targets') or [])
            apply_room_tags = bool(request.get('apply_room_tags'))
            apply_finishes = bool(request.get('apply_finishes'))
        else:
            # 구버전 호출 호환
            targets = list(request or [])
            apply_room_tags = True
            apply_finishes = False

        if not targets:
            QMessageBox.information(self, "수정사항 CAD에 적용", "CAD에 적용할 대상이 없습니다.")
            return

        self.rooms_tab.set_cad_connected_mode(True)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            summaries = []
            details = []
            room_report = None
            finish_report = None
            audit = {'issues': [], 'issue_count': 0}

            if apply_room_tags:
                room_targets = [room for room in targets if getattr(room, 'tag_handle', '')]
                missing_tag_targets = len(targets) - len(room_targets)
                if room_targets:
                    room_report = self.cad_bridge.update_room_tags(room_targets, self.room_rules)
                    summaries.append(
                        f"실명 적용: {room_report['updated']}/{len(room_targets)}개"
                        f" / 핸들 없음 {room_report['missing'] + missing_tag_targets}"
                        f" / 실패 {room_report['failed']}"
                    )
                    details.extend(room_report.get('details', []))
                else:
                    summaries.append(f"실명 적용: 적용 가능한 실명블록 없음 / 핸들 없음 {len(targets)}")

                # V18.2: 선택 적용 후 전체 실을 다시 COM 재검사하지 않는다.
                # 대상 실만 빠르게 확인하여 17개 수정 뒤 94개 전체를 다시 읽는 지연을 제거한다.
                audit = self.cad_bridge.audit_room_tags(room_targets, self.room_rules, scan_orphans=False) if room_targets else {'issues': [], 'issue_count': 0}
                self.current_room_errors = list(audit['issues'])
                self.rooms_tab.set_cad_validation_errors(audit['issues'])
                details.extend(audit.get('issues', []))

            if apply_finishes:
                finish_report = self.cad_bridge.sync_wall_finish_lines(targets, self.room_rules)
                summaries.append(
                    f"마감 적용(벽체 구간선): 생성 {finish_report['created']}개"
                    f" / 기존·공용 중복 {finish_report['skipped_existing']}개"
                    f" / 실패 {finish_report['failed']}개"
                )
                details.extend(finish_report.get('details', []))

            # V18.2: 실명/실번호 속성 변경은 entity.Update()만으로 충분하다.
            # 전체 CAD REGEN과 배경 재생성은 실제 도형(벽체 마감선)이 바뀐 경우에만 수행한다.
            geometry_changed = bool(finish_report and finish_report.get('created', 0) > 0)
            if geometry_changed:
                try:
                    self.cad_bridge.regen()
                except Exception:
                    pass

            self.rooms_tab.set_cad_connected_mode(True)
            background_refresh_started = False
            if geometry_changed:
                try:
                    self.request_background_regeneration(force_new=True)
                    background_refresh_started = True
                except Exception:
                    pass

            self.rooms_tab.data_changed.emit()
            status = ' / '.join(summaries) if summaries else '반영 항목 없음'
            if background_refresh_started:
                status += ' / 프로그램 화면 갱신 중'
            self.status_label.setText('CAD 수정사항 적용 완료: ' + status)

            summary = '\n'.join(summaries) or '반영된 항목이 없습니다.'
            if apply_room_tags:
                summary += f"\n빠른 재검사 오류/확인사항: {audit.get('issue_count', 0)}건"
            if background_refresh_started:
                summary += '\n프로그램 화면: CAD 배경 재생성 중'
            if details:
                summary += '\n\n상세\n' + '\n'.join(details[:20])
                if len(details) > 20:
                    summary += f"\n외 {len(details)-20}건"

            has_problem = bool(
                (room_report and (room_report.get('failed') or room_report.get('missing')))
                or (finish_report and finish_report.get('failed'))
                or audit.get('issue_count', 0)
            )
            message_type = QMessageBox.warning if has_problem else QMessageBox.information
            message_type(self, "수정사항 CAD 적용 결과", summary)
        except CadConnectionError as exc:
            QMessageBox.warning(self, "수정사항 CAD에 적용", str(exc))
        except Exception:
            QMessageBox.critical(self, "수정사항 CAD 적용 오류", traceback.format_exc())
        finally:
            self.rooms_tab.set_cad_connected_mode(self.cad_bridge.connected)
            QApplication.restoreOverrideCursor()

    def handle_cad_export(self, dialog, values: dict):
        self._save_cad_sync_values(values)
        if not self.cad_bridge.connected:
            QMessageBox.information(dialog, "프로그램 → CAD", "먼저 현재 활성 CAD를 확인하고 연결하십시오.")
            return
        if not self.current_rooms:
            QMessageBox.information(dialog, "프로그램 → CAD", "프로그램에 내보낼 실 데이터가 없습니다.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            reports = []
            unsupported = []
            if values.get("export_boundaries"):
                report = self.cad_bridge.update_room_boundaries(self.current_rooms)
                reports.append(f"실구분선 수정 {report['updated']} / 실패 {report['failed']} / 핸들 없음 {report['missing']}")
            if values.get("export_room_tags"):
                report = self.cad_bridge.update_room_tags(self.current_rooms, self.room_rules)
                reports.append(f"실정보 수정 {report['updated']} / 실패 {report['failed']} / 핸들 없음 {report['missing']}")
            if values.get("export_wall_finishes"):
                unsupported.append("벽마감선 신규·삭제 전송")
            if values.get("export_floor_finishes"):
                unsupported.append("바닥마감 객체")
            if values.get("export_ceiling_finishes"):
                unsupported.append("천장마감 객체")
            if values.get("export_new_objects"):
                unsupported.append("신규 객체 생성")
            if values.get("export_deleted_objects"):
                unsupported.append("삭제 객체 반영")
            if values.get("export_regen"):
                self.cad_bridge.regen()
                reports.append("CAD 화면 REGEN 완료")
            dialog.connection_label.setText("연결 상태: 내보내기 완료")
            self.status_label.setText("CAD 내보내기 완료")
            text = "\n".join(reports) or "반영된 항목이 없습니다."
            if unsupported:
                text += "\n\n현재 미지원: " + ", ".join(dict.fromkeys(unsupported))
            QMessageBox.information(dialog, "프로그램 → CAD", text)
        except CadConnectionError as exc:
            QMessageBox.warning(dialog, "CAD 내보내기", str(exc))
        except Exception:
            QMessageBox.critical(dialog, "CAD 내보내기 오류", traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()


    def on_main_tab_name_edited(self, index: int, new_name: str):
        widget = self.tabs.widget(index)
        if widget is self.material_tab:
            key = "material"
            ini_key = "MainMaterial"
        elif widget is self.rooms_tab:
            key = "rooms"
            ini_key = "MainRooms"
        elif widget is self.finish_materials_tab:
            key = "finish"
            ini_key = "MainFinish"
        else:
            return
        self.main_tab_names[key] = new_name
        self.settings.set("TabNames", ini_key, new_name)
        self.settings.save()
        self.refresh_main_tab_titles()

    def on_material_tab_name_edited(self, category: str, new_name: str):
        ini_keys = {
            "재료 후보": "MaterialCandidate",
            "보류": "MaterialHold",
            "제외": "MaterialExcluded",
        }
        ini_key = ini_keys.get(category)
        if not ini_key:
            return
        self.settings.set("TabNames", ini_key, new_name)
        self.settings.save()

    def open_material_dialog(self):
        self.material_dialog.show()
        self.material_dialog.raise_()
        self.material_dialog.activateWindow()
        self.apply_filter()

    def _save_project_overview(self, text: str):
        if self.active_project is None:
            return
        try:
            self.active_project = self.project_manager.update_overview(
                self.active_project.project_id, str(text or "")
            )
        except Exception:
            self.status_label.setText("프로젝트 개요 저장 실패")

    def _save_project_overview_fields(self, fields: dict):
        if self.active_project is None:
            return
        previous_name = str(self.active_project.name or "")
        try:
            self.active_project = self.project_manager.update_overview_fields(
                self.active_project.project_id, dict(fields or {})
            )
            if self.active_project.name != previous_name:
                # 개요 입력모드에서 공사명을 바꾼 경우 창 제목/프로젝트 표시도 즉시 동기화한다.
                self.setWindowTitle(f"{self._base_window_title} · {self.active_project.name}")
                self.recent_settings.set("BackgroundDrawing", "ProjectName", self.active_project.name)
                self.recent_settings.save()
                if str(self.path_label.text() or "").startswith("프로젝트 캐시:"):
                    self._set_project_path_label(self.current_path)
        except Exception as exc:
            self.status_label.setText(f"프로젝트 개요 항목 저장 실패: {exc}")
            # 저장 실패(예: 중복 공사명) 시 화면 값을 실제 프로젝트명으로 되돌린다.
            try:
                self.overview_tab.set_project(self.active_project)
            except Exception:
                pass

    def _apply_summary_markups_to_materials(self, markups: dict):
        try:
            changed = self.finish_materials_tab.apply_summary_markups(markups)
            if changed:
                QMessageBox.information(self, "할증값 반영", f"기본 재료 {changed:,}개에 집계 할증값을 반영했습니다.")
            else:
                QMessageBox.information(self, "할증값 반영", "반영할 기본 재료가 없거나 값이 이미 동일합니다.")
        except Exception as exc:
            QMessageBox.warning(self, "할증값 반영", f"재료 할증값을 반영하지 못했습니다.\n{exc}")

    def _refresh_summary_tab(self, *_args):
        if not hasattr(self, "summary_tab") or not hasattr(self, "rooms_tab"):
            return
        try:
            self.summary_tab.set_material_trades(self.material_trades)
            rows = list(self.rooms_tab.finish_aggregate_rows())
            if hasattr(self, "other_tab"):
                rows.extend(self.other_tab.summary_rows())
            self.summary_tab.set_data(
                rows, self.rooms_tab.summary_markups
            )
        except Exception:
            # 집계 화면 오류가 내부 작업 자체를 막지 않도록 한다.
            pass

    def _save_finish_group_type_labels(self, labels):
        labels = dict(labels or {})
        mapping = {"마감": "Finish", "단열": "Insulation", "방수": "Waterproof", "외부": "Exterior", "기타": "Other"}
        for key, ini_key in mapping.items():
            value = str(labels.get(key, key) or key).strip() or key
            self.finish_group_type_labels[key] = value
            self.recent_settings.set("FinishGroupTabs", ini_key, value)
        self.recent_settings.save()

    def _save_finish_group_tab_settings(self, settings):
        settings = dict(settings or {})
        divisions = dict(settings.get("divisions", {}) or {})
        show_other = dict(settings.get("show_other_filters", {}) or {})
        mapping = {"마감": "Finish", "단열": "Insulation", "방수": "Waterproof", "외부": "Exterior", "기타": "Other"}
        normalized_divisions = {}
        normalized_show_other = {}
        for key, ini_key in mapping.items():
            values = []
            for value in divisions.get(key, []) or []:
                text = str(value or "").strip()
                if text and text.casefold() not in {item.casefold() for item in values}:
                    values.append(text)
            if not values:
                values = [key]
            enabled = bool(show_other.get(key, False))
            normalized_divisions[key] = values
            normalized_show_other[key] = enabled
            self.recent_settings.set(
                "FinishGroupDivisions", ini_key, json.dumps(values, ensure_ascii=False)
            )
            self.recent_settings.set(
                "FinishGroupDisplay", f"{ini_key}ShowOther", "True" if enabled else "False"
            )
        self.finish_group_tab_settings = {
            "divisions": normalized_divisions,
            "show_other_filters": normalized_show_other,
        }
        self.recent_settings.save()

    def refresh_main_tab_titles(self):
        rooms_text = self.main_tab_names["rooms"]
        if self.current_rooms:
            rooms_text = f"{rooms_text} ({len(self.current_rooms):,})"
        index = self.tabs.indexOf(self.rooms_tab)
        if index >= 0:
            self.tabs.setTabText(index, rooms_text)
        for key, widget in (("overview", self.overview_tab), ("external", self.external_tab),
                            ("structure", self.structure_tab), ("other", self.other_tab),
                            ("finish", self.finish_materials_tab), ("summary", self.summary_tab)):
            tab_index = self.tabs.indexOf(widget)
            if tab_index >= 0:
                self.tabs.setTabText(tab_index, self.main_tab_names[key])
        # 창호 탭은 정적 main_tab_names 값으로 되돌리지 않고 항상 실제 마스터 개수를 표시한다.
        self._refresh_window_tab_title()
        self._refresh_summary_tab()

    def _background_exclude_patterns(self):
        return [line.strip() for line in self.room_rules.get("background_exclude_patterns", "").splitlines() if line.strip()]

    def _business_layers(self):
        return [self.room_rules.get(key, "") for key in LAYER_KEYS]

    def _background_filter_signature(self):
        # V19.0: 프로젝트 DXF 배경은 업무레이어까지 포함하여 도곽 내부를 전부 그린다.
        # 따라서 캐시 서명도 사용자가 명시한 배경 제외 패턴만 반영한다.
        values = self._background_exclude_patterns()
        return "|".join(sorted(str(value).strip().casefold() for value in values if str(value).strip()))

    def _block_library_status(self):
        block_name = str(self.room_rules.get("block_name", "") or "").strip()
        if not block_name:
            return "실명 블록명이 지정되지 않았습니다."
        path = BLOCK_LIBRARY_DIR / f"{block_name}.dwg"
        if path.exists():
            return f"가져옴: {block_name}\n{path}"
        return f"미등록: {block_name} (CAD 연동에서 블록 가져오기를 실행하십시오.)"

    def _settings_values(self):
        return {
            "background_render_mode": self.background_render_mode,
            "background_max_pixels": max(400, min(5000, int(self.background_max_pixels))),
            "edge_length_font_size": self.edge_length_font_size,
            "room_label_area_scale": self.room_label_area_scale,
            "room_label_scale_mode": self.room_label_scale_mode,
            "edge_length_offset": self.edge_length_offset,
            "edge_length_width_ratio": self.edge_length_width_ratio,
            "edge_length_show_unit_m": self.edge_length_show_unit_m,
            "label_full_view_weight": self.label_full_view_weight,
            "label_selected_view_weight": self.label_selected_view_weight,
            "background_style": self.background_style,
            "background_default_line_width": self.background_line_width,
            "background_zoomout_width_weight": self.background_zoomout_width_weight,
            "background_aci_widths": self.background_aci_widths,
            "background_keep_red": self.background_keep_red,
            "background_opacity": self.background_opacity,
            "boundary_color": self.boundary_color,
            "boundary_width": self.boundary_width,
            "deduction_color": self.deduction_color,
            "deduction_width": self.deduction_width,
            "render_quality": self.render_quality,
            "selection_color": self.selection_color,
            "selection_width": self.selection_width,
            "wall_selection_color": self.wall_selection_color,
            "wall_selection_width": self.wall_selection_width,
            "opening_line_color": self.opening_line_color,
            "opening_line_width": self.opening_line_width,
            "wall_selection_zoom_scale": self.wall_selection_zoom_scale,
            "group_overlay_opacity": self.group_overlay_opacity,
            **self.room_rules,
            "room_category_rules": normalize_room_category_rules(self.room_category_rules),
            "finish_formula_codes": normalize_formula_codes(self.finish_formula_codes),
            "finish_categories": normalize_finish_categories(self.finish_categories),
            "material_trades": normalize_material_trades(self.material_trades),
            "material_units": normalize_material_units(self.material_units),
            "window_categories": normalize_window_categories(self.window_categories),
            "auto_backup_minutes": self.auto_backup_minutes,
            "block_library_status": self._block_library_status(),
        }

    def create_required_layers(self, dialog=None):
        if not self.current_path:
            QMessageBox.information(self, "레이어 생성", "먼저 DXF 파일을 선택하십시오.")
            return
        values = dialog.values() if dialog is not None else self._settings_values()
        layer_specs = [
            (values.get(key, default_layer_name(key)), values.get(f"{key}_color", default_layer_color(key)))
            for key in LAYER_KEYS
        ]
        try:
            doc = ezdxf.readfile(self.current_path)
            created = []
            existing = {layer.dxf.name.casefold() for layer in doc.layers}
            from app.cad.colors import nearest_aci
            for name, color in layer_specs:
                name = str(name).strip()
                if name and name.casefold() not in existing:
                    doc.layers.add(name, color=nearest_aci(color))
                    existing.add(name.casefold())
                    created.append(name)
            source = Path(self.current_path)
            output = source.with_name(source.stem + "_필수레이어추가.dxf")
            doc.saveas(output)
            detail = ", ".join(created) if created else "추가할 레이어 없음"
            QMessageBox.information(self, "레이어 생성 완료", f"{detail}\n\n저장 위치:\n{output}")
        except Exception as exc:
            QMessageBox.critical(self, "레이어 생성 오류", str(exc))

    def open_cad_background_settings(self):
        """표시 설정을 모델리스/항상 위 창으로 연다.

        V24.10: 표시 설정을 띄운 상태에서도 도곽 Pan/Zoom이 가능해야 하므로
        exec() 기반 모달 창을 사용하지 않는다. 적용은 사용자가 확인/적용을
        눌렀을 때만 수행한다.
        """
        existing = getattr(self, "_display_settings_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        dialog = DisplaySettingsDialog(self._settings_values(), self)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._display_settings_dialog = dialog
        dialog.apply_requested.connect(lambda d=dialog: self._apply_cad_background_settings(d))

        def _clear_dialog_ref(*_args, d=dialog):
            if getattr(self, "_display_settings_dialog", None) is d:
                self._display_settings_dialog = None

        dialog.destroyed.connect(_clear_dialog_ref)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_cad_background_settings(self, dialog: DisplaySettingsDialog):
        """현재 표시설정 창 값을 저장하고 즉시 현재 도면에 적용한다."""
        if dialog is None:
            return
        values = dialog.values()
        old_background_signature = (
            self.background_max_pixels, self.background_style, self._background_filter_signature(),
            self.background_line_width, self.background_aci_widths, self.background_keep_red,
        )

        self.background_render_mode = str(values.get("background_render_mode", self.background_render_mode) or "png").strip().lower()
        if self.background_render_mode not in {"png", "vector"}:
            self.background_render_mode = "png"
        self.background_max_pixels = max(400, min(5000, int(values["background_max_pixels"])))
        self.edge_length_font_size = values["edge_length_font_size"]
        self.room_label_area_scale = values["room_label_area_scale"]
        self.room_label_scale_mode = str(values.get("room_label_scale_mode", "room_area"))
        self.edge_length_offset = values["edge_length_offset"]
        self.edge_length_width_ratio = values["edge_length_width_ratio"]
        self.edge_length_show_unit_m = bool(values["edge_length_show_unit_m"])
        self.label_full_view_weight = float(values["label_full_view_weight"])
        self.label_selected_view_weight = float(values["label_selected_view_weight"])
        self.background_style = values["background_style"]
        self.background_opacity = values["background_opacity"]
        self.boundary_color = values["boundary_color"]
        self.boundary_width = values["boundary_width"]
        self.deduction_color = values["deduction_color"]
        self.deduction_width = values["deduction_width"]
        self.render_quality = values["render_quality"]
        self.selection_color = values["selection_color"]
        self.selection_width = values["selection_width"]
        self.wall_selection_color = values["wall_selection_color"]
        self.wall_selection_width = values["wall_selection_width"]
        self.opening_line_color = values.get("opening_line_color", self.opening_line_color)
        self.opening_line_width = int(values.get("opening_line_width", self.opening_line_width))
        self.wall_selection_zoom_scale = float(values.get("wall_selection_zoom_scale", self.wall_selection_zoom_scale))
        self.group_overlay_opacity = values["group_overlay_opacity"]
        self.background_line_width = float(values["background_default_line_width"])
        self.background_zoomout_width_weight = max(0.0, min(1.0, float(values.get("background_zoomout_width_weight", self.background_zoomout_width_weight))))
        self.background_aci_widths = str(values["background_aci_widths"])
        self.background_keep_red = bool(values["background_keep_red"])

        for key, value in {
            "BackgroundRenderMode": self.background_render_mode,
            "BackgroundMaxPixels": self.background_max_pixels,
            "EdgeLengthFontSize": self.edge_length_font_size,
            "RoomLabelAreaScale": self.room_label_area_scale,
            "RoomLabelScaleMode": self.room_label_scale_mode,
            "EdgeLengthOffset": self.edge_length_offset,
            "EdgeLengthWidthRatio": self.edge_length_width_ratio,
            "EdgeLengthShowUnitM": self.edge_length_show_unit_m,
            "LabelFullViewWeight": self.label_full_view_weight,
            "LabelSelectedViewWeight": self.label_selected_view_weight,
            "BackgroundStyle": self.background_style,
            "BackgroundOpacity": self.background_opacity,
            "BoundaryColor": self.boundary_color,
            "BoundaryWidth": self.boundary_width,
            "DeductionColor": self.deduction_color,
            "DeductionWidth": self.deduction_width,
            "RenderQuality": self.render_quality,
            "SelectionColor": self.selection_color,
            "SelectionWidth": self.selection_width,
            "WallSelectionColor": self.wall_selection_color,
            "WallSelectionWidth": self.wall_selection_width,
            "OpeningLineColor": self.opening_line_color,
            "OpeningLineWidth": self.opening_line_width,
            "WallSelectionZoomScale": self.wall_selection_zoom_scale,
            "GroupOverlayOpacity": self.group_overlay_opacity,
            "BackgroundDefaultLineWidth": self.background_line_width,
            "BackgroundZoomoutWidthWeight": self.background_zoomout_width_weight,
            "BackgroundACIWidths": self.background_aci_widths,
            "BackgroundKeepRed": self.background_keep_red,
        }.items():
            self.recent_settings.set("Preview", key, value)
        self.recent_settings.save()

        self.rooms_tab.apply_preview_settings(
            self.background_max_pixels, self.edge_length_font_size, self.edge_length_offset, self.edge_length_width_ratio,
            self.background_style, self.background_opacity, self.boundary_color, self.boundary_width, self.render_quality,
            self.selection_color, self.selection_width, self.background_line_width, self.background_aci_widths, self.background_keep_red,
            self.edge_length_show_unit_m, self.label_full_view_weight, self.label_selected_view_weight, self.room_label_scale_mode,
            self.deduction_color, self.deduction_width,
            self.wall_selection_color, self.wall_selection_width,
            self.group_overlay_opacity, self.wall_selection_zoom_scale, self.edge_length_visible,
            self.opening_line_color, self.opening_line_width, self.background_zoomout_width_weight,
        )
        self.rooms_tab.preview.set_label_area_scale(self.room_label_area_scale)
        self.rooms_tab.set_background_render_mode(self.background_render_mode)

        new_background_signature = (
            self.background_max_pixels, self.background_style, self._background_filter_signature(),
            self.background_line_width, self.background_aci_widths, self.background_keep_red,
        )
        question_parent = dialog if dialog.isVisible() else self
        # 벡터 방식으로 전환했는데 아직 도곽 벡터 캐시가 없으면 1회 생성 여부를 묻는다.
        if (
            self.current_path and self.background_render_mode == "vector"
            and not self.rooms_tab.preview.floor_vector_cache
        ):
            answer = QMessageBox.question(
                question_parent, "벡터 배경 캐시 없음",
                "현재 도면의 도곽 벡터 캐시가 없습니다.\n베이스 DXF에서 벡터 캐시를 생성할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if answer == QMessageBox.Yes:
                self.request_background_regeneration(force_new=True)
                return
        if (
            self.current_path and self.background_render_mode == "png"
            and not self.rooms_tab.preview.floor_background_files
        ):
            answer = QMessageBox.question(
                question_parent, "PNG 배경 캐시 없음",
                "현재 도면의 PNG 배경 캐시가 없습니다.\n도곽 벡터 원본에서 PNG 배경을 생성할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if answer == QMessageBox.Yes:
                self.request_background_regeneration(force_new=True)
                return
        if self.current_path and old_background_signature != new_background_signature:
            answer = QMessageBox.question(
                question_parent, "CAD 배경 표시 설정 변경",
                "CAD 배경에 영향을 주는 표시 설정이 변경되었습니다.\n새 설정으로 배경을 재생성할까요?\n\n아니오를 누르면 설정값만 저장하고 현재 배경은 그대로 유지합니다.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if answer == QMessageBox.Yes:
                self.request_background_regeneration(force_new=True)

        self.status_label.setText(f"표시 설정 적용 완료 · CAD 배경 투명도 {self.background_opacity}%")


    def _save_cad_background_mode(self, mode: str):
        mode = str(mode or "black").strip().lower()
        if mode not in {"none", "black", "white"}:
            mode = "black"
        self.cad_background_mode = mode
        self.recent_settings.set("Preview", "CADBackgroundMode", mode)
        self.recent_settings.save()

    def _save_room_group_by_floor(self, enabled: bool):
        self.recent_settings.set("RoomList", "GroupByFloor", bool(enabled))
        self.recent_settings.save()

    def _save_room_column_widths(self, values):
        """내부탭 실 목록의 사용자 조정 열너비를 settings2.ini에 저장한다."""
        widths = RoomsTab._normalize_room_column_widths(values)
        self.recent_settings.set("RoomList", "ColumnWidths", ",".join(str(value) for value in widths))
        self.recent_settings.set("RoomList", "ColumnOrderVersion", "V20.4")
        self.recent_settings.save()

    def _save_finish_summary_column_order(self, values):
        """평면 중앙 하단 재료마감 집계의 사용자 열순서를 settings2.ini에 저장한다."""
        order = RoomsTab._normalize_finish_summary_column_order(values)
        self.recent_settings.set("RoomList", "FinishSummaryColumnOrder", ",".join(str(value) for value in order))
        self.recent_settings.save()

    def _save_room_number_entry_settings(self, values: dict):
        """번호 기입창의 마지막 양식을 settings2.ini에 저장한다."""
        values = dict(values or {})
        try:
            digits = max(1, min(5, int(values.get("digits", 3))))
        except Exception:
            digits = 3
        self.room_number_entry_settings = {
            "digits": digits,
            "floor_pattern": str(values.get("floor_pattern", "") or ""),
            "separator": str(values.get("separator", "-") if values.get("separator", "-") is not None else "-"),
            "suffix": str(values.get("suffix", "") or ""),
        }
        self.recent_settings.set("RoomNumbering", "Digits", self.room_number_entry_settings["digits"])
        self.recent_settings.set("RoomNumbering", "FloorPattern", self.room_number_entry_settings["floor_pattern"])
        self.recent_settings.set("RoomNumbering", "Separator", self.room_number_entry_settings["separator"])
        self.recent_settings.set("RoomNumbering", "Suffix", self.room_number_entry_settings["suffix"])
        self.recent_settings.save()

    def on_room_label_size_changed(self, value: int):
        """내부탭 실명 문자크기는 작업 편의값이므로 settings2.ini에 즉시 보관한다."""
        self.room_label_font_size = max(6, min(500, int(value)))
        self.recent_settings.set("Preview", "RoomLabelFontSize", self.room_label_font_size)
        self.recent_settings.save()

    def on_dimension_settings_changed(self, values: dict):
        """선택실 치수 빠른 설정을 즉시 저장한다."""
        self.edge_length_font_size = int(values.get("edge_length_font_size", self.edge_length_font_size))
        self.edge_length_width_ratio = int(values.get("edge_length_width_ratio", self.edge_length_width_ratio))
        self.edge_length_offset = int(values.get("edge_length_offset", self.edge_length_offset))
        self.edge_length_show_unit_m = bool(values.get("edge_length_show_unit_m", self.edge_length_show_unit_m))
        self.edge_length_visible = bool(values.get("edge_length_visible", self.edge_length_visible))
        self.recent_settings.set("Preview", "EdgeLengthFontSize", self.edge_length_font_size)
        self.recent_settings.set("Preview", "EdgeLengthWidthRatio", self.edge_length_width_ratio)
        self.recent_settings.set("Preview", "EdgeLengthOffset", self.edge_length_offset)
        self.recent_settings.set("Preview", "EdgeLengthShowUnitM", self.edge_length_show_unit_m)
        self.recent_settings.set("Preview", "EdgeLengthVisible", self.edge_length_visible)
        self.recent_settings.save()

    def _on_material_units_changed(self, units):
        """재료표에서 미등록 단위를 추가했을 때 설정과 다른 재료 선택창에 즉시 공유한다."""
        self.material_units = normalize_material_units(units)
        self.settings.set("Materials", "Units", serialize_material_units(self.material_units))
        self.settings.save()
        self.rooms_tab.finish_panel.set_calculation_settings(
            self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units
        )
        if hasattr(self, "other_tab"):
            self.other_tab.set_material_units(self.material_units)
        self.window_tab.set_calculation_settings(self.window_categories, self.material_trades, self.material_units)
        self.rooms_tab.set_window_catalog_provider(self.window_tab.window_catalog)
        self._refresh_summary_tab()
        self._schedule_current_cad_project_cache_save()

    def open_settings(self):
        # V15.3: 일반 설정창은 CAD 업무 설정만 담당한다. 표시 관련 값은 '표시 설정'으로 이동.
        old_filter_signature = self._background_filter_signature()
        dialog = SettingsDialog(self._settings_values(), bool(self.current_path), self)
        dialog.create_layers_requested.connect(lambda: self.create_required_layers(dialog))
        if not dialog.exec():
            return
        values = dialog.values()
        self.room_category_rules = normalize_room_category_rules(values.get("room_category_rules"))
        self.finish_formula_codes = normalize_formula_codes(values.get("finish_formula_codes"))
        self.finish_categories = normalize_finish_categories(values.get("finish_categories"))
        self.material_trades = normalize_material_trades(values.get("material_trades"))
        self.material_units = normalize_material_units(values.get("material_units"))
        self.window_categories = normalize_window_categories(values.get("window_categories"))
        self.auto_backup_minutes = max(1, min(1440, int(values.get("auto_backup_minutes", 30) or 30)))
        layer_keys = list(LAYER_KEYS)
        layer_color_keys = [f"{key}_color" for key in LAYER_KEYS]
        self.room_rules = {key: values[key] for key in (
            *layer_keys, *layer_color_keys,
            "block_name", "floor_tag", "room_name_tag", "room_number_tags", "background_exclude_patterns"
        )}
        save_values = {
            spec["ini_name"]: self.room_rules[key]
            for key, spec in LAYER_DEFINITIONS.items()
        }
        save_values.update({
            spec["ini_color"]: self.room_rules[f"{key}_color"]
            for key, spec in LAYER_DEFINITIONS.items()
        })
        save_values.update({
            "BlockName": self.room_rules["block_name"],
            "FloorTag": self.room_rules["floor_tag"],
            "RoomNameTag": self.room_rules["room_name_tag"],
            "RoomNumberTags": self.room_rules["room_number_tags"],
            "BackgroundExcludePatterns": self.room_rules["background_exclude_patterns"],
        })
        for key, value in save_values.items():
            self.settings.set("CAD", key, value)
        for category in ROOM_CATEGORIES:
            self.settings.set(
                "RoomCategories",
                ROOM_CATEGORY_INI_KEYS[category],
                "\n".join(self.room_category_rules[category]),
            )
        self.settings.set("FinishCalculation", "FormulaCodes", serialize_formula_codes(self.finish_formula_codes))
        self.settings.set("FinishCalculation", "FinishCategories", serialize_finish_categories(self.finish_categories))
        self.settings.set("Materials", "Trades", serialize_material_trades(self.material_trades))
        self.settings.set("Materials", "Units", serialize_material_units(self.material_units))
        self.settings.set("Windows", "Categories", serialize_window_categories(self.window_categories))
        self.settings.set("Common", "AutoBackupMinutes", self.auto_backup_minutes)
        self.settings.save()
        self.rooms_tab.set_room_rules(self.room_rules)
        self.rooms_tab.set_room_category_rules(self.room_category_rules)
        self.finish_materials_tab.set_calculation_settings(self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units)
        self.rooms_tab.finish_panel.set_calculation_settings(self.finish_formula_codes, self.finish_categories, self.material_trades, self.material_units)
        self.other_tab.set_material_units(self.material_units)
        self.window_tab.set_calculation_settings(self.window_categories, self.material_trades, self.material_units)
        self.rooms_tab.set_window_catalog_provider(self.window_tab.window_catalog)
        self._refresh_summary_tab()
        self._restart_project_autosave_timer()
        if self.current_path and old_filter_signature != self._background_filter_signature():
            self.request_background_regeneration(force_new=True)

    def _safe_drawing_project_name(self, name: str) -> str:
        import re
        value = (name or "").strip()
        value = re.sub(r'[<>:"/\\|?*]+', '_', value).strip().rstrip('.')
        return value or "새 도면"

    def _drawing_library_files(self):
        DRAWINGS_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(
            [p for p in DRAWINGS_DIR.glob("*.dxf") if p.is_file()],
            key=lambda p: p.stem.lower(),
        )

    def open_drawing_library(self):
        DRAWINGS_DIR.mkdir(parents=True, exist_ok=True)
        dialog = QDialog(self)
        dialog.setWindowTitle("DXF 도면 목록")
        dialog.resize(720, 460)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 2, dialog)
        table.setHorizontalHeaderLabels(["프로젝트명", "DXF 파일"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(table, 1)

        button_row = QHBoxLayout()
        load_btn = QPushButton("도면 불러오기")
        apply_btn = QPushButton("도면 적용")
        rename_btn = QPushButton("프로젝트명 입력")
        location_btn = QPushButton("DXF 위치 열기")
        location_btn.setToolTip("선택한 DXF 파일을 탐색기에서 선택 상태로 엽니다.")
        delete_btn = QPushButton("삭제")
        delete_btn.setToolTip("선택한 목록의 실제 DXF 파일을 삭제합니다.")
        close_btn = QPushButton("닫기")
        button_row.addWidget(load_btn)
        button_row.addWidget(apply_btn)
        button_row.addWidget(rename_btn)
        button_row.addWidget(location_btn)
        button_row.addWidget(delete_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        def refresh(select_path: str = ""):
            files = self._drawing_library_files()
            table.setRowCount(len(files))
            selected_row = -1
            for row, file_path in enumerate(files):
                name_item = QTableWidgetItem(file_path.stem)
                file_item = QTableWidgetItem(file_path.name)
                name_item.setData(Qt.UserRole, str(file_path))
                file_item.setData(Qt.UserRole, str(file_path))
                table.setItem(row, 0, name_item)
                table.setItem(row, 1, file_item)
                if select_path and str(file_path) == select_path:
                    selected_row = row
            if selected_row >= 0:
                table.selectRow(selected_row)
            elif files:
                table.selectRow(0)
            apply_btn.setEnabled(bool(files))
            rename_btn.setEnabled(bool(files))
            location_btn.setEnabled(bool(files))
            delete_btn.setEnabled(bool(files))

        def selected_path() -> Path | None:
            rows = table.selectionModel().selectedRows() if table.selectionModel() else []
            if not rows:
                return None
            item = table.item(rows[0].row(), 0)
            if not item:
                return None
            value = item.data(Qt.UserRole)
            return Path(value) if value else None

        def open_dxf_location():
            source_path = selected_path()
            if not source_path or not source_path.is_file():
                QMessageBox.information(dialog, "DXF 위치 열기", "위치를 열 DXF 도면을 선택해 주세요.")
                return
            try:
                if os.name == "nt":
                    subprocess.Popen(["explorer.exe", "/select,", str(source_path.resolve())])
                else:
                    subprocess.Popen(["xdg-open", str(source_path.resolve().parent)])
            except Exception as exc:
                try:
                    if os.name == "nt":
                        os.startfile(str(source_path.resolve().parent))
                        return
                except Exception:
                    pass
                QMessageBox.warning(dialog, "DXF 위치 열기", f"탐색기를 열지 못했습니다.\n{exc}")

        def load_drawing():
            start_dir = self.settings.get("Path", "LastDrawingLibraryFolder", self.settings.get("Path", "LastDXFFolder", ""))
            source, _ = QFileDialog.getOpenFileName(dialog, "도면 불러오기", start_dir, "DXF 파일 (*.dxf)")
            if not source:
                return
            source_path = Path(source)
            self.settings.set("Path", "LastDrawingLibraryFolder", str(source_path.parent))
            self.settings.save()
            try:
                readable_source = _prepare_readable_dxf(source_path)
            except Exception:
                detail = (
                    "DXF 도면 등록 실패\n\n"
                    f"선택 DXF: {source_path}\n\n"
                    f"[오류 상세]\n{traceback.format_exc()}"
                )
                log_path = None
                try:
                    TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    log_path = TEMP_PROJECT_CACHE_DIR / "DXF도면등록_오류.txt"
                    log_path.write_text(detail, encoding="utf-8")
                    detail += f"\n\n오류 로그: {log_path}"
                except Exception:
                    pass
                self._show_copyable_error(dialog, "DXF 등록 실패", "정상 DXF 파일만 도면목록에 등록할 수 있습니다.", detail, log_path)
                return
            project_name, ok = QInputDialog.getText(
                dialog, "프로젝트명 입력", "프로젝트명을 입력하세요.", text=source_path.stem
            )
            if not ok:
                return
            project_name = self._safe_drawing_project_name(project_name)
            destination = DRAWINGS_DIR / f"{project_name}.dxf"
            if destination.exists() and destination.resolve() != source_path.resolve():
                answer = QMessageBox.question(
                    dialog, "도면 불러오기", f"'{project_name}' 도면이 이미 있습니다.\n덮어쓸까요?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                if answer != QMessageBox.Yes:
                    return
            import shutil
            shutil.copy2(str(readable_source), str(destination))
            refresh(str(destination))

        def rename_project():
            old_path = selected_path()
            if not old_path or not old_path.is_file():
                QMessageBox.information(dialog, "프로젝트명 입력", "수정할 도면을 선택해 주세요.")
                return
            new_name, ok = QInputDialog.getText(
                dialog, "프로젝트명 입력", "프로젝트명을 입력하세요.", text=old_path.stem
            )
            if not ok:
                return
            new_name = self._safe_drawing_project_name(new_name)
            new_path = old_path.with_name(f"{new_name}.dxf")
            if new_path == old_path:
                return
            if new_path.exists():
                QMessageBox.warning(dialog, "프로젝트명 입력", "같은 프로젝트명의 도면이 이미 있습니다.")
                return
            old_path.rename(new_path)
            refresh(str(new_path))

        def delete_drawing():
            source_path = selected_path()
            if not source_path or not source_path.is_file():
                QMessageBox.information(dialog, "DXF 삭제", "삭제할 DXF 도면을 선택해 주세요.")
                return
            answer = QMessageBox.warning(
                dialog,
                "DXF 파일 삭제",
                f"선택한 DXF 파일을 실제로 삭제합니다.\n\n{source_path.name}\n\n삭제 후 복구할 수 없습니다. 계속할까요?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                selected_resolved = source_path.resolve()
                applied = Path(self.applied_dxf_path).resolve() if self.applied_dxf_path else None
                current = Path(self.current_path).resolve() if self.current_path else None
                source_path.unlink()

                # 현재 프로그램이 바로 이 파일을 직접 연동 중인 경우 상태도 함께 해제한다.
                if applied is not None and applied == selected_resolved:
                    self.applied_dxf_path = ""
                    self.dxf_link_active = False
                if current is not None and current == selected_resolved:
                    self.current_path = ""
                    self.current_drawing_divisions = []
                    self.rooms_tab.set_source_path("")
                    self.rooms_tab.set_base_dxf_path("")
                    self.rooms_tab.clear_background_cache_files()
                    self.rooms_tab.set_vector_background_cache({})
                    self.rooms_tab.set_vector_display_stage("none")
                    self.rooms_tab.set_background_ready(False)
                self._update_window_title()
                refresh()
                self.status_label.setText(f"DXF 파일 삭제 완료: {source_path.name}")
            except Exception:
                detail = (
                    "DXF 파일 삭제 실패\n\n"
                    f"삭제 대상: {source_path}\n\n"
                    f"[오류 상세]\n{traceback.format_exc()}"
                )
                log_path = None
                try:
                    TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    log_path = TEMP_PROJECT_CACHE_DIR / "DXF도면삭제_오류.txt"
                    log_path.write_text(detail, encoding="utf-8")
                    detail += f"\n\n오류 로그: {log_path}"
                except Exception:
                    pass
                self._show_copyable_error(dialog, "DXF 삭제 실패", "DXF 파일을 삭제하지 못했습니다.", detail, log_path)

        def _prepare_readable_dxf(source_path: Path) -> Path:
            """원본은 건드리지 않고 DXF를 검증하고, 복구 가능한 손상은 임시 복구본으로 사용한다."""
            try:
                ezdxf.readfile(str(source_path))
                return source_path
            except Exception as first_exc:
                first_trace = traceback.format_exc()

            # ezdxf 일반 읽기에 실패해도 recover가 살릴 수 있는 DXF가 있다.
            try:
                from ezdxf import recover
                recovered_doc, auditor = recover.readfile(str(source_path))
                TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                repaired_path = TEMP_PROJECT_CACHE_DIR / f"{source_path.stem}_복구.dxf"
                recovered_doc.saveas(str(repaired_path))
                # 저장한 복구본이 실제로 다시 열리는지 한 번 더 검증한다.
                ezdxf.readfile(str(repaired_path))
                return repaired_path
            except Exception:
                recover_trace = traceback.format_exc()

            # 확장자만 DXF인 다른 형식인지 헤더로 구분해 상세 원인을 남긴다.
            try:
                header = source_path.read_bytes()[:256]
            except Exception:
                header = b""
            if header.startswith(b"AC10"):
                kind = "파일 확장자는 .dxf지만 실제 내용은 DWG 형식(AC10 헤더)으로 보입니다."
            elif header.startswith(b"AutoCAD Binary DXF"):
                kind = "Binary DXF로 보이지만 현재 ezdxf에서 정상 해석되지 않았습니다."
            elif b"SECTION" in header.upper():
                kind = "DXF 헤더는 확인되지만 파일 구조가 손상되었거나 지원되지 않는 형식입니다."
            else:
                kind = "파일 내용에서 정상 DXF 헤더를 확인하지 못했습니다. 확장자만 .dxf인 파일일 수 있습니다."
            raise RuntimeError(
                f"{kind}\n\n[일반 읽기 실패]\n{first_trace}\n[복구 읽기 실패]\n{recover_trace}"
            )

        def apply_drawing():
            source_path = selected_path()
            if not source_path or not source_path.is_file():
                QMessageBox.information(dialog, "도면 적용", "적용할 도면을 선택해 주세요.")
                return

            # V24.03.3: 도면 적용을 누른 즉시 목록창부터 닫아 버튼 반응을 명확히 보여준다.
            # 파일 검증/복사/벡터 읽기는 창이 닫힌 다음 수행하며 실패 시 메인창 위에 오류를 띄운다.
            dialog.accept()
            QApplication.processEvents()
            self.status_label.setText(f"DXF 도면 적용 준비 중... · {source_path.name}")

            # V23.93: 실구분선 유무와 관계없이 DXF를 프로젝트 기본/배경 도면으로 적용할 수 있다.
            # CAD 연동 없이 DXF만으로 시작하는 경우도 허용한다.
            if not self.current_path and self.active_project is None:
                QMessageBox.information(self, "도면 적용", "먼저 프로젝트를 선택하거나 DXF 파일을 열어 주세요.")
                return
            if any(worker.isRunning() for worker in self.background_workers):
                QMessageBox.information(self, "도면 적용", "현재 CAD 배경 작업이 진행 중입니다. 완료 후 다시 적용해 주세요.")
                return
            # 프로젝트에 복사/등록하기 전에 실제 DXF 형식인지 검증한다.
            # 일반 읽기에 실패해도 recover로 살릴 수 있으면 원본을 건드리지 않고 임시 복구본을 적용한다.
            try:
                validated_source = _prepare_readable_dxf(source_path)
            except Exception:
                log_path = None
                detail = (
                    "DXF 도면 적용 실패\n\n"
                    f"선택 DXF: {source_path}\n"
                    f"현재 작업원본: {self.current_path or '(없음)'}\n\n"
                    f"[오류 상세]\n{traceback.format_exc()}"
                )
                try:
                    TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    log_path = TEMP_PROJECT_CACHE_DIR / "DXF도면적용_오류.txt"
                    log_path.write_text(detail, encoding="utf-8")
                    detail += f"\n\n오류 로그: {log_path}"
                except Exception:
                    pass
                self.dxf_link_active = False
                self._update_window_title()
                self._show_copyable_error(self, "도면 적용 실패", "선택한 파일을 DXF로 읽지 못했습니다.", detail, log_path)
                return
            import shutil
            TEMP_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
            applied_source = validated_source
            try:
                if self.active_project is not None:
                    self.active_project = self.project_manager.assign_base_drawing(
                        self.active_project.project_id, validated_source
                    )
                    if self.active_project.base_drawing_path:
                        applied_source = self.active_project.base_drawing_path
            except Exception as exc:
                QMessageBox.warning(self, "도면 적용", str(exc))
                return
            # 적용된 실제 DXF를 그대로 배경/벡터 원본으로 사용한다.
            # 이전 버전처럼 임시프로젝트\원본CAD.dxf로 복사한 뒤 그 사본을 읽지 않는다.
            try:
                ezdxf.readfile(str(applied_source))
            except Exception as exc:
                log_path = None
                detail = (
                    "DXF 도면 적용 실패\n\n"
                    f"적용 DXF: {applied_source}\n"
                    f"현재 작업원본: {self.current_path or '(없음)'}\n\n"
                    f"[오류 상세]\n{traceback.format_exc()}"
                )
                try:
                    TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    log_path = TEMP_PROJECT_CACHE_DIR / "DXF도면적용_오류.txt"
                    log_path.write_text(detail, encoding="utf-8")
                    detail += f"\n\n오류 로그: {log_path}"
                except Exception:
                    pass
                self.dxf_link_active = False
                self._update_window_title()
                self._show_copyable_error(self, "도면 적용 실패", "적용된 DXF 확인 중 오류가 발생했습니다.", detail, log_path)
                return
            self.applied_dxf_path = str(applied_source)
            self.dxf_link_active = True
            self._update_window_title()
            project_label = self.active_project.name if self.active_project is not None else source_path.stem
            self.recent_settings.set("BackgroundDrawing", "ProjectName", project_label)
            self.recent_settings.set("BackgroundDrawing", "LibraryPath", str(applied_source))
            self.recent_settings.save()

            # CAD 연동 없이 도면목록에서 DXF만 적용해 시작한 경우에는 선택 DXF를 현재 작업원본으로 삼아
            # 재료/실 데이터까지 읽는다. 실구분선이 없어도 정상적으로 0개 상태로 프로젝트를 준비한다.
            if not self.current_path:
                self.load_dxf_path(str(applied_source))
                return

            self.status_label.setText(f"배경 도면 적용: {project_label} · 백그라운드 생성 시작")
            self.start_background_scan(
                str(applied_source),
                cache_source_path=self.current_path or str(applied_source),
                rooms=list(self.current_rooms),
                preserve_existing=True,
                refresh_source=True,
            )

        load_btn.clicked.connect(load_drawing)
        rename_btn.clicked.connect(rename_project)
        apply_btn.clicked.connect(apply_drawing)
        location_btn.clicked.connect(open_dxf_location)
        delete_btn.clicked.connect(delete_drawing)
        close_btn.clicked.connect(dialog.reject)
        table.doubleClicked.connect(lambda _idx: apply_drawing())
        refresh()
        dialog.exec()

    def open_file(self):
        start_dir = self.settings.get("Path", "LastDXFFolder", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "DXF 파일 선택", start_dir, "DXF 파일 (*.dxf)"
        )
        if path:
            self.load_dxf_path(path)

    def load_dxf_path(self, path: str):
        path = str(Path(path))
        if not Path(path).is_file():
            QMessageBox.warning(self, "최근 DXF 열기", f"파일을 찾을 수 없습니다.\n\n{path}")
            return
        self._save_current_cad_project_cache()
        self._switch_to_dxf_mode()
        self.settings.set("Path", "LastDXFFolder", str(Path(path).parent))
        self.recent_settings.set("Path", "LastDXFPath", path)
        self.recent_settings.save()

        self.current_path = path
        self.applied_dxf_path = path
        self.dxf_link_active = False
        self._update_window_title()
        self.path_label.setText(path)

        # V23.93: CAD 연동 없이 DXF만 열어도 도곽/도서구분/층 지정창을 동일하게 사용한다.
        # 도곽명이 비어 있으면 저장된 지정값을 복원하거나 사용자에게 지정받아 배경/실 판정에 사용한다.
        try:
            division_doc = ezdxf.readfile(path)
            raw_divisions = read_drawing_divisions(division_doc.modelspace(), self.room_rules)
            resolved_divisions = self._resolve_drawing_divisions(raw_divisions)
            if resolved_divisions is None:
                self.status_label.setText("DXF 도곽 지정 취소")
                return
            self.current_drawing_divisions = list(resolved_divisions or [])
            self.dxf_link_active = True
            self._update_window_title()
        except Exception as exc:
            self.current_drawing_divisions = []
            QMessageBox.warning(self, "DXF 도곽 읽기", f"DXF 도곽 정보를 읽는 중 오류가 발생했습니다.\n{exc}")

        self.open_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._set_progress_status("DXF 읽기 시작")

        self.rooms_tab.clear_background_cache_files()
        self.rooms_tab.set_background_ready(False)

        self.worker = ScanWorker(path, self.classifier, self.room_rules, self.current_drawing_divisions)
        self.worker.progress.connect(self._set_progress_status)
        self.worker.finished_ok.connect(self.scan_done)
        self.worker.failed.connect(self.scan_failed)
        self.worker.start()

    def scan_done(self, result, rooms, room_errors, total_count):
        restored_category_count = restore_room_categories(
            rooms, load_room_category_snapshots(self.current_path)
        )
        self.current_result = result
        self.current_rooms = rooms
        self.current_room_errors = room_errors
        for name, rows in result.items():
            self.material_tab.set_category_rows(name, rows)
        self.rooms_tab.set_source_path(self.current_path)
        self.rooms_tab.set_data(rooms, room_errors)
        self.rooms_tab.set_cad_connected_mode(False)
        self._save_current_cad_project_cache()
        self.refresh_main_tab_titles()

        # 기본 데이터가 준비되면 즉시 UI를 다시 사용 가능하게 합니다.
        self.progress.setVisible(False)
        self.open_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.apply_sort()
        cache_files = load_background_cache(
            self.current_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style, self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
        )
        vector_cache = self._load_vector_background_cache(self.current_path)
        if cache_files or vector_cache:
            if cache_files:
                self.rooms_tab.set_background_cache_files(cache_files)
            self.rooms_tab.set_background_ready(True)
            background_text = "CAD 배경 준비됨"
        else:
            self.rooms_tab.clear_background_cache_files()
            self.rooms_tab.set_background_ready(False)
            background_text = "CAD 배경 없음 · 재생성 필요"

        self._clear_progress_status_style()
        self.status_label.setText(
            f"기본 분석 완료: 문자 {total_count:,}개 / 실 {len(rooms):,}개 / "
            f"실 오류 {len(room_errors):,}개 · {background_text}"
            + (f" · 구분 캐시 {restored_category_count:,}개 복원" if restored_category_count else "")
        )

        # V23.93: DXF 단독 작업도 CAD 연동과 동일하게 배경을 사용할 수 있다.
        # 기존 캐시가 없으면 실 0개 상태라도 평면도 도곽/층을 기준으로 즉시 생성한다.
        if not cache_files and not vector_cache and self.current_path and str(self.current_path).lower().endswith(".dxf"):
            self.status_label.setText(
                f"기본 분석 완료: 문자 {total_count:,}개 / 실 {len(rooms):,}개 · 평면도 도곽 기준 배경 생성 시작"
            )
            self.start_background_scan(
                self._background_dxf_source_path() or self.current_path,
                cache_source_path=self.current_path,
                rooms=list(self.current_rooms),
                preserve_existing=False,
                refresh_source=True,
            )

    def start_background_scan(self, path: str, cache_source_path: str | None = None, rooms=None, preserve_existing: bool = False, refresh_source: bool = False):
        target_rooms = list(rooms if rooms is not None else self.current_rooms)
        # V23.93: 평면도 도곽/층 지정만 되어 있어도 배경을 만들 수 있다.
        # 실구분선(current_rooms)은 배경 생성의 필수조건이 아니다.
        if not path:
            return
        BACKGROUND_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self.rooms_tab.set_base_dxf_path(path)
        self.rooms_tab.set_background_loading(True)
        worker = BackgroundScanWorker(
            path, target_rooms, str(BACKGROUND_CACHE_ROOT),
            self.background_max_pixels, self.background_style,
            self._background_exclude_patterns(), [],
            # V19.0: 전용 DXF 배경은 업무레이어도 제외하지 않고 도곽 내부 도형을 전부 PNG에 포함한다.
            cache_source_path=cache_source_path or path,
            room_rules=self.room_rules,
            background_line_width=self.background_line_width,
            background_aci_widths=self.background_aci_widths,
            background_keep_red=self.background_keep_red,
            preserve_existing=preserve_existing,
            refresh_source=refresh_source,
            divisions_override=self.current_drawing_divisions,
            render_mode=self.background_render_mode,
        )
        worker.cache_result_key = cache_source_path or path
        self.background_workers.append(worker)
        worker.progress.connect(self.background_scan_progress)
        worker.finished_ok.connect(
            lambda _path, key=worker.cache_result_key, w=worker:
                self.background_scan_done(key, getattr(w, "block_diagnostic_text", ""), getattr(w, "block_diagnostic_path", ""))
        )
        worker.failed.connect(self.background_scan_failed)
        worker.finished.connect(lambda w=worker: self.cleanup_background_worker(w))
        worker.start()

    def start_cad_background_scan(self, cad_values: dict | None = None, rooms=None, preserve_existing: bool = False, refresh_source: bool = False):
        """연결된 CAD 원본을 직접 읽어 벡터/PNG 배경 캐시를 생성한다.

        DWG를 이름만 .dxf로 복사/변환하지 않는다. CAD 원본 저장도 수행하지 않는다.
        """
        if not self.current_path or not self.cad_bridge.connected:
            return
        target_rooms = list(rooms if rooms is not None else self.current_rooms)
        BACKGROUND_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self.rooms_tab.set_base_dxf_path("")
        self.rooms_tab.set_background_loading(True)
        values = dict(cad_values or self._settings_values())
        values["import_background"] = True
        values["import_selected_only"] = False
        worker = CadBackgroundWorker(
            self.current_path, target_rooms, str(BACKGROUND_CACHE_ROOT),
            self.background_max_pixels, self.background_style,
            values, self.room_rules,
            self._background_exclude_patterns(), [],
            self._background_filter_signature(),
            self.background_line_width, self.background_aci_widths, self.background_keep_red,
            preserve_existing=preserve_existing, refresh_source=refresh_source,
            divisions_override=self.current_drawing_divisions,
            render_mode=self.background_render_mode,
            applied_dxf_path=str(getattr(self, "applied_dxf_path", "") or ""),
        )
        worker.cache_result_key = self.current_path
        self.background_workers.append(worker)
        worker.progress.connect(self.background_scan_progress)
        worker.finished_ok.connect(
            lambda _path, key=worker.cache_result_key, w=worker:
                self.background_scan_done(key, getattr(w, "block_diagnostic_text", ""), getattr(w, "block_diagnostic_path", ""))
        )
        worker.failed.connect(self.background_scan_failed)
        worker.finished.connect(lambda w=worker: self.cleanup_background_worker(w))
        worker.start()

    def open_vector_filter_dialog(self):
        """2단 원본 정리 → 3단 최적화 도면을 분리해서 관리한다."""
        key = str(self.current_path or self.applied_dxf_path or "").strip()
        if not key:
            QMessageBox.information(self, "도면 정리", "먼저 프로젝트/DXF 배경 캐시를 준비해 주세요.")
            return
        source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
        if not source_cache:
            QMessageBox.information(
                self, "도면 정리",
                "원본 정리 캐시가 없습니다.\n\n[CAD 배경 재생성]으로 원본 캐시를 먼저 생성해 주세요."
            )
            return
        catalog = build_filter_catalog(source_cache)
        program_cache = load_background_program_cache(BACKGROUND_CACHE_ROOT, key)
        selection = dict(
            (program_cache or {}).get("draft_selection")
            or (program_cache or {}).get("selection")
            or default_selection(catalog)
        )

        old = getattr(self, "_vector_filter_dialog", None)
        if old is not None:
            try:
                old.close()
                old.deleteLater()
            except Exception:
                pass

        dialog = VectorFilterDialog(
            catalog, selection, program_cache, self,
            raw_apply_callback=lambda key=key: self._apply_original_vector_view(key),
            build_callback=lambda selection, key=key: self._build_optimized_vector_cache(key, selection),
            optimized_apply_callback=lambda settings, key=key: self._apply_optimized_group_settings(key, settings),
            selection_state_callback=lambda selection, key=key: self._save_vector_filter_draft(key, selection),
            layer_apply_callback=lambda selection, key=key: self._apply_vector_layer_filter(key, selection),
            direct_select_callback=lambda selection, dialog, catalog, key=key: self._start_vector_direct_select(key, selection, dialog, catalog),
            group_edit_callback=lambda dialog, key=key: self._start_vector_group_edit(key, dialog),
        )
        self._vector_filter_dialog = dialog
        dialog.destroyed.connect(
            lambda *_args, d=dialog: setattr(self, "_vector_filter_dialog", None)
            if getattr(self, "_vector_filter_dialog", None) is d else None
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _group_edit_display_floors(self, cache: dict, source_group: str) -> dict:
        """그룹 편집용 임시 표시. 편집 그룹은 원래 선명도로, 나머지는 90% 투명으로 표시한다."""
        floors = deepcopy(dict(cache.get("floors") or {}))
        source_group = str(source_group)
        for _floor, info in floors.items():
            settings = dict(info.get("group_settings") or cache.get("group_settings") or {})
            for group_key in VectorFilterDialog.GROUPS:
                key = str(group_key[0])
                cfg = dict(settings.get(key) or {})
                cfg["visible"] = True
                if key == source_group:
                    # 편집 대상은 저장된 원래 투명도 그대로 사용한다.
                    cfg.pop("absolute_opacity", None)
                else:
                    # 위치 파악용 보조 그룹: 90% 투명 = 10% 불투명.
                    # 저장 데이터에는 없는 그룹편집 전용 임시 값이며 종료 시 원상복구된다.
                    cfg["absolute_opacity"] = 10
                settings[key] = cfg
            info["group_settings"] = settings
        return floors

    def _start_vector_group_edit(self, key: str, dialog=None):
        program = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}
        if not program.get("floors"):
            QMessageBox.information(self, "그룹 편집", "먼저 최적화 도면을 생성해 주세요.")
            return
        labels = [(k, label) for k, label in VectorFilterDialog.GROUPS]
        items = [label for _k, label in labels]
        selected_label, ok = QInputDialog.getItem(self, "그룹 편집", "편집할 그룹", items, 0, False)
        if not ok:
            return
        source_group = next((k for k, label in labels if label == selected_label), "")
        if not source_group:
            return
        self._end_vector_direct_select(close_status=True)
        self._end_vector_group_edit(close_status=True)
        working = ensure_program_edit_ids(deepcopy(program))
        self._vector_group_edit_key = str(key)
        self._vector_group_edit_source_group = str(source_group)
        self._vector_group_edit_working = working
        self._vector_group_edit_saved = deepcopy(working)
        self._vector_group_edit_undo = []
        self._vector_group_edit_dirty = False
        self._vector_group_edit_live = False
        self._vector_group_edit_dialog = dialog
        preview = self.rooms_tab.preview
        try:
            preview.vector_group_edit_selection_changed.disconnect(self._on_vector_group_edit_selection_changed)
        except Exception:
            pass
        try:
            preview.vector_group_edit_transition_requested.disconnect(self._apply_vector_group_edit_transition)
        except Exception:
            pass
        try:
            preview.vector_group_edit_undo_requested.disconnect(self._undo_vector_group_edit)
        except Exception:
            pass
        preview.vector_group_edit_selection_changed.connect(self._on_vector_group_edit_selection_changed)
        preview.vector_group_edit_transition_requested.connect(self._apply_vector_group_edit_transition)
        preview.vector_group_edit_undo_requested.connect(self._undo_vector_group_edit)
        preview.set_vector_group_edit_mode(True, str(source_group))
        self.rooms_tab.set_vector_background_cache(self._group_edit_display_floors(working, source_group), preserve_layout=True)
        self.rooms_tab.set_vector_display_stage("optimized")
        status = VectorGroupEditStatusDialog(str(source_group), labels, self)
        self._vector_group_edit_status = status
        status.transition_requested.connect(self._apply_vector_group_edit_transition)
        status.undo_requested.connect(self._undo_vector_group_edit)
        status.save_requested.connect(self._save_vector_group_edit)
        status.close_requested.connect(self._request_close_vector_group_edit)
        status.live_apply_changed.connect(lambda value: setattr(self, "_vector_group_edit_live", bool(value)))
        status.specified_mode_requested.connect(self._choose_vector_group_edit_layer)
        status.show(); status.raise_(); status.activateWindow()
        try:
            if dialog is not None:
                dialog.hide()
        except Exception:
            pass
        self.status_label.setText(f"최적화 그룹 편집 · {selected_label} 편집 · 다른 그룹 90% 투명 표시")

    def _choose_vector_group_edit_layer(self):
        status = getattr(self, "_vector_group_edit_status", None)
        working = getattr(self, "_vector_group_edit_working", None)
        if status is None or not isinstance(working, dict):
            return
        dlg = LayerSelectDialog(program_layer_names(working), status)
        if dlg.exec() == QDialog.Accepted:
            layer = dlg.selected_layer()
            if layer:
                status.set_specified_layer(layer)
            else:
                status.set_specified_layer("")
        elif not status.specified_layer():
            status._choose_color()

    def _on_vector_group_edit_selection_changed(self, values):
        selected = set(values or set())
        status = getattr(self, "_vector_group_edit_status", None)
        if status is not None:
            status.set_selection_count(len(selected))
        if selected and bool(getattr(self, "_vector_group_edit_live", False)):
            self._apply_vector_group_edit_transition()

    def _apply_vector_group_edit_transition(self):
        preview = self.rooms_tab.preview
        ids = set(getattr(preview, "selected_vector_group_edit_ids", set()) or set())
        if not ids:
            return
        status = getattr(self, "_vector_group_edit_status", None)
        working = getattr(self, "_vector_group_edit_working", None)
        if status is None or not isinstance(working, dict):
            return
        target_group = status.target_group()
        mode = status.mode()
        target_layer = status.specified_layer()
        if not target_group or target_group == str(getattr(self, "_vector_group_edit_source_group", "")):
            QMessageBox.information(status, "그룹 편집", "이동할 다른 그룹을 선택해 주세요.")
            return
        if mode == "specified" and not target_layer:
            self._choose_vector_group_edit_layer()
            target_layer = status.specified_layer()
            if not target_layer:
                return
        self._vector_group_edit_undo.append(deepcopy(working))
        result = move_program_edit_items(working, ids, target_group, mode, target_layer,
                                         str(getattr(self, "_vector_group_edit_source_group", "")))
        if int(result.get("moved", 0) or 0) <= 0:
            self._vector_group_edit_undo.pop()
            return
        self._vector_group_edit_dirty = True
        preview.clear_vector_group_edit_selection()
        source_group = str(getattr(self, "_vector_group_edit_source_group", ""))
        self.rooms_tab.set_vector_background_cache(self._group_edit_display_floors(working, source_group), preserve_layout=True)
        self.status_label.setText(f"그룹 편집 전환 완료 · {result.get('moved', 0)}개 객체 → {target_group} · 레이어 {', '.join(result.get('layers') or [])}")

    def _undo_vector_group_edit(self):
        stack = list(getattr(self, "_vector_group_edit_undo", []) or [])
        if not stack:
            return
        self._vector_group_edit_working = stack.pop()
        self._vector_group_edit_undo = stack
        saved = getattr(self, "_vector_group_edit_saved", {}) or {}
        self._vector_group_edit_dirty = self._vector_group_edit_working != saved
        source_group = str(getattr(self, "_vector_group_edit_source_group", ""))
        self.rooms_tab.set_vector_background_cache(self._group_edit_display_floors(self._vector_group_edit_working, source_group), preserve_layout=True)
        try: self.rooms_tab.preview.clear_vector_group_edit_selection()
        except Exception: pass
        self.status_label.setText("그룹 편집 · 직전 전환을 되돌렸습니다.")

    def _save_vector_group_edit(self):
        key = str(getattr(self, "_vector_group_edit_key", "") or "")
        working = getattr(self, "_vector_group_edit_working", None)
        if not key or not isinstance(working, dict):
            return
        save_background_program_cache(BACKGROUND_CACHE_ROOT, key, working)
        self._vector_group_edit_saved = deepcopy(working)
        self._vector_group_edit_dirty = False
        self._vector_group_edit_undo = []
        self._save_background_project_snapshot_now(key)
        self.status_label.setText("그룹 편집 · 현재 최적화 그룹/레이어 변경사항을 저장했습니다.")

    def _request_close_vector_group_edit(self):
        if bool(getattr(self, "_vector_group_edit_dirty", False)):
            answer = QMessageBox.question(
                self, "그룹 편집", "현재 변경사항을 저장하지 않고 종료할까요?\n저장하지 않으면 마지막 저장 상태로 되돌립니다.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self._vector_group_edit_working = deepcopy(getattr(self, "_vector_group_edit_saved", {}) or {})
        self._end_vector_group_edit(close_status=True)

    def _end_vector_group_edit(self, close_status: bool = True):
        key = str(getattr(self, "_vector_group_edit_key", "") or "")
        preview = getattr(getattr(self, "rooms_tab", None), "preview", None)
        if preview is not None:
            try: preview.vector_group_edit_selection_changed.disconnect(self._on_vector_group_edit_selection_changed)
            except Exception: pass
            try: preview.vector_group_edit_transition_requested.disconnect(self._apply_vector_group_edit_transition)
            except Exception: pass
            try: preview.vector_group_edit_undo_requested.disconnect(self._undo_vector_group_edit)
            except Exception: pass
            try: preview.set_vector_group_edit_mode(False, "")
            except Exception: pass
        if key:
            cache = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or getattr(self, "_vector_group_edit_saved", {}) or {}
            if cache.get("floors"):
                self.rooms_tab.set_vector_background_cache(cache.get("floors") or {}, preserve_layout=True)
                self.rooms_tab.set_vector_display_stage("optimized")
        status = getattr(self, "_vector_group_edit_status", None)
        if close_status and status is not None:
            try:
                status._allow_close = True; status.close()
            except Exception: pass
        dialog = getattr(self, "_vector_group_edit_dialog", None)
        if dialog is not None:
            try:
                dialog._program_cache = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}
                dialog._populate_group_rows()
                dialog.show(); dialog.raise_()
            except Exception: pass
        self._vector_group_edit_status = None
        self._vector_group_edit_working = {}
        self._vector_group_edit_saved = {}
        self._vector_group_edit_undo = []
        self._vector_group_edit_dirty = False
        self.status_label.setText("최적화 그룹 편집 종료")

    def _start_vector_direct_select(self, key: str, selection: dict, dialog=None, catalog: dict | None = None):
        """도면정리 창을 닫고 도면에서 레이어/블록을 직접 골라 OFF하는 연속 작업 모드."""
        self._end_vector_direct_select(close_status=True)
        self._vector_direct_key = str(key or "")
        self._vector_direct_selection = {
            "layers": set((selection or {}).get("layers") or []),
            "blocks": set((selection or {}).get("blocks") or []),
        }
        self._vector_direct_all_items = {
            "layers": set(dict((catalog or {}).get("layer") or {}).keys()),
            "blocks": set(dict((catalog or {}).get("block") or {}).keys()),
        }
        self._vector_direct_saved_selection = {
            "layers": set(self._vector_direct_selection["layers"]),
            "blocks": set(self._vector_direct_selection["blocks"]),
        }
        self._vector_direct_dirty = False
        self._vector_direct_undo_stack = []
        self._vector_direct_live_apply = False
        self._vector_direct_live_busy = False
        self._vector_direct_all_layers_view = False
        self._vector_direct_pending_on = set()
        self._vector_direct_pending_on_history = []
        self._vector_direct_stage_before_all_layers = str(getattr(self.rooms_tab, "vector_display_stage", "") or "raw").strip().lower()
        preview = self.rooms_tab.preview
        try:
            preview.vector_direct_selection_changed.disconnect(self._on_vector_direct_selection_changed)
        except Exception:
            pass
        try:
            preview.vector_direct_undo_requested.disconnect(self._undo_vector_direct_off)
        except Exception:
            pass
        try:
            preview.vector_direct_off_requested.disconnect(self._apply_vector_direct_off)
        except Exception:
            pass
        preview.vector_direct_selection_changed.connect(self._on_vector_direct_selection_changed)
        preview.vector_direct_undo_requested.connect(self._undo_vector_direct_off)
        preview.vector_direct_off_requested.connect(self._apply_vector_direct_off)
        preview.set_vector_direct_select_mode(True)

        status = VectorDirectSelectStatusDialog(self)
        self._vector_direct_status = status
        status.off_requested.connect(self._apply_vector_direct_off)
        status.save_requested.connect(self._save_vector_direct_state)
        status.undo_requested.connect(self._undo_vector_direct_off)
        status.close_requested.connect(self._request_close_vector_direct_select)
        status.live_apply_changed.connect(self._set_vector_direct_live_apply)
        status.all_layers_view_changed.connect(self._set_vector_direct_all_layers_view)
        status.set_all_layers_view(False)
        status.set_selection(set())
        status.show()
        # 메인 프로그램 상단 중앙에 배치한다.
        try:
            geo = self.frameGeometry()
            x = geo.x() + max(0, (geo.width() - status.width()) // 2)
            y = geo.y() + 55
            status.move(x, y)
        except Exception:
            pass
        status.raise_()
        if dialog is not None:
            try:
                dialog.close()
            except Exception:
                pass
        self.status_label.setText("도면 직접선택 · 클릭/드래그=추가 · Ctrl+클릭/드래그=해제 · Space=레이어 OFF · Ctrl+Z=되돌리기")

    def _set_vector_direct_live_apply(self, enabled: bool):
        self._vector_direct_live_apply = bool(enabled)
        if self._vector_direct_live_apply:
            picked = set(getattr(self.rooms_tab.preview, "selected_vector_filter_keys", set()) or set())
            if picked:
                self._apply_vector_direct_off()

    def _set_vector_direct_all_layers_view(self, enabled: bool):
        """모든 레이어 보기: 전체를 임시표시하고 ON 대상을 고른 뒤 해제 시 실제 상태에 합친다."""
        enabled = bool(enabled)
        if enabled == bool(getattr(self, "_vector_direct_all_layers_view", False)):
            return
        preview = self.rooms_tab.preview
        key = str(getattr(self, "_vector_direct_key", "") or "")
        if enabled:
            self._vector_direct_all_layers_view = True
            self._vector_direct_pending_on = set()
            self._vector_direct_pending_on_history = []
            # V24.31: 최적화 캐시에서 빠진 레이어/블록까지 고를 수 있어야 하므로
            # 모든 레이어 보기에서는 3단 최적화 결과가 아니라 2단 원본 정리 캐시 전체를 임시 표시한다.
            self._vector_direct_stage_before_all_layers = str(
                getattr(self.rooms_tab, "vector_display_stage", "") or "raw"
            ).strip().lower()
            try:
                preview.set_vector_direct_all_layers_view(True)
                preview.clear_vector_direct_selection()
            except Exception:
                pass
            all_items = getattr(self, "_vector_direct_all_items", {}) or {}
            if key:
                source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
                if source_cache and source_cache.get("floors"):
                    all_selection = {
                        "layers": sorted(set(all_items.get("layers") or set()), key=str.casefold),
                        "blocks": sorted(set(all_items.get("blocks") or set()), key=str.casefold),
                    }
                    raw_floors = build_raw_preview_floors(source_cache, all_selection)
                    self.rooms_tab.set_vector_background_cache(raw_floors, preserve_layout=True)
                    self.rooms_tab.set_vector_display_stage("raw")
                    self.rooms_tab.set_background_ready(bool(raw_floors))
            status = getattr(self, "_vector_direct_status", None)
            if status is not None:
                status.set_all_layers_view(True)
            self.status_label.setText("도면 직접선택 · 모든 레이어 보기 ON · 최적화 미사용 레이어 포함 · 파란색 선택 후 레이어 ON으로 저장")
            return

        # 체크 해제 시 지금까지 저장한 ON 대상을 실제 표시상태에 합친다.
        before = {
            "layers": set(getattr(self, "_vector_direct_selection", {}).get("layers") or set()),
            "blocks": set(getattr(self, "_vector_direct_selection", {}).get("blocks") or set()),
        }
        pending = set(getattr(self, "_vector_direct_pending_on", set()) or set())
        current = {"layers": set(before["layers"]), "blocks": set(before["blocks"])}
        for kind, name in pending:
            if kind == "block":
                current["blocks"].add(str(name))
            else:
                current["layers"].add(str(name))
        if current != before:
            self._vector_direct_undo_stack.append(before)
            self._vector_direct_selection = current
            self._vector_direct_dirty = True
        self._vector_direct_all_layers_view = False
        self._vector_direct_pending_on = set()
        self._vector_direct_pending_on_history = []
        try:
            preview.set_vector_direct_all_layers_view(False)
            preview.clear_vector_direct_selection()
        except Exception:
            pass
        if key:
            selection_payload = {
                "layers": sorted(current["layers"], key=str.casefold),
                "blocks": sorted(current["blocks"], key=str.casefold),
            }
            stage_before = str(getattr(self, "_vector_direct_stage_before_all_layers", "") or "raw").strip().lower()
            program_cache = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}
            source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key) or {}
            # V24.31: 모든 레이어 보기에서 새로 ON으로 저장한 항목이 기존 최적화 생성 때
            # 제외됐던 항목이면, 현재 선택값으로 3단 캐시를 다시 만들어 해당 그룹에 편입한다.
            if pending and stage_before == "optimized" and program_cache.get("floors") and source_cache.get("floors"):
                group_settings = dict(program_cache.get("group_settings") or {})
                rebuilt = build_program_cache(source_cache, selection_payload, group_settings=group_settings)
                # 다음 작업용 체크/표시 상태도 새 선택값과 맞춘다.
                rebuilt["draft_selection"] = dict(selection_payload)
                rebuilt["display_selection"] = dict(selection_payload)
                save_background_program_cache(BACKGROUND_CACHE_ROOT, key, rebuilt)
                self.rooms_tab.set_vector_background_cache(rebuilt.get("floors") or {}, preserve_layout=True)
                self.rooms_tab.set_vector_display_stage("optimized")
                self.rooms_tab.set_background_ready(bool(rebuilt.get("floors")))
                self._save_background_project_snapshot_now(key)
            else:
                self._apply_vector_layer_filter(key, selection_payload)
        status = getattr(self, "_vector_direct_status", None)
        if status is not None:
            status.set_all_layers_view(False)
        self.status_label.setText(
            f"도면 직접선택 · 모든 레이어 보기 OFF · 저장한 ON 대상 {len(pending)}개 반영"
            if pending else "도면 직접선택 · 모든 레이어 보기 OFF"
        )

    def _on_vector_direct_selection_changed(self, values):
        picked = set(values or set())
        status = getattr(self, "_vector_direct_status", None)
        if status is not None:
            try:
                status.set_selection(picked)
            except Exception:
                pass
        # 실시간 적용은 일반 모드에서는 즉시 OFF, 모든 레이어 보기에서는 즉시 ON 대상 저장.
        if picked and bool(getattr(self, "_vector_direct_live_apply", False)) and not bool(getattr(self, "_vector_direct_live_busy", False)):
            self._vector_direct_live_busy = True
            try:
                self._apply_vector_direct_off()
            finally:
                self._vector_direct_live_busy = False

    def _apply_vector_direct_off(self):
        preview = self.rooms_tab.preview
        picked = set(getattr(preview, "selected_vector_filter_keys", set()) or set())
        if not picked:
            return

        # 모든 레이어 보기에서는 실제 표시상태를 즉시 바꾸지 않고 ON 대상에 누적 저장한다.
        if bool(getattr(self, "_vector_direct_all_layers_view", False)):
            pending = set(getattr(self, "_vector_direct_pending_on", set()) or set())
            self._vector_direct_pending_on_history = list(getattr(self, "_vector_direct_pending_on_history", []) or []) + [set(picked)]
            pending.update(picked)
            self._vector_direct_pending_on = pending
            preview.clear_vector_direct_selection()
            self.status_label.setText(f"도면 직접선택 · 레이어 ON 대상 {len(pending)}개 저장 · 모든 레이어 보기 해제 시 반영")
            return

        before = {
            "layers": set(getattr(self, "_vector_direct_selection", {}).get("layers") or set()),
            "blocks": set(getattr(self, "_vector_direct_selection", {}).get("blocks") or set()),
        }
        self._vector_direct_undo_stack.append(before)
        current = {"layers": set(before["layers"]), "blocks": set(before["blocks"])}
        for kind, name in picked:
            if kind == "block":
                current["blocks"].discard(str(name))
            else:
                current["layers"].discard(str(name))
        self._vector_direct_selection = current
        self._vector_direct_dirty = True
        key = str(getattr(self, "_vector_direct_key", "") or "")
        if key:
            self._apply_vector_layer_filter(key, {
                "layers": sorted(current["layers"], key=str.casefold),
                "blocks": sorted(current["blocks"], key=str.casefold),
            })
        preview.clear_vector_direct_selection()
        self.status_label.setText(f"도면 직접선택 OFF 완료 · {len(picked)}개 레이어/블록 숨김 · 계속 선택할 수 있습니다.")

    def _undo_vector_direct_off(self):
        if bool(getattr(self, "_vector_direct_all_layers_view", False)):
            history = list(getattr(self, "_vector_direct_pending_on_history", []) or [])
            if history:
                history.pop()
                self._vector_direct_pending_on_history = history
                pending = set()
                for batch in history:
                    pending.update(set(batch or set()))
                self._vector_direct_pending_on = pending
                try:
                    self.rooms_tab.preview.clear_vector_direct_selection()
                except Exception:
                    pass
                self.status_label.setText(f"도면 직접선택 · 레이어 ON 대상 되돌림 · 현재 {len(pending)}개 저장")
            return
        stack = list(getattr(self, "_vector_direct_undo_stack", []) or [])
        if not stack:
            return
        previous = stack.pop()
        self._vector_direct_undo_stack = stack
        self._vector_direct_selection = {"layers": set(previous["layers"]), "blocks": set(previous["blocks"])}
        saved = getattr(self, "_vector_direct_saved_selection", {}) or {}
        self._vector_direct_dirty = not (
            set(previous["layers"]) == set(saved.get("layers") or set()) and
            set(previous["blocks"]) == set(saved.get("blocks") or set())
        )
        key = str(getattr(self, "_vector_direct_key", "") or "")
        if key:
            self._apply_vector_layer_filter(key, {
                "layers": sorted(previous["layers"], key=str.casefold),
                "blocks": sorted(previous["blocks"], key=str.casefold),
            })
        try:
            self.rooms_tab.preview.clear_vector_direct_selection()
        except Exception:
            pass
        self.status_label.setText("도면 직접선택 · 직전 OFF 작업을 되돌렸습니다.")

    def _save_vector_direct_state(self):
        """현재 표시 상태를 직접선택 작업의 저장 기준점으로 확정한다."""
        if bool(getattr(self, "_vector_direct_all_layers_view", False)):
            status = getattr(self, "_vector_direct_status", None)
            if status is not None:
                blocker = QSignalBlocker(status.all_layers_view_check)
                status.all_layers_view_check.setChecked(False)
                del blocker
            self._set_vector_direct_all_layers_view(False)
        current = {
            "layers": set(getattr(self, "_vector_direct_selection", {}).get("layers") or set()),
            "blocks": set(getattr(self, "_vector_direct_selection", {}).get("blocks") or set()),
        }
        key = str(getattr(self, "_vector_direct_key", "") or "")
        if key:
            self._save_vector_filter_draft(key, {
                "layers": sorted(current["layers"], key=str.casefold),
                "blocks": sorted(current["blocks"], key=str.casefold),
            })
        self._vector_direct_saved_selection = {"layers": set(current["layers"]), "blocks": set(current["blocks"])}
        self._vector_direct_dirty = False
        self._vector_direct_undo_stack = []
        self.status_label.setText("도면 직접선택 · 현재 레이어/블록 표시상태를 저장했습니다.")

    def _request_close_vector_direct_select(self):
        """저장하지 않은 변경이 있으면 확인 후 저장 기준점으로 복원하고 종료한다."""
        if bool(getattr(self, "_vector_direct_all_layers_view", False)):
            self._vector_direct_all_layers_view = False
            self._vector_direct_pending_on = set()
            self._vector_direct_pending_on_history = []
            try:
                self.rooms_tab.preview.set_vector_direct_all_layers_view(False)
                self.rooms_tab.preview.clear_vector_direct_selection()
            except Exception:
                pass
            current = getattr(self, "_vector_direct_selection", {}) or {}
            key = str(getattr(self, "_vector_direct_key", "") or "")
            if key:
                self._apply_vector_layer_filter(key, {
                    "layers": sorted(set(current.get("layers") or set()), key=str.casefold),
                    "blocks": sorted(set(current.get("blocks") or set()), key=str.casefold),
                })
        if bool(getattr(self, "_vector_direct_dirty", False)):
            answer = QMessageBox.question(
                self, "도면 직접선택",
                "현재 변경사항을 저장하지 않고 종료할까요?\n저장하지 않으면 마지막 저장 상태로 되돌립니다.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            saved = getattr(self, "_vector_direct_saved_selection", {}) or {}
            restore = {
                "layers": set(saved.get("layers") or set()),
                "blocks": set(saved.get("blocks") or set()),
            }
            self._vector_direct_selection = restore
            key = str(getattr(self, "_vector_direct_key", "") or "")
            if key:
                self._apply_vector_layer_filter(key, {
                    "layers": sorted(restore["layers"], key=str.casefold),
                    "blocks": sorted(restore["blocks"], key=str.casefold),
                })
        self._end_vector_direct_select(close_status=True)

    def _end_vector_direct_select(self, close_status: bool = True):
        preview = getattr(getattr(self, "rooms_tab", None), "preview", None)
        if preview is not None:
            try:
                preview.vector_direct_selection_changed.disconnect(self._on_vector_direct_selection_changed)
            except Exception:
                pass
            try:
                preview.vector_direct_undo_requested.disconnect(self._undo_vector_direct_off)
            except Exception:
                pass
            try:
                preview.vector_direct_off_requested.disconnect(self._apply_vector_direct_off)
            except Exception:
                pass
            try:
                preview.set_vector_direct_all_layers_view(False)
                preview.set_vector_direct_select_mode(False)
            except Exception:
                pass
        status = getattr(self, "_vector_direct_status", None)
        self._vector_direct_status = None
        if close_status and status is not None:
            try:
                status.blockSignals(True)
                status._allow_close = True
                status.close()
                status.deleteLater()
            except Exception:
                pass
        self._vector_direct_key = ""
        self._vector_direct_selection = {}
        self._vector_direct_all_items = {}
        self._vector_direct_saved_selection = {}
        self._vector_direct_pending_on = set()
        self._vector_direct_pending_on_history = []
        self._vector_direct_all_layers_view = False
        self._vector_direct_stage_before_all_layers = "raw"
        self._vector_direct_dirty = False
        self._vector_direct_undo_stack = []
        self._vector_direct_live_apply = False
        self._vector_direct_live_busy = False

    def _save_vector_filter_draft(self, key: str, selection: dict):
        """2단 레이어/블록 체크 상태만 프로젝트 캐시 쪽에 저장한다. 3단 도형은 재생성하지 않는다."""
        payload = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}
        payload = dict(payload)
        payload["draft_selection"] = {
            "layers": sorted(set((selection or {}).get("layers") or []), key=str.casefold),
            "blocks": sorted(set((selection or {}).get("blocks") or []), key=str.casefold),
        }
        payload.setdefault("floors", {})
        payload.setdefault("group_settings", {})
        payload.setdefault("stats", {})
        save_background_program_cache(BACKGROUND_CACHE_ROOT, key, payload)
        return payload

    def _apply_vector_layer_filter(self, key: str, selection: dict):
        """레이어 탭 체크값을 현재 표시 단계에 적용한다. 최적화 geometry는 재생성하지 않는다."""
        selection = {
            "layers": sorted(set((selection or {}).get("layers") or []), key=str.casefold),
            "blocks": sorted(set((selection or {}).get("blocks") or []), key=str.casefold),
        }
        self._save_vector_filter_draft(key, selection)
        stage = str(getattr(self.rooms_tab, "vector_display_stage", "") or "").strip().lower()
        program_cache = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}

        if stage == "optimized" and program_cache.get("floors"):
            payload = dict(program_cache)
            # 그룹 편집에서 새로 만든 최적화 전용 레이어는 2단 원본 레이어 목록에 없으므로
            # 원본 레이어 체크를 다시 적용해도 사라지지 않도록 항상 표시 선택에 합친다.
            optimized_selection = dict(selection)
            optimized_layers = set(optimized_selection.get("layers") or [])
            optimized_layers.update(str(v) for v in payload.get("manual_layers") or [])
            optimized_selection["layers"] = sorted(optimized_layers, key=str.casefold)
            payload["display_selection"] = dict(optimized_selection)
            save_background_program_cache(BACKGROUND_CACHE_ROOT, key, payload)
            source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
            filtered = filter_program_floors(payload, optimized_selection, source_cache)
            self.rooms_tab.set_vector_background_cache(filtered, preserve_layout=True)
            self.rooms_tab.set_vector_display_stage("optimized")
            self.rooms_tab.set_background_ready(bool(filtered))
            self._save_background_project_snapshot_now(key)
            return {"mode": "optimized", "program_cache": payload}

        source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
        if not source_cache or not source_cache.get("floors"):
            raise RuntimeError("원본 정리 캐시가 없습니다.")
        filtered = build_raw_preview_floors(source_cache, selection)
        self.rooms_tab.set_vector_background_cache(filtered, preserve_layout=True)
        self.rooms_tab.set_vector_display_stage("raw")
        self.rooms_tab.set_background_ready(bool(filtered))
        return {"mode": "raw", "program_cache": program_cache}

    def _apply_original_vector_view(self, key: str):
        source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
        if not source_cache or not source_cache.get("floors"):
            raise RuntimeError("원본 정리 캐시를 찾지 못했습니다.")
        # 2단 원본 캐시(XCLIP 처리 완료)를 가공/필터 없이 그대로 현재 도곽에 표시한다.
        # 3단 최적화 캐시는 삭제하거나 덮어쓰지 않는다.
        original_floors = dict(source_cache.get("floors") or {})
        self.rooms_tab.set_vector_background_cache(original_floors, preserve_layout=True)
        self.rooms_tab.set_vector_display_stage("raw")
        self.rooms_tab.set_background_ready(True)
        source_items = (
            len(source_cache.get("segments") or []) +
            len(source_cache.get("annotation_segments") or []) +
            len(source_cache.get("texts") or []) +
            len(source_cache.get("fills") or [])
        )
        self.status_label.setText(f"원도면 표시 · 2단 원본 캐시 {source_items:,}개 · 최적화 캐시는 보존")
        return source_cache

    def _build_optimized_vector_cache(self, key: str, selection: dict):
        source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
        if not source_cache:
            raise RuntimeError("원본 정리 캐시가 없습니다.")
        old_program = load_background_program_cache(BACKGROUND_CACHE_ROOT, key) or {}
        # 새 최적화 도면 생성은 현재 레이어/블록 선택으로 geometry를 다시 만드는 작업이다.
        # 이전 최적화 보기에서 BLOCK/HATCH 등을 숨겼던 상태까지 승계하면
        # 새로 생성한 도형이 누락된 것처럼 보이므로 표시상태는 전부 ON으로 초기화한다.
        # 사용자가 조정한 투명도 가중치만 유지한다.
        previous_group_settings = dict(old_program.get("group_settings") or {})
        old_group_settings = {}
        for group_key in ("LINE", "WINDOW", "BLOCK", "DIMENSION", "TEXT", "HATCH"):
            raw_group = dict(previous_group_settings.get(group_key) or {})
            try:
                opacity_offset = int(raw_group.get("opacity_offset", 0) or 0)
            except Exception:
                opacity_offset = 0
            old_group_settings[group_key] = {
                "visible": True,
                "opacity_offset": max(-100, min(100, opacity_offset)),
            }
        self._set_progress_status("최적화 도면 생성 중 · 원본 정리 캐시는 유지")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            payload = build_program_cache(source_cache, selection, group_settings=old_group_settings)
            save_background_program_cache(BACKGROUND_CACHE_ROOT, key, payload)
            self.rooms_tab.set_vector_background_cache(payload.get("floors") or {}, preserve_layout=True)
            self.rooms_tab.set_vector_display_stage("optimized")
            self.rooms_tab.set_background_ready(bool(payload.get("floors")))
            stats = dict(payload.get("stats") or {})
            counts = dict(stats.get("group_counts") or {})
            self.status_label.setText(
                "최적화 도면 생성 완료 · "
                f"선 {counts.get('LINE', 0):,} · 창호 {counts.get('WINDOW', 0):,} · 블록 {counts.get('BLOCK', 0):,} · "
                f"치수 {counts.get('DIMENSION', 0):,} · 문자 {counts.get('TEXT', 0):,} · 해치 {counts.get('HATCH', 0):,} · "
                f"중복 제거 {stats.get('duplicates_removed', 0):,} · 직선 병합 {stats.get('collinear_merged', 0):,} · "
                f"POLYLINE {stats.get('polylines_created', 0):,}개 재구성 "
                f"(선분 {stats.get('polyline_source_segments', 0):,}→{stats.get('polylines_created', 0):,})"
            )
            self._schedule_current_cad_project_cache_save()
            self._save_background_project_snapshot_now(key)
            return payload
        except Exception:
            detail = traceback.format_exc()
            self._show_copyable_error(
                self, "도면 정리 오류",
                "최적화 도면 생성 중 오류가 발생했습니다. 원본 정리 캐시는 변경하지 않았습니다.",
                detail, None,
            )
            raise
        finally:
            QApplication.restoreOverrideCursor()

    def _apply_optimized_group_settings(self, key: str, group_settings: dict):
        payload = load_background_program_cache(BACKGROUND_CACHE_ROOT, key)
        if not payload or not payload.get("floors"):
            raise RuntimeError("먼저 [최적화 도면 생성]을 실행해 주세요.")
        payload = dict(payload)
        payload["group_settings"] = dict(group_settings or {})
        floors = {}
        for floor, raw in dict(payload.get("floors") or {}).items():
            info = dict(raw or {})
            info["group_settings"] = dict(payload["group_settings"])
            floors[str(floor)] = info
        payload["floors"] = floors
        save_background_program_cache(BACKGROUND_CACHE_ROOT, key, payload)
        display_selection = dict(payload.get("display_selection") or {})
        source_cache = load_background_source_cache(BACKGROUND_CACHE_ROOT, key)
        display_floors = filter_program_floors(payload, display_selection, source_cache) if display_selection else floors
        self.rooms_tab.set_vector_background_cache(display_floors, preserve_layout=True)
        self.rooms_tab.set_vector_display_stage("optimized")
        self.rooms_tab.set_background_ready(True)
        self._save_background_project_snapshot_now(key)
        return payload

    def request_background_regeneration(self, force_new: bool = False, scope: str | None = None):
        if not self.current_path:
            QMessageBox.information(self, "CAD 배경 재생성", "먼저 DXF 파일 또는 CAD 도면을 불러오십시오.")
            return

        # V18.4: 사용자가 재생성 버튼을 누르면 반드시 현재층/전체층 범위를 먼저 선택한다.
        # 설정 변경 등 내부 호출(force_new=True)은 기존 동작을 유지하기 위해 전체층을 기본으로 한다.
        current_floor = self.rooms_tab.selected_floor()
        if scope not in ("current", "all"):
            if force_new:
                scope = "all"
            else:
                box = QMessageBox(self)
                box.setWindowTitle("CAD 배경 재생성 범위")
                box.setIcon(QMessageBox.Question)
                floor_text = str(current_floor or "현재층")
                box.setText("어느 범위의 CAD 배경을 재생성할까요?")
                box.setInformativeText(f"현재층: {floor_text}\n\n현재층만 생성하면 다른 층의 기존 배경 캐시는 그대로 유지합니다.")
                current_btn = box.addButton(f"현재층만 ({floor_text})", QMessageBox.AcceptRole)
                all_btn = box.addButton("전체층", QMessageBox.ActionRole)
                cancel_btn = box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(current_btn)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel_btn or clicked is None:
                    return
                scope = "current" if clicked is current_btn else "all"

        if scope == "current" and not current_floor:
            QMessageBox.information(self, "CAD 배경 재생성", "현재 선택된 층을 확인할 수 없습니다.")
            return

        target_rooms = list(self.current_rooms)
        preserve_existing = False
        if scope == "current":
            target_rooms = [room for room in self.current_rooms if room.floor == current_floor]
            preserve_existing = True
            # 실이 없는 초기 단계에서는 도곽/층 정보로 해당 층 배경을 만들 수 있으므로
            # 빈 target_rooms 자체를 오류로 취급하지 않는다. BackgroundScanWorker가 도곽에서 층을 판정한다.

        background_dxf = self._background_dxf_source_path()
        use_connected_cad = bool(
            self.cad_bridge.connected
            and self.current_path
            and not str(self.current_path).lower().endswith(".dxf")
        )
        if not use_connected_cad and not background_dxf:
            QMessageBox.information(
                self,
                "CAD 배경 재생성",
                f"DXF 배경 원본을 찾지 못했습니다.\n\n{TEMP_PROJECT_DIR}",
            )
            return

        if any(worker.isRunning() for worker in self.background_workers):
            self.status_label.setText("CAD 배경 작업이 이미 진행 중입니다. 다른 작업은 계속할 수 있습니다.")
            return

        target_text = f"{current_floor} 배경" if scope == "current" else "전체층 배경"
        self._set_progress_status(f"{target_text} 프로젝트 DXF 백그라운드 작업 시작 · 다른 작업 가능")
        # 실제 적용된 DXF를 읽고, 캐시 키는 현재 작업 원본 경로를 유지하여 이후 계속 재사용한다.
        if use_connected_cad:
            self.start_cad_background_scan(
                self._settings_values(), rooms=target_rooms,
                preserve_existing=preserve_existing, refresh_source=(not force_new),
            )
        else:
            self.start_background_scan(
                background_dxf,
                cache_source_path=self.current_path,
                rooms=target_rooms,
                preserve_existing=preserve_existing,
                refresh_source=(not force_new),
            )

    def cleanup_background_worker(self, worker):
        if worker in self.background_workers:
            self.background_workers.remove(worker)
        worker.deleteLater()

    def _set_progress_status(self, message: str):
        """작업 진행/작업률 문구는 파란색 굵은 글씨로 표시한다."""
        self.status_label.setStyleSheet("color: #2196F3; font-weight: 600;")
        self.status_label.setText(str(message or ""))

    def _clear_progress_status_style(self):
        self.status_label.setStyleSheet("")

    def background_scan_progress(self, path: str, message: str):
        self._set_progress_status(message)

    def background_scan_done(self, path: str, diagnostic_text: str = "", diagnostic_path: str = ""):
        self.rooms_tab.set_background_loading(False)
        self._clear_progress_status_style()
        if path != self.current_path:
            return
        cache_files = {}
        if self.background_render_mode != "vector":
            cache_files = load_background_cache(
                self.current_path, BACKGROUND_CACHE_ROOT, self.background_max_pixels, self.background_style, self._background_filter_signature(), self.background_line_width, self.background_aci_widths, self.background_keep_red
            )
        vector_cache = self._load_vector_background_cache(self.current_path)
        if cache_files or vector_cache:
            if cache_files:
                self.rooms_tab.set_background_cache_files(cache_files)
            self.rooms_tab.set_background_ready(True)
            mode_text = "벡터" if self.background_render_mode == "vector" else "PNG"
            self._save_background_project_snapshot_now(self.current_path)
            self.status_label.setText(f"CAD 배경 캐시 생성 완료 · {mode_text} 표시 · 프로젝트 캐시 저장 완료 · 이후 계속 재사용")
        else:
            self.rooms_tab.set_background_ready(False)
            self.status_label.setText("CAD 배경 로딩 실패")

        # V24.03.3: 벡터 읽기가 끝난 뒤 블록/XREF 진단을 바로 복사할 수 있게 띄운다.
        # 사용자가 별도 TXT를 찾아갈 필요가 없도록 하며 TXT 저장은 보조로 유지한다.
        if diagnostic_text:
            text = str(diagnostic_text)
            log = str(diagnostic_path or "")
            signature = (str(path), text)
            # 같은 작업의 finished 신호/후속 갱신 때문에 동일 진단창이 중복 표시되지 않게 한다.
            if getattr(self, "_last_vector_diagnostic_signature", None) != signature:
                self._last_vector_diagnostic_signature = signature
                QTimer.singleShot(0, lambda text=text, log=log: self._show_copyable_error(
                    self,
                    "블록/XREF 벡터 진단",
                    "벡터 읽기가 완료되었습니다. 아래 진단 내용을 바로 복사할 수 있습니다.",
                    text,
                    Path(log) if log else None,
                ))

    def background_scan_failed(self, path: str, detail: str):
        self.rooms_tab.set_background_loading(False)
        self._clear_progress_status_style()
        # 기존 배경이 있으면 그대로 유지한다. 백그라운드 작업 실패 때문에 화면을 비우지 않는다.
        if (
            path == self.current_path
            and not self.rooms_tab.preview.floor_background_files
            and not self.rooms_tab.preview.floor_vector_cache
        ):
            self.rooms_tab.set_background_ready(False)

        detail_text = str(detail or "오류 상세가 전달되지 않았습니다.")
        source_text = str(path or "(경로 없음)")
        current_text = str(self.current_path or "(현재 도면 없음)")
        log_text = (
            "CAD 배경 백그라운드 작업 실패\n"
            f"배경 원본/캐시 키: {source_text}\n"
            f"현재 도면: {current_text}\n"
            f"도곽/층 지정 수: {len(self.current_drawing_divisions or [])}\n"
            f"실 수: {len(self.current_rooms or [])}\n\n"
            "[오류 상세]\n"
            f"{detail_text}"
        )
        log_path = None
        try:
            TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            log_path = TEMP_PROJECT_CACHE_DIR / "CAD배경_백그라운드_오류.txt"
            log_path.write_text(log_text, encoding="utf-8")
        except Exception:
            log_path = None

        self.status_label.setText("CAD 배경 백그라운드 작업 실패 · 오류 로그 확인")
        self._show_copyable_error(
            self,
            "CAD 배경 백그라운드 작업 실패",
            "CAD 배경 생성 중 오류가 발생했습니다. 아래 내용을 그대로 복사할 수 있습니다.",
            log_text,
            log_path,
        )

    def scan_failed(self, detail):
        self.progress.setVisible(False)
        self._clear_progress_status_style()
        self.open_btn.setEnabled(True)
        self._show_copyable_error(self, "DXF 처리 오류", "DXF 처리 중 오류가 발생했습니다.", str(detail or ""))
        self.status_label.setText("오류 발생")

    def _refresh_window_tab_title(self):
        try:
            index = self.tabs.indexOf(self.window_tab)
            if index >= 0:
                self.tabs.setTabText(index, f"창호({len(self.window_tab.window_catalog()):,})")
        except Exception:
            pass

    def current_material_table(self):
        # 재료 화면은 V23.17부터 메인탭이 아니라 별도 창으로 연다.
        return self.material_tab.current_table() if hasattr(self, "material_tab") else None

    def apply_filter(self):
        table = self.current_material_table()
        self.search.setEnabled(table is not None)
        self.sort_combo.setEnabled(table is not None)
        if table is None:
            return
        needle = self.search.text().strip().casefold()
        for row in range(table.rowCount()):
            texts = []
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item:
                    texts.append(item.text().casefold())
            table.setRowHidden(row, bool(needle) and needle not in " ".join(texts))

    def apply_sort(self):
        mode = self.sort_combo.currentText()
        for table in self.tables.values():
            if mode == "출현횟수 많은 순":
                table.sortItems(4, Qt.DescendingOrder)
            else:
                table.sortItems(0, Qt.AscendingOrder)

    def export_excel(self):
        start_dir = self.settings.get("Path", "LastExcelFolder", "")
        default_name = Path(self.current_path).stem + "_추출결과.xlsx"
        suggested = str(Path(start_dir) / default_name) if start_dir else default_name
        out, _ = QFileDialog.getSaveFileName(
            self, "Excel 저장", suggested, "Excel 통합문서 (*.xlsx)"
        )
        if not out:
            return
        self.settings.set("Path", "LastExcelFolder", str(Path(out).parent))
        self.settings.save()

        try:
            wb = Workbook()
            wb.remove(wb.active)

            for category in ["재료 후보", "보류", "제외"]:
                ws = wb.create_sheet(category)
                headers = ResultTable.headers
                ws.append(headers)
                for row in self.current_result[category]:
                    ws.append([
                        row.normalized, row.raw_example, row.material, row.thickness,
                        row.count, row.layers, row.entity_types, row.handles
                    ])
                self.format_sheet(ws, [50, 50, 22, 14, 12, 28, 16, 35])

            ws = wb.create_sheet("실 목록")
            ws.append(["대표층", "적용층", "개소", "NUMBER", "실명", "구분", "층높이(m)", "천정고(m)", "1개소 둘레(m)", "1개소 면적(M2)", "산출 둘레(m)", "산출 면적(M2)", "구분선 핸들", "실명블록 핸들", "닫힘"])
            for room in self.current_rooms:
                ws.append([
                    room.floor, room.floor_scope or room.floor, max(1, int(room.repeat_count or 1)), room.room_number, room.room_name,
                    room.room_category, room.floor_height_m, room.ceiling_height_m,
                    room.perimeter_m, room.area_m2, room.quantity_perimeter_m, room.quantity_area_m2,
                    room.boundary_handle, room.tag_handle,
                    "예" if room.is_closed else "아니오"
                ])
            self.format_sheet(ws, [12, 18, 8, 12, 28, 12, 12, 12, 14, 14, 14, 14, 18, 18, 10])

            ws = wb.create_sheet("실 오류")
            ws.append(["오류 / 확인사항"])
            combined_errors = list(self.current_room_errors)
            for msg in validate_room_numbers(self.current_rooms):
                if msg not in combined_errors:
                    combined_errors.append(msg)
            for msg in combined_errors:
                ws.append([msg])
            self.format_sheet(ws, [100])

            wb.save(out)
            QMessageBox.information(self, "저장 완료", out)
        except Exception:
            QMessageBox.critical(self, "Excel 저장 오류", traceback.format_exc())

    @staticmethod
    def format_sheet(ws, widths):
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for i, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    def _migrate_recent_settings_once(self):
        """기존 settings.ini의 최근 작업 키를 settings2.ini로 1회 이전한다."""
        if RECENT_SETTINGS_PATH.exists():
            return
        keys = ("LastDXFPath", "LastCADDocumentPath", "LastCADDocumentName")
        copied = False
        for key in keys:
            value = self.settings.get("Path", key, "").strip()
            if value:
                self.recent_settings.set("Path", key, value)
                copied = True
        if copied:
            self.recent_settings.save()

    def _capture_launch_bat(self):
        """run.bat이 전달한 실제 실행 BAT 경로를 최근 작업 설정에 보관한다."""
        bat_text = os.environ.get("DXF_LAUNCH_BAT", "").strip().strip('"')
        if not bat_text:
            return
        try:
            bat_path = Path(bat_text).resolve()
        except Exception:
            bat_path = Path(bat_text)
        if bat_path.is_file() and bat_path.suffix.lower() == ".bat":
            self.recent_settings.set("Path", "LastRunBat", str(bat_path))
            self.recent_settings.save()

    def _save_recent_cad(self, info):
        self.recent_settings.set("Path", "LastCADDocumentPath", info.document_path or "")
        self.recent_settings.set("Path", "LastCADDocumentName", info.document_name or "")
        self.recent_settings.save()

    def _startup_active_cad_name(self) -> str:
        """시작창 표시용 CAD 도면명만 읽는다.

        V16.3: 시작창 표시 단계에서 CadBridge.connect_to_active()/disconnect()를 호출하지 않는다.
        브리지 상태를 잠깐 연결했다 끊는 과정이 이후 실제 연동 시 ActiveDocument 초기화 순서에
        영향을 줄 수 있으므로, 표시용 조회는 독립 COM 객체로 이름만 읽고 즉시 버린다.
        """
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            prog_ids = list(self.cad_bridge.PROG_IDS)
            last_prog_id = getattr(self.cad_bridge, "_last_prog_id", "")
            if last_prog_id in prog_ids:
                prog_ids.remove(last_prog_id)
                prog_ids.insert(0, last_prog_id)
            for prog_id in prog_ids:
                try:
                    app = win32com.client.GetActiveObject(prog_id)
                    doc = app.ActiveDocument
                    if doc is None:
                        continue
                    name = str(getattr(doc, "Name", "") or "").strip()
                    if name:
                        return name
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    def show_startup_restore_dialog(self):
        last_dxf = self.recent_settings.get("Path", "LastDXFPath", "").strip()
        active_cad_name = self._startup_active_cad_name()
        if not last_dxf and not active_cad_name:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("최근 작업 계속하기")
        dialog.setModal(True)
        dialog.setFixedWidth(560)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        guide = QLabel("최근 작업을 열거나 현재 실행 중인 CAD에 바로 연결할 수 있습니다.")
        layout.addWidget(guide)

        if active_cad_name:
            cad_label = QLabel(f"현재 CAD: {active_cad_name}")
            cad_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(cad_label)
        else:
            layout.addWidget(QLabel("현재 CAD: 실행 중인 도면 없음"))

        if last_dxf:
            dxf_name = Path(last_dxf).name or last_dxf
            dxf_label = QLabel(f"최근 DXF: {dxf_name}")
            dxf_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(dxf_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        if last_dxf:
            dxf_btn = QPushButton("최근 DXF 열기")
            dxf_btn.setEnabled(Path(last_dxf).is_file())
            dxf_btn.clicked.connect(lambda: (dialog.accept(), self.load_dxf_path(last_dxf)))
            button_row.addWidget(dxf_btn, 1)

        cad_btn = QPushButton("현재 CAD 연동")
        cad_btn.setEnabled(bool(active_cad_name))
        cad_btn.clicked.connect(lambda: self._startup_connect_cad(dialog))
        button_row.addWidget(cad_btn, 1)

        blank_btn = QPushButton("빈 화면 시작")
        blank_btn.clicked.connect(dialog.reject)
        button_row.addWidget(blank_btn, 1)
        layout.addLayout(button_row)

        dialog.adjustSize()
        dialog.exec()

    def _startup_connect_cad(self, dialog):
        """시작창 연동도 일반 CAD 연동과 동일한 실제 연결 순서를 사용한다."""
        dialog.accept()
        # 모달 시작창이 완전히 닫힌 다음 연결한다. 시작창 표시용 COM 조회와 실제 연결이
        # 같은 이벤트 사이클에서 겹치지 않게 하여 ZWCAD ActiveDocument를 확정한다.
        QTimer.singleShot(120, self._startup_connect_cad_after_dialog)

    def _startup_connect_cad_after_dialog(self):
        try:
            self._connect_active_cad_common()
            # 연결 직후 ActiveDocument를 한 번 더 확인해 브리지의 doc/info를 현재 활성 도면으로
            # 확정한다. 이 단계에서도 ModelSpace/레이어/블록은 읽지 않는다.
            self._refresh_connected_active_document()
            self._check_connected_cad_project(None, show_warning=False)
        except CadConnectionError as exc:
            self.cad_bridge.disconnect()
            self.rooms_tab.set_cad_connected_mode(False)
            QMessageBox.warning(self, "CAD 연결", str(exc))
        except Exception:
            self.cad_bridge.disconnect()
            self.rooms_tab.set_cad_connected_mode(False)
            QMessageBox.critical(self, "CAD 연결 오류", traceback.format_exc())

    def restart_application(self):
        # settings2.ini에 저장된 실제 실행 BAT만 사용한다. Windows Unicode 경로는 os.startfile로 재실행한다.
        bat_text = self.recent_settings.get("Path", "LastRunBat", "").strip().strip('"')
        if not bat_text:
            QMessageBox.warning(self, "재실행", "settings2.ini에 마지막 실행 BAT 경로가 없습니다.\nrun.bat으로 한 번 실행한 뒤 다시 시도하십시오.")
            return
        bat_path = Path(bat_text)
        if not bat_path.is_file() or bat_path.suffix.lower() != ".bat":
            QMessageBox.warning(self, "재실행", f"마지막 실행 BAT를 찾을 수 없습니다.\n\n{bat_path}")
            return
        answer = QMessageBox.question(self, "프로그램 재실행", f"프로그램을 종료하고 다시 실행하시겠습니까?\n\n{bat_path}", QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        try:
            # CreateProcess의 wide-char argv로 BAT 경로를 자식 Python에 전달한다.
            # 자식은 현재 PID가 종료된 뒤 os.startfile()을 사용하므로 한글/공백 경로를 CMD 파일에 기록하지 않는다.
            helper_code = (
                "import os,sys,time,subprocess\n"
                "pid=int(sys.argv[1]); bat=sys.argv[2]\n"
                "for _ in range(300):\n"
                " r=subprocess.run(['tasklist','/FI',f'PID eq {pid}'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout\n"
                " if str(pid).encode('ascii') not in r: break\n"
                " time.sleep(0.1)\n"
                "os.startfile(bat)\n"
            )
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([sys.executable, "-c", helper_code, str(os.getpid()), str(bat_path)], cwd=str(bat_path.parent), creationflags=flags, close_fds=True)
            self.close()
        except Exception as exc:
            QMessageBox.critical(self, "재실행 오류", str(exc))

    def closeEvent(self, event):
        if hasattr(self, "project_autosave_timer"):
            self.project_autosave_timer.stop()
        if self.project_autosave_worker is not None and self.project_autosave_worker.isRunning():
            self.project_autosave_worker.wait()
        self.settings.set("Window", "Width", self.width())
        self.settings.set("Window", "Height", self.height())
        self.settings.set("Window", "Maximized", self.isMaximized())
        self.settings.save()
        super().closeEvent(event)



def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from app.common.ui_style import apply_global_table_style
    apply_global_table_style(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
