from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.common.project_manager import ProjectError, ProjectManager, ProjectRecord


class BuildingGroupTable(QTableWidget):
    """동명 목록: Enter로 현재 셀 편집을 시작한다."""

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
            item = self.currentItem()
            if item is not None and bool(item.flags() & Qt.ItemIsEditable):
                self.editItem(item)
                return
        super().keyPressEvent(event)


class DrawingLibraryPicker(QDialog):
    """기존 공용 DXF 도면목록과 같은 저장소에서 프로젝트 기본도면을 고른다."""

    def __init__(self, manager: ProjectManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.selected_path: Path | None = None
        self.setWindowTitle("도면 목록에서 기본도면 적용")
        self.resize(760, 500)

        root = QVBoxLayout(self)
        guide = QLabel("공용 도면목록에서 기본도면을 선택합니다. 외부 DXF는 먼저 공용 목록에 불러올 수 있습니다.")
        guide.setWordWrap(True)
        root.addWidget(guide)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["도면명", "DXF 파일"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.load_btn = QPushButton("도면 불러오기")
        self.apply_btn = QPushButton("선택 도면 적용")
        close_btn = QPushButton("닫기")
        row.addWidget(self.load_btn)
        row.addWidget(self.apply_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        root.addLayout(row)

        self.load_btn.clicked.connect(self._load_drawing)
        self.apply_btn.clicked.connect(self._accept_selected)
        close_btn.clicked.connect(self.reject)
        self.table.doubleClicked.connect(lambda _index: self._accept_selected())
        self._refresh()

    def _files(self) -> list[Path]:
        self.manager.drawings_dir.mkdir(parents=True, exist_ok=True)
        return sorted(self.manager.drawings_dir.glob("*.dxf"), key=lambda path: path.stem.casefold())

    def _refresh(self, selected: Path | None = None):
        files = self._files()
        self.table.setRowCount(len(files))
        selected_row = -1
        for row, path in enumerate(files):
            name_item = QTableWidgetItem(path.stem)
            file_item = QTableWidgetItem(path.name)
            name_item.setData(Qt.UserRole, str(path))
            file_item.setData(Qt.UserRole, str(path))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, file_item)
            if selected is not None and path.resolve() == selected.resolve():
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif files:
            self.table.selectRow(0)
        self.apply_btn.setEnabled(bool(files))

    def _selected(self) -> Path | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(Qt.UserRole) if item else ""
        return Path(value) if value else None

    def _load_drawing(self):
        source, _ = QFileDialog.getOpenFileName(self, "도면 불러오기", "", "DXF 파일 (*.dxf)")
        if not source:
            return
        source_path = Path(source)
        name, ok = QInputDialog.getText(self, "도면명 입력", "공용 도면목록에 표시할 이름을 입력하세요.", text=source_path.stem)
        if not ok:
            return
        safe_name = self.manager.safe_library_name(name)
        destination = self.manager.drawings_dir / f"{safe_name}.dxf"
        if destination.exists() and destination.resolve() != source_path.resolve():
            answer = QMessageBox.question(
                self,
                "도면 불러오기",
                f"'{safe_name}' 도면이 이미 있습니다. 덮어쓸까요?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if destination.resolve() != source_path.resolve():
            shutil.copy2(source_path, destination)
        self._refresh(destination)

    def _accept_selected(self):
        path = self._selected()
        if not path or not path.is_file():
            QMessageBox.information(self, "도면 적용", "적용할 도면을 선택해 주세요.")
            return
        self.selected_path = path
        self.accept()


class ProjectListDialog(QDialog):
    def __init__(self, manager: ProjectManager, last_project_id: str = "", parent=None, startup: bool = False):
        super().__init__(parent)
        self.manager = manager
        self.last_project_id = str(last_project_id or "")
        self.startup = bool(startup)
        self.selected_project: ProjectRecord | None = None
        self._records: dict[str, ProjectRecord] = {}
        self._current_project_id = ""
        self._loading_details = False
        self._loaded_project_name = ""
        self._details_dirty = False
        self._building_dirty = False

        self.setWindowTitle("프로젝트 목록")
        self.resize(1280, 820)
        self.setMinimumSize(1050, 700)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        toolbar = QHBoxLayout()
        self.create_btn = QPushButton("생성")
        self.delete_btn = QPushButton("삭제")
        self.drawing_btn = QPushButton("도면 적용")
        self.duplicate_btn = QPushButton("복제")
        self.export_btn = QPushButton("내보내기")
        self.import_btn = QPushButton("가져오기")
        for button in (
            self.create_btn,
            self.delete_btn,
            self.drawing_btn,
            self.duplicate_btn,
            self.export_btn,
            self.import_btn,
        ):
            button.setMinimumHeight(32)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("검색"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("프로젝트명, 의뢰처, 위치 또는 개요 검색")
        search_row.addWidget(self.search_edit, 1)
        root.addLayout(search_row)

        list_group = QGroupBox("프로젝트 목록")
        list_group.setMinimumHeight(220)
        list_layout = QVBoxLayout(list_group)
        self.project_table = QTableWidget(0, 4)
        self.project_table.setHorizontalHeaderLabels(["번호", "프로젝트명", "작성일", "적용 도면"])
        self.project_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.project_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.project_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.project_table.setColumnWidth(0, 52)
        self.project_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.project_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.project_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        list_layout.addWidget(self.project_table)

        detail_group = QGroupBox("프로젝트 정보")
        detail_group.setMinimumWidth(420)
        detail_group.setMinimumWidth(520)
        detail_layout = QVBoxLayout(detail_group)
        detail_layout.setSpacing(5)
        form = QGridLayout()
        form.setHorizontalSpacing(7)
        form.setVerticalSpacing(4)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)
        self.name_edit = QLineEdit()
        self.created_edit = QLineEdit()
        self.created_edit.setReadOnly(True)
        self.base_drawing_edit = QLineEdit()
        self.base_drawing_edit.setReadOnly(True)
        self.client_edit = QLineEdit()
        self.location_edit = QLineEdit()

        def metric_spin(suffix: str, maximum: float = 999_999_999.0):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, maximum)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setGroupSeparatorShown(True)
            spin.setSpecialValueText("")
            spin.setSuffix(suffix)
            return spin

        self.building_area_spin = metric_spin(" M2")
        self.gross_area_spin = metric_spin(" M2")
        self.height_spin = metric_spin(" m", 100_000.0)
        self.depth_spin = metric_spin(" m", 100_000.0)
        self.construction_months_spin = QSpinBox()
        self.construction_months_spin.setRange(0, 1200)
        self.construction_months_spin.setSpecialValueText("")
        self.construction_months_spin.setSuffix(" 개월")
        self.construction_type_edit = QLineEdit()
        self.exterior_material_edit = QLineEdit()
        self.building_structure_edit = QLineEdit()
        self.insulation_combo = QComboBox()
        self.insulation_combo.addItems(["", "내단열", "외단열", "복합"])
        self.insulation_combo.setFrame(False)

        form.addWidget(QLabel("프로젝트명"), 0, 0)
        form.addWidget(self.name_edit, 0, 1, 1, 3)
        form.addWidget(QLabel("작성일"), 1, 0)
        form.addWidget(self.created_edit, 1, 1)
        form.addWidget(QLabel("적용 도면"), 1, 2)
        form.addWidget(self.base_drawing_edit, 1, 3)
        form.addWidget(QLabel("의뢰처"), 2, 0)
        form.addWidget(self.client_edit, 2, 1)
        form.addWidget(QLabel("위치"), 2, 2)
        form.addWidget(self.location_edit, 2, 3)
        form.addWidget(QLabel("건축면적"), 3, 0)
        form.addWidget(self.building_area_spin, 3, 1)
        form.addWidget(QLabel("연면적"), 3, 2)
        form.addWidget(self.gross_area_spin, 3, 3)
        form.addWidget(QLabel("높이"), 4, 0)
        form.addWidget(self.height_spin, 4, 1)
        form.addWidget(QLabel("깊이"), 4, 2)
        form.addWidget(self.depth_spin, 4, 3)
        form.addWidget(QLabel("공사기간"), 5, 0)
        form.addWidget(self.construction_months_spin, 5, 1)
        form.addWidget(QLabel("공사종류"), 5, 2)
        form.addWidget(self.construction_type_edit, 5, 3)
        form.addWidget(QLabel("외장재"), 6, 0)
        form.addWidget(self.exterior_material_edit, 6, 1)
        form.addWidget(QLabel("건물구조"), 6, 2)
        form.addWidget(self.building_structure_edit, 6, 3)
        form.addWidget(QLabel("단열구분"), 7, 0)
        form.addWidget(self.insulation_combo, 7, 1)
        detail_layout.addLayout(form)
        detail_layout.addWidget(QLabel("개요"))
        self.overview_edit = QPlainTextEdit()
        self.overview_edit.setPlaceholderText("프로젝트 개요를 입력하세요.")
        detail_layout.addWidget(self.overview_edit, 1)
        self.details_status = QLabel("프로젝트명 변경은 확인 후 저장되고, 나머지 정보는 자동 저장됩니다.")
        self.details_status.setStyleSheet("color:#666;")
        detail_layout.addWidget(self.details_status)
        building_group = QGroupBox("동명 목록")
        building_group.setMinimumHeight(180)
        building_layout = QVBoxLayout(building_group)
        building_toolbar = QHBoxLayout()
        self.building_add_btn = QPushButton("추가")
        self.building_delete_btn = QPushButton("삭제")
        self.building_duplicate_btn = QPushButton("복제")
        for button in (self.building_add_btn, self.building_delete_btn, self.building_duplicate_btn):
            button.setFixedSize(58, 24)
        building_toolbar.addWidget(self.building_add_btn)
        building_toolbar.addWidget(self.building_delete_btn)
        building_toolbar.addWidget(self.building_duplicate_btn)
        building_toolbar.addStretch(1)
        self.building_table = BuildingGroupTable(0, 7)
        self.building_table.setHorizontalHeaderLabels(["번호", "동이름", "지하", "지상", "옥탑", "연면적(M2)", "연면적(평)"])
        self.building_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.building_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.building_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.building_table.verticalHeader().setVisible(False)
        self.building_table.verticalHeader().setDefaultSectionSize(23)
        header = self.building_table.horizontalHeader()
        header.setMinimumSectionSize(35)
        header.setStretchLastSection(False)
        for column in range(self.building_table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        # 초기 폭만 적당히 지정하고, 이후에는 사용자가 헤더 경계 드래그로 자유롭게 조절한다.
        for column, width in enumerate((48, 150, 48, 48, 48, 90, 90)):
            self.building_table.setColumnWidth(column, width)
        building_layout.addWidget(self.building_table)
        note = QLabel("※ 연면적을 입력하면 평은 자동 환산됩니다. 동별 내부실·재료마감·캐시 연결은 후속 작업에서 추가합니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        building_layout.addWidget(note)
        building_layout.addLayout(building_toolbar)

        # 왼쪽 절반은 프로젝트 목록과 동명 목록을 위아래로 나누고,
        # 오른쪽 프로젝트 정보는 두 목록 높이를 합친 전체 세로 공간을 사용한다.
        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.setChildrenCollapsible(False)
        self.left_splitter.setHandleWidth(5)
        self.left_splitter.addWidget(list_group)
        self.left_splitter.addWidget(building_group)
        self.left_splitter.setStretchFactor(0, 1)
        self.left_splitter.setStretchFactor(1, 1)
        self.left_splitter.setSizes([350, 350])

        self.content_splitter = QSplitter(Qt.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(5)
        self.content_splitter.addWidget(self.left_splitter)
        self.content_splitter.addWidget(detail_group)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([630, 630])
        root.addWidget(self.content_splitter, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label, 1)
        self.open_btn = QPushButton("선택 프로젝트 열기")
        self.open_btn.setMinimumHeight(32)
        close_btn = QPushButton("프로젝트 없이 시작" if self.startup else "닫기")
        close_btn.setMinimumHeight(32)
        bottom.addWidget(self.open_btn)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self._details_save_timer = QTimer(self)
        self._details_save_timer.setSingleShot(True)
        self._details_save_timer.setInterval(500)
        self._details_save_timer.timeout.connect(self._save_details_now)
        self._building_save_timer = QTimer(self)
        self._building_save_timer.setSingleShot(True)
        self._building_save_timer.setInterval(500)
        self._building_save_timer.timeout.connect(self._save_building_groups_now)

        self.create_btn.clicked.connect(self._create_project)
        self.delete_btn.clicked.connect(self._delete_project)
        self.drawing_btn.clicked.connect(self._assign_drawing)
        self.duplicate_btn.clicked.connect(self._duplicate_project)
        self.export_btn.clicked.connect(self._export_project)
        self.import_btn.clicked.connect(self._import_project)
        self.search_edit.textChanged.connect(lambda _text: self.refresh(self._current_project_id))
        self.project_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.project_table.doubleClicked.connect(lambda _index: self._open_selected())
        self.name_edit.editingFinished.connect(self._commit_name_change)
        for edit in (
            self.client_edit, self.location_edit, self.construction_type_edit,
            self.exterior_material_edit, self.building_structure_edit,
        ):
            edit.textChanged.connect(self._queue_details_save)
        for spin in (
            self.building_area_spin, self.gross_area_spin, self.height_spin,
            self.depth_spin, self.construction_months_spin,
        ):
            spin.valueChanged.connect(self._queue_details_save)
        self.insulation_combo.currentIndexChanged.connect(self._queue_details_save)
        self.overview_edit.textChanged.connect(self._queue_details_save)
        self.building_table.itemChanged.connect(self._on_building_item_changed)
        self.building_table.itemSelectionChanged.connect(self._update_building_buttons)
        self.building_add_btn.clicked.connect(self._add_building_group)
        self.building_delete_btn.clicked.connect(self._delete_building_group)
        self.building_duplicate_btn.clicked.connect(self._duplicate_building_group)
        self.open_btn.clicked.connect(self._open_selected)
        close_btn.clicked.connect(self.reject)
        self.refresh(self.last_project_id)

    def _show_error(self, title: str, exc: Exception):
        QMessageBox.warning(self, title, str(exc))

    def _selected_id(self) -> str:
        rows = self.project_table.selectionModel().selectedRows() if self.project_table.selectionModel() else []
        if not rows:
            return ""
        item = self.project_table.item(rows[0].row(), 0)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def _selected_record(self) -> ProjectRecord | None:
        return self._records.get(self._selected_id())

    def refresh(self, select_id: str = ""):
        if hasattr(self, "_details_save_timer") and not self._flush_current_edits():
            return
        records = self.manager.list_projects()
        needle = self.search_edit.text().strip().casefold()
        if needle:
            records = [
                record for record in records
                if needle in f"{record.name} {record.client} {record.location} {record.overview}".casefold()
            ]
        self._records = {record.project_id: record for record in records}
        self.project_table.blockSignals(True)
        try:
            self.project_table.setRowCount(len(records))
            selected_row = -1
            for row, record in enumerate(records):
                base = record.base_drawing_path.name if record.base_drawing_path else ""
                values = [row + 1, record.name, record.created_date, base]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.UserRole, record.project_id)
                    if column == 0:
                        item.setTextAlignment(Qt.AlignCenter)
                    self.project_table.setItem(row, column, item)
                if record.project_id == select_id:
                    selected_row = row
            if selected_row < 0 and records:
                selected_row = 0
            if selected_row >= 0:
                self.project_table.selectRow(selected_row)
        finally:
            self.project_table.blockSignals(False)
        self._load_details(self._selected_record())
        self.status_label.setText(f"프로젝트 {len(records)}개")

    def _on_selection_changed(self):
        previous_id = self._current_project_id
        if not self._flush_current_edits():
            self._select_project_row(previous_id)
            return
        self._load_details(self._selected_record())

    def _select_project_row(self, project_id: str):
        if not project_id:
            return
        self.project_table.blockSignals(True)
        try:
            for row in range(self.project_table.rowCount()):
                item = self.project_table.item(row, 0)
                if item and item.data(Qt.UserRole) == project_id:
                    self.project_table.selectRow(row)
                    break
        finally:
            self.project_table.blockSignals(False)

    def _load_details(self, record: ProjectRecord | None):
        self._loading_details = True
        try:
            self._current_project_id = record.project_id if record else ""
            self.name_edit.setText(record.name if record else "")
            self._loaded_project_name = record.name if record else ""
            self.created_edit.setText(record.created_date if record else "")
            base = record.base_drawing_path.name if record and record.base_drawing_path else ""
            self.base_drawing_edit.setText(base)
            self.client_edit.setText(record.client if record else "")
            self.location_edit.setText(record.location if record else "")
            self.building_area_spin.setValue(record.building_area_m2 if record else 0.0)
            self.gross_area_spin.setValue(record.gross_area_m2 if record else 0.0)
            self.height_spin.setValue(record.height_m if record else 0.0)
            self.depth_spin.setValue(record.depth_m if record else 0.0)
            self.construction_months_spin.setValue(record.construction_months if record else 0)
            self.construction_type_edit.setText(record.construction_type if record else "")
            self.exterior_material_edit.setText(record.exterior_material if record else "")
            self.building_structure_edit.setText(record.building_structure if record else "")
            self.insulation_combo.setCurrentText(record.insulation_type if record else "")
            self.overview_edit.setPlainText(record.overview if record else "")
            self._load_building_groups(record)
            enabled = record is not None
            for widget in (
                self.name_edit, self.client_edit, self.location_edit,
                self.building_area_spin, self.gross_area_spin, self.height_spin,
                self.depth_spin, self.construction_months_spin,
                self.construction_type_edit, self.exterior_material_edit,
                self.building_structure_edit, self.insulation_combo,
                self.overview_edit, self.building_table, self.building_add_btn,
            ):
                widget.setEnabled(enabled)
            for button in (self.delete_btn, self.drawing_btn, self.duplicate_btn, self.export_btn, self.open_btn):
                button.setEnabled(enabled)
            self._details_dirty = False
            self._building_dirty = False
            self.details_status.setText(
                "프로젝트명 변경은 확인 후 저장되고, 나머지 정보는 자동 저장됩니다."
                if enabled else "프로젝트를 선택해 주세요."
            )
            self._update_building_buttons()
        finally:
            self._loading_details = False

    def _load_building_groups(self, record: ProjectRecord | None):
        groups = list(record.building_groups or []) if record else []
        self.building_table.blockSignals(True)
        try:
            self.building_table.setRowCount(len(groups))
            for row, data in enumerate(groups):
                values = (
                    data.get("code", ""),
                    data.get("name", ""),
                    data.get("basement", 0),
                    data.get("ground", 0),
                    data.get("roof", 0),
                    f"{float(data.get('area_m2', 0.0) or 0.0):.3f}",
                    f"{float(data.get('area_py', 0.0) or 0.0):.3f}",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if column in (2, 3, 4):
                        item.setTextAlignment(Qt.AlignCenter)
                    elif column in (5, 6):
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if column == 6:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setBackground(QBrush(QColor("#F0F0F0")))
                    self.building_table.setItem(row, column, item)
        finally:
            self.building_table.blockSignals(False)
        self._update_building_buttons()

    def _commit_name_change(self) -> bool:
        if self._loading_details or not self._current_project_id:
            return True
        entered = self.name_edit.text()
        if self.manager.normalize_name(entered) == self.manager.normalize_name(self._loaded_project_name):
            self.name_edit.setText(self._loaded_project_name)
            return True
        answer = QMessageBox.question(
            self,
            "프로젝트명 변경",
            f"프로젝트명을 변경할까요?\n\n{self._loaded_project_name}\n→ {entered.strip()}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.name_edit.setText(self._loaded_project_name)
            return True
        try:
            record = self.manager.update_name(self._current_project_id, entered)
        except Exception as exc:
            self.name_edit.setText(self._loaded_project_name)
            self._show_error("프로젝트명 변경", exc)
            return False
        self._records[record.project_id] = record
        self._loaded_project_name = record.name
        self.name_edit.setText(record.name)
        for row in range(self.project_table.rowCount()):
            item = self.project_table.item(row, 0)
            if item and item.data(Qt.UserRole) == record.project_id:
                self.project_table.item(row, 1).setText(record.name)
                break
        self.details_status.setText("프로젝트명 저장됨")
        return True

    def _detail_values(self) -> dict:
        return {
            "client": self.client_edit.text(),
            "location": self.location_edit.text(),
            "building_area_m2": self.building_area_spin.value(),
            "gross_area_m2": self.gross_area_spin.value(),
            "height_m": self.height_spin.value(),
            "depth_m": self.depth_spin.value(),
            "construction_months": self.construction_months_spin.value(),
            "construction_type": self.construction_type_edit.text(),
            "exterior_material": self.exterior_material_edit.text(),
            "building_structure": self.building_structure_edit.text(),
            "insulation_type": self.insulation_combo.currentText(),
            "overview": self.overview_edit.toPlainText(),
        }

    def _queue_details_save(self, *_args):
        if not self._loading_details and self._current_project_id:
            self._details_dirty = True
            self.details_status.setText("프로젝트 정보 저장 중...")
            self._details_save_timer.start()

    def _save_details_now(self) -> bool:
        self._details_save_timer.stop()
        if self._loading_details or not self._current_project_id or not self._details_dirty:
            return True
        try:
            record = self.manager.update_details(self._current_project_id, self._detail_values())
            self._records[record.project_id] = record
            self._details_dirty = False
            self.details_status.setText("프로젝트 정보 저장됨")
            return True
        except Exception as exc:
            self.details_status.setText(f"프로젝트 정보 저장 실패: {exc}")
            return False

    @staticmethod
    def _number_text(value: str, label: str, integer: bool = False):
        text = str(value or "").replace(",", "").strip()
        if not text:
            return 0 if integer else 0.0
        try:
            number = float(text)
        except ValueError as exc:
            raise ProjectError(f"{label}에는 숫자를 입력해 주세요.") from exc
        if number < 0 or (integer and not number.is_integer()):
            raise ProjectError(f"{label}에는 0 이상의 {'정수' if integer else '숫자'}를 입력해 주세요.")
        return int(number) if integer else number

    def _building_groups_from_table(self) -> list[dict]:
        groups = []
        for row in range(self.building_table.rowCount()):
            text = lambda column: self.building_table.item(row, column).text().strip() if self.building_table.item(row, column) else ""
            groups.append({
                "code": text(0),
                "name": text(1),
                "basement": self._number_text(text(2), f"{row + 1}행 지하", integer=True),
                "ground": self._number_text(text(3), f"{row + 1}행 지상", integer=True),
                "roof": self._number_text(text(4), f"{row + 1}행 옥탑", integer=True),
                "area_m2": self._number_text(text(5), f"{row + 1}행 연면적"),
            })
        return groups

    def _update_area_py_cell(self, row: int):
        item = self.building_table.item(row, 5)
        try:
            area = self._number_text(item.text() if item else "", "연면적")
            text = f"{area / 3.305785:.3f}" if area else "0.000"
        except ProjectError:
            text = "-"
        self.building_table.blockSignals(True)
        try:
            py_item = self.building_table.item(row, 6)
            if py_item is None:
                py_item = QTableWidgetItem()
                py_item.setFlags(py_item.flags() & ~Qt.ItemIsEditable)
                py_item.setBackground(QBrush(QColor("#F0F0F0")))
                self.building_table.setItem(row, 6, py_item)
            py_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            py_item.setText(text)
        finally:
            self.building_table.blockSignals(False)

    def _on_building_item_changed(self, item: QTableWidgetItem):
        if self._loading_details or not self._current_project_id:
            return
        if item.column() == 5:
            self._update_area_py_cell(item.row())
        self._building_dirty = True
        self.details_status.setText("동명 목록 저장 중...")
        self._building_save_timer.start()

    def _save_building_groups_now(self) -> bool:
        self._building_save_timer.stop()
        if self._loading_details or not self._current_project_id or not self._building_dirty:
            return True
        try:
            record = self.manager.update_building_groups(
                self._current_project_id,
                self._building_groups_from_table(),
            )
            self._records[record.project_id] = record
            self._building_dirty = False
            self.details_status.setText("동명 목록 저장됨")
            return True
        except Exception as exc:
            self.details_status.setText(f"동명 목록 저장 실패: {exc}")
            return False

    def _flush_current_edits(self) -> bool:
        return self._commit_name_change() and self._save_details_now() and self._save_building_groups_now()

    def _next_building_code(self, groups: list[dict]) -> str:
        used = {str(group.get("code", "") or "").casefold() for group in groups}
        number = 1
        while f"A{number:02d}".casefold() in used:
            number += 1
        return f"A{number:02d}"

    def _add_building_group(self):
        if not self._current_project_id or not self._save_building_groups_now():
            return
        try:
            groups = self._building_groups_from_table()
            groups.append({
                "code": self._next_building_code(groups), "name": "",
                "basement": 0, "ground": 0, "roof": 0, "area_m2": 0.0,
            })
            record = self.manager.update_building_groups(self._current_project_id, groups)
            self._records[record.project_id] = record
            self._load_building_groups(record)
            row = self.building_table.rowCount() - 1
            if row >= 0:
                self.building_table.selectRow(row)
                self.building_table.setCurrentCell(row, 1)
                self.building_table.editItem(self.building_table.item(row, 1))
            self.details_status.setText("동명 추가됨")
        except Exception as exc:
            self._show_error("동명 추가", exc)

    def _delete_building_group(self):
        row = self.building_table.currentRow()
        if row < 0 or not self._current_project_id:
            return
        code_item = self.building_table.item(row, 0)
        name_item = self.building_table.item(row, 1)
        label = " ".join(filter(None, [code_item.text() if code_item else "", name_item.text() if name_item else ""]))
        if QMessageBox.question(
            self, "동명 삭제", f"'{label or '선택 동'}'을(를) 삭제할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            groups = self._building_groups_from_table()
            groups.pop(row)
            record = self.manager.update_building_groups(self._current_project_id, groups)
            self._records[record.project_id] = record
            self._load_building_groups(record)
            if self.building_table.rowCount():
                self.building_table.selectRow(min(row, self.building_table.rowCount() - 1))
            self.details_status.setText("동명 삭제됨")
        except Exception as exc:
            self._show_error("동명 삭제", exc)

    def _duplicate_building_group(self):
        row = self.building_table.currentRow()
        if row < 0 or not self._current_project_id:
            return
        try:
            groups = self._building_groups_from_table()
            copied = dict(groups[row])
            copied["code"] = self._next_building_code(groups)
            copied["name"] = f"{copied.get('name', '')} - 사본".strip(" -")
            groups.append(copied)
            record = self.manager.update_building_groups(self._current_project_id, groups)
            self._records[record.project_id] = record
            self._load_building_groups(record)
            self.building_table.selectRow(self.building_table.rowCount() - 1)
            self.details_status.setText("동명 복제됨")
        except Exception as exc:
            self._show_error("동명 복제", exc)

    def _update_building_buttons(self):
        has_project = bool(self._current_project_id)
        has_row = has_project and self.building_table.currentRow() >= 0
        self.building_add_btn.setEnabled(has_project)
        self.building_delete_btn.setEnabled(has_row)
        self.building_duplicate_btn.setEnabled(has_row)

    def _create_project(self):
        if not self._flush_current_edits():
            return
        name, ok = QInputDialog.getText(self, "프로젝트 생성", "프로젝트 이름을 입력하세요.")
        if not ok:
            return
        try:
            record = self.manager.create(name)
            self.search_edit.clear()
            self.refresh(record.project_id)
        except Exception as exc:
            self._show_error("프로젝트 생성", exc)

    def _delete_project(self):
        if not self._flush_current_edits():
            return
        record = self._selected_record()
        if not record:
            return
        answer = QMessageBox.question(
            self,
            "프로젝트 삭제",
            f"'{record.name}' 프로젝트를 삭제할까요?\n\n"
            "프로젝트 폴더 안의 사용자 파일만 삭제됩니다.\n공용 도면목록과 원본 CAD/DXF 파일은 삭제되지 않습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.manager.delete(record.project_id)
            self._current_project_id = ""
            self.refresh()
        except Exception as exc:
            self._show_error("프로젝트 삭제", exc)

    def _assign_drawing(self):
        if not self._flush_current_edits():
            return
        record = self._selected_record()
        if not record:
            return
        picker = DrawingLibraryPicker(self.manager, self)
        if picker.exec() != QDialog.Accepted or picker.selected_path is None:
            return
        try:
            updated = self.manager.assign_base_drawing(record.project_id, picker.selected_path)
            self.refresh(updated.project_id)
            self.status_label.setText(f"기본도면 적용: {picker.selected_path.name}")
        except Exception as exc:
            self._show_error("도면 적용", exc)

    def _duplicate_project(self):
        if not self._flush_current_edits():
            return
        record = self._selected_record()
        if not record:
            return
        name, ok = QInputDialog.getText(self, "프로젝트 복제", "복제할 프로젝트 이름을 입력하세요.", text=f"{record.name} - 사본")
        if not ok:
            return
        try:
            copied = self.manager.duplicate(record.project_id, name)
            self.search_edit.clear()
            self.refresh(copied.project_id)
        except Exception as exc:
            self._show_error("프로젝트 복제", exc)

    def _export_project(self):
        if not self._flush_current_edits():
            return
        record = self._selected_record()
        if not record:
            return
        default = str(Path.cwd() / f"{record.name}_프로젝트.zip")
        output, _ = QFileDialog.getSaveFileName(self, "프로젝트 내보내기", default, "프로젝트 ZIP (*.zip)")
        if not output:
            return
        try:
            saved = self.manager.export_project(record.project_id, output)
            QMessageBox.information(self, "프로젝트 내보내기", f"프로젝트 ZIP을 만들었습니다.\n\n{saved}")
        except Exception as exc:
            self._show_error("프로젝트 내보내기", exc)

    def _import_project(self):
        if not self._flush_current_edits():
            return
        source, _ = QFileDialog.getOpenFileName(self, "프로젝트 가져오기", "", "프로젝트 ZIP (*.zip)")
        if not source:
            return
        try:
            original = self.manager.inspect_import(source)
            name, ok = QInputDialog.getText(
                self,
                "프로젝트 가져오기",
                "추가할 프로젝트 이름을 입력하세요.",
                text=original.name,
            )
            if not ok:
                return
            imported = self.manager.import_project(source, name)
            self.search_edit.clear()
            self.refresh(imported.project_id)
        except Exception as exc:
            self._show_error("프로젝트 가져오기", exc)

    def _open_selected(self):
        if not self._flush_current_edits():
            return
        project_id = self._selected_id()
        if not project_id:
            QMessageBox.information(self, "프로젝트 열기", "열 프로젝트를 선택해 주세요.")
            return
        try:
            self.selected_project = self.manager.get(project_id)
            self.accept()
        except Exception as exc:
            self._show_error("프로젝트 열기", exc)

    def reject(self):
        if self._flush_current_edits():
            super().reject()
