from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.common.project_snapshot import AUTOSAVE_DIR_NAME, save_project_snapshot


class ProjectAutosaveThread(QThread):
    """프로젝트 캐시를 UI 스레드와 분리하여 자동저장한다."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        sections,
        *,
        source_path: str,
        material_payload: dict | None = None,
        room_payload: dict | None = None,
        rendered_background_cache: Path | None = None,
        source_geometry_cache: Path | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.sections = list(sections or [])
        self.source_path = str(source_path or "")
        self.material_payload = material_payload
        self.room_payload = room_payload
        self.rendered_background_cache = rendered_background_cache
        self.source_geometry_cache = source_geometry_cache

    def run(self):
        try:
            report = save_project_snapshot(
                self.project_path,
                self.sections,
                source_path=self.source_path,
                material_payload=self.material_payload,
                room_payload=self.room_payload,
                rendered_background_cache=self.rendered_background_cache,
                source_geometry_cache=self.source_geometry_cache,
                directory_name=AUTOSAVE_DIR_NAME,
            )
            self.completed.emit(report)
        except Exception:
            self.failed.emit(traceback.format_exc())
