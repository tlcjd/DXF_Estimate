from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)


class CadSyncDialog(QDialog):
    connection_requested = Signal()
    existing_link_open_requested = Signal()
    disconnection_requested = Signal()
    registration_requested = Signal()
    import_requested = Signal(dict)
    export_requested = Signal(dict)
    create_layers_requested = Signal()
    import_blocks_requested = Signal()
    apply_library_blocks_requested = Signal()

    def __init__(self, values: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAD 연동")
        self.setModal(True)
        self.resize(820, 610)
        self.setMinimumSize(760, 540)
        self._drawing_values = {
            "drawing_overview": bool(values.get("drawing_overview", False)),
            "drawing_plan": bool(values.get("drawing_plan", True)),
            "drawing_elevation": bool(values.get("drawing_elevation", False)),
            "drawing_section": bool(values.get("drawing_section", False)),
            "drawing_finish_schedule": bool(values.get("drawing_finish_schedule", False)),
        }

        root = QVBoxLayout(self)
        connection_group = QGroupBox("현재 ZWCAD 도면 확인 및 연결")
        connection_layout = QVBoxLayout(connection_group)
        self.foreground_label = QLabel("확인 창: 확인 전")
        self.cad_info_label = QLabel("연결 도면: 연결 안 됨")
        self.cad_info_label.setWordWrap(True)
        self.project_match_label = QLabel("프로젝트 도면 확인: 연결 후 확인")
        self.project_match_label.setWordWrap(True)
        self.project_match_label.setStyleSheet("color:#666666;")
        self.existing_link_label = QLabel("기존 연동 CAD: 없음")
        self.existing_link_label.setWordWrap(True)
        self.existing_link_label.setStyleSheet("color:#666666;")
        buttons = QHBoxLayout()
        self.connect_button = QPushButton("현재 실행 중인 CAD 연동")
        self.open_existing_button = QPushButton("기존 연동된 CAD 열기")
        self.open_existing_button.setEnabled(False)
        self.open_existing_button.setToolTip("현재 프로젝트에 저장된 CAD 경로의 도면을 열고 연결합니다.")
        self.disconnect_button = QPushButton("연결 해제")
        self.disconnect_button.setEnabled(False)
        self.register_button = QPushButton("CAD 연동 등록")
        self.register_button.setEnabled(False)
        self.register_button.setToolTip(
            "현재 CAD의 도면구분 레이어에 프로젝트 식별 마커를 만들고 연동 Handle을 저장합니다."
        )
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.open_existing_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.register_button)
        buttons.addStretch(1)
        connection_layout.addWidget(self.foreground_label)
        connection_layout.addWidget(self.cad_info_label)
        connection_layout.addWidget(self.project_match_label)
        connection_layout.addWidget(self.existing_link_label)
        connection_layout.addLayout(buttons)
        root.addWidget(connection_group)

        guide = QLabel(
            "버튼을 누르면 현재 실행 중인 ZWCAD ActiveDocument에 바로 연결합니다. "
            "연결 시 저장된 CAD 식별 Handle 객체 하나만 확인하며, 실제 도면 내용은 CAD 도서 읽기를 눌렀을 때 가져옵니다. "
            "미등록 도면은 연결 후 'CAD 연동 등록' 버튼으로 현재 프로젝트와 연결할 수 있습니다."
        )
        guide.setWordWrap(True)
        root.addWidget(guide)

        columns = QHBoxLayout(); columns.setSpacing(14)
        import_group = QGroupBox("CAD → 프로그램")
        import_layout = QVBoxLayout(import_group)
        self.import_button = QPushButton("CAD 도서 읽기")
        self.import_button.setEnabled(False)
        import_layout.addWidget(self.import_button)
        self.import_background = self._check("원본도면 배경", values, "import_background", False)
        self.import_material_list = self._check("재료리스트 (도면 전체 문자)", values, "import_material_list", True)
        self.import_boundaries = self._check("실구분선", values, "import_boundaries", True)
        self.import_room_tags = self._check("실정보 블록·문자", values, "import_room_tags", True)
        self.import_wall_finishes = self._check("벽마감선", values, "import_wall_finishes", True)
        self.import_floor_finishes = self._check("바닥마감 객체", values, "import_floor_finishes", False)
        self.import_ceiling_finishes = self._check("천장마감 객체", values, "import_ceiling_finishes", False)
        self.import_openings = self._check("개구부", values, "import_openings", False)
        self.import_selected_only = self._check("현재 CAD 선택 객체만", values, "import_selected_only", False)
        for widget in self._import_checks(): import_layout.addWidget(widget)
        self.import_blocks_button = QPushButton("블록 가져오기")
        self.import_blocks_button.setEnabled(False)
        self.import_blocks_button.setToolTip(
            "현재 연결된 CAD 도면에서 설정된 블록 정의와 속성 태그를 검사하여 "
            "프로그램 블록 라이브러리에 저장합니다."
        )
        import_layout.addStretch(1)
        import_layout.addWidget(self.import_blocks_button)

        export_group = QGroupBox("프로그램 → CAD")
        export_layout = QVBoxLayout(export_group)
        self.export_button = QPushButton("현재 상태 내보내기")
        self.export_button.setEnabled(False)
        export_layout.addWidget(self.export_button)
        self.export_boundaries = self._check("실구분선 변경분", values, "export_boundaries", True)
        self.export_room_tags = self._check("실정보 블록·문자 변경분", values, "export_room_tags", True)
        self.export_wall_finishes = self._check("벽마감선 변경분", values, "export_wall_finishes", True)
        self.export_floor_finishes = self._check("바닥마감 객체", values, "export_floor_finishes", False)
        self.export_ceiling_finishes = self._check("천장마감 객체", values, "export_ceiling_finishes", False)
        self.export_new_objects = self._check("신규 객체 반영", values, "export_new_objects", True)
        self.export_deleted_objects = self._check("삭제 객체 반영", values, "export_deleted_objects", True)
        self.export_regen = self._check("반영 후 CAD 화면 재생성(REGEN)", values, "export_regen", True)
        for widget in self._export_checks(): export_layout.addWidget(widget)
        export_layout.addStretch(1)
        self.apply_library_blocks_button = QPushButton("블록 라이브러리 적용")
        self.apply_library_blocks_button.setEnabled(False)
        self.apply_library_blocks_button.setToolTip(
            "프로그램 블록 라이브러리 목록을 열고, 선택한 블록 정의를 현재 CAD 도면에 추가하거나 갱신합니다."
        )
        export_layout.addWidget(self.apply_library_blocks_button)
        self.create_layers_button = QPushButton("연동 레이어 생성")
        self.create_layers_button.setEnabled(False)
        export_layout.addWidget(self.create_layers_button)

        columns.addWidget(import_group, 1); columns.addWidget(export_group, 1)
        root.addLayout(columns, 1)
        bottom = QHBoxLayout()
        self.connection_label = QLabel("연결 상태: 연결 안 됨")
        self.close_button = QPushButton("닫기")
        bottom.addWidget(self.connection_label, 1); bottom.addWidget(self.close_button)
        root.addLayout(bottom)

        self.connect_button.clicked.connect(self.connection_requested.emit)
        self.open_existing_button.clicked.connect(self.existing_link_open_requested.emit)
        self.disconnect_button.clicked.connect(self.disconnection_requested.emit)
        self.register_button.clicked.connect(self.registration_requested.emit)
        self.import_button.clicked.connect(self._show_drawing_import_dialog)
        self.export_button.clicked.connect(self._emit_export)
        self.create_layers_button.clicked.connect(self.create_layers_requested.emit)
        self.import_blocks_button.clicked.connect(self.import_blocks_requested.emit)
        self.apply_library_blocks_button.clicked.connect(self.apply_library_blocks_requested.emit)
        self.close_button.clicked.connect(self.accept)

    @staticmethod
    def _check(text, values, key, default):
        box = QCheckBox(text); box.setChecked(bool(values.get(key, default))); return box

    def set_connection_info(self, connected: bool, foreground: str = "", cad_text: str = ""):
        self.foreground_label.setText(f"확인 창: {foreground or '(확인 불가)'}")
        self.cad_info_label.setText(f"연결 도면: {cad_text or '연결 안 됨'}")
        self.connection_label.setText("연결 상태: 연결됨" if connected else "연결 상태: 연결 안 됨")
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.register_button.setEnabled(connected)
        self.import_button.setEnabled(connected)
        self.export_button.setEnabled(connected)
        self.create_layers_button.setEnabled(connected)
        self.import_blocks_button.setEnabled(connected)
        self.apply_library_blocks_button.setEnabled(connected)
        if not connected:
            self.set_project_match_info("unknown", "연결 후 확인")

    def set_existing_link(self, document_path: str = ""):
        path = str(document_path or "").strip()
        self.existing_link_label.setText(f"기존 연동 CAD: {path or '없음'}")
        self.open_existing_button.setEnabled(bool(path))
        if path:
            self.open_existing_button.setToolTip(f"저장된 CAD 도면을 열고 연결합니다.\n{path}")
        else:
            self.open_existing_button.setToolTip("현재 프로젝트에 저장된 CAD 연동 경로가 없습니다.")

    def set_project_match_info(self, status: str, text: str):
        colors = {"same": "#187A2F", "different": "#C62828", "unlinked": "#B26A00", "unknown": "#666666"}
        labels = {"same": "등록 도면", "different": "연동표시 불일치", "unlinked": "미등록 도면", "unknown": "확인 불가"}
        key = status if status in colors else "unknown"
        self.project_match_label.setText(f"프로젝트 도면 확인: {labels[key]} · {text}")
        self.project_match_label.setStyleSheet(f"color:{colors[key]};")

    def _import_checks(self):
        return [self.import_background, self.import_material_list, self.import_boundaries, self.import_room_tags, self.import_wall_finishes,
                self.import_floor_finishes, self.import_ceiling_finishes, self.import_openings, self.import_selected_only]

    def _export_checks(self):
        return [self.export_boundaries, self.export_room_tags, self.export_wall_finishes, self.export_floor_finishes,
                self.export_ceiling_finishes, self.export_new_objects, self.export_deleted_objects, self.export_regen]

    def values(self):
        return {"import_background": self.import_background.isChecked(), "import_material_list": self.import_material_list.isChecked(),
                "import_boundaries": self.import_boundaries.isChecked(),
                "import_room_tags": self.import_room_tags.isChecked(), "import_wall_finishes": self.import_wall_finishes.isChecked(),
                "import_floor_finishes": self.import_floor_finishes.isChecked(), "import_ceiling_finishes": self.import_ceiling_finishes.isChecked(),
                "import_openings": self.import_openings.isChecked(), "import_selected_only": self.import_selected_only.isChecked(),
                "export_boundaries": self.export_boundaries.isChecked(), "export_room_tags": self.export_room_tags.isChecked(),
                "export_wall_finishes": self.export_wall_finishes.isChecked(), "export_floor_finishes": self.export_floor_finishes.isChecked(),
                "export_ceiling_finishes": self.export_ceiling_finishes.isChecked(), "export_new_objects": self.export_new_objects.isChecked(),
                "export_deleted_objects": self.export_deleted_objects.isChecked(), "export_regen": self.export_regen.isChecked(),
                **self._drawing_values}


    def _show_drawing_import_dialog(self):
        picker = QDialog(self)
        picker.setWindowTitle("가져올 도면")
        picker.setModal(True)
        picker.resize(460, 360)
        layout = QVBoxLayout(picker)

        title = QLabel("가져올 도면을 선택하십시오.")
        layout.addWidget(title)

        drawing_checks = {}
        items = [
            ("overview", "개요", False),
            ("plan", "평면도", True),
            ("elevation", "입면도", False),
            ("section", "단면도", False),
            ("finish_schedule", "재료마감표", False),
        ]
        for key, text, enabled in items:
            box = QCheckBox(text)
            box.setChecked(bool(self._drawing_values.get(f"drawing_{key}", key == "plan")))
            box.setEnabled(enabled)
            if not enabled:
                box.setToolTip("현재 버전에서는 준비 중인 기능입니다.")
            drawing_checks[key] = box
            layout.addWidget(box)

        layout.addSpacing(10)
        notice1 = QLabel(
            "※ 배경 재생성은 실구분선·실명·벽마감선 등 프로그램 작업 레이어를 제외하고, "
            "현재 CAD 화면에서 보이는 레이어 상태대로 가져옵니다."
        )
        notice1.setWordWrap(True)
        layout.addWidget(notice1)
        notice2 = QLabel(
            "※ 배경은 평면도·입면도·단면도만 생성 대상으로 사용합니다. "
            "현재 버전에서는 평면도 읽기만 동작합니다."
        )
        notice2.setWordWrap(True)
        layout.addWidget(notice2)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok_button = QPushButton("확인")
        cancel_button = QPushButton("취소")
        buttons.addWidget(ok_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)
        ok_button.clicked.connect(picker.accept)
        cancel_button.clicked.connect(picker.reject)

        if picker.exec() != QDialog.Accepted:
            return
        if not drawing_checks["plan"].isChecked():
            QMessageBox.information(self, "CAD 도서 읽기", "현재 버전에서는 평면도만 읽을 수 있습니다.")
            return

        self._drawing_values = {
            "drawing_overview": drawing_checks["overview"].isChecked(),
            "drawing_plan": drawing_checks["plan"].isChecked(),
            "drawing_elevation": drawing_checks["elevation"].isChecked(),
            "drawing_section": drawing_checks["section"].isChecked(),
            "drawing_finish_schedule": drawing_checks["finish_schedule"].isChecked(),
        }
        values = self.values()
        values.update(self._drawing_values)
        self.import_requested.emit(values)

    def _emit_import(self):
        values = self.values()
        if not any(values[k] for k in values if k.startswith("import_")):
            QMessageBox.information(self, "CAD 가져오기", "가져올 항목을 하나 이상 선택하십시오."); return
        self.import_requested.emit(values)

    def _emit_export(self):
        values = self.values()
        if not any(values[k] for k in values if k.startswith("export_") and k != "export_regen"):
            QMessageBox.information(self, "CAD 내보내기", "내보낼 항목을 하나 이상 선택하십시오."); return
        self.export_requested.emit(values)
