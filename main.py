from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path


_CRASH_LOG_HANDLE = None


def _crash_log_path() -> Path:
    root = Path(__file__).resolve().parent / "사용자파일" / "프로젝트" / "임시프로젝트" / "캐쉬"
    root.mkdir(parents=True, exist_ok=True)
    return root / "프로그램_치명오류.txt"


def _append_crash_log(title: str, detail: str) -> None:
    try:
        path = _crash_log_path()
        with path.open("a", encoding="utf-8") as fp:
            fp.write("\n" + "=" * 80 + "\n")
            fp.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {title}\n")
            fp.write(str(detail or "오류 상세 없음"))
            if not str(detail or "").endswith("\n"):
                fp.write("\n")
    except Exception:
        pass


def _install_crash_logging() -> None:
    global _CRASH_LOG_HANDLE
    try:
        path = _crash_log_path()
        _CRASH_LOG_HANDLE = path.open("a", encoding="utf-8", buffering=1)
        _CRASH_LOG_HANDLE.write(
            f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 프로그램 시작 · V24.19\n"
        )
        faulthandler.enable(file=_CRASH_LOG_HANDLE, all_threads=True)
    except Exception:
        _CRASH_LOG_HANDLE = None

    def _sys_hook(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _append_crash_log("처리되지 않은 메인 스레드 오류", detail)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Critical)
                box.setWindowTitle("프로그램 오류")
                box.setText("처리되지 않은 오류가 발생했습니다. 상세 로그를 저장했습니다.")
                box.setInformativeText(str(_crash_log_path()))
                box.setDetailedText(detail)
                box.exec()
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            detail = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            _append_crash_log(f"처리되지 않은 작업 스레드 오류 · {getattr(args.thread, 'name', '')}", detail)
            try:
                threading.__excepthook__(args)
            except Exception:
                pass
        threading.excepthook = _thread_hook


_install_crash_logging()

from app.main_window import main

if __name__ == "__main__":
    main()
