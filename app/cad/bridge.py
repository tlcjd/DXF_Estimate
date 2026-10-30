from __future__ import annotations

from app.common.geometry import polygon_label_point
from app.common.layer_defaults import LAYER_DEFINITIONS, LAYER_KEYS, default_layer_name, default_layer_color
import ctypes
import fnmatch
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CadConnectionError(RuntimeError):
    pass


@dataclass
class CadDocumentInfo:
    application_name: str
    application_version: str
    document_name: str
    document_path: str
    foreground_title: str


class CadBridge:
    """실행 중인 ZWCAD의 활성 도면과 COM으로 통신한다.

    COM 호출은 UI 스레드에서 짧게 수행하는 것을 전제로 한다. 대량 배경 읽기는
    사용자가 명시적으로 체크한 경우에만 수행한다.
    """

    PROG_IDS = (
        "ZWCAD.Application",
        "ZwCAD.Application",
        "ZWCAD.Application.2026",
        "ZWCAD.Application.2025",
        "ZWCAD.Application.2024",
        "ZWCAD.Application.2023",
        "ZWCAD.Application.2022",
    )
    PROJECT_LINK_PREFIX = "DXF_ESTIMATE_PROJECT_LINK"

    def __init__(self):
        self.app = None
        self.doc = None
        self.info: CadDocumentInfo | None = None
        # 한 번 연결에 성공한 COM ProgID를 기억해 같은 실행 중 재연결을 빠르게 한다.
        self._last_prog_id: str | None = None
        # 마지막 CAD 도서읽기에서 원본 CAD 기준으로 판정한 도곽 메타데이터.
        # 임시 ezdxf 문서에서 제목/층을 다시 추정하지 않고 동일 판정값을 실 분류에 사용한다.
        self.last_selected_divisions: list[dict] = []
        self.last_all_divisions: list[dict] = []
        # 마지막 CAD 도서읽기의 단계별 진단 정보. 층 누락 원인 확인용이며 도면을 수정하지 않는다.
        self.last_import_diagnostics: dict = {}
        # 원본 CAD에서 계산한 최종 표시색. 임시 DXF의 BYLAYER/TrueColor 재해석에 의존하지 않는다.
        # key=원본/임시 객체 handle(대문자), value=((r,g,b), 선가중치용 ACI 또는 None)
        self.last_background_color_overrides: dict[str, tuple[tuple[int, int, int], int | None]] = {}
        # 도곽 지정창 등 짧은 UI 미리보기용 강조 객체 Handle. 대량 SelectionSet은 만들지 않는다.
        self._highlighted_handle: str = ""
        # 같은 도곽 지정창에서 '도곽명 기입'을 반복해도 TEXT가 중복 생성되지 않도록
        # 이번 CAD 연결 세션에서 만든 도곽명 TEXT Handle을 도곽 Handle별로 기억한다.
        self._drawing_title_handles: dict[str, str] = {}

    @staticmethod
    def foreground_window_title() -> str:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()

    @staticmethod
    def _looks_like_cad_window(title: str) -> bool:
        value = title.casefold()
        return "zwcad" in value or "zwsim" in value

    def inspect_foreground(self) -> tuple[bool, str]:
        title = self.foreground_window_title()
        if not title:
            return False, "현재 활성 창 제목을 확인하지 못했습니다."
        if not self._looks_like_cad_window(title):
            return False, f"현재 활성 창은 ZWCAD로 확인되지 않습니다: {title}"
        return True, title

    def connect_to_active(self, require_foreground: bool = False) -> CadDocumentInfo:
        """실행 중인 ZWCAD의 ActiveDocument를 확인한다.

        CAD 연동 창 자체가 전면에 떠 있어도 연결할 수 있도록 Windows 전경 창은
        연결 조건으로 사용하지 않는다. 실제 연결 대상은 ZWCAD COM의 ActiveDocument다.
        """
        foreground_title = self.foreground_window_title()
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise CadConnectionError(
                "CAD 연동 모듈(pywin32)이 설치되지 않았습니다.\n"
                "run.bat를 다시 실행하거나 'pip install pywin32'를 실행하십시오."
            ) from exc

        pythoncom.CoInitialize()
        last_error: Exception | None = None
        app = None
        prog_ids = list(self.PROG_IDS)
        if self._last_prog_id in prog_ids:
            prog_ids.remove(self._last_prog_id)
            prog_ids.insert(0, self._last_prog_id)
        for prog_id in prog_ids:
            try:
                app = win32com.client.GetActiveObject(prog_id)
                self._last_prog_id = prog_id
                break
            except Exception as exc:  # COM 등록 버전별 차이를 순차 확인
                last_error = exc
        if app is None:
            raise CadConnectionError(
                "실행 중인 ZWCAD COM 객체를 찾지 못했습니다.\n"
                "ZWCAD Professional에서 대상 DWG를 먼저 연 뒤 다시 시도하십시오."
            ) from last_error

        try:
            doc = app.ActiveDocument
            if doc is None:
                raise CadConnectionError("ZWCAD에 열린 도면이 없습니다.")
            name = str(getattr(doc, "Name", "") or "")
            full_name = str(getattr(doc, "FullName", "") or "")
            path = full_name or str(Path(str(getattr(doc, "Path", "") or "")) / name)
            app_name = str(getattr(app, "Name", "ZWCAD") or "ZWCAD")
            version = str(getattr(app, "Version", "") or "")
        except CadConnectionError:
            raise
        except Exception as exc:
            raise CadConnectionError(f"활성 ZWCAD 도면 정보를 읽지 못했습니다: {exc}") from exc

        self.app = app
        self.doc = doc
        self.info = CadDocumentInfo(app_name, version, name, path, foreground_title)
        return self.info

    def open_document(self, source_path: str) -> CadDocumentInfo:
        """저장된 CAD 도면 경로를 열고 그 문서에 연결한다.

        프로젝트의 '기존 연동된 CAD 열기' 전용 경로다. 원본 도면은 열기만 하며
        Save/SaveAs/Export 같은 저장 동작은 수행하지 않는다. 이미 같은 도면이 열려
        있으면 새로 열지 않고 해당 문서를 활성화한다.
        """
        source = Path(str(source_path or '')).expanduser()
        if not source.is_file():
            raise CadConnectionError(f"기존 연동 CAD 파일을 찾지 못했습니다.\n\n{source}")
        if source.suffix.lower() not in {'.dwg', '.dxf'}:
            raise CadConnectionError(f"CAD 도면 파일만 열 수 있습니다.\n\n{source}")
        try:
            source_resolved = source.resolve()
        except Exception:
            source_resolved = source

        foreground_title = self.foreground_window_title()
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise CadConnectionError(
                "CAD 연동 모듈(pywin32)이 설치되지 않았습니다.\n"
                "run.bat를 다시 실행하거나 'pip install pywin32'를 실행하십시오."
            ) from exc

        pythoncom.CoInitialize()
        app = self.app
        last_error: Exception | None = None
        if app is None:
            prog_ids = list(self.PROG_IDS)
            if self._last_prog_id in prog_ids:
                prog_ids.remove(self._last_prog_id)
                prog_ids.insert(0, self._last_prog_id)
            for prog_id in prog_ids:
                try:
                    app = win32com.client.GetActiveObject(prog_id)
                    self._last_prog_id = prog_id
                    break
                except Exception as exc:
                    last_error = exc
            if app is None:
                for prog_id in prog_ids:
                    try:
                        app = win32com.client.Dispatch(prog_id)
                        self._last_prog_id = prog_id
                        try:
                            app.Visible = True
                        except Exception:
                            pass
                        break
                    except Exception as exc:
                        last_error = exc
            if app is None:
                raise CadConnectionError(
                    "ZWCAD를 실행하거나 연결할 수 없습니다.\n"
                    "ZWCAD Professional 설치/COM 등록 상태를 확인하십시오."
                ) from last_error

        doc = None
        try:
            documents = app.Documents
            count = int(getattr(documents, 'Count', 0) or 0)
            target_key = str(source_resolved).casefold()
            for index in range(count):
                try:
                    candidate = documents.Item(index)
                    full_name = str(getattr(candidate, 'FullName', '') or '')
                    if full_name and str(Path(full_name).resolve()).casefold() == target_key:
                        doc = candidate
                        break
                except Exception:
                    continue
            if doc is None:
                # ReadOnly=False: 사용자가 기존에 하던 일반 CAD 작업과 동일하게 연다.
                # 프로그램은 이 단계에서 어떠한 저장도 수행하지 않는다.
                doc = documents.Open(str(source_resolved), False)
            try:
                doc.Activate()
            except Exception:
                pass
            name = str(getattr(doc, 'Name', '') or source.name)
            full_name = str(getattr(doc, 'FullName', '') or '')
            path = full_name or str(source_resolved)
            app_name = str(getattr(app, 'Name', 'ZWCAD') or 'ZWCAD')
            version = str(getattr(app, 'Version', '') or '')
        except Exception as exc:
            raise CadConnectionError(f"기존 연동 CAD를 열지 못했습니다.\n\n{source_resolved}\n\n{exc}") from exc

        self.app = app
        self.doc = doc
        self.info = CadDocumentInfo(app_name, version, name, path, foreground_title)
        return self.info

    def disconnect(self):
        try:
            self.clear_highlight()
        except Exception:
            pass
        self.doc = None
        self.app = None
        self.info = None
        self._drawing_title_handles = {}

    @property
    def connected(self) -> bool:
        return self.doc is not None

    def _require_doc(self):
        if self.doc is None:
            raise CadConnectionError("먼저 '현재 활성 CAD 확인·연결'을 실행하십시오.")
        return self.doc

    def list_xref_files(self) -> list[dict]:
        """현재 연결 도면의 XREF 블록 이름/원본 경로를 읽는다.

        원본 CAD는 수정하지 않는다. 상대경로는 현재 도면 폴더 기준으로 해석한다.
        """
        doc = self._require_doc()
        try:
            base_dir = Path(str(getattr(doc, "FullName", "") or "")).resolve().parent
        except Exception:
            base_dir = Path(str(getattr(doc, "Path", "") or "") or ".")
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        blocks = getattr(doc, "Blocks", None)
        count = int(getattr(blocks, "Count", 0) or 0) if blocks is not None else 0
        for index in range(count):
            try:
                block = blocks.Item(index)
                if not bool(getattr(block, "IsXRef", False)):
                    continue
                name = str(getattr(block, "Name", "") or "").strip()
                raw_path = ""
                for attr_name in ("Path", "XRefPath", "XrefPath", "FileName", "Filename"):
                    try:
                        value = str(getattr(block, attr_name, "") or "").strip()
                    except Exception:
                        value = ""
                    if value:
                        raw_path = value
                        break
                resolved = ""
                if raw_path:
                    try:
                        candidate = Path(raw_path)
                        if not candidate.is_absolute():
                            candidate = base_dir / candidate
                        resolved = str(candidate.resolve())
                    except Exception:
                        resolved = str(base_dir / raw_path)
                key = (name.casefold(), resolved.casefold())
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "name": name,
                    "raw_path": raw_path,
                    "path": resolved,
                    "exists": bool(resolved and Path(resolved).is_file()),
                })
            except Exception:
                continue
        return results

    def save_xref_dwg_as_dxf_copy(self, source_path: str | Path, output_path: str | Path) -> dict:
        """XREF DWG를 CAD에 읽기전용으로 열고 별도 DXF 사본으로 SaveAs 한다.

        연결된 본도면과 XREF 원본 파일에는 저장하지 않는다. SaveAs 대상은 output_path뿐이다.
        ZWCAD/AutoCAD 계열의 2018 DXF SaveAsType(65)을 우선 사용하고, 구현 차이가 있으면
        확장자 추론 SaveAs를 한 번 더 시도한다.
        """
        source = Path(source_path)
        output = Path(output_path)
        if not source.is_file():
            raise CadConnectionError(f"XREF 원본 파일이 없습니다.\n\n{source}")
        if source.suffix.lower() != ".dwg":
            raise CadConnectionError(f"DWG XREF만 사본 DXF 변환 테스트할 수 있습니다.\n\n{source}")

        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            if output.exists():
                output.unlink()
        except Exception as exc:
            raise CadConnectionError(f"기존 테스트 DXF를 삭제하지 못했습니다.\n\n{output}\n\n{exc}") from exc

        original_doc = self._require_doc()
        opened = None
        save_method = ""
        try:
            # 원본 XREF는 읽기전용으로만 연다. SaveAs는 새 output_path로만 수행한다.
            opened = self.app.Documents.Open(str(source), True)
            try:
                opened.SaveAs(str(output), 65)  # ac2018_dxf 계열
                save_method = "SaveAs(format=65)"
            except Exception as first_exc:
                try:
                    opened.SaveAs(str(output))
                    save_method = "SaveAs(extension)"
                except Exception as second_exc:
                    raise CadConnectionError(
                        "XREF DWG → DXF 사본 SaveAs 실패\n\n"
                        f"원본: {source}\n"
                        f"사본: {output}\n"
                        f"format=65 오류: {first_exc}\n"
                        f"확장자 추론 오류: {second_exc}"
                    ) from second_exc
        finally:
            if opened is not None:
                try:
                    opened.Close(False)
                except Exception:
                    pass
            try:
                original_doc.Activate()
            except Exception:
                pass

        if not output.is_file() or output.stat().st_size <= 0:
            raise CadConnectionError(
                "CAD SaveAs 호출은 끝났지만 DXF 사본 파일이 생성되지 않았습니다.\n\n"
                f"원본: {source}\n사본: {output}"
            )
        return {
            "success": True,
            "source": str(source),
            "output": str(output),
            "size": int(output.stat().st_size),
            "method": save_method,
        }


    def build_ezdxf_supplement_blocks_fast(self, block_names, progress_callback=None):
        """빈 XCLIP 블록 보충 전용 고속 경로.

        CAD 블록 정의를 Python에서 객체별로 해석하지 않고, CAD의 CopyObjects로
        임시 도면 ModelSpace에 묶음 복사한 뒤 DXF로 SaveAs 한다. 이후 ezdxf가
        그 DXF를 읽어 원래 블록명 정의로 감싼다. 중첩 블록/레이어/색상 등 의존성은
        CAD 엔진의 CopyObjects가 함께 복사하므로 대형 블록에서 COM 왕복을 크게 줄인다.
        원본 CAD/DXF는 수정하지 않는다.
        """
        try:
            import ezdxf
            from ezdxf.addons import Importer
        except ImportError as exc:
            raise CadConnectionError("ezdxf 모듈이 설치되지 않았습니다.") from exc
        import tempfile

        def report(message):
            if callable(progress_callback):
                try:
                    progress_callback(str(message))
                except Exception:
                    pass

        original_doc = self._require_doc()
        names = []
        seen = set()
        for value in block_names or []:
            name = str(value or "").strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
        if not names:
            raise CadConnectionError("CAD 보충 대상 블록명이 없습니다.")

        result_doc = ezdxf.new("R2010")
        stats = {"blocks": {}, "errors": []}

        for block_no, name in enumerate(names, start=1):
            report(f"고속 블록 보충 준비: {name} · {block_no}/{len(names)}")
            try:
                source_block = original_doc.Blocks.Item(name)
            except Exception as exc:
                stats["errors"].append(f"{name}: CAD 블록 조회 실패: {type(exc).__name__}: {exc}")
                continue
            try:
                count = int(getattr(source_block, "Count", 0) or 0)
            except Exception:
                count = 0
            if count <= 0:
                stats["errors"].append(f"{name}: CAD 블록 정의가 비어 있음")
                continue

            base_point = (0.0, 0.0, 0.0)
            for prop in ("Origin", "BasePoint"):
                try:
                    base_point = self._point(getattr(source_block, prop))
                    break
                except Exception:
                    pass

            # _NewEnum 기반 열거를 우선 사용하면 Item(index) 반복보다 COM 왕복이 적다.
            report(f"고속 블록 객체 배열 준비: {name} · {count:,}개")
            entities = []
            try:
                for idx, entity in enumerate(source_block, start=1):
                    entities.append(entity)
                    if idx == 1 or idx % 2000 == 0 or idx == count:
                        report(f"고속 블록 객체 배열: {name} · {idx:,}/{count:,}")
            except Exception:
                entities = []
                for idx in range(count):
                    entities.append(source_block.Item(idx))
                    done = idx + 1
                    if done == 1 or done % 2000 == 0 or done == count:
                        report(f"고속 블록 객체 배열(Item): {name} · {done:,}/{count:,}")
            if not entities:
                stats["errors"].append(f"{name}: CAD 블록 구성객체 배열 생성 실패")
                continue

            temp_doc = None
            try:
                with tempfile.TemporaryDirectory(prefix="dxf_est_block_") as temp_dir:
                    temp_dxf = Path(temp_dir) / "block_export.dxf"
                    temp_doc = self.app.Documents.Add()
                    target_space = temp_doc.ModelSpace

                    # 너무 큰 SAFEARRAY 1회를 피하면서도 객체별 CopyObjects는 하지 않는다.
                    chunk_size = 2000
                    copied_total = 0
                    report(f"CAD 내부 일괄복사 시작: {name} · {len(entities):,}개")
                    for start in range(0, len(entities), chunk_size):
                        chunk = tuple(entities[start:start + chunk_size])
                        original_doc.CopyObjects(chunk, target_space)
                        copied_total += len(chunk)
                        report(f"CAD 내부 일괄복사: {name} · {copied_total:,}/{len(entities):,}")

                    report(f"임시 DXF 저장 시작: {name}")
                    try:
                        temp_doc.SaveAs(str(temp_dxf), 65)
                    except Exception:
                        temp_doc.SaveAs(str(temp_dxf))
                    try:
                        temp_doc.Close(False)
                    finally:
                        temp_doc = None
                    try:
                        original_doc.Activate()
                    except Exception:
                        pass

                    if not temp_dxf.is_file() or temp_dxf.stat().st_size <= 0:
                        raise CadConnectionError(f"{name}: 임시 DXF 파일이 생성되지 않았습니다.")

                    report(f"임시 DXF 읽기 시작: {name} · {temp_dxf.stat().st_size / (1024*1024):.1f}MB")
                    exported_doc = ezdxf.readfile(temp_dxf)
                    target_block = result_doc.blocks.get(name)
                    if target_block is None:
                        target_block = result_doc.blocks.new(name, base_point=base_point)
                    importer = Importer(exported_doc, result_doc)
                    importer.import_modelspace(target_layout=target_block)
                    importer.finalize()
                    target_count = len(target_block)
                    stats["blocks"][name] = {
                        "source_count": int(count),
                        "target_count": int(target_count),
                        "export_size": int(temp_dxf.stat().st_size),
                    }
                    report(f"고속 블록 보충 완료: {name} · CAD {count:,}개 → DXF 블록 {target_count:,}개")
            except Exception as exc:
                stats["errors"].append(f"{name}: 고속 보충 실패: {type(exc).__name__}: {exc}")
                report(stats["errors"][-1])
            finally:
                if temp_doc is not None:
                    try:
                        temp_doc.Close(False)
                    except Exception:
                        pass
                try:
                    original_doc.Activate()
                except Exception:
                    pass

        return result_doc, stats

    def build_active_modelspace_snapshot_fast(self, progress_callback=None):
        """현재 활성 CAD의 최신 도면을 원본 수정 없이 임시 DXF로 만든다.

        V0.24.11:
        - 1순위는 Document.Export(..., "DXF", SelectionSet)로 현재 열린 도면 전체를
          직접 DXF로 내보낸다. DXF Export에서는 SelectionSet 내용이 무시되고 도면 전체가
          출력되므로, ZWCAD에서 실패하던 SelectionSet.Select(5)와 Documents.Add()가 필요 없다.
        - Export가 구현되지 않은 CAD 호환 환경에만 기존 WBlock/CopyObjects를 폴백으로 남긴다.
        - 어떤 경로에서도 원본 DWG를 Save/SaveAs 하지 않는다.
        """
        try:
            import ezdxf
        except ImportError as exc:
            raise CadConnectionError("ezdxf 모듈이 설치되지 않았습니다.") from exc
        import tempfile

        def report(message):
            if callable(progress_callback):
                try:
                    progress_callback(str(message))
                except Exception:
                    pass

        original_doc = self._require_doc()
        source_space = original_doc.ModelSpace
        try:
            count = int(getattr(source_space, "Count", 0) or 0)
        except Exception:
            count = 0
        report(f"현재 CAD 최신 사본 준비: ModelSpace {count:,}개")

        errors = []
        with tempfile.TemporaryDirectory(prefix="dxf_est_live_") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_dwg = temp_dir_path / "active_snapshot.dwg"
            temp_dxf = temp_dir_path / "active_snapshot.dxf"

            # 1순위: 현재 열린 도면 자체를 CAD 엔진의 DXF Export로 직접 내보낸다.
            # DXF Export는 SelectionSet의 실제 내용과 무관하게 전체 도면을 출력하므로
            # Select(5) / AddItems / 별도 Documents.Add()를 모두 피할 수 있다.
            export_ss = None
            try:
                report("현재 CAD DXF 직접 Export 시작")
                selection_sets = original_doc.SelectionSets
                ss_name = "DXF_ESTIMATE_LIVE_EXPORT"
                try:
                    selection_sets.Item(ss_name).Delete()
                except Exception:
                    pass
                export_ss = selection_sets.Add(ss_name)

                export_base = temp_dir_path / "active_snapshot"
                report("현재 CAD DXF Export 호출")
                original_doc.Export(str(export_base), "DXF", export_ss)
                report("현재 CAD DXF Export 호출 완료")

                # AutoCAD 계열 Export는 보통 FileName에 확장자를 자동 부여한다.
                # 호환 CAD의 대소문자/확장자 처리 차이를 고려해 실제 생성 파일을 탐색한다.
                candidates = [
                    temp_dxf,
                    Path(str(export_base) + ".DXF"),
                    Path(str(export_base) + ".dxf"),
                ]
                exported = next((p for p in candidates if p.is_file() and p.stat().st_size > 0), None)
                if exported is None:
                    for p in temp_dir_path.glob("active_snapshot*"):
                        if p.is_file() and p.suffix.casefold() == ".dxf" and p.stat().st_size > 0:
                            exported = p
                            break
                if exported is None:
                    raise CadConnectionError("현재 CAD DXF Export 호출은 끝났지만 DXF 파일이 생성되지 않았습니다.")

                report(f"현재 CAD DXF Export 완료: {exported.stat().st_size / (1024*1024):.1f}MB")
                doc = ezdxf.readfile(exported)
                stats = {
                    "method": "Document.Export(DXF)",
                    "source_count": int(count),
                    "copied_count": int(count),
                    "file_size": int(exported.stat().st_size),
                    "modelspace_count": len(list(doc.modelspace())),
                    "block_count": len(list(doc.blocks)),
                    "errors": list(errors),
                }
                report(
                    f"현재 CAD 최신 사본 완료(DXF Export): ModelSpace {stats['modelspace_count']:,}개 · "
                    f"블록 {stats['block_count']:,}개"
                )
                return doc, stats
            except Exception as exc:
                errors.append(f"DXF 직접 Export 실패: {type(exc).__name__}: {exc}")
                report(errors[-1])
            finally:
                if export_ss is not None:
                    try:
                        export_ss.Delete()
                    except Exception:
                        pass
                try:
                    original_doc.Activate()
                except Exception:
                    pass

            # 2순위: SelectionSet 전체선택 -> WBlock.
            ss = None
            opened = None
            try:
                report("현재 CAD WBlock 최신사본 생성 시작")
                selection_sets = original_doc.SelectionSets
                ss_name = "DXF_ESTIMATE_LIVE_SNAPSHOT"
                try:
                    selection_sets.Item(ss_name).Delete()
                except Exception:
                    pass
                ss = selection_sets.Add(ss_name)

                report("현재 CAD WBlock 전체선택 시작")
                ss.Select(5)  # acSelectionSetAll
                try:
                    selected_count = int(getattr(ss, "Count", 0) or 0)
                except Exception:
                    selected_count = 0
                report(f"현재 CAD WBlock 전체선택 완료: {selected_count:,}개")
                if count > 0 and selected_count <= 0:
                    raise CadConnectionError("현재 CAD WBlock 전체선택 결과가 0개입니다.")

                report(f"현재 CAD WBlock 쓰기 시작: {selected_count:,}개")
                original_doc.WBlock(str(temp_dwg), ss)
                report("현재 CAD WBlock 쓰기 완료")
                try:
                    ss.Delete()
                finally:
                    ss = None

                if not temp_dwg.is_file() or temp_dwg.stat().st_size <= 0:
                    raise CadConnectionError("현재 CAD WBlock 임시 DWG가 생성되지 않았습니다.")
                report(f"현재 CAD WBlock DWG 생성: {temp_dwg.stat().st_size / (1024*1024):.1f}MB")

                opened = self.app.Documents.Open(str(temp_dwg), True)
                report("현재 CAD WBlock DWG → DXF 저장 시작")
                try:
                    opened.SaveAs(str(temp_dxf), 65)
                except Exception as first_save_exc:
                    report(f"현재 CAD WBlock DXF 형식지정 저장 실패, 기본 SaveAs 재시도: {type(first_save_exc).__name__}: {first_save_exc}")
                    opened.SaveAs(str(temp_dxf))
                try:
                    opened.Close(False)
                finally:
                    opened = None
                try:
                    original_doc.Activate()
                except Exception:
                    pass

                if not temp_dxf.is_file() or temp_dxf.stat().st_size <= 0:
                    raise CadConnectionError("현재 CAD WBlock DXF 사본이 생성되지 않았습니다.")
                report(f"현재 CAD WBlock DXF 읽기: {temp_dxf.stat().st_size / (1024*1024):.1f}MB")
                doc = ezdxf.readfile(temp_dxf)
                stats = {
                    "method": "WBlockSelectAll+SaveAsDXF",
                    "source_count": int(count),
                    "copied_count": int(selected_count),
                    "file_size": int(temp_dxf.stat().st_size),
                    "modelspace_count": len(list(doc.modelspace())),
                    "block_count": len(list(doc.blocks)),
                    "errors": list(errors),
                }
                report(
                    f"현재 CAD 최신 사본 완료(WBlock 전체선택): ModelSpace {stats['modelspace_count']:,}개 · "
                    f"블록 {stats['block_count']:,}개"
                )
                return doc, stats
            except Exception as exc:
                errors.append(f"WBlock 전체선택 실패: {type(exc).__name__}: {exc}")
                report(errors[-1])
            finally:
                if opened is not None:
                    try:
                        opened.Close(False)
                    except Exception:
                        pass
                if ss is not None:
                    try:
                        ss.Delete()
                    except Exception:
                        pass
                try:
                    original_doc.Activate()
                except Exception:
                    pass

            # 3순위 폴백: CopyObjects.
            temp_doc = None
            try:
                report("현재 CAD CopyObjects 폴백용 객체 배열 시작")
                entities = []
                try:
                    for idx, entity in enumerate(source_space, start=1):
                        entities.append(entity)
                        if idx == 1 or idx % 1000 == 0 or idx == count:
                            report(f"현재 CAD 객체 배열: {idx:,}/{count:,}")
                except Exception:
                    entities = []
                    for idx in range(count):
                        entities.append(source_space.Item(idx))
                        done = idx + 1
                        if done == 1 or done % 1000 == 0 or done == count:
                            report(f"현재 CAD 객체 배열(Item): {done:,}/{count:,}")
                if not entities and count > 0:
                    raise CadConnectionError("현재 CAD ModelSpace 객체 배열을 만들지 못했습니다.")

                report("현재 CAD CopyObjects 폴백 시작")
                temp_doc = self.app.Documents.Add()
                target_space = temp_doc.ModelSpace
                chunk_size = 1000
                copied_total = 0
                report(f"현재 CAD 임시문서 일괄복사 시작: {len(entities):,}개")
                for start_idx in range(0, len(entities), chunk_size):
                    chunk = tuple(entities[start_idx:start_idx + chunk_size])
                    if chunk:
                        original_doc.CopyObjects(chunk, target_space)
                        copied_total += len(chunk)
                        report(f"현재 CAD 임시문서 일괄복사: {copied_total:,}/{len(entities):,}")

                report("현재 CAD 임시 DXF 저장 시작")
                try:
                    temp_doc.SaveAs(str(temp_dxf), 65)
                except Exception:
                    temp_doc.SaveAs(str(temp_dxf))
                try:
                    temp_doc.Close(False)
                finally:
                    temp_doc = None
                try:
                    original_doc.Activate()
                except Exception:
                    pass

                if not temp_dxf.is_file() or temp_dxf.stat().st_size <= 0:
                    raise CadConnectionError("현재 CAD 임시 DXF 사본이 생성되지 않았습니다.")
                report(f"현재 CAD 임시 DXF 읽기: {temp_dxf.stat().st_size / (1024*1024):.1f}MB")
                doc = ezdxf.readfile(temp_dxf)
                stats = {
                    "method": "CopyObjects+SaveAsDXF",
                    "source_count": int(count),
                    "copied_count": int(copied_total),
                    "file_size": int(temp_dxf.stat().st_size),
                    "modelspace_count": len(list(doc.modelspace())),
                    "block_count": len(list(doc.blocks)),
                    "errors": list(errors),
                }
                report(
                    f"현재 CAD 최신 사본 완료(CopyObjects): ModelSpace {stats['modelspace_count']:,}개 · "
                    f"블록 {stats['block_count']:,}개"
                )
                return doc, stats
            except Exception as exc:
                errors.append(f"CopyObjects 실패: {type(exc).__name__}: {exc}")
                report(errors[-1])
                raise CadConnectionError("현재 CAD 최신 사본 생성 실패 · " + " / ".join(errors)) from exc
            finally:
                if temp_doc is not None:
                    try:
                        temp_doc.Close(False)
                    except Exception:
                        pass
                try:
                    original_doc.Activate()
                except Exception:
                    pass

    @staticmethod
    def _point(value: Any) -> tuple[float, float, float]:
        seq = list(value)
        return float(seq[0]), float(seq[1]), float(seq[2] if len(seq) > 2 else 0.0)

    @staticmethod
    def _entity_kind(entity) -> str:
        name = str(getattr(entity, "ObjectName", "") or "").casefold()
        # AcDbPolyline = LWPOLYLINE(좌표 x,y 반복),
        # AcDb2dPolyline/AcDb3dPolyline = 구형 POLYLINE(좌표 x,y,z 반복).
        # 이전에는 이름이 polyline으로 끝나는 객체를 전부 LWPOLYLINE으로 처리해
        # 구형 POLYLINE의 좌표열을 2개씩 잘못 끊으면서 일부 층 실구분선이 유실될 수 있었다.
        if "lwpolyline" in name or name == "acdbpolyline":
            return "LWPOLYLINE"
        if "2dpolyline" in name or "3dpolyline" in name:
            return "POLYLINE"
        if name.endswith("polyline"):
            return "POLYLINE"
        if "blockreference" in name:
            return "INSERT"
        if "mtext" in name:
            return "MTEXT"
        if name.endswith("text"):
            return "TEXT"
        if name.endswith("line"):
            return "LINE"
        if name.endswith("arc"):
            return "ARC"
        if name.endswith("circle"):
            return "CIRCLE"
        if "hatch" in name:
            return "HATCH"
        if name.endswith("solid") or name.endswith("trace"):
            return "SOLID"
        if name.endswith("ellipse"):
            return "ELLIPSE"
        if name.endswith("spline"):
            return "SPLINE"
        return ""

    @staticmethod
    def _polyline_xy_points(entity, kind: str) -> list[tuple[float, float]]:
        coords = list(getattr(entity, "Coordinates", ()) or ())
        if not coords:
            return []
        if kind == "LWPOLYLINE":
            step = 2
        else:
            # ActiveX의 구형 2D/3D POLYLINE Coordinates는 WCS XYZ 반복이다.
            step = 3
        points = []
        for i in range(0, len(coords) - (step - 1), step):
            try:
                points.append((float(coords[i]), float(coords[i + 1])))
            except Exception:
                continue
        return points

    def _iter_entities(self, selected_only: bool = False):
        doc = self._require_doc()
        if selected_only:
            try:
                selection_sets = doc.SelectionSets
                name = "DXF_ESTIMATE_SYNC_SELECTION"
                try:
                    selection_sets.Item(name).Delete()
                except Exception:
                    pass
                ss = selection_sets.Add(name)
                ss.SelectOnScreen()
                entities = [ss.Item(i) for i in range(ss.Count)]
                ss.Delete()
                return entities
            except Exception as exc:
                raise CadConnectionError(f"CAD 선택 객체를 읽지 못했습니다: {exc}") from exc
        try:
            model_space = doc.ModelSpace
            return [model_space.Item(i) for i in range(model_space.Count)]
        except Exception as exc:
            raise CadConnectionError(f"CAD ModelSpace를 읽지 못했습니다: {exc}") from exc

    @classmethod
    def project_link_text(cls, project_id: str, token: str, handle: str = "") -> str:
        values = [cls.PROJECT_LINK_PREFIX, str(project_id or "").strip(), str(token or "").strip()]
        clean_handle = str(handle or "").strip().upper()
        if clean_handle:
            values.append(clean_handle)
        return "|".join(values)

    def project_link_status(self, handle: str, project_id: str, token: str, layer_name: str) -> dict:
        """저장된 Handle로 CAD 객체 하나만 조회하여 프로젝트 연결을 확인한다."""
        doc = self._require_doc()
        clean_handle = str(handle or "").strip().upper()
        clean_token = str(token or "").strip()
        if not clean_handle or not clean_token:
            return {"status": "unlinked", "text": "저장된 CAD 식별 마커가 없습니다."}
        try:
            entity = doc.HandleToObject(clean_handle)
        except Exception as exc:
            return {
                "status": "different",
                "text": f"저장된 CAD 식별 Handle({clean_handle})을 현재 도면에서 찾지 못했습니다.",
                "error": str(exc),
            }
        expected_text = self.project_link_text(project_id, clean_token, clean_handle)
        base_text = self.project_link_text(project_id, clean_token)
        actual_text = str(getattr(entity, "TextString", "") or "")
        actual_layer = str(getattr(entity, "Layer", "") or "")
        expected_layer = str(layer_name or "").strip()
        # 일부 ZWCAD 버전은 생성 직후 TextString 재지정에서 E_POINTER를 반환한다.
        # 저장된 Handle 자체로 객체를 특정할 수 있으므로 Handle 없는 기본 마커문자도 허용한다.
        if actual_text not in {expected_text, base_text}:
            return {
                "status": "different",
                "text": f"Handle({clean_handle})은 있지만 다른 프로젝트의 식별 객체입니다.",
            }
        if expected_layer and actual_layer.casefold() != expected_layer.casefold():
            return {
                "status": "different",
                "text": f"식별 객체의 레이어가 다릅니다: {actual_layer or '(없음)'}",
            }
        return {
            "status": "same",
            "text": f"CAD 식별 Handle {clean_handle} 일치",
            "handle": clean_handle,
            "layer": actual_layer,
        }

    def ensure_project_link_marker(
        self,
        project_id: str,
        token: str,
        layer_name: str,
        existing_handle: str = "",
        save_document: bool = True,
    ) -> dict:
        """도곽 레이어에 숨김 식별 TEXT를 만들고 CAD가 부여한 Handle을 반환한다."""
        doc = self._require_doc()
        clean_layer = str(layer_name or "0").strip() or "0"
        status = self.project_link_status(existing_handle, project_id, token, clean_layer)
        if status.get("status") == "same":
            if save_document:
                self.save_active_document()
            return {**status, "created": False, "document": str(getattr(doc, "FullName", "") or "")}

        try:
            target_layer = doc.Layers.Item(clean_layer)
        except Exception:
            try:
                target_layer = doc.Layers.Add(clean_layer)
            except Exception as exc:
                raise CadConnectionError(f"CAD 식별 마커 레이어를 만들지 못했습니다: {clean_layer}\n{exc}") from exc

        previous_layer = None
        entity = None
        try:
            import pythoncom
            import win32com.client
            from array import array

            marker_text = self.project_link_text(project_id, token)
            point_values = (0.0, 0.0, 0.0)
            insertion_variants = []
            try:
                insertion_variants.append((
                    "VARIANT",
                    win32com.client.VARIANT(
                        pythoncom.VT_ARRAY | pythoncom.VT_R8,
                        point_values,
                    ),
                ))
            except Exception:
                pass
            insertion_variants.extend([
                ("array", array("d", point_values)),
                ("tuple", point_values),
                ("list", list(point_values)),
            ])

            # ZWCAD 버전에 따라 AddText가 VT_ARRAY를 거부하거나 AddMText만 정상인 경우가 있다.
            # 대상 레이어를 잠시 현재 레이어로 두면 생성 후 Layer 지정이 E_POINTER인 버전도 대응된다.
            try:
                previous_layer = doc.ActiveLayer
                doc.ActiveLayer = target_layer
            except Exception:
                previous_layer = None

            create_errors = []
            for point_name, insertion in insertion_variants:
                try:
                    entity = doc.ModelSpace.AddText(marker_text, insertion, 0.1)
                    if entity is not None:
                        break
                except Exception as exc:
                    create_errors.append(f"AddText/{point_name}: {exc}")
            if entity is None:
                for point_name, insertion in insertion_variants:
                    try:
                        entity = doc.ModelSpace.AddMText(insertion, 1.0, marker_text)
                        if entity is not None:
                            break
                    except Exception as exc:
                        create_errors.append(f"AddMText/{point_name}: {exc}")
            if entity is None:
                detail = "\n".join(create_errors[-4:]) or "CAD가 TEXT 객체를 반환하지 않았습니다."
                raise CadConnectionError(f"CAD 식별 TEXT 생성 단계가 실패했습니다.\n{detail}")

            try:
                entity.Layer = clean_layer
            except Exception as exc:
                actual_layer = str(getattr(entity, "Layer", "") or "")
                if actual_layer.casefold() != clean_layer.casefold():
                    raise CadConnectionError(
                        f"CAD 식별 TEXT는 생성했지만 레이어를 지정하지 못했습니다: {clean_layer}\n{exc}"
                    ) from exc
            try:
                entity.Visible = False
            except Exception:
                pass
            handle = str(getattr(entity, "Handle", "") or "").strip().upper()
            if not handle:
                raise CadConnectionError("CAD가 식별 마커 Handle을 반환하지 않았습니다.")
            try:
                entity.TextString = self.project_link_text(project_id, token, handle)
            except Exception:
                # Handle 없는 기본문자도 project_link_status에서 같은 프로젝트로 인정한다.
                pass
            if previous_layer is not None:
                try:
                    doc.ActiveLayer = previous_layer
                except Exception:
                    pass
            if save_document:
                self.save_active_document()
            return {
                "status": "same",
                "text": f"CAD 식별 Handle {handle} 등록",
                "handle": handle,
                "layer": clean_layer,
                "created": True,
                "document": str(getattr(doc, "FullName", "") or ""),
            }
        except CadConnectionError:
            if entity is not None:
                try:
                    entity.Delete()
                except Exception:
                    pass
            if previous_layer is not None:
                try:
                    doc.ActiveLayer = previous_layer
                except Exception:
                    pass
            raise
        except Exception as exc:
            if entity is not None:
                try:
                    entity.Delete()
                except Exception:
                    pass
            if previous_layer is not None:
                try:
                    doc.ActiveLayer = previous_layer
                except Exception:
                    pass
            raise CadConnectionError(f"CAD 식별 마커를 만들지 못했습니다: {exc}") from exc

    def save_active_document(self):
        """프로젝트 연결이 등록된 현재 CAD 문서를 디스크에 저장한다."""
        doc = self._require_doc()
        try:
            doc.Save()
        except Exception as exc:
            raise CadConnectionError(f"현재 CAD 도면을 저장하지 못했습니다: {exc}") from exc

    def _iter_entities_filtered(self, layer_names: list[str], entity_types: list[str]):
        """CAD SelectionSet 필터로 필요한 레이어·객체만 먼저 선별한다.

        전체 ModelSpace를 Python COM으로 순회하지 않고 CAD 내부에서 필터링한 뒤
        결과 객체만 가져온다. ZWCAD 버전별 COM 차이로 필터 호출이 실패하면
        기존 전체 순회 방식으로 안전하게 되돌아간다.
        """
        doc = self._require_doc()
        clean_layers = [str(v).strip() for v in layer_names if str(v).strip()]
        clean_types = [str(v).strip().upper() for v in entity_types if str(v).strip()]
        if not clean_layers and not clean_types:
            return self._iter_entities(False)

        if not clean_layers:
            try:
                import pythoncom
                import win32com.client

                selection_sets = doc.SelectionSets
                name = "DXF_ESTIMATE_SYNC_TYPES"
                try:
                    selection_sets.Item(name).Delete()
                except Exception:
                    pass
                ss = selection_sets.Add(name)
                code_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_I2, [0]
                )
                value_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [",".join(clean_types)]
                )
                ss.Select(5, None, None, code_array, value_array)
                entities = [ss.Item(i) for i in range(ss.Count)]
                ss.Delete()
                return entities
            except Exception:
                try:
                    ss.Delete()
                except Exception:
                    pass
                # 객체 종류 필터를 지원하지 않는 CAD 버전만 전체 ModelSpace에서 문자 객체를 추린다.
                allowed_types = set(clean_types)
                return [
                    entity for entity in self._iter_entities(False)
                    if self._entity_kind(entity) in allowed_types
                ]

        try:
            import pythoncom
            import win32com.client

            selection_sets = doc.SelectionSets
            name = "DXF_ESTIMATE_SYNC_FILTERED"
            try:
                selection_sets.Item(name).Delete()
            except Exception:
                pass
            ss = selection_sets.Add(name)

            filter_codes = [8]
            filter_values = [",".join(clean_layers)]
            if clean_types:
                filter_codes.insert(0, 0)
                filter_values.insert(0, ",".join(clean_types))

            code_array = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_I2,
                filter_codes,
            )
            value_array = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
                filter_values,
            )
            # 5 = acSelectionSetAll
            ss.Select(5, None, None, code_array, value_array)
            entities = [ss.Item(i) for i in range(ss.Count)]
            ss.Delete()
            return entities
        except Exception:
            try:
                ss.Delete()
            except Exception:
                pass
            # ZWCAD 버전에 따라 객체종류+레이어 복합 필터가 실패할 수 있다.
            # 이때 전체 ModelSpace로 되돌아가지 않고 레이어 필터만 한 번 더 시도한다.
            try:
                import pythoncom
                import win32com.client
                selection_sets = doc.SelectionSets
                name = "DXF_ESTIMATE_SYNC_LAYER_ONLY"
                try:
                    selection_sets.Item(name).Delete()
                except Exception:
                    pass
                ss2 = selection_sets.Add(name)
                code_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_I2, [8]
                )
                value_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [",".join(clean_layers)]
                )
                ss2.Select(5, None, None, code_array, value_array)
                entities = []
                allowed_types = set(clean_types)
                for i in range(ss2.Count):
                    entity = ss2.Item(i)
                    if allowed_types and self._entity_kind(entity) not in allowed_types:
                        continue
                    entities.append(entity)
                ss2.Delete()
                return entities
            except Exception as exc:
                try:
                    ss2.Delete()
                except Exception:
                    pass
                raise CadConnectionError(
                    "CAD 레이어 선택 필터를 실행하지 못했습니다. 전체 도면 순회는 속도 보호를 위해 생략합니다.\n"
                    f"대상 레이어: {', '.join(clean_layers)}\n{exc}"
                ) from exc


    @staticmethod
    def _rects_overlap(a, b, tolerance: float = 1e-6):
        ax1, ay1, ax2, ay2 = (float(v) for v in a)
        bx1, by1, bx2, by2 = (float(v) for v in b)
        return not (ax2 < bx1 - tolerance or bx2 < ax1 - tolerance or ay2 < by1 - tolerance or by2 < ay1 - tolerance)

    def _entity_bounds(self, entity):
        """COM 객체의 2D 외곽범위를 최대한 안전하게 얻는다.

        ZWCAD/AutoCAD의 GetBoundingBox 반환형 차이를 우선 처리하고, 실패 시
        대표 좌표(Start/End, Coordinates, InsertionPoint 등)로 보완한다.
        """
        try:
            result = entity.GetBoundingBox()
            if result and len(result) >= 2:
                p1, p2 = result[0], result[1]
                return (
                    min(float(p1[0]), float(p2[0])),
                    min(float(p1[1]), float(p2[1])),
                    max(float(p1[0]), float(p2[0])),
                    max(float(p1[1]), float(p2[1])),
                )
        except Exception:
            pass

        kind = self._entity_kind(entity)
        points = []
        try:
            if kind == "LINE":
                points = [self._point(entity.StartPoint), self._point(entity.EndPoint)]
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                points = [(x, y, 0.0) for x, y in self._polyline_xy_points(entity, kind)]
            elif kind in ("TEXT", "MTEXT", "INSERT"):
                points = [self._point(entity.InsertionPoint)]
            elif kind in ("CIRCLE", "ARC"):
                c = self._point(entity.Center)
                r = float(getattr(entity, "Radius", 0.0) or 0.0)
                return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        except Exception:
            points = []
        if not points:
            return None
        xs = [float(pt[0]) for pt in points]
        ys = [float(pt[1]) for pt in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def _entity_intersects_rects(self, entity, rects):
        bounds = self._entity_bounds(entity)
        if bounds is None:
            # 외곽범위를 얻을 수 없는 객체는 CAD가 union crossing으로 골랐다는 사실을 신뢰한다.
            return True
        return any(self._rects_overlap(bounds, rect) for rect in rects)

    def _iter_entities_in_rects(self, rects: list[tuple[float, float, float, float]], entity_types: list[str] | None = None):
        """선택 도곽 전체를 한 번의 Crossing Selection으로 읽고 도곽별 범위로 재필터한다.

        V17.2: ZWCAD에서 SelectionSet.Select를 여러 도곽에 연속 호출하면 첫/활성 도곽만
        정상 반환하고 뒤 도곽이 0개가 되는 사례가 있어, 도곽마다 SelectionSet을 만들지 않는다.
        모든 선택 도곽의 합집합 외곽을 한 번만 CAD에 요청한 뒤 Python에서 각 도곽과
        교차하는 객체만 남긴다. 전체 ModelSpace 순회는 하지 않는다.
        """
        if not rects:
            return []
        doc = self._require_doc()
        ss = None
        try:
            import pythoncom
            import win32com.client

            normalized = []
            for rect in rects:
                x1, y1, x2, y2 = (float(v) for v in rect)
                normalized.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
            union_rect = (
                min(r[0] for r in normalized),
                min(r[1] for r in normalized),
                max(r[2] for r in normalized),
                max(r[3] for r in normalized),
            )

            selection_sets = doc.SelectionSets
            name = "DXF_ESTIMATE_FRAMES"
            try:
                selection_sets.Item(name).Delete()
            except Exception:
                pass
            ss = selection_sets.Add(name)
            xmin, ymin, xmax, ymax = union_rect
            p1 = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8, (xmin, ymin, 0.0)
            )
            p2 = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8, (xmax, ymax, 0.0)
            )
            clean_types = [str(v).strip().upper() for v in (entity_types or []) if str(v).strip()]
            if clean_types:
                code_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_I2, [0]
                )
                value_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [",".join(clean_types)]
                )
                ss.Select(1, p1, p2, code_array, value_array)
            else:
                ss.Select(1, p1, p2)

            entities = []
            seen = set()
            allowed_types = set(clean_types)
            for item_index in range(ss.Count):
                entity = ss.Item(item_index)
                if allowed_types and self._entity_kind(entity) not in allowed_types:
                    continue
                if not self._entity_intersects_rects(entity, normalized):
                    continue
                key = str(getattr(entity, "Handle", "") or "")
                if not key:
                    key = f"OID:{getattr(entity, 'ObjectID', id(entity))}"
                if key in seen:
                    continue
                seen.add(key)
                entities.append(entity)
            try:
                ss.Delete()
            except Exception:
                pass
            return entities
        except Exception as exc:
            try:
                if ss is not None:
                    ss.Delete()
            except Exception:
                pass
            raise CadConnectionError(
                "선택한 도곽 범위의 CAD 객체를 가져오지 못했습니다. 전체 도면 순회는 속도 보호를 위해 생략합니다.\n"
                f"{exc}"
            ) from exc


    def detect_opening_candidates(self, rect, window_acis=(4, 5)) -> dict:
        """현재 평면 도곽 범위 안에서 문/창 후보의 대략적인 중심만 찾는다.

        TEST 기능 전용이다. 전체 ModelSpace를 순회하지 않고 전달된 현재층 사각 도곽을
        CAD SelectionSet Crossing으로 한 번만 읽는다. 정확한 창호 치수/형상 판정이 목적이
        아니라 프로그램 도곽 위에 임시 부호를 표시할 중심점 검출이 목적이다.
        """
        doc = self._require_doc()
        try:
            x1, y1, x2, y2 = (float(v) for v in rect)
        except Exception as exc:
            raise CadConnectionError(f"창호 읽기 도곽 범위가 올바르지 않습니다: {rect}") from exc
        scan_rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        wanted_acis = {max(1, min(255, int(v))) for v in (window_acis or (4, 5))}
        if not wanted_acis:
            wanted_acis = {4, 5}

        # V23.46 TEST는 사용자가 싫어하는 "현재층의 모든 선 읽기"도 피한다.
        # 1) INSERT/ARC만 현재 도곽에서 읽고, 2) 창 후보 일반선은 ACI 4/5 또는
        #    해당 ACI를 쓰는 레이어만 CAD SelectionSet 단계에서 골라 읽는다.
        # 전체 ModelSpace 순회 fallback은 사용하지 않는다.
        filter_warnings = []

        def select_in_rect(name_suffix: str, filter_codes, filter_values):
            ss = None
            try:
                import pythoncom
                import win32com.client
                selection_sets = doc.SelectionSets
                name = f"DXF_EST_OPENING_{name_suffix}"
                try:
                    selection_sets.Item(name).Delete()
                except Exception:
                    pass
                ss = selection_sets.Add(name)
                p1 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8, (scan_rect[0], scan_rect[1], 0.0)
                )
                p2 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8, (scan_rect[2], scan_rect[3], 0.0)
                )
                code_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_I2, tuple(int(v) for v in filter_codes)
                )
                value_array = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, tuple(filter_values)
                )
                ss.Select(1, p1, p2, code_array, value_array)
                result = []
                for index in range(ss.Count):
                    entity = ss.Item(index)
                    if self._entity_intersects_rects(entity, [scan_rect]):
                        result.append(entity)
                ss.Delete()
                return result
            except Exception as exc:
                try:
                    if ss is not None:
                        ss.Delete()
                except Exception:
                    pass
                filter_warnings.append(f"{name_suffix}: {type(exc).__name__}: {exc}")
                return []

        # 문 블록과 90도 ARC는 종류필터만으로 현재 도곽에서 선별한다.
        entities = select_in_rect("BLOCK_ARC", [0], ["INSERT,ARC"])
        if not entities:
            # 객체종류 Crossing filter가 지원되지 않는 CAD에서만 기존 도곽 한정 선택을 사용한다.
            # 이 fallback도 INSERT/ARC만 가져오므로 전체 일반선은 읽지 않는다.
            try:
                entities = self._iter_entities_in_rects([scan_rect], ["INSERT", "ARC"])
            except Exception:
                entities = []

        # ACI 4/5가 객체에 직접 지정된 일반 창호선.
        for aci in sorted(wanted_acis):
            entities.extend(select_in_rect(
                f"ACI_{aci}", [0, 62], ["LINE,LWPOLYLINE,POLYLINE,ARC", int(aci)]
            ))

        # BYLAYER인 창호선도 놓치지 않도록 ACI 4/5 레이어 이름만 먼저 구한다.
        colored_layers = []
        try:
            layers = doc.Layers
            for index in range(int(layers.Count)):
                layer = layers.Item(index)
                raw = abs(int(getattr(layer, "Color", 7) or 7))
                if raw in wanted_acis:
                    name = str(getattr(layer, "Name", "") or "").strip()
                    if name:
                        colored_layers.append(name)
        except Exception as exc:
            filter_warnings.append(f"LAYER_COLOR: {type(exc).__name__}: {exc}")
        if colored_layers:
            entities.extend(select_in_rect(
                "LAYER_COLOR", [0, 8],
                ["LINE,LWPOLYLINE,POLYLINE,ARC", ",".join(colored_layers)]
            ))

        # 여러 SelectionSet에서 같은 객체가 잡혀도 Handle/ObjectID 기준으로 한 번만 분석한다.
        unique_entities = []
        seen_entities = set()
        for entity in entities:
            try:
                key = str(getattr(entity, "Handle", "") or "")
            except Exception:
                key = ""
            if not key:
                try:
                    key = f"OID:{getattr(entity, 'ObjectID')}"
                except Exception:
                    key = f"PY:{id(entity)}"
            if key in seen_entities:
                continue
            seen_entities.add(key)
            unique_entities.append(entity)
        entities = unique_entities

        from app.cad.colors import nearest_aci

        layer_aci_cache: dict[str, int] = {}

        def layer_aci(layer_name: str) -> int:
            key = str(layer_name or "").casefold()
            if key in layer_aci_cache:
                return layer_aci_cache[key]
            value = 7
            try:
                layer = doc.Layers.Item(str(layer_name or "0"))
                raw = abs(int(getattr(layer, "Color", 7) or 7))
                if raw not in (0, 256):
                    value = max(1, min(255, raw))
            except Exception:
                value = 7
            layer_aci_cache[key] = value
            return value

        def entity_aci(entity, parent_aci: int | None = None) -> int:
            try:
                raw = abs(int(getattr(entity, "Color", 256) or 256))
            except Exception:
                raw = 256
            if raw not in (0, 256):
                return max(1, min(255, raw))
            if raw == 0 and parent_aci not in (None, 0, 256):
                return max(1, min(255, int(parent_aci)))
            if raw == 256:
                try:
                    return layer_aci(str(getattr(entity, "Layer", "0") or "0"))
                except Exception:
                    pass
            # TrueColor만 지정된 객체도 최근접 ACI로 보조 판정한다.
            try:
                tc = getattr(entity, "TrueColor", None)
                method = int(getattr(tc, "ColorMethod", -1)) if tc is not None else -1
                if tc is not None and method == 194:
                    r = max(0, min(255, int(getattr(tc, "Red", 0))))
                    g = max(0, min(255, int(getattr(tc, "Green", 0))))
                    b = max(0, min(255, int(getattr(tc, "Blue", 0))))
                    return nearest_aci(f"#{r:02X}{g:02X}{b:02X}")
            except Exception:
                pass
            return 7

        def bounds_center(bounds):
            if not bounds:
                return None
            return ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)

        def bounds_size(bounds):
            if not bounds:
                return 0.0, 0.0
            return max(0.0, bounds[2] - bounds[0]), max(0.0, bounds[3] - bounds[1])

        def plausible_opening_bounds(bounds) -> bool:
            if not bounds:
                return False
            width, height = bounds_size(bounds)
            longest = max(width, height)
            # 도곽/타이틀블록 같은 대형 블록을 제외하고, 너무 작은 심볼도 제외한다.
            return 80.0 <= longest <= 30000.0

        def window_like_bounds(bounds) -> bool:
            """평면 창 블록은 대체로 벽을 따라 길고 벽두께 방향은 얇다.

            색상만 Cyan/Blue인 계단/난간 블록을 창으로 오인하지 않기 위한 보조조건이다.
            블록명에 AW/WINDOW 등이 명확하면 이 조건 없이도 창 후보가 될 수 있다.
            """
            width, height = bounds_size(bounds)
            longest = max(width, height)
            shortest = min(width, height)
            if longest < 120.0 or shortest <= 1e-6:
                return False
            return longest / shortest >= 1.8 and shortest <= 1500.0

        def block_names(entity):
            values = []
            for attr in ("EffectiveName", "Name"):
                try:
                    value = str(getattr(entity, attr, "") or "").strip()
                except Exception:
                    value = ""
                if value and value not in values:
                    values.append(value)
            return values

        def name_scores(names):
            raw = " ".join(names).upper().replace(" ", "")
            door = 0
            window = 0
            if any(token in raw for token in ("DOOR", "DOOR-", "문", "출입문")):
                door += 5
            if any(token in raw for token in ("WINDOW", "WIN", "창호", "창문")):
                window += 5
            # 국내 도면에서 흔히 쓰는 부호명은 보조점수로만 사용한다.
            import re
            if re.search(r"(^|[^A-Z])(SD|FD|DD|PD|D)\d", raw):
                door += 2
            if re.search(r"(^|[^A-Z])(AW|CW|PW|SW|W)\d", raw):
                window += 3
            return door, window

        block_cache: dict[tuple[str, int], dict] = {}

        def block_stats_from_name(name: str, inherited_aci: int, depth=0, visited=None):
            key = (str(name or "").casefold(), int(inherited_aci or 7))
            if depth == 0 and key in block_cache:
                return dict(block_cache[key])
            stats = {"arc90": 0, "window_color": 0, "entity_count": 0}
            if depth > 2 or not name:
                return stats
            visited = set(visited or ())
            visit_key = str(name).casefold()
            if visit_key in visited:
                return stats
            visited.add(visit_key)
            try:
                block = doc.Blocks.Item(name)
            except Exception:
                return stats
            try:
                count = int(block.Count)
            except Exception:
                count = 0
            for idx in range(count):
                try:
                    child = block.Item(idx)
                    kind = self._entity_kind(child)
                    stats["entity_count"] += 1
                    aci = entity_aci(child, inherited_aci)
                    if aci in wanted_acis:
                        stats["window_color"] += 1
                    if kind == "ARC":
                        try:
                            start = float(getattr(child, "StartAngle", 0.0) or 0.0)
                            end = float(getattr(child, "EndAngle", 0.0) or 0.0)
                            sweep = (end - start) % (2.0 * math.pi)
                            quarter = math.pi / 2.0
                            if abs(sweep - quarter) <= math.radians(18.0):
                                stats["arc90"] += 1
                        except Exception:
                            pass
                    elif kind == "INSERT":
                        nested_names = block_names(child)
                        nested_aci = entity_aci(child, inherited_aci)
                        for nested in nested_names[:1]:
                            child_stats = block_stats_from_name(nested, nested_aci, depth + 1, visited)
                            for field in stats:
                                stats[field] += int(child_stats.get(field, 0) or 0)
                except Exception:
                    continue
            if depth == 0:
                block_cache[key] = dict(stats)
            return stats

        def block_stats(entity):
            inherited_aci = entity_aci(entity)
            result = {"arc90": 0, "window_color": 0, "entity_count": 0}
            for name in block_names(entity):
                current = block_stats_from_name(name, inherited_aci)
                # EffectiveName/Name가 같은 정의를 가리키는 경우 중복 합산하지 않도록 최댓값 사용.
                for field in result:
                    result[field] = max(result[field], int(current.get(field, 0) or 0))
            return result

        def record_candidate(kind: str, center, confidence: int, source: str, entity=None, bounds=None, aci=None, block_name=""):
            if center is None:
                return None
            try:
                x, y = float(center[0]), float(center[1])
            except Exception:
                return None
            if not (scan_rect[0] <= x <= scan_rect[2] and scan_rect[1] <= y <= scan_rect[3]):
                return None
            detected_length = 0.0
            if bounds:
                try:
                    detected_length = max(abs(float(bounds[2]) - float(bounds[0])), abs(float(bounds[3]) - float(bounds[1])))
                except Exception:
                    detected_length = 0.0
            return {
                "kind": kind,
                "x": x,
                "y": y,
                "confidence": int(confidence),
                "source": str(source or ""),
                "aci": int(aci) if aci not in (None, "") else None,
                "block_name": str(block_name or ""),
                "handle": str(getattr(entity, "Handle", "") or "") if entity is not None else "",
                "bounds": tuple(float(v) for v in bounds) if bounds else None,
                "detected_length_mm": float(detected_length),
            }

        block_candidates = []
        neutral_blocks = []
        color_entities = []
        free_door_arcs = []

        for entity in entities:
            try:
                kind = self._entity_kind(entity)
                bounds = self._entity_bounds(entity)
                if kind == "INSERT":
                    if not plausible_opening_bounds(bounds):
                        continue
                    center = bounds_center(bounds)
                    names = block_names(entity)
                    door_name_score, window_name_score = name_scores(names)
                    stats = block_stats(entity)
                    ref_aci = entity_aci(entity)
                    door_score = door_name_score + (5 if stats["arc90"] else 0)
                    window_score = window_name_score
                    shape_like_window = window_like_bounds(bounds)
                    if ref_aci in wanted_acis:
                        window_score += 4
                    if stats["window_color"] >= 2:
                        window_score += 5
                    elif stats["window_color"] == 1:
                        window_score += 3
                    if shape_like_window:
                        window_score += 2
                    chosen = None
                    # 90도 개폐호가 있으면 색상이 파랑/하늘색이어도 문을 우선한다.
                    if door_score >= 5 and door_score >= window_score:
                        chosen = record_candidate(
                            "door", center, door_score, "block", entity, bounds, ref_aci,
                            names[0] if names else "",
                        )
                    # 색상만으로 창을 잡을 때는 평면상 '길고 얇은' 창 형상까지 요구한다.
                    elif window_score >= 4 and (window_name_score >= 5 or shape_like_window):
                        chosen = record_candidate(
                            "window", center, window_score, "block", entity, bounds, ref_aci,
                            names[0] if names else "",
                        )
                    if chosen:
                        block_candidates.append(chosen)
                    else:
                        neutral_blocks.append({
                            "entity": entity, "center": center, "bounds": bounds,
                            "aci": ref_aci, "block_name": names[0] if names else "",
                            "window_shape": shape_like_window,
                            "window_name_score": window_name_score,
                        })
                    continue

                aci = entity_aci(entity)
                if aci in wanted_acis and kind in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"}:
                    if bounds:
                        color_entities.append({"entity": entity, "bounds": bounds, "center": bounds_center(bounds), "aci": aci})

                if kind == "ARC":
                    try:
                        start = float(getattr(entity, "StartAngle", 0.0) or 0.0)
                        end = float(getattr(entity, "EndAngle", 0.0) or 0.0)
                        sweep = (end - start) % (2.0 * math.pi)
                        radius = float(getattr(entity, "Radius", 0.0) or 0.0)
                        if abs(sweep - math.pi / 2.0) <= math.radians(15.0) and 300.0 <= radius <= 2500.0:
                            center = self._point(entity.Center)
                            arc_candidate = record_candidate(
                                "door", (center[0], center[1]), 4, "arc90", entity, bounds, aci,
                            )
                            if arc_candidate:
                                arc_candidate["detected_length_mm"] = float(radius)
                            free_door_arcs.append(arc_candidate)
                    except Exception:
                        pass
            except Exception:
                continue

        # ACI 4/5 일반 선들은 가까이 붙어 있는 것끼리 묶어서 창 후보 중심으로 사용한다.
        # 블록 바깥에 파란 선을 추가한 도면도 이 묶음이 근처 블록과 결합되도록 한다.
        def rect_gap(a, b):
            dx = max(a[0] - b[2], b[0] - a[2], 0.0)
            dy = max(a[1] - b[3], b[1] - a[3], 0.0)
            return math.hypot(dx, dy)

        clusters = []
        remaining = set(range(len(color_entities)))
        while remaining:
            seed = remaining.pop()
            group = {seed}
            queue = [seed]
            while queue:
                current = queue.pop()
                cb = color_entities[current]["bounds"]
                near = [idx for idx in list(remaining) if rect_gap(cb, color_entities[idx]["bounds"]) <= 250.0]
                for idx in near:
                    remaining.remove(idx)
                    group.add(idx)
                    queue.append(idx)
            entries = [color_entities[idx] for idx in sorted(group)]
            xmin = min(v["bounds"][0] for v in entries)
            ymin = min(v["bounds"][1] for v in entries)
            xmax = max(v["bounds"][2] for v in entries)
            ymax = max(v["bounds"][3] for v in entries)
            bounds = (xmin, ymin, xmax, ymax)
            width, height = bounds_size(bounds)
            longest = max(width, height)
            if longest < 80.0 or longest > 20000.0:
                continue
            clusters.append({
                "bounds": bounds,
                "center": bounds_center(bounds),
                "count": len(entries),
                "aci": entries[0]["aci"] if entries else None,
            })

        # 중립 블록 근처에 파란/하늘색 선 묶음이 있으면 블록 중심을 창 위치로 우선 사용한다.
        consumed_clusters = set()
        for cluster_index, cluster in enumerate(clusters):
            best = None
            best_gap = float("inf")
            for block in neutral_blocks:
                # 주변 파란선이 계단/난간 블록에 붙어 있는 경우를 막기 위해
                # 중립 블록도 창처럼 길고 얇거나 창호명이 명확한 경우에만 결합한다.
                if not bool(block.get("window_shape")) and int(block.get("window_name_score", 0) or 0) < 5:
                    continue
                gap = rect_gap(cluster["bounds"], block["bounds"])
                if gap <= 500.0 and gap < best_gap:
                    best = block
                    best_gap = gap
            if best is not None:
                candidate = record_candidate(
                    "window", best["center"], 5, "block+color", best["entity"], best["bounds"],
                    cluster["aci"], best["block_name"],
                )
                if candidate:
                    block_candidates.append(candidate)
                    consumed_clusters.add(cluster_index)

        for cluster_index, cluster in enumerate(clusters):
            if cluster_index in consumed_clusters:
                continue
            # 한 줄만 외따로 있는 ACI 4/5 객체는 일반 도면선일 가능성이 높아 TEST 후보에서 제외한다.
            if cluster["count"] < 2:
                continue
            candidate = record_candidate(
                "window", cluster["center"], 3, "color-cluster", None, cluster["bounds"], cluster["aci"],
            )
            if candidate:
                block_candidates.append(candidate)

        # 블록으로 이미 잡힌 문 주변의 개별 90도 ARC는 중복 표시하지 않는다.
        for candidate in [v for v in free_door_arcs if v]:
            duplicate = False
            for existing in block_candidates:
                if existing["kind"] != "door":
                    continue
                distance = math.hypot(candidate["x"] - existing["x"], candidate["y"] - existing["y"])
                if distance <= 1800.0:
                    duplicate = True
                    break
            if not duplicate:
                block_candidates.append(candidate)

        # 같은 종류의 후보가 거의 같은 위치에 겹쳐 검출되면 높은 신뢰도 하나만 남긴다.
        ordered = sorted(block_candidates, key=lambda v: (-int(v.get("confidence", 0)), v.get("kind", "")))
        final = []
        for candidate in ordered:
            duplicate = False
            for existing in final:
                if candidate["kind"] != existing["kind"]:
                    continue
                if math.hypot(candidate["x"] - existing["x"], candidate["y"] - existing["y"]) <= 300.0:
                    duplicate = True
                    break
            if not duplicate:
                final.append(candidate)

        # 화면용 임시 부호는 좌상단->우하단 순으로 안정적으로 번호를 붙인다.
        final.sort(key=lambda v: (-float(v.get("y", 0.0)), float(v.get("x", 0.0)), v.get("kind", "")))
        door_no = 0
        window_no = 0
        import re
        for candidate in final:
            block_label = str(candidate.get("block_name", "") or "").strip()
            clean_label = block_label.upper().replace(" ", "")
            known_code = bool(re.fullmatch(r"(?:AW|CW|PW|SW|W|AD|SD|FD|DD|PD|D)[-_]?\d{1,4}", clean_label))
            candidate["auto_label"] = not known_code
            if candidate["kind"] == "door":
                door_no += 1
                candidate["label"] = block_label if known_code else f"D?{door_no:02d}"
            else:
                window_no += 1
                candidate["label"] = block_label if known_code else f"W?{window_no:02d}"

        return {
            "rect": scan_rect,
            "scanned": len(entities),
            "candidates": final,
            "door_count": door_no,
            "window_count": window_no,
            "window_acis": sorted(wanted_acis),
            "filter_warnings": filter_warnings,
        }

    @staticmethod
    def _set_handle(entity, handle: str):
        try:
            entity.dxf.handle = handle
        except Exception:
            pass

    def build_ezdxf_document(self, values: dict, room_rules: dict, color_diagnostic=None, division_resolver=None, progress_callback=None):
        try:
            import ezdxf
        except ImportError as exc:
            raise CadConnectionError("ezdxf 모듈이 설치되지 않았습니다.") from exc

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        def report(message):
            if callable(progress_callback):
                try:
                    progress_callback(str(message))
                except Exception:
                    pass

        report("CAD 임시 벡터 문서 생성 시작")
        layers = set()
        if values.get("import_boundaries"):
            layers.add(room_rules["boundary_layer"].casefold())
            layers.add(room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer")).casefold())
            layers.add(room_rules.get("deduction_layer", default_layer_name("deduction_layer")).casefold())
        if values.get("import_room_tags"):
            layers.add(room_rules["tag_layer"].casefold())
        if values.get("import_wall_finishes"):
            layers.add(room_rules["wall_finish_layer"].casefold())
        include_background = bool(values.get("import_background"))
        include_material_texts = bool(values.get("import_material_list"))
        material_only = bool(values.get("_material_only"))
        selected_only = bool(values.get("import_selected_only"))
        exclude_patterns = [line.strip().casefold() for line in str(room_rules.get("background_exclude_patterns", "")).splitlines() if line.strip()]
        background_layer_cache: dict[str, bool] = {}
        target_layer_cache: dict[int, set[str]] = {}
        source_layer_color_cache: dict[str, tuple[tuple[int, int, int], int | None, str]] = {}
        # 매 CAD 읽기마다 새로 계산한다. probe/final 복사 모두 같은 원본 handle을 사용하므로 최종 map으로 재사용 가능.
        self.last_background_color_overrides = {}

        def _com_truecolor_snapshot(obj):
            """COM TrueColor의 원시 상태를 진단용으로 읽는다. 색상 판정에는 사용하지 않는다."""
            result = {"method": None, "rgb": None, "index": None}
            try:
                tc = getattr(obj, "TrueColor", None)
                if tc is None:
                    return result
                try:
                    result["method"] = int(getattr(tc, "ColorMethod", -1))
                except Exception:
                    pass
                try:
                    result["rgb"] = (
                        max(0, min(255, int(getattr(tc, "Red", 0)))),
                        max(0, min(255, int(getattr(tc, "Green", 0)))),
                        max(0, min(255, int(getattr(tc, "Blue", 0)))),
                    )
                except Exception:
                    pass
                try:
                    result["index"] = int(getattr(tc, "ColorIndex", -1))
                except Exception:
                    pass
            except Exception:
                pass
            return result

        def _com_truecolor_rgb(obj):
            """V18.4 기준: COM AcCmColor가 명시 RGB일 때만 (r,g,b)를 반환한다.

            BYLAYER/BYBLOCK/ACI 객체의 TrueColor 접근값을 그대로 복사하면 색상 상속이
            끊기므로 ColorMethod=ByRGB(194)인 경우만 true_color로 취급한다.
            """
            snap = _com_truecolor_snapshot(obj)
            if snap.get("method") != 194:  # acColorMethodByRGB
                return None
            return snap.get("rgb")

        def _aci_rgb(aci: int):
            try:
                from ezdxf.colors import aci2rgb
                value = aci2rgb(max(1, min(255, abs(int(aci)))))
                return (int(value.r), int(value.g), int(value.b))
            except Exception:
                return (190, 190, 190)

        def _source_layer_color_info(layer_name: str, with_reason: bool = False):
            """V18.4 기준 원본 CAD 레이어 RGB/ACI를 확정한다."""
            name = str(layer_name or "0")
            key = name.casefold()
            cached = source_layer_color_cache.get(key)
            if cached is not None:
                if with_reason:
                    return cached
                return cached[0], cached[1]
            rgb = (190, 190, 190)
            aci = None
            reason = "LAYER_FALLBACK_190"
            try:
                layer_obj = self._require_doc().Layers.Item(name)
                try:
                    raw_aci = int(getattr(layer_obj, "Color", 7) or 7)
                    if 1 <= abs(raw_aci) <= 255:
                        aci = abs(raw_aci)
                except Exception:
                    pass
                explicit_rgb = _com_truecolor_rgb(layer_obj)
                if explicit_rgb is not None:
                    rgb = explicit_rgb
                    reason = "LAYER_TRUECOLOR"
                elif aci is not None:
                    rgb = _aci_rgb(aci)
                    reason = "LAYER_ACI"
            except Exception:
                reason = "LAYER_LOOKUP_ERROR_FALLBACK_190"
            source_layer_color_cache[key] = (rgb, aci, reason)
            if with_reason:
                return rgb, aci, reason
            return rgb, aci

        def _source_entity_color_info(source, layer_name: str, with_reason: bool = False):
            """V18.4 기준 객체 표시 RGB 계산. 진단판에서는 결과와 선택 근거를 함께 기록한다."""
            layer_rgb, layer_aci, layer_reason = _source_layer_color_info(layer_name, True)
            explicit_rgb = _com_truecolor_rgb(source)
            try:
                raw_aci = int(getattr(source, "Color", 256) or 256)
            except Exception:
                raw_aci = 256
            if explicit_rgb is not None:
                width_aci = abs(raw_aci) if 1 <= abs(raw_aci) <= 255 else layer_aci
                result = (explicit_rgb, width_aci, "ENTITY_TRUECOLOR")
            elif 1 <= abs(raw_aci) <= 255:
                aci = abs(raw_aci)
                result = (_aci_rgb(aci), aci, "ENTITY_ACI")
            else:
                result = (layer_rgb, layer_aci, f"ENTITY_BYLAYER_OR_BYBLOCK->{layer_reason}")
            if with_reason:
                return result
            return result[0], result[1]

        def _source_layer_diag(layer_name: str):
            data = {"color": None, "tc_method": None, "tc_rgb": None, "tc_index": None}
            try:
                layer_obj = self._require_doc().Layers.Item(str(layer_name or "0"))
                try:
                    data["color"] = int(getattr(layer_obj, "Color", 7) or 7)
                except Exception:
                    pass
                snap = _com_truecolor_snapshot(layer_obj)
                data["tc_method"] = snap.get("method")
                data["tc_rgb"] = snap.get("rgb")
                data["tc_index"] = snap.get("index")
            except Exception:
                pass
            return data

        def ensure_target_layer(target_doc, layer_name: str):
            """원본 CAD 레이어의 ACI/TrueColor를 임시 ezdxf 문서에 1회 복사한다."""
            name = str(layer_name or "0")
            key = name.casefold()
            doc_key = id(target_doc)
            cached_layers = target_layer_cache.setdefault(doc_key, set())
            if key in cached_layers:
                return
            cached_layers.add(key)
            try:
                target_layer = target_doc.layers.get(name)
            except Exception:
                try:
                    target_layer = target_doc.layers.add(name)
                except Exception:
                    return
            try:
                source_layer = self._require_doc().Layers.Item(name)
            except Exception:
                return
            try:
                aci = int(getattr(source_layer, "Color", 7) or 7)
                if 1 <= abs(aci) <= 255:
                    target_layer.color = aci
            except Exception:
                pass
            rgb = _com_truecolor_rgb(source_layer)
            if rgb is not None:
                try:
                    target_layer.rgb = rgb
                except Exception:
                    pass

        def background_layer_allowed(layer_name: str) -> bool:
            folded = layer_name.casefold()
            if folded in layers:
                return True
            cached = background_layer_cache.get(folded)
            if cached is not None:
                return cached
            if any(fnmatch.fnmatchcase(folded, pattern) for pattern in exclude_patterns):
                background_layer_cache[folded] = False
                return False
            allowed = True
            try:
                # 같은 레이어의 수천 객체마다 COM Layers.Item을 반복 호출하지 않도록 레이어별 1회만 확인한다.
                layer_obj = self._require_doc().Layers.Item(layer_name)
                if not bool(getattr(layer_obj, "LayerOn", True)):
                    allowed = False
                if bool(getattr(layer_obj, "Freeze", False)):
                    allowed = False
            except Exception:
                pass
            background_layer_cache[folded] = allowed
            return allowed

        block_copied: set[str] = set()
        block_copying: set[str] = set()
        cad_block_stats = {"definitions": 0, "entities": 0, "xrefs": 0, "xref_loaded": 0, "xref_empty": 0, "xref_skipped": 0, "errors": [], "xclip_applied": 0, "xclip_missing": 0}
        supplement_target_blocks = {
            str(v or "").strip().casefold()
            for v in (values.get("_cad_supplement_target_blocks") or [])
            if str(v or "").strip()
        }
        xref_xclip_by_handle = {
            str(k or '').strip().upper(): dict(v or {})
            for k, v in dict(values.get("xref_xclip_by_handle") or {}).items()
            if str(k or '').strip()
        }

        def _hatch_loop_points(hatch_source):
            loops = []
            try:
                count = int(getattr(hatch_source, "NumberOfLoops", getattr(hatch_source, "NumLoops", 0)) or 0)
            except Exception:
                count = 0
            for loop_index in range(max(0, count)):
                try:
                    members = hatch_source.GetLoopAt(loop_index)
                    members = list(members or [])
                except Exception:
                    continue
                pts = []
                for member in members:
                    try:
                        mkind = self._entity_kind(member)
                        if mkind in ("LWPOLYLINE", "POLYLINE"):
                            mpts = self._polyline_xy_points(member, mkind)
                        elif mkind == "LINE":
                            a = self._point(member.StartPoint); b = self._point(member.EndPoint)
                            mpts = [(a[0],a[1]), (b[0],b[1])]
                        elif mkind == "ARC":
                            c = self._point(member.Center); r = float(member.Radius)
                            a0 = float(member.StartAngle); a1 = float(member.EndAngle)
                            while a1 < a0: a1 += math.tau
                            steps = max(6, min(96, int(abs(a1-a0)*r/250.0)+2))
                            mpts = [(c[0]+r*math.cos(a0+(a1-a0)*i/steps), c[1]+r*math.sin(a0+(a1-a0)*i/steps)) for i in range(steps+1)]
                        elif mkind == "CIRCLE":
                            c = self._point(member.Center); r = float(member.Radius); steps = 48
                            mpts = [(c[0]+r*math.cos(math.tau*i/steps), c[1]+r*math.sin(math.tau*i/steps)) for i in range(steps)]
                        else:
                            mpts = []
                        for pt in mpts:
                            if not pts or math.hypot(pt[0]-pts[-1][0], pt[1]-pts[-1][1]) > 1e-6:
                                pts.append((float(pt[0]), float(pt[1])))
                    except Exception:
                        continue
                if len(pts) >= 3:
                    loops.append(pts)
            return loops

        def ensure_block_definition(target_doc, block_name: str, phase="FINAL"):
            name = str(block_name or "").strip()
            key = name.casefold()
            if not name or key in block_copied or key in block_copying:
                return
            block_copying.add(key)
            try:
                try:
                    target_block = target_doc.blocks.get(name)
                except Exception:
                    target_block = None
                # ezdxf BlocksSection.get()은 없는 블록에서 예외 대신 None을 반환할 수 있다.
                # 이 경우 target_block=None 상태로 add_line/add_blockref 등을 호출하게 되어
                # CAD 원본 블록을 전부 읽고도 임시 정의가 비어 있는 문제가 생긴다.
                if target_block is None:
                    target_block = target_doc.blocks.new(name)
                    if supplement_target_blocks:
                        report(f"임시 블록 정의 생성: {name}")
                if supplement_target_blocks:
                    report(f"블록 정의 조회 예정: {name}")
                try:
                    source_block = self._require_doc().Blocks.Item(name)
                except Exception as exc:
                    cad_block_stats["errors"].append(f"{name}: 블록 정의 조회 실패: {exc}")
                    return
                if supplement_target_blocks:
                    report(f"블록 정의 조회 완료: {name} · IsXRef 확인 예정")
                cad_block_stats["definitions"] += 1
                try:
                    is_xref = bool(getattr(source_block, "IsXRef", False))
                except Exception:
                    is_xref = False
                if supplement_target_blocks:
                    report(f"블록 정의 IsXRef 확인 완료: {name} · XREF={is_xref} · Count 확인 예정")
                if is_xref:
                    cad_block_stats["xrefs"] += 1
                try:
                    count = int(getattr(source_block, "Count", 0) or 0)
                except Exception:
                    count = 0
                if supplement_target_blocks:
                    report(f"블록 정의 Count 확인 완료: {name} · 내부 객체 {count:,}개")
                if is_xref and count <= 0:
                    cad_block_stats["xref_empty"] += 1
                elif is_xref:
                    cad_block_stats["xref_loaded"] += 1
                # V24.42: 빈 XCLIP 블록 CAD 보충에서는 대상 블록 정의만 필요하다.
                # form 같은 무관한 실제 XREF 내부를 COM으로 재귀 탐색하면 외부참조 1개에서
                # 장시간 대기할 수 있으므로, 보충 대상이 아닌 XREF 정의는 열지 않는다.
                # 보충 대상 자체가 XREF인 경우에는 계속 읽어서 판정 가능하게 유지한다.
                if is_xref and supplement_target_blocks and key not in supplement_target_blocks:
                    cad_block_stats["xref_skipped"] += 1
                    report(f"XREF 블록 보충 대상 외 건너뜀: {name} · 내부 객체 {count:,}개")
                    block_copied.add(key)
                    return
                if is_xref:
                    report(f"XREF 블록 읽기 시작: {name} · 내부 객체 {count:,}개")
                copied_count = 0
                for index in range(count):
                    log_step = (index == 0 or (index + 1) % 250 == 0 or index + 1 == count)
                    if supplement_target_blocks and log_step:
                        report(f"블록 내부 객체 처리 예정: {name} · {index + 1:,}/{count:,}")
                    elif is_xref and log_step:
                        report(f"XREF 블록 읽는 중: {name} · {index + 1:,}/{count:,}")
                    try:
                        child = source_block.Item(index)
                        _target, child_kind = copy_source(target_doc, target_block, child, phase)
                        if _target is not None:
                            copied_count += 1
                        if child_kind:
                            cad_block_stats["entities"] += 1
                    except Exception as exc:
                        cad_block_stats["errors"].append(f"{name}[{index}]: {type(exc).__name__}: {exc}")
                if supplement_target_blocks:
                    try:
                        target_count = len(target_block)
                    except Exception:
                        target_count = -1
                    report(f"블록 정의 복사 완료: {name} · CAD {count:,}개 → 임시블록 {target_count:,}개 · 직접생성 {copied_count:,}개")
                block_copied.add(key)
            finally:
                block_copying.discard(key)

        def copy_source(target_doc, target_msp, source, phase="FINAL"):
            layer = str(getattr(source, "Layer", "0") or "0")
            ensure_target_layer(target_doc, layer)
            kind = self._entity_kind(source)
            if not kind:
                return None, ""
            handle = str(getattr(source, "Handle", "") or "")
            try:
                raw_source_color = getattr(source, "Color", 256)
                raw_source_color = 256 if raw_source_color is None else int(raw_source_color)
            except Exception:
                raw_source_color = 256
            # 기존 V18.4 동작 유지: 0(BYBLOCK)은 `or 256` 때문에 복사 입력에서는 256으로 바뀐다.
            # 진단 로그에는 raw_source_color와 실제 복사 입력 color를 둘 다 기록한다.
            color = int(getattr(source, "Color", 256) or 256)
            dxfattribs = {"layer": layer, "color": color}
            target = None
            if kind == "LINE":
                target = target_msp.add_line(self._point(source.StartPoint), self._point(source.EndPoint), dxfattribs=dxfattribs)
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                points = self._polyline_xy_points(source, kind)
                if len(points) >= 2:
                    # 임시 ezdxf 문서에서는 구형 POLYLINE도 2D LWPOLYLINE으로 정규화한다.
                    # 실구분선 산출에 필요한 XY 형상/닫힘 상태/핸들은 그대로 유지한다.
                    target = target_msp.add_lwpolyline(
                        points,
                        close=bool(getattr(source, "Closed", False)),
                        dxfattribs=dxfattribs,
                    )
            elif kind == "TEXT":
                p = self._point(source.InsertionPoint)
                target = target_msp.add_text(str(source.TextString), dxfattribs={**dxfattribs, "insert": p, "height": float(getattr(source, "Height", 100.0) or 100.0), "rotation": float(getattr(source, "Rotation", 0.0) or 0.0)})
            elif kind == "MTEXT":
                p = self._point(source.InsertionPoint)
                target = target_msp.add_mtext(str(source.TextString), dxfattribs={**dxfattribs, "insert": p, "char_height": float(getattr(source, "Height", 100.0) or 100.0), "rotation": float(getattr(source, "Rotation", 0.0) or 0.0)})
            elif kind == "INSERT":
                if supplement_target_blocks:
                    report(f"INSERT 처리: handle={handle or '-'} · InsertionPoint 읽기 예정")
                p = self._point(source.InsertionPoint)
                if supplement_target_blocks:
                    report(f"INSERT 처리: handle={handle or '-'} · InsertionPoint 완료 · Name 읽기 예정")
                block_name = str(getattr(source, "Name", "") or room_rules["block_name"])
                if supplement_target_blocks:
                    report(f"INSERT 처리: handle={handle or '-'} · Name={block_name} · 블록 정의 확인 예정")
                ensure_block_definition(target_doc, block_name, phase)
                if supplement_target_blocks:
                    report(f"INSERT 처리: handle={handle or '-'} · Name={block_name} · 블록 정의 확인 완료")
                if block_name not in target_doc.blocks:
                    target_doc.blocks.new(block_name)
                ins_attribs = dict(dxfattribs)
                try: ins_attribs["rotation"] = math.degrees(float(getattr(source, "Rotation", 0.0) or 0.0)) if abs(float(getattr(source, "Rotation", 0.0) or 0.0)) <= math.tau*2 else float(getattr(source, "Rotation", 0.0) or 0.0)
                except Exception: pass
                try: ins_attribs["xscale"] = float(getattr(source, "XScaleFactor", 1.0) or 1.0)
                except Exception: pass
                try: ins_attribs["yscale"] = float(getattr(source, "YScaleFactor", 1.0) or 1.0)
                except Exception: pass
                try: ins_attribs["zscale"] = float(getattr(source, "ZScaleFactor", 1.0) or 1.0)
                except Exception: pass
                target = target_msp.add_blockref(block_name, p, dxfattribs=ins_attribs)

                # V24.05.2: XREF INSERT마다 다른 XCLIP을 유지한다.
                # 적용 DXF에서 읽은 WCS 경계를 임시 ezdxf INSERT에 부착하면,
                # 이후 벡터 전개 단계가 같은 XREF 원본을 9번 전부 펼치지 않고
                # 각 INSERT의 실제 표시영역만 남길 수 있다.
                if values.get("xref_only_background") and handle:
                    clip_info = xref_xclip_by_handle.get(str(handle).strip().upper())
                    if clip_info:
                        try:
                            vertices = list(clip_info.get("vertices") or [])
                            if len(vertices) >= 3:
                                from ezdxf.xclip import XClip
                                xclip = XClip(target)
                                xclip.set_wcs_clipping_path(vertices)
                                xclip.enable_clipping()
                                cad_block_stats["xclip_applied"] += 1
                        except Exception as exc:
                            cad_block_stats["errors"].append(
                                f"{block_name}({handle}) XCLIP 적용 실패: {type(exc).__name__}: {exc}"
                            )
                    else:
                        cad_block_stats["xclip_missing"] += 1
                try:
                    attrs = source.GetAttributes()
                except Exception:
                    attrs = []
                for attr in attrs:
                    ap = self._point(getattr(attr, "InsertionPoint", p))
                    added = target.add_attrib(str(attr.TagString), str(attr.TextString), insert=ap)
                    self._set_handle(added, str(getattr(attr, "Handle", "") or ""))
            elif kind == "CIRCLE":
                target = target_msp.add_circle(self._point(source.Center), float(source.Radius), dxfattribs=dxfattribs)
            elif kind == "ARC":
                # ActiveX Arc 각도는 radian, ezdxf add_arc는 degree.
                target = target_msp.add_arc(self._point(source.Center), float(source.Radius), math.degrees(float(source.StartAngle)), math.degrees(float(source.EndAngle)), dxfattribs=dxfattribs)
            elif kind == "HATCH":
                loops = _hatch_loop_points(source)
                solid = str(getattr(source, "PatternName", "") or "").strip().upper() == "SOLID"
                first = None
                for pts in loops:
                    try:
                        boundary = target_msp.add_lwpolyline(pts, close=True, dxfattribs=dxfattribs)
                        if first is None: first = boundary
                    except Exception:
                        pass
                if solid and loops:
                    try:
                        hatch = target_msp.add_hatch(color=max(1, min(255, abs(color) if color not in (0,256) else 7)), dxfattribs=dxfattribs)
                        hatch.set_solid_fill(color=max(1, min(255, abs(color) if color not in (0,256) else 7)))
                        for pts in loops:
                            hatch.paths.add_polyline_path(pts, is_closed=True)
                        target = hatch
                    except Exception:
                        target = first
                else:
                    target = first
            elif kind == "SOLID":
                pts=[]
                for attr in ("Coordinate",):
                    pass
                for prop in ("Point1", "Point2", "Point3", "Point4"):
                    try:
                        q=self._point(getattr(source, prop)); pts.append((q[0],q[1]))
                    except Exception:
                        pass
                if len(pts)>=3:
                    target = target_msp.add_solid(pts[:4], dxfattribs=dxfattribs)
            if target is not None:
                # V18.4 색상 처리 자체는 그대로 두고, 진단판에서는 각 단계 값을 별도로 기록한다.
                display_rgb = (190, 190, 190)
                width_aci = None
                color_reason = "SOURCE_COLOR_EXCEPTION_FALLBACK_190"
                try:
                    display_rgb, width_aci, color_reason = _source_entity_color_info(source, layer, True)
                    map_handle = str(handle or getattr(target.dxf, "handle", "") or "").strip().upper()
                    if map_handle:
                        self.last_background_color_overrides[map_handle] = (
                            (int(display_rgb[0]), int(display_rgb[1]), int(display_rgb[2])),
                            int(width_aci) if width_aci is not None else None,
                        )
                except Exception:
                    map_handle = str(handle or getattr(target.dxf, "handle", "") or "").strip().upper()

                if color_diagnostic is not None and phase == "FINAL":
                    try:
                        source_tc = _com_truecolor_snapshot(source)
                        layer_diag = _source_layer_diag(layer)
                        color_diagnostic.log(
                            "ZWCAD_SOURCE",
                            handle=str(handle or "").strip().upper(),
                            type=kind,
                            layer=layer,
                            color=raw_source_color,
                            bridge_copy_color=color,
                            truecolor_method=source_tc.get("method"),
                            truecolor_rgb=source_tc.get("rgb"),
                            truecolor_index=source_tc.get("index"),
                            layer_color=layer_diag.get("color"),
                            layer_truecolor_method=layer_diag.get("tc_method"),
                            layer_truecolor_rgb=layer_diag.get("tc_rgb"),
                            layer_truecolor_index=layer_diag.get("tc_index"),
                            display_rgb=display_rgb,
                            display_aci=width_aci,
                            reason=color_reason,
                        )
                        if "FALLBACK_190" in str(color_reason):
                            color_diagnostic.mark_fallback("ZWCAD_SOURCE", handle, color_reason)
                    except Exception:
                        pass

                # 모든 객체에 TrueColor를 강제하던 V17.9 방식은 제거한다.
                # BYLAYER/ACI는 원래 Color 값을 그대로 유지하고 레이어 색을 임시 DXF에 복사한다.
                # 실제 TrueColor(ByRGB) 객체에만 true_color를 기록하여 ezdxf 렌더 경로를 깨지 않게 한다.
                explicit_rgb = _com_truecolor_rgb(source)
                if explicit_rgb is not None:
                    try:
                        target.dxf.true_color = (explicit_rgb[0] << 16) | (explicit_rgb[1] << 8) | explicit_rgb[2]
                    except Exception:
                        pass
                self._set_handle(target, handle)

                if color_diagnostic is not None and phase == "FINAL":
                    try:
                        temp_handle = str(getattr(target.dxf, "handle", "") or "").strip().upper()
                        temp_color = int(getattr(target.dxf, "color", 256) or 256)
                        temp_tc = int(getattr(target.dxf, "true_color", 0) or 0)
                        temp_tc_rgb = (
                            ((temp_tc >> 16) & 255, (temp_tc >> 8) & 255, temp_tc & 255)
                            if temp_tc else None
                        )
                        target_layer_color = None
                        target_layer_tc = 0
                        target_layer_rgb = None
                        try:
                            target_layer = target_doc.layers.get(layer)
                            target_layer_color = int(getattr(target_layer, "color", 7) or 7)
                            target_layer_tc = int(getattr(target_layer.dxf, "true_color", 0) or 0)
                            target_layer_rgb = getattr(target_layer, "rgb", None)
                            if target_layer_rgb is not None:
                                target_layer_rgb = tuple(int(x) for x in target_layer_rgb[:3])
                        except Exception:
                            pass
                        color_diagnostic.log(
                            "TEMP_DXF",
                            source_handle=str(handle or "").strip().upper(),
                            temp_handle=temp_handle,
                            handle_match=(str(handle or "").strip().upper() == temp_handle),
                            type=target.dxftype(),
                            layer=str(getattr(target.dxf, "layer", "0") or "0"),
                            color=temp_color,
                            true_color_int=temp_tc,
                            true_color_rgb=temp_tc_rgb,
                            layer_color=target_layer_color,
                            layer_true_color_int=target_layer_tc,
                            layer_rgb=target_layer_rgb,
                            override_key=map_handle,
                            override_rgb=display_rgb,
                            override_aci=width_aci,
                        )
                    except Exception:
                        pass
            return target, kind

        counts: dict[str, int] = {}
        skipped = 0
        source_polyline_types: dict[str, int] = {}

        # CAD 도서 읽기는 도면 전체를 먼저 해석하지 않는다.
        # 1) 도면구분 레이어의 도곽 폴리선만 찾는다.
        # 2) 제목/층 문자도 같은 도면구분 레이어의 TEXT/MTEXT만 읽는다.
        # 3) 다른 레이어 문자는 도서구분 판정에서 일절 검색하지 않는다.
        # 4) 체크된 도서 도곽 안 객체만 최종 수집한다.
        sources = None
        self.last_selected_divisions = []
        self.last_import_diagnostics = {
            "drawing_layer": str(room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer"))),
            "frame_sources": 0,
            "detected_divisions": 0,
            "selected_types": [],
            "selected_frames": [],
            "selected_source_count": 0,
            "duplicate_source_count": 0,
        }
        if material_only:
            sources = self._iter_entities_filtered([], ["TEXT", "MTEXT", "INSERT"])
            self.last_import_diagnostics["frame_selection_mode"] = "재료리스트: 도면 전체 문자·블록속성"
        elif not selected_only:
            drawing_layer = room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer"))
            report(f"도곽 레이어 검색: {drawing_layer}")
            frame_sources = self._iter_entities_filtered(
                [drawing_layer],
                ["LWPOLYLINE", "POLYLINE"],
            )
            report(f"도곽 후보 {len(frame_sources):,}개")
            self.last_import_diagnostics["frame_sources"] = len(frame_sources)
            probe_doc = ezdxf.new("R2010")
            probe_msp = probe_doc.modelspace()
            for source in frame_sources:
                try:
                    copy_source(probe_doc, probe_msp, source, "PROBE")
                except Exception:
                    pass
            try:
                from app.rooms.core import read_drawing_divisions
                frame_only_divisions = read_drawing_divisions(probe_msp, room_rules)
            except Exception:
                frame_only_divisions = []

            frame_rects = []
            for frame in frame_only_divisions:
                pts = frame.get("points") or []
                if len(pts) < 3:
                    continue
                xs = [float(x) for x, _ in pts]
                ys = [float(y) for _, y in pts]
                frame_rects.append((min(xs), min(ys), max(xs), max(ys)))

            # V17.0: 사용 기준상 도곽 제목도 설정된 도면구분 레이어에 있으므로
            # 도곽별 Crossing Selection보다 먼저 해당 레이어의 TEXT/MTEXT를 한 번에 직접 읽는다.
            # ZWCAD에서 여러 사각형 SelectionSet을 반복할 때 맨 위/마지막 도곽 문자만 잡히는 경우를 피한다.
            copied_title_handles = set()
            try:
                division_texts = self._iter_entities_filtered([drawing_layer], ["TEXT", "MTEXT"])
            except Exception:
                division_texts = []
            for source in division_texts:
                try:
                    handle = str(getattr(source, "Handle", "") or "")
                    copy_source(probe_doc, probe_msp, source, "PROBE")
                    if handle:
                        copied_title_handles.add(handle)
                except Exception:
                    pass

            try:
                from app.rooms.core import read_drawing_divisions
                divisions = read_drawing_divisions(probe_msp, room_rules)
            except Exception:
                divisions = []

            # 미지정 도곽이 있어도 다른 레이어의 문자는 보조 검색하지 않는다.
            # 바로 도곽 지정 UI로 넘겨 사용자가 도서구분/층을 지정한다.

            # 도곽 제목/층이 비어 있으면 UI에서 전체 도곽을 정렬해 사용자가 구분/층을 지정할 수 있다.
            # resolver는 CAD 자체를 수정하지 않고 이번 읽기에서 사용할 프로그램 도곽 메타데이터만 반환한다.
            if callable(division_resolver):
                resolved = division_resolver([dict(frame) for frame in divisions])
                if resolved is None:
                    raise CadConnectionError("도곽 구분/층 지정을 취소했습니다.")
                divisions = [dict(frame) for frame in resolved]

            self.last_all_divisions = [dict(frame) for frame in divisions]
            self.last_import_diagnostics["drawing_title_texts"] = len(division_texts)
            self.last_import_diagnostics["detected_divisions"] = len(divisions)
            report(f"도곽/층 판정 완료: {len(divisions):,}개")
            self.last_import_diagnostics["detected_frame_details"] = [
                {
                    "handle": str(frame.get("handle", "") or ""),
                    "title": str(frame.get("title", "") or ""),
                    "drawing_type": str(frame.get("drawing_type", "") or ""),
                    "floors": list(frame.get("floors") or []),
                }
                for frame in divisions
            ]

            selected_types = set()
            if values.get("drawing_overview"):
                selected_types.add("개요")
            if values.get("drawing_plan"):
                selected_types.add("평면도")
            if values.get("drawing_elevation"):
                selected_types.add("입면도")
            if values.get("drawing_section"):
                selected_types.add("단면도")
            if values.get("drawing_finish_schedule"):
                selected_types.add("재료마감표")

            self.last_import_diagnostics["selected_types"] = sorted(selected_types)
            selected_frames = [
                frame for frame in divisions
                if str(frame.get("drawing_type", "")) in selected_types and len(frame.get("points") or []) >= 3
            ]
            self.last_import_diagnostics["selected_frames"] = [
                {
                    "handle": str(frame.get("handle", "") or ""),
                    "title": str(frame.get("title", "") or ""),
                    "drawing_type": str(frame.get("drawing_type", "") or ""),
                    "floors": list(frame.get("floors") or []),
                }
                for frame in selected_frames
            ]
            # 원본 CAD에서 판정한 도곽/층 정보를 실 분류 단계까지 그대로 전달한다.
            self.last_selected_divisions = [
                {
                    "points": [(float(x), float(y)) for x, y in (frame.get("points") or [])],
                    "area": float(frame.get("area", 0.0) or 0.0),
                    "handle": str(frame.get("handle", "") or ""),
                    "title": str(frame.get("title", "") or ""),
                    "drawing_type": str(frame.get("drawing_type", "") or ""),
                    "floors": list(frame.get("floors") or []),
                    "floor": str(frame.get("floor", "") or ""),
                }
                for frame in selected_frames
            ]
            rects = []
            for frame in selected_frames:
                xs = [float(x) for x, _ in frame["points"]]
                ys = [float(y) for _, y in frame["points"]]
                rects.append((min(xs), min(ys), max(xs), max(ys)))

            if rects:
                critical_layers: list[str] = []
                critical_types: list[str] = []
                if values.get("import_boundaries"):
                    critical_layers.extend([
                        room_rules["boundary_layer"],
                        room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer")),
                        room_rules.get("deduction_layer", default_layer_name("deduction_layer")),
                    ])
                    critical_types.extend(["LINE", "LWPOLYLINE", "POLYLINE", "TEXT", "MTEXT"])
                if values.get("import_room_tags"):
                    critical_layers.append(room_rules["tag_layer"])
                    critical_types.extend(["INSERT", "TEXT", "MTEXT"])
                if values.get("import_wall_finishes"):
                    critical_layers.append(room_rules["wall_finish_layer"])
                    critical_types.extend(["LINE", "LWPOLYLINE", "POLYLINE"])

                critical_layers = list(dict.fromkeys(v for v in critical_layers if str(v).strip()))
                critical_types = list(dict.fromkeys(critical_types))
                self.last_import_diagnostics["critical_layer_candidates"] = 0
                self.last_import_diagnostics["critical_frame_matches"] = 0
                self.last_import_diagnostics["critical_merge_added"] = 0

                if not include_background:
                    # V18.5: 배경 OFF에서는 도곽 합집합의 모든 일반 CAD 객체를 훑지 않는다.
                    # 체크된 업무용 레이어/객체형식만 CAD SelectionSet 필터로 직접 읽고,
                    # 그 소수 객체만 선택 도곽 범위로 재필터한다.
                    self.last_import_diagnostics["frame_selection_mode"] = "배경 OFF: 업무레이어 직접 필터 + 도곽 재필터"
                    if critical_layers:
                        critical_candidates = self._iter_entities_filtered(critical_layers, critical_types)
                        self.last_import_diagnostics["critical_layer_candidates"] = len(critical_candidates)
                        sources = [
                            entity for entity in critical_candidates
                            if self._entity_intersects_rects(entity, rects)
                        ]
                        self.last_import_diagnostics["critical_frame_matches"] = len(sources)
                    else:
                        sources = []
                else:
                    # 배경 ON일 때만 일반 객체가 필요하므로 선택 도곽 합집합 Crossing을 수행한다.
                    # 핵심 업무레이어는 Crossing 누락에 대비해 도면 전체에서 1회 보강한다.
                    self.last_import_diagnostics["frame_selection_mode"] = "배경 ON: 단일 합집합 Crossing + 핵심레이어 1회 보강 + 도곽 재필터"
                    report(f"선택 도곽 {len(rects):,}개 범위의 CAD 객체 검색 중")
                    sources = self._iter_entities_in_rects(rects)
                    report(f"도곽 내부 CAD 객체 {len(sources):,}개 검색 완료")
                    if critical_layers:
                        try:
                            critical_candidates = self._iter_entities_filtered(critical_layers, critical_types)
                            self.last_import_diagnostics["critical_layer_candidates"] = len(critical_candidates)
                            critical_matches = [
                                entity for entity in critical_candidates
                                if self._entity_intersects_rects(entity, rects)
                            ]
                            self.last_import_diagnostics["critical_frame_matches"] = len(critical_matches)

                            merged = list(sources)
                            seen_merge_keys = set()
                            for entity in merged:
                                handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                                key = handle or f"OID:{getattr(entity, 'ObjectID', id(entity))}"
                                seen_merge_keys.add(key)
                            added = 0
                            for entity in critical_matches:
                                handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                                key = handle or f"OID:{getattr(entity, 'ObjectID', id(entity))}"
                                if key in seen_merge_keys:
                                    continue
                                seen_merge_keys.add(key)
                                merged.append(entity)
                                added += 1
                            sources = merged
                            self.last_import_diagnostics["critical_merge_added"] = added
                        except Exception as exc:
                            # 보강 경로 실패만으로 기존 Crossing 결과까지 버리지는 않는다.
                            self.last_import_diagnostics["critical_merge_error"] = str(exc)
            elif selected_types:
                # 체크된 도서의 도곽을 못 찾은 경우 무거운 전체도면 분석을 하지 않는다.
                raise CadConnectionError(
                    "체크한 도서 종류의 도곽을 찾지 못했습니다.\n"
                    f"도면구분 레이어: {drawing_layer}\n"
                    f"선택 도서: {', '.join(sorted(selected_types))}"
                )

        if sources is None:
            if selected_only:
                sources = self._iter_entities(True)
            elif not include_background:
                entity_types: list[str] = []
                if values.get("import_boundaries") or values.get("import_wall_finishes"):
                    entity_types.extend(["LINE", "LWPOLYLINE", "POLYLINE"])
                if values.get("import_boundaries"):
                    entity_types.extend(["TEXT", "MTEXT"])
                if values.get("import_room_tags"):
                    entity_types.extend(["INSERT", "TEXT", "MTEXT"])
                selected_layers: list[str] = []
                if values.get("import_boundaries"):
                    selected_layers.extend([room_rules["boundary_layer"], room_rules.get("drawing_division_layer", default_layer_name("drawing_division_layer")), room_rules.get("deduction_layer", default_layer_name("deduction_layer"))])
                if values.get("import_room_tags"):
                    selected_layers.append(room_rules["tag_layer"])
                if values.get("import_wall_finishes"):
                    selected_layers.append(room_rules["wall_finish_layer"])
                sources = self._iter_entities_filtered(selected_layers, list(dict.fromkeys(entity_types)))
            else:
                sources = self._iter_entities(False)

        if include_material_texts and not material_only:
            # 재료리스트는 선택 도곽과 무관하게 현재 도면 전체의 문자와 블록 속성을 읽는다.
            material_sources = self._iter_entities_filtered([], ["TEXT", "MTEXT", "INSERT"])
            merged = list(sources or [])
            merged_keys = set()
            for entity in merged:
                handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                merged_keys.add(handle or f"OID:{getattr(entity, 'ObjectID', id(entity))}")
            added = 0
            for entity in material_sources:
                handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                key = handle or f"OID:{getattr(entity, 'ObjectID', id(entity))}"
                if key in merged_keys:
                    continue
                merged_keys.add(key)
                merged.append(entity)
                added += 1
            sources = merged
            self.last_import_diagnostics["material_text_source_count"] = len(material_sources)
            self.last_import_diagnostics["material_text_merge_added"] = added

        # V24.05.1: CAD 연동 벡터 배경에서 XREF만 별도로 읽을 수 있다.
        # 본도면 일반 객체는 적용 DXF를 ezdxf로 직접 읽고, 느린 COM 순회는 XREF INSERT에만 제한한다.
        if values.get("xref_only_background"):
            xref_names: set[str] = set()
            try:
                blocks = self._require_doc().Blocks
                block_count = int(getattr(blocks, "Count", 0) or 0)
                report(f"CAD XREF 정의 검색 시작: 블록 {block_count:,}개")
                for block_index in range(block_count):
                    try:
                        block = blocks.Item(block_index)
                        if bool(getattr(block, "IsXRef", False)):
                            name = str(getattr(block, "Name", "") or "").strip()
                            if name:
                                xref_names.add(name.casefold())
                    except Exception:
                        continue
                report(f"CAD XREF 정의 검색 완료: {len(xref_names):,}개")
            except Exception as exc:
                report(f"CAD XREF 정의 검색 실패: {type(exc).__name__}: {exc}")

            filtered_sources = []
            for source in list(sources or []):
                try:
                    if self._entity_kind(source) != "INSERT":
                        continue
                    name = str(getattr(source, "EffectiveName", "") or getattr(source, "Name", "") or "").strip().casefold()
                    if name in xref_names:
                        filtered_sources.append(source)
                except Exception:
                    continue
            sources = filtered_sources
            self.last_import_diagnostics["xref_only_names"] = sorted(xref_names)
            self.last_import_diagnostics["xref_only_insert_count"] = len(sources)
            report(f"CAD XREF INSERT만 변환: {len(sources):,}개")

        seen_source_handles: set[str] = set()
        duplicate_sources = 0
        total_sources = len(sources or [])
        report(f"CAD 객체 벡터 변환 시작: {total_sources:,}개")
        trace_each_source = bool(supplement_target_blocks)
        for source_index, source in enumerate(sources, 1):
            if trace_each_source:
                # COM 객체의 속성 조회 자체에서 멈출 수 있으므로 아무 속성도 읽기 전에 먼저 남긴다.
                report(f"CAD 객체 처리 예정: {source_index:,}/{total_sources:,}")
            elif source_index == 1 or source_index % 100 == 0 or source_index == total_sources:
                report(f"CAD 객체 벡터 변환: {source_index:,}/{total_sources:,}")
            try:
                if trace_each_source:
                    report(f"CAD 객체 {source_index:,}/{total_sources:,}: Handle 읽기 예정")
                source_handle = str(getattr(source, "Handle", "") or "").strip().upper()
                if trace_each_source:
                    report(f"CAD 객체 {source_index:,}/{total_sources:,}: Handle={source_handle or '-'} · Layer 읽기 예정")
                if source_handle:
                    if source_handle in seen_source_handles:
                        duplicate_sources += 1
                        continue
                    seen_source_handles.add(source_handle)
                layer = str(getattr(source, "Layer", "0") or "0")
                if trace_each_source:
                    report(f"CAD 객체 {source_index:,}/{total_sources:,}: Layer={layer} · 종류 판정 예정")
                source_kind = self._entity_kind(source)
                if trace_each_source:
                    report(f"CAD 객체 {source_index:,}/{total_sources:,}: 종류={source_kind or '-'} · 필터 판정 예정")
                is_material_text = include_material_texts and source_kind in ("TEXT", "MTEXT", "INSERT")
                if not include_background and layer.casefold() not in layers and not is_material_text:
                    continue
                if include_background and not background_layer_allowed(layer):
                    if trace_each_source:
                        report(f"CAD 객체 {source_index:,}/{total_sources:,}: 배경 필터 제외 완료")
                    continue
                if trace_each_source:
                    report(
                        f"CAD 객체 {source_index:,}/{total_sources:,}: 복사 진입 예정 · "
                        f"handle={source_handle or '-'} · layer={layer} · 종류={source_kind or '-'}"
                    )
                target, kind = copy_source(doc, msp, source, "FINAL")
                if trace_each_source:
                    report(f"CAD 객체 {source_index:,}/{total_sources:,}: 복사 완료 · 종류={kind or '-'}")
                if kind in ("LWPOLYLINE", "POLYLINE"):
                    source_polyline_types[kind] = source_polyline_types.get(kind, 0) + 1
                if not kind:
                    skipped += 1
                elif target is not None:
                    counts[kind] = counts.get(kind, 0) + 1
            except Exception:
                skipped += 1
        if duplicate_sources:
            counts["중복핸들제외"] = duplicate_sources
        self.last_import_diagnostics["selected_source_count"] = len(seen_source_handles)
        self.last_import_diagnostics["duplicate_source_count"] = duplicate_sources
        self.last_import_diagnostics["copied_counts"] = dict(counts)
        self.last_import_diagnostics["source_polyline_types"] = dict(source_polyline_types)
        self.last_import_diagnostics["cad_block_stats"] = dict(cad_block_stats)
        self.last_import_diagnostics["skipped"] = int(skipped)
        report(f"CAD 객체 벡터 변환 완료: 성공 {sum(counts.values()):,} · 제외 {skipped:,}")
        report(
            f"블록/XREF: 정의 {cad_block_stats.get('definitions', 0):,} · "
            f"XREF {cad_block_stats.get('xrefs', 0):,} · XREF읽기 {cad_block_stats.get('xref_loaded', 0):,} · "
            f"XREF건너뜀 {cad_block_stats.get('xref_skipped', 0):,} · "
            f"XCLIP 적용 {cad_block_stats.get('xclip_applied', 0):,} · XCLIP 없음 {cad_block_stats.get('xclip_missing', 0):,}"
        )
        return doc, counts, skipped

    def ensure_configured_layers(self, room_rules: dict) -> dict:
        """CAD 설정에 등록된 업무 레이어를 현재 도면에 생성한다.

        이미 존재하는 레이어는 변경하지 않고 건너뛴다. 빈 레이어명은 제외한다.
        """
        doc = self._require_doc()
        names = []
        for key in LAYER_KEYS:
            name = str(room_rules.get(key, "") or "").strip()
            if name and name.casefold() not in {item.casefold() for item in names}:
                names.append(name)

        result = {"created": [], "existing": [], "failed": []}
        try:
            layers = doc.Layers
        except Exception as exc:
            raise CadConnectionError(f"CAD 레이어 목록을 읽지 못했습니다: {exc}") from exc

        existing_map = {}
        try:
            for index in range(layers.Count):
                layer = layers.Item(index)
                existing_map[str(getattr(layer, "Name", "") or "").casefold()] = layer
        except Exception:
            # 일부 COM 구현은 인덱스 열거가 불안정할 수 있어 Item(name) 조회로 보완한다.
            existing_map = {}

        from app.cad.colors import nearest_aci
        name_colors = {}
        for key, spec in LAYER_DEFINITIONS.items():
            name = str(room_rules.get(key, default_layer_name(key)) or "").strip()
            if name:
                name_colors[name.casefold()] = nearest_aci(
                    room_rules.get(f"{key}_color", default_layer_color(key))
                )

        for name in names:
            try:
                if name.casefold() in existing_map:
                    result["existing"].append(name)
                    continue
                try:
                    layers.Item(name)
                    result["existing"].append(name)
                    continue
                except Exception:
                    pass
                layer = layers.Add(name)
                try:
                    layer.Color = int(name_colors.get(name.casefold(), 7))
                except Exception:
                    pass
                result["created"].append(name)
            except Exception as exc:
                result["failed"].append(f"{name}: {exc}")
        return result

    @staticmethod
    def _configured_block_names(room_rules: dict) -> list[str]:
        """설정에 등록된 블록명만 중복 없이 반환한다.

        현재 설정 화면은 실명 블록 1개를 사용하지만 쉼표/세미콜론으로 여러 이름을
        등록한 경우도 처리하여 향후 설정 확장 시 전체 블록 검색으로 되돌아가지 않는다.
        """
        raw_values = [str(room_rules.get("block_name", "") or "")]
        names = []
        seen = set()
        for raw in raw_values:
            for value in raw.replace(";", ",").split(","):
                name = value.strip()
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    names.append(name)
        return names

    @staticmethod
    def _point2(value):
        try:
            return [float(value[0]), float(value[1])]
        except Exception:
            return None

    def _block_preview_geometry(self, block, max_entities: int = 300) -> list[dict]:
        """블록 선택창 미리보기용 2D 도형과 문자 정보를 가볍게 추출한다."""
        result = []
        count = min(int(getattr(block, "Count", 0) or 0), max_entities)
        for index in range(count):
            try:
                entity = block.Item(index)
                kind = str(getattr(entity, "ObjectName", "") or "").casefold()
                if "line" in kind and "polyline" not in kind:
                    a = self._point2(getattr(entity, "StartPoint", None))
                    b = self._point2(getattr(entity, "EndPoint", None))
                    if a and b:
                        result.append({"type": "line", "points": [a, b]})
                elif "polyline" in kind:
                    coords = list(getattr(entity, "Coordinates", ()) or ())
                    pts = []
                    step = 3 if len(coords) % 3 == 0 and len(coords) >= 6 else 2
                    for i in range(0, len(coords) - 1, step):
                        pts.append([float(coords[i]), float(coords[i + 1])])
                    if len(pts) >= 2:
                        result.append({
                            "type": "polyline",
                            "points": pts,
                            "closed": bool(getattr(entity, "Closed", False)),
                        })
                elif "solid" in kind or "trace" in kind:
                    coords = list(getattr(entity, "Coordinates", ()) or ())
                    pts = []
                    step = 3 if len(coords) % 3 == 0 and len(coords) >= 9 else 2
                    for i in range(0, len(coords) - 1, step):
                        pts.append([float(coords[i]), float(coords[i + 1])])
                    if len(pts) >= 3:
                        result.append({"type": "solid", "points": pts[:4]})
                elif "circle" in kind:
                    c = self._point2(getattr(entity, "Center", None))
                    r = float(getattr(entity, "Radius", 0) or 0)
                    if c and r > 0:
                        result.append({"type": "circle", "center": c, "radius": r})
                elif "arc" in kind:
                    c = self._point2(getattr(entity, "Center", None))
                    r = float(getattr(entity, "Radius", 0) or 0)
                    if c and r > 0:
                        result.append({
                            "type": "arc", "center": c, "radius": r,
                            "start_angle": float(getattr(entity, "StartAngle", 0) or 0),
                            "end_angle": float(getattr(entity, "EndAngle", math.tau) or math.tau),
                        })
                elif "text" in kind or "attribute" in kind:
                    p = self._point2(getattr(entity, "InsertionPoint", None))
                    align_p = self._point2(getattr(entity, "TextAlignmentPoint", None))
                    height = float(getattr(entity, "Height", getattr(entity, "TextHeight", 1.0)) or 1.0)
                    rotation = float(getattr(entity, "Rotation", 0) or 0)
                    width_factor = float(getattr(entity, "ScaleFactor", 1.0) or 1.0)
                    text = str(getattr(entity, "TextString", "") or "").strip()
                    tag = str(getattr(entity, "TagString", "") or "").strip()
                    prompt = str(getattr(entity, "PromptString", "") or "").strip()
                    if not text:
                        text = prompt or tag or "속성"
                    if p:
                        result.append({
                            "type": "text",
                            "point": align_p or p,
                            "text": text.replace("\\P", " "),
                            "height": max(height, 0.1),
                            "rotation": rotation,
                            "width_factor": max(0.2, min(width_factor, 5.0)),
                            "alignment": int(getattr(entity, "Alignment", 0) or 0),
                            "is_attribute": "attribute" in kind,
                        })
            except Exception:
                continue
        return result

    def list_configured_importable_blocks(self, room_rules: dict, library_dir: Path) -> list[dict]:
        """전체 블록을 순회하지 않고 설정에 등록된 블록 정의만 직접 조회한다."""
        doc = self._require_doc()
        library_dir = Path(library_dir)
        names = self._configured_block_names(room_rules)
        if not names:
            raise CadConnectionError("CAD 설정에 등록된 블록명이 없습니다.")

        items = []
        for name in names:
            try:
                block = doc.Blocks.Item(name)
            except Exception:
                items.append({
                    "name": name, "found": False, "exists": (library_dir / f"{name}.dwg").exists(),
                    "entity_count": 0, "preview": [],
                })
                continue
            items.append({
                "name": name,
                "found": True,
                "exists": (library_dir / f"{name}.dwg").exists(),
                "entity_count": int(getattr(block, "Count", 0) or 0),
                "preview": self._block_preview_geometry(block),
            })
        return items

    def import_block_to_library(self, block_name: str, library_dir: Path, target_layer: str = "0") -> dict:
        """지정 블록 정의를 라이브러리 DWG로 저장한다. 같은 이름은 덮어쓴다."""
        doc = self._require_doc()
        block_name = str(block_name or "").strip()
        target_layer = str(target_layer or "0").strip() or "0"
        if not block_name:
            raise CadConnectionError("가져올 블록명이 비어 있습니다.")
        try:
            block = doc.Blocks.Item(block_name)
        except Exception as exc:
            raise CadConnectionError(f"현재 CAD 도면에서 블록을 찾지 못했습니다.\n\n블록명: {block_name}") from exc

        entities = []
        found_tags = set()
        try:
            for index in range(block.Count):
                entity = block.Item(index)
                entities.append(entity)
                object_name = str(getattr(entity, "ObjectName", "") or "").casefold()
                if "attributedefinition" in object_name or "attribute" in object_name:
                    tag = str(getattr(entity, "TagString", "") or "").strip().upper()
                    if tag:
                        found_tags.add(tag)
        except Exception as exc:
            raise CadConnectionError(f"블록 정의 '{block_name}'의 구성 객체를 읽지 못했습니다: {exc}") from exc
        if not entities:
            raise CadConnectionError(f"블록 정의 '{block_name}'에 저장할 구성 객체가 없습니다.")

        library_dir = Path(library_dir)
        library_dir.mkdir(parents=True, exist_ok=True)
        output = library_dir / f"{block_name}.dwg"
        temp_output = library_dir / f".{block_name}.importing.dwg"
        temp_doc = None
        try:
            if temp_output.exists():
                temp_output.unlink()
            temp_doc = self.app.Documents.Add()
            target_space = temp_doc.ModelSpace
            try:
                temp_doc.Layers.Item(target_layer)
            except Exception:
                temp_doc.Layers.Add(target_layer)
            doc.CopyObjects(tuple(entities), target_space)
            layer_changed = 0
            layer_failed = 0
            for index in range(int(getattr(target_space, "Count", 0) or 0)):
                try:
                    copied = target_space.Item(index)
                    copied.Layer = target_layer
                    layer_changed += 1
                except Exception:
                    layer_failed += 1
            try:
                temp_doc.SaveAs(str(temp_output))
            except Exception:
                temp_doc.SaveAs(str(temp_output), 64)
            try:
                temp_doc.Close(False)
            finally:
                temp_doc = None
            if output.exists():
                output.unlink()
            temp_output.replace(output)
        except Exception as exc:
            raise CadConnectionError(
                "블록 정의를 라이브러리 DWG로 저장하지 못했습니다.\n"
                "ZWCAD COM의 CopyObjects/SaveAs 지원 여부를 확인하십시오.\n\n"
                f"상세: {exc}"
            ) from exc
        finally:
            if temp_doc is not None:
                try:
                    temp_doc.Close(False)
                except Exception:
                    pass
            try:
                if temp_output.exists():
                    temp_output.unlink()
            except Exception:
                pass

        return {
            "success": True,
            "block_name": block_name,
            "found_tags": sorted(found_tags),
            "missing_tags": [],
            "path": str(output),
            "entity_count": len(entities),
            "target_layer": target_layer,
            "layer_changed": layer_changed,
            "layer_failed": layer_failed,
        }

    def list_library_blocks(self, library_dir: Path) -> list[dict]:
        """프로그램 블록 라이브러리 DWG 목록과 현재 CAD 보유 여부를 반환한다."""
        doc = self._require_doc()
        library_dir = Path(library_dir)
        library_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for path in sorted(library_dir.glob("*.dwg"), key=lambda p: p.stem.casefold()):
            name = path.stem
            try:
                doc.Blocks.Item(name)
                in_cad = True
            except Exception:
                in_cad = False

            preview = []
            entity_count = 0
            source_doc = None
            try:
                source_doc = self.app.Documents.Open(str(path), True)
                source_space = source_doc.ModelSpace
                entity_count = int(getattr(source_space, "Count", 0) or 0)
                preview = self._block_preview_geometry(source_space)
            except Exception:
                preview = []
            finally:
                if source_doc is not None:
                    try:
                        source_doc.Close(False)
                    except Exception:
                        pass
            items.append({
                "name": name,
                "path": str(path),
                "in_cad": in_cad,
                "entity_count": entity_count,
                "preview": preview,
            })
        return items

    def apply_library_blocks_to_cad(self, block_names: list[str], library_dir: Path, overwrite: bool = False) -> dict:
        """라이브러리 DWG를 현재 CAD의 블록 정의로 추가하거나 갱신한다.

        기존 블록을 갱신할 때는 블록 참조를 삭제하지 않고 정의 내부 객체만 교체하여
        도면에 배치된 기존 참조도 새 형상으로 갱신되게 한다.
        """
        doc = self._require_doc()
        library_dir = Path(library_dir)
        result = {"added": [], "updated": [], "skipped": [], "failed": []}
        for raw_name in block_names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            path = library_dir / f"{name}.dwg"
            if not path.exists():
                result["failed"].append(f"{name}: 라이브러리 파일 없음")
                continue

            try:
                try:
                    target_block = doc.Blocks.Item(name)
                    exists = True
                except Exception:
                    target_block = None
                    exists = False

                if exists and not overwrite:
                    result["skipped"].append(name)
                    continue

                source_doc = None
                try:
                    source_doc = self.app.Documents.Open(str(path), True)
                    source_space = source_doc.ModelSpace
                    source_entities = tuple(source_space.Item(i) for i in range(int(source_space.Count)))
                    if not source_entities:
                        raise CadConnectionError("라이브러리 블록에 구성 객체가 없습니다.")

                    if target_block is None:
                        base_point = self._variant_point(0.0, 0.0, 0.0)
                        target_block = doc.Blocks.Add(base_point, name)
                    else:
                        # 뒤에서부터 삭제해야 COM 컬렉션 인덱스가 흔들리지 않는다.
                        for index in range(int(target_block.Count) - 1, -1, -1):
                            try:
                                target_block.Item(index).Delete()
                            except Exception:
                                pass

                    source_doc.CopyObjects(source_entities, target_block)
                    if exists:
                        result["updated"].append(name)
                    else:
                        result["added"].append(name)
                finally:
                    if source_doc is not None:
                        try:
                            source_doc.Close(False)
                        except Exception:
                            pass
            except Exception as exc:
                result["failed"].append(f"{name}: {exc}")

        try:
            doc.Regen(1)
        except Exception:
            pass
        return result

    def import_configured_blocks(self, room_rules: dict, library_dir: Path) -> dict:
        """현재 CAD 도면의 설정 블록을 프로그램 블록 라이브러리로 저장한다.

        실명 블록 정의 내부의 속성 정의 태그를 검사한 뒤, 새 임시 도면의
        ModelSpace로 블록 구성 객체를 복사하여 DWG 라이브러리 파일로 저장한다.
        ZWCAD COM 구현 차이로 직접 복사가 지원되지 않으면 명확한 오류를 반환한다.
        """
        doc = self._require_doc()
        block_name = str(room_rules.get("block_name", "") or "").strip()
        target_layer = str(room_rules.get("tag_layer", "0") or "0").strip() or "0"
        floor_tag = str(room_rules.get("floor_tag", "") or "").strip().upper()
        room_name_tag = str(room_rules.get("room_name_tag", "") or "").strip().upper()
        number_tags = [x.strip().upper() for x in str(room_rules.get("room_number_tags", "") or "").split(",") if x.strip()]
        if not block_name:
            raise CadConnectionError("CAD 설정의 실명 블록명이 비어 있습니다.")
        required = [tag for tag in (floor_tag, room_name_tag, *(number_tags[:1])) if tag]

        try:
            block = doc.Blocks.Item(block_name)
        except Exception as exc:
            raise CadConnectionError(
                f"현재 CAD 도면에서 설정된 실명 블록을 찾지 못했습니다.\n\n블록명: {block_name}"
            ) from exc

        entities = []
        found_tags = set()
        try:
            for index in range(block.Count):
                entity = block.Item(index)
                entities.append(entity)
                object_name = str(getattr(entity, "ObjectName", "") or "").casefold()
                if "attributedefinition" in object_name or "attribute" in object_name:
                    tag = str(getattr(entity, "TagString", "") or "").strip().upper()
                    if tag:
                        found_tags.add(tag)
        except Exception as exc:
            raise CadConnectionError(f"블록 정의 '{block_name}'의 구성 객체를 읽지 못했습니다: {exc}") from exc

        missing = [tag for tag in required if tag not in found_tags]
        if missing:
            return {
                "success": False,
                "block_name": block_name,
                "found_tags": sorted(found_tags),
                "missing_tags": missing,
                "path": "",
                "entity_count": len(entities),
            }
        if not entities:
            raise CadConnectionError(f"블록 정의 '{block_name}'에 저장할 구성 객체가 없습니다.")

        library_dir = Path(library_dir)
        library_dir.mkdir(parents=True, exist_ok=True)
        output = library_dir / f"{block_name}.dwg"
        temp_doc = None
        try:
            temp_doc = self.app.Documents.Add()
            target_space = temp_doc.ModelSpace
            try:
                temp_doc.Layers.Item(target_layer)
            except Exception:
                temp_doc.Layers.Add(target_layer)
            # pywin32 COM 배열 변환은 튜플이 가장 호환성이 높다.
            doc.CopyObjects(tuple(entities), target_space)
            layer_changed = 0
            layer_failed = 0
            for index in range(int(getattr(target_space, "Count", 0) or 0)):
                try:
                    copied = target_space.Item(index)
                    copied.Layer = target_layer
                    layer_changed += 1
                except Exception:
                    layer_failed += 1
            try:
                temp_doc.SaveAs(str(output))
            except Exception:
                # 일부 ZWCAD 버전은 두 번째 인수 없이 SaveAs를 지원하지 않을 수 있다.
                temp_doc.SaveAs(str(output), 64)
        except Exception as exc:
            raise CadConnectionError(
                "블록 정의를 라이브러리 DWG로 저장하지 못했습니다.\n"
                "ZWCAD COM의 CopyObjects/SaveAs 지원 여부를 확인하십시오.\n\n"
                f"상세: {exc}"
            ) from exc
        finally:
            if temp_doc is not None:
                try:
                    temp_doc.Close(False)
                except Exception:
                    pass

        return {
            "success": True,
            "block_name": block_name,
            "found_tags": sorted(found_tags),
            "missing_tags": [],
            "path": str(output),
            "entity_count": len(entities),
            "target_layer": target_layer,
            "layer_changed": layer_changed,
            "layer_failed": layer_failed,
        }


    @staticmethod
    def _polygon_centroid(points):
        points = list(points or [])
        if not points:
            return 0.0, 0.0
        if len(points) < 3:
            return (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))
        area2 = 0.0
        cx = 0.0
        cy = 0.0
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            cross = x1 * y2 - x2 * y1
            area2 += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if abs(area2) < 1e-9:
            return (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))
        return cx / (3.0 * area2), cy / (3.0 * area2)

    def create_room_tags(self, rooms, room_rules: dict, library_dir: Path | None = None) -> dict:
        """연결된 CAD 활성 도면에 실명 블록을 직접 삽입한다.

        실패 원인을 블록 준비, 경계, 삽입점, CAD 삽입, 속성 갱신 단계로
        나누어 반환한다. UI는 errors와 warnings를 그대로 사용자에게 표시할 수 있다.
        """
        doc = self._require_doc()
        room_list = list(rooms or [])
        block_name = str(room_rules.get("block_name", "") or "").strip()
        tag_layer = str(room_rules.get("tag_layer", "0") or "0").strip() or "0"
        if not block_name:
            raise CadConnectionError("CAD 설정의 실명 블록명이 비어 있습니다.")

        try:
            doc.Layers.Item(tag_layer)
        except Exception:
            try:
                doc.Layers.Add(tag_layer)
            except Exception as exc:
                raise CadConnectionError(f"실명 블록 레이어를 만들지 못했습니다: {tag_layer}\n{exc}") from exc

        block_source = block_name
        block_source_type = "현재 CAD 블록 정의"
        library_path = Path(library_dir or "") / f"{block_name}.dwg"
        try:
            doc.Blocks.Item(block_name)
        except Exception:
            if not library_path.exists():
                raise CadConnectionError(
                    "실명 블록 생성 준비 실패: 블록 정의를 찾지 못했습니다.\n\n"
                    f"설정 블록명: {block_name}\n"
                    f"현재 CAD 블록 정의: 없음\n"
                    f"블록 라이브러리 파일: 없음\n"
                    f"확인 경로: {library_path}"
                )
            block_source = str(library_path)
            block_source_type = "블록 라이브러리 DWG"

        floor_tag = str(room_rules.get("floor_tag", "") or "").strip().upper()
        room_name_tag = str(room_rules.get("room_name_tag", "") or "").strip().upper()
        number_tags = [x.strip().upper() for x in str(room_rules.get("room_number_tags", "") or "").split(",") if x.strip()]
        required_tags = [x for x in [floor_tag, room_name_tag, *number_tags] if x]
        result = {
            "requested": len(room_list),
            "created": 0,
            "failed": 0,
            "already_exists": 0,
            "boundary_missing": 0,
            "invalid_insert_point": 0,
            "attribute_warning": 0,
            "block_name": block_name,
            "block_source": block_source,
            "block_source_type": block_source_type,
            "errors": [],
            "warnings": [],
        }

        for room in room_list:
            room_label = f"{getattr(room, 'floor', '')} / {getattr(room, 'room_number', '')} / {getattr(room, 'room_name', '')}"
            if getattr(room, "tag_handle", ""):
                result["already_exists"] += 1
                continue
            points = getattr(room, "points", None)
            if not points:
                result["boundary_missing"] += 1
                result["errors"].append({
                    "stage": "경계",
                    "room": room_label,
                    "message": "실 경계 좌표가 없어 삽입점을 계산할 수 없습니다.",
                })
                continue

            try:
                x, y = polygon_label_point(points)
                if not (math.isfinite(float(x)) and math.isfinite(float(y))):
                    raise ValueError(f"유효하지 않은 좌표: ({x}, {y})")
            except Exception as exc:
                result["invalid_insert_point"] += 1
                result["failed"] += 1
                result["errors"].append({
                    "stage": "삽입점",
                    "room": room_label,
                    "message": str(exc),
                })
                continue

            try:
                # ZWCAD COM은 삽입점을 일반 Python tuple로 넘기면 일부 버전에서
                # DISP_E_EXCEPTION(-2147352567)을 반환한다. VT_ARRAY|VT_R8 형식의
                # COM VARIANT 좌표를 사용해야 안정적으로 InsertBlock이 실행된다.
                try:
                    import pythoncom
                    import win32com.client
                    insert_point = win32com.client.VARIANT(
                        pythoncom.VT_ARRAY | pythoncom.VT_R8,
                        (float(x), float(y), 0.0),
                    )
                except Exception:
                    # pywin32 형식 생성 자체가 불가능한 예외 환경에만 tuple로 후퇴한다.
                    insert_point = (float(x), float(y), 0.0)
                ref = doc.ModelSpace.InsertBlock(
                    insert_point, block_source, 1.0, 1.0, 1.0, 0.0
                )
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append({
                    "stage": "CAD 블록 삽입",
                    "room": room_label,
                    "point": [float(x), float(y)],
                    "block_source": block_source,
                    "message": f"{type(exc).__name__}: {exc}",
                })
                continue

            try:
                ref.Layer = tag_layer
            except Exception as exc:
                result["warnings"].append({
                    "stage": "레이어 지정",
                    "room": room_label,
                    "message": f"{type(exc).__name__}: {exc}",
                })

            attrs = {}
            try:
                attrs = {str(a.TagString).upper(): a for a in ref.GetAttributes()}
            except Exception as exc:
                result["attribute_warning"] += 1
                result["warnings"].append({
                    "stage": "속성 읽기",
                    "room": room_label,
                    "message": f"블록은 생성했지만 속성을 읽지 못했습니다: {type(exc).__name__}: {exc}",
                })

            missing_tags = [tag for tag in required_tags if tag not in attrs]
            if missing_tags:
                result["attribute_warning"] += 1
                result["warnings"].append({
                    "stage": "속성 태그",
                    "room": room_label,
                    "message": "블록에 설정된 속성 태그가 없습니다: " + ", ".join(missing_tags),
                })

            try:
                if floor_tag in attrs:
                    attrs[floor_tag].TextString = str(room.floor)
                if room_name_tag in attrs:
                    attrs[room_name_tag].TextString = str(room.room_name)
                number_attr = next((attrs[tag] for tag in number_tags if tag in attrs), None)
                if number_attr is not None:
                    number_attr.TextString = str(room.room_number)
                try:
                    ref.Update()
                except Exception:
                    pass
            except Exception as exc:
                result["attribute_warning"] += 1
                result["warnings"].append({
                    "stage": "속성 입력",
                    "room": room_label,
                    "message": f"블록은 생성했지만 속성값 입력 중 오류: {type(exc).__name__}: {exc}",
                })

            room.tag_handle = str(getattr(ref, "Handle", "") or "")
            room.tag_layer = tag_layer
            room.tag_x = float(x)
            room.tag_y = float(y)
            result["created"] += 1
            # 라이브러리 DWG로 첫 삽입이 성공하면 이후에는 현재 도면에 등록된 블록명을 사용한다.
            block_source = block_name

        try:
            doc.Regen(1)
        except Exception as exc:
            result["warnings"].append({
                "stage": "화면 재생성",
                "room": "전체",
                "message": f"{type(exc).__name__}: {exc}",
            })
        return result

    def export_active_document_to_dxf(self, output_path: Path) -> str:
        """사용 금지: 원본 활성 DWG에서 Export를 호출하지 않는다.

        V16.1부터 CAD 배경은 선택 도곽 객체를 메모리상의 ezdxf 문서로
        변환해 직접 생성한다. 원본 문서 보호를 위해 이 이전 경로는 차단한다.
        """
        raise CadConnectionError(
            "원본 CAD 보호를 위해 활성 도면 Export 기능은 사용하지 않습니다.\n"
            "CAD 배경은 선택 도곽 객체를 읽기 전용으로 가져와 생성합니다."
        )

    def update_room_tags(self, rooms, room_rules: dict) -> dict:
        doc = self._require_doc()
        result = {"updated": 0, "missing": 0, "failed": 0, "details": []}
        floor_tag = room_rules["floor_tag"].upper()
        room_name_tag = room_rules["room_name_tag"].upper()
        number_tags = tuple(x.strip().upper() for x in room_rules["room_number_tags"].split(",") if x.strip())
        for room in rooms:
            label = f"{room.floor} / {room.room_number} / {room.room_name}"
            if not room.tag_handle:
                result["missing"] += 1
                result["details"].append(f"[블록 핸들 없음] {label}")
                continue
            try:
                entity = doc.HandleToObject(room.tag_handle)
                attrs = {str(a.TagString).upper(): a for a in entity.GetAttributes()}
                missing_tags = []
                if floor_tag and floor_tag not in attrs:
                    missing_tags.append(floor_tag)
                if room_name_tag and room_name_tag not in attrs:
                    missing_tags.append(room_name_tag)
                if number_tags and not any(tag in attrs for tag in number_tags):
                    missing_tags.append("/".join(number_tags))
                if missing_tags:
                    raise CadConnectionError("필수 속성 태그 없음: " + ", ".join(missing_tags))
                if floor_tag in attrs:
                    attrs[floor_tag].TextString = room.floor
                if room_name_tag in attrs:
                    attrs[room_name_tag].TextString = room.room_name
                number_attr = next((attrs[tag] for tag in number_tags if tag in attrs), None)
                if number_attr is not None:
                    number_attr.TextString = room.room_number
                entity.Update()
                result["updated"] += 1
            except Exception as exc:
                result["failed"] += 1
                result["details"].append(f"[갱신 실패] {label}: {type(exc).__name__}: {exc}")
        return result

    def sync_wall_finish_lines(self, rooms, room_rules: dict) -> dict:
        """프로그램의 실 경계 절점 사이를 현재 CAD의 벽마감 레이어 LINE으로 보낸다.

        현재 마감 CAD 적용의 1단계 구현이다. 공용벽/기존 동일 선분은 중복 생성하지 않으며,
        향후 바닥·천장 객체도 같은 적용 선택창에서 확장할 수 있도록 별도 메서드로 둔다.
        """
        doc = self._require_doc()
        layer_name = str(room_rules.get('wall_finish_layer', '') or '').strip()
        if not layer_name:
            raise CadConnectionError('CAD 설정의 벽마감 레이어명이 비어 있습니다.')
        try:
            doc.Layers.Item(layer_name)
        except Exception:
            try:
                doc.Layers.Add(layer_name)
            except Exception as exc:
                raise CadConnectionError(f'벽마감 레이어를 생성하지 못했습니다: {layer_name} / {exc}') from exc

        def seg_key(x1, y1, x2, y2):
            a = (round(float(x1), 3), round(float(y1), 3))
            b = (round(float(x2), 3), round(float(y2), 3))
            return tuple(sorted((a, b)))

        existing = set()
        try:
            for entity in self._iter_entities_filtered([layer_name], ['LINE', 'LWPOLYLINE', 'POLYLINE']):
                try:
                    kind = self._entity_kind(entity)
                    if kind == 'LINE':
                        a = self._point(entity.StartPoint); b = self._point(entity.EndPoint)
                        existing.add(seg_key(a[0], a[1], b[0], b[1]))
                    elif kind == 'LWPOLYLINE':
                        coords = list(entity.Coordinates)
                        pts = [(float(coords[i]), float(coords[i+1])) for i in range(0, len(coords)-1, 2)]
                        closed = bool(getattr(entity, 'Closed', False))
                        limit = len(pts) if closed else max(0, len(pts)-1)
                        for i in range(limit):
                            existing.add(seg_key(*pts[i], *pts[(i+1) % len(pts)]))
                except Exception:
                    continue
        except Exception:
            # 기존선 조회 실패가 신규 적용 전체 실패로 이어지지는 않게 한다.
            existing = set()

        model = doc.ModelSpace
        seen = set(existing)
        result = {'requested': 0, 'created': 0, 'skipped_existing': 0, 'skipped_degenerate': 0, 'failed': 0, 'layer': layer_name, 'details': []}
        for room in rooms:
            pts = list(getattr(room, 'points', []) or [])
            if len(pts) < 2:
                continue
            for i, (x1, y1) in enumerate(pts):
                x2, y2 = pts[(i + 1) % len(pts)]
                result['requested'] += 1
                if ((float(x2)-float(x1))**2 + (float(y2)-float(y1))**2) ** 0.5 <= 1e-6:
                    result['skipped_degenerate'] += 1
                    continue
                key = seg_key(x1, y1, x2, y2)
                if key in seen:
                    result['skipped_existing'] += 1
                    continue
                try:
                    line = model.AddLine(
                        self._variant_point(float(x1), float(y1), 0.0),
                        self._variant_point(float(x2), float(y2), 0.0),
                    )
                    line.Layer = layer_name
                    try:
                        line.Update()
                    except Exception:
                        pass
                    seen.add(key)
                    result['created'] += 1
                except Exception as exc:
                    result['failed'] += 1
                    if len(result['details']) < 20:
                        result['details'].append(f"{getattr(room, 'floor', '')}/{getattr(room, 'room_number', '')} 구간 {i+1}: {type(exc).__name__}: {exc}")
        return result

    def audit_room_tags(self, rooms, room_rules: dict, scan_orphans: bool = False) -> dict:
        """프로그램 실 목록과 현재 CAD 실명블록 상태를 재대조한다.

        scan_orphans=False가 기본값이다. CAD 모델공간 전체 순회는 대형 도면에서
        COM 호출이 수만 번 발생하여 UI가 장시간 멈출 수 있으므로, 일반 적용 후
        검사는 프로그램에 연결된 핸들만 확인한다. 미연결 블록 전체 검색이 꼭
        필요할 때만 scan_orphans=True로 명시한다.
        """
        doc = self._require_doc()
        block_name = str(room_rules.get("block_name", "") or "").strip()
        floor_tag = str(room_rules.get("floor_tag", "") or "").strip().upper()
        room_name_tag = str(room_rules.get("room_name_tag", "") or "").strip().upper()
        number_tags = tuple(x.strip().upper() for x in str(room_rules.get("room_number_tags", "") or "").split(",") if x.strip())
        issues = []
        valid_handles = set()

        for room in rooms:
            label = f"{room.floor} / {room.room_number} / {room.room_name}"
            handle = str(getattr(room, "tag_handle", "") or "").strip().upper()
            if not handle:
                issues.append(f"[CAD 실명블록 없음] {label}: 신규 생성이 필요합니다.")
                continue
            if handle in valid_handles:
                issues.append(f"[블록 핸들 중복] {label}: 핸들 {handle}")
            valid_handles.add(handle)
            try:
                entity = doc.HandleToObject(handle)
                effective_name = str(getattr(entity, "EffectiveName", "") or getattr(entity, "Name", "") or "").strip()
                if block_name and effective_name and effective_name.casefold() != block_name.casefold():
                    issues.append(f"[블록명 불일치] {label}: CAD={effective_name}, 설정={block_name}")
                attrs = {str(a.TagString).upper(): a for a in entity.GetAttributes()}
                missing = []
                if floor_tag and floor_tag not in attrs:
                    missing.append(floor_tag)
                if room_name_tag and room_name_tag not in attrs:
                    missing.append(room_name_tag)
                if number_tags and not any(tag in attrs for tag in number_tags):
                    missing.append("/".join(number_tags))
                if missing:
                    issues.append(f"[속성 태그 누락] {label}: " + ", ".join(missing))
                    continue
                cad_floor = str(attrs[floor_tag].TextString) if floor_tag in attrs else ""
                cad_name = str(attrs[room_name_tag].TextString) if room_name_tag in attrs else ""
                number_attr = next((attrs[tag] for tag in number_tags if tag in attrs), None)
                cad_number = str(number_attr.TextString) if number_attr is not None else ""
                diffs = []
                if cad_floor != str(room.floor):
                    diffs.append(f"층 CAD={cad_floor!r}/프로그램={room.floor!r}")
                if cad_number != str(room.room_number):
                    diffs.append(f"실번호 CAD={cad_number!r}/프로그램={room.room_number!r}")
                if cad_name != str(room.room_name):
                    diffs.append(f"실명 CAD={cad_name!r}/프로그램={room.room_name!r}")
                if diffs:
                    issues.append(f"[속성값 불일치] {label}: " + ", ".join(diffs))
            except Exception as exc:
                issues.append(f"[CAD 블록 참조 없음] {label}: 핸들 {handle} / {type(exc).__name__}: {exc}")

        # 대형 도면의 ModelSpace 전체 COM 순회는 매우 느리므로 기본 검사에서는 제외한다.
        orphan_handles = []
        if scan_orphans:
            try:
                model = doc.ModelSpace
                for index in range(int(model.Count)):
                    entity = model.Item(index)
                    object_name = str(getattr(entity, "ObjectName", "") or "").casefold()
                    if "blockreference" not in object_name:
                        continue
                    effective_name = str(getattr(entity, "EffectiveName", "") or getattr(entity, "Name", "") or "").strip()
                    if block_name and effective_name.casefold() != block_name.casefold():
                        continue
                    handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                    if handle and handle not in valid_handles:
                        orphan_handles.append(handle)
            except Exception as exc:
                issues.append(f"[CAD 블록 전체검사 실패] {type(exc).__name__}: {exc}")
            if orphan_handles:
                preview = ", ".join(orphan_handles[:10])
                extra = f" 외 {len(orphan_handles)-10}개" if len(orphan_handles) > 10 else ""
                issues.append(f"[연결되지 않은 CAD 실명블록] {len(orphan_handles)}개: {preview}{extra}")

        return {
            "issues": issues,
            "issue_count": len(issues),
            "orphan_count": len(orphan_handles),
            "full_scan_performed": bool(scan_orphans),
        }

    def update_room_boundaries(self, rooms) -> dict:
        doc = self._require_doc()
        result = {"updated": 0, "missing": 0, "failed": 0}
        for room in rooms:
            if not room.boundary_handle:
                result["missing"] += 1
                continue
            try:
                entity = doc.HandleToObject(room.boundary_handle)
                coords = []
                for x, y in room.points:
                    coords.extend((float(x), float(y)))
                entity.Coordinates = tuple(coords)
                try:
                    entity.Closed = True
                except Exception:
                    pass
                entity.Update()
                result["updated"] += 1
            except Exception:
                result["failed"] += 1
        return result

    def clear_highlight(self):
        """UI 미리보기로 Highlight한 단일 CAD 객체를 해제한다."""
        handle = str(self._highlighted_handle or "").strip().upper()
        self._highlighted_handle = ""
        if not handle or self.doc is None:
            return
        try:
            entity = self.doc.HandleToObject(handle)
            entity.Highlight(False)
            try:
                entity.Update()
            except Exception:
                pass
        except Exception:
            pass

    def highlight_handle(self, handle: str):
        """Handle 하나만 즉시 강조한다. 같은 Handle이면 COM 호출을 반복하지 않는다."""
        clean = str(handle or "").strip().upper()
        if not clean or self.doc is None:
            return
        if clean == str(self._highlighted_handle or "").strip().upper():
            return
        self.clear_highlight()
        try:
            entity = self.doc.HandleToObject(clean)
            entity.Highlight(True)
            try:
                entity.Update()
            except Exception:
                pass
            self._highlighted_handle = clean
        except Exception:
            self._highlighted_handle = ""

    @staticmethod
    def _frame_bounds(frame: dict):
        points = list(frame.get("points") or [])
        if len(points) < 3:
            return None
        try:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
        except Exception:
            return None
        if not xs or not ys:
            return None
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        if xmax <= xmin or ymax <= ymin:
            return None
        return xmin, ymin, xmax, ymax

    @staticmethod
    def _com_point_variants(x: float, y: float, z: float = 0.0):
        """ZWCAD 버전별 VARIANT/배열 차이를 흡수하는 3D point 후보를 만든다."""
        values = (float(x), float(y), float(z))
        result = []
        try:
            import pythoncom
            import win32com.client
            result.append(win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values))
        except Exception:
            pass
        try:
            from array import array
            result.append(array("d", values))
        except Exception:
            pass
        result.extend([values, list(values)])
        return result

    def focus_frame(self, frame: dict):
        """도곽 한 개의 범위로 CAD 화면을 ZoomWindow한다. 도곽 자체 형상은 다시 읽지 않는다."""
        self._require_doc()
        bounds = self._frame_bounds(frame)
        if bounds is None:
            raise CadConnectionError("도곽 좌표를 확인할 수 없습니다.")
        xmin, ymin, xmax, ymax = bounds
        width, height = xmax - xmin, ymax - ymin
        margin = max(width, height) * 0.05
        if margin <= 0:
            margin = 1.0
        low = (xmin - margin, ymin - margin, 0.0)
        high = (xmax + margin, ymax + margin, 0.0)

        app = self.app
        if app is None:
            raise CadConnectionError("CAD Application 객체가 연결되어 있지 않습니다.")
        errors = []
        lows = self._com_point_variants(*low)
        highs = self._com_point_variants(*high)
        for p1 in lows:
            for p2 in highs:
                try:
                    app.ZoomWindow(p1, p2)
                    handle = str(frame.get("handle", "") or "").strip().upper()
                    if handle:
                        self.highlight_handle(handle)
                    try:
                        self.doc.Regen(1)
                    except Exception:
                        pass
                    return
                except Exception as exc:
                    errors.append(str(exc))
        detail = errors[-1] if errors else "ZoomWindow 호출 실패"
        raise CadConnectionError(f"도곽 위치로 화면을 이동하지 못했습니다: {detail}")

    def write_drawing_frame_titles(self, frames: list[dict], layer_name: str) -> dict:
        """적용 도곽 명칭을 도곽 좌상단 안쪽에 TEXT로 작성한다.

        문자높이는 각 도곽 짧은 변의 약 1/100로 잡는다. 삽입점은 좌상단에서
        문자높이 2배만큼 안쪽으로 넣어 TEXT가 도곽 바깥으로 걸리지 않게 한다.
        """
        doc = self._require_doc()
        clean_layer = str(layer_name or "").strip()
        if not clean_layer:
            raise CadConnectionError("도면구분 레이어가 비어 있습니다.")
        try:
            target_layer = doc.Layers.Item(clean_layer)
        except Exception:
            try:
                target_layer = doc.Layers.Add(clean_layer)
            except Exception as exc:
                raise CadConnectionError(f"도곽명 레이어를 만들지 못했습니다: {clean_layer}\n{exc}") from exc

        # 같은 버튼을 두 번 눌러도 이미 동일 문자가 있으면 중복 생성하지 않는다.
        # 검색 대상은 도면구분 레이어의 TEXT/MTEXT만이므로 다른 레이어는 전혀 훑지 않는다.
        existing_texts = []
        try:
            for entity in self._iter_entities_filtered([clean_layer], ["TEXT", "MTEXT"]):
                try:
                    kind = self._entity_kind(entity)
                    text = str(getattr(entity, "TextString", "") or "").strip()
                    if not text:
                        continue
                    point = self._point(getattr(entity, "InsertionPoint"))
                    existing_texts.append({
                        "entity": entity,
                        "handle": str(getattr(entity, "Handle", "") or "").strip().upper(),
                        "text": text,
                        "x": float(point[0]),
                        "y": float(point[1]),
                        "kind": kind,
                    })
                except Exception:
                    continue
        except Exception:
            existing_texts = []

        try:
            from app.rooms.core import point_in_polygon
        except Exception:
            point_in_polygon = None

        previous_layer = None
        try:
            previous_layer = doc.ActiveLayer
            doc.ActiveLayer = target_layer
        except Exception:
            previous_layer = None

        result = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "handles": {}}
        try:
            for frame in frames or []:
                title = str(frame.get("title", "") or "").strip()
                if not title:
                    result["skipped"] += 1
                    continue
                bounds = self._frame_bounds(frame)
                points = list(frame.get("points") or [])
                if bounds is None or len(points) < 3:
                    result["failed"] += 1
                    continue
                xmin, ymin, xmax, ymax = bounds
                width, height = xmax - xmin, ymax - ymin
                # '도곽 사이즈의 1/100'은 과도하게 긴 가로도곽에서도 문자만 커지지 않도록 짧은 변 기준.
                text_height = max(1.0, min(width, height) / 100.0)
                inset = max(text_height * 2.0, min(width, height) * 0.015)
                x = min(xmax - text_height, xmin + inset)
                y = max(ymin + text_height, ymax - inset - text_height)
                frame_handle = str(frame.get("handle", "") or "").strip().upper()

                # 이번 연결 세션에서 이 도곽에 만든 TEXT가 있으면 새로 만들지 않고 갱신한다.
                remembered = str(self._drawing_title_handles.get(frame_handle, "") or "").strip().upper() if frame_handle else ""
                if remembered:
                    try:
                        entity = doc.HandleToObject(remembered)
                        entity.TextString = title
                        try:
                            entity.Height = float(text_height)
                        except Exception:
                            pass
                        placed = False
                        for point in self._com_point_variants(x, y, 0.0):
                            try:
                                entity.InsertionPoint = point
                                placed = True
                                break
                            except Exception:
                                continue
                        try:
                            entity.Layer = clean_layer
                        except Exception:
                            pass
                        try:
                            entity.Update()
                        except Exception:
                            pass
                        result["updated"] += 1
                        result["handles"][frame_handle] = remembered
                        continue
                    except Exception:
                        self._drawing_title_handles.pop(frame_handle, None)

                # 이미 같은 레이어/같은 도곽 내부에 동일 제목이 있으면 그대로 사용한다.
                same = None
                if point_in_polygon is not None:
                    for item in existing_texts:
                        try:
                            if item["text"] == title and point_in_polygon(item["x"], item["y"], points):
                                same = item
                                break
                        except Exception:
                            continue
                if same is not None:
                    if frame_handle and same.get("handle"):
                        self._drawing_title_handles[frame_handle] = same["handle"]
                        result["handles"][frame_handle] = same["handle"]
                    result["skipped"] += 1
                    continue

                entity = None
                create_error = None
                for point in self._com_point_variants(x, y, 0.0):
                    try:
                        entity = doc.ModelSpace.AddText(title, point, float(text_height))
                        if entity is not None:
                            break
                    except Exception as exc:
                        create_error = exc
                if entity is None:
                    result["failed"] += 1
                    continue
                try:
                    entity.Layer = clean_layer
                except Exception:
                    try:
                        actual_layer = str(getattr(entity, "Layer", "") or "")
                    except Exception:
                        actual_layer = ""
                    if actual_layer.casefold() != clean_layer.casefold():
                        result["failed"] += 1
                        try:
                            entity.Delete()
                        except Exception:
                            pass
                        continue
                try:
                    entity.Color = 256  # BYLAYER
                except Exception:
                    pass
                try:
                    entity.Update()
                except Exception:
                    pass
                text_handle = str(getattr(entity, "Handle", "") or "").strip().upper()
                if frame_handle and text_handle:
                    self._drawing_title_handles[frame_handle] = text_handle
                    result["handles"][frame_handle] = text_handle
                existing_texts.append({"entity": entity, "handle": text_handle, "text": title, "x": x, "y": y, "kind": "TEXT"})
                result["created"] += 1
        finally:
            if previous_layer is not None:
                try:
                    doc.ActiveLayer = previous_layer
                except Exception:
                    pass

        try:
            doc.Regen(1)
        except Exception:
            pass
        return result

    def regen(self):
        doc = self._require_doc()
        try:
            doc.Regen(1)
        except Exception as exc:
            raise CadConnectionError(f"CAD REGEN 실행에 실패했습니다: {exc}") from exc
    def write_room_wall_link_geometry(self, items, layers=None) -> dict:
        """프로그램 그룹 도형을 연결 CAD의 설정된 실/벽 연동 레이어에 작성한다."""
        from app.rooms.core import (
            ROOM_WALL_LINK_LAYERS, ROOM_WALL_LINK_APPID,
            room_wall_link_xdata_values, parse_room_wall_link_xdata_values,
        )
        from ezdxf.colors import aci2rgb
        link_layers = dict(layers or ROOM_WALL_LINK_LAYERS)
        doc = self._require_doc()
        model = doc.ModelSpace

        def ensure_link_appid():
            try:
                doc.RegisteredApplications.Item(ROOM_WALL_LINK_APPID)
                return
            except Exception:
                pass
            try:
                doc.RegisteredApplications.Add(ROOM_WALL_LINK_APPID)
            except Exception:
                pass

        def set_link_xdata(entity, item):
            """연결 CAD 객체에 프로그램 고유 ID를 XData로 기록한다. 실패 시 형상검증 fallback은 유지된다."""
            values = room_wall_link_xdata_values(dict(item or {}))
            if not values:
                return False
            ensure_link_appid()
            types = [1001] + [1000] * len(values)
            payload = [ROOM_WALL_LINK_APPID] + values
            try:
                import pythoncom
                import win32com.client
                type_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_I2, tuple(types))
                value_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, tuple(payload))
                entity.SetXData(type_variant, value_variant)
                return True
            except Exception:
                try:
                    entity.SetXData(tuple(types), tuple(payload))
                    return True
                except Exception:
                    return False

        def get_link_xdata(entity):
            try:
                raw = entity.GetXData(ROOM_WALL_LINK_APPID)
            except Exception:
                return {}
            try:
                if isinstance(raw, tuple) and len(raw) >= 2:
                    types, values = list(raw[0] or []), list(raw[1] or [])
                    strings = [str(value) for code, value in zip(types, values) if int(code) == 1000]
                    return parse_room_wall_link_xdata_values(strings)
            except Exception:
                pass
            return {}

        def com_array(values):
            try:
                import pythoncom
                import win32com.client
                return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in values))
            except Exception:
                return tuple(float(v) for v in values)

        def com_point(x, y, z=0.0):
            return com_array((x, y, z))

        def hex_rgb(value):
            text = str(value or "").strip().lstrip("#")
            try:
                return tuple(int(text[i:i+2], 16) for i in (0, 2, 4)) if len(text) == 6 else (255,255,255)
            except Exception:
                return (255,255,255)

        aci_cache = {}
        def nearest_aci(value):
            rgb = hex_rgb(value)
            if rgb in aci_cache:
                return aci_cache[rgb]
            best = (1e18, 7)
            for aci in range(1, 256):
                try:
                    c = aci2rgb(aci)
                    d = (int(c.r)-rgb[0])**2 + (int(c.g)-rgb[1])**2 + (int(c.b)-rgb[2])**2
                    if d < best[0]:
                        best = (d, aci)
                except Exception:
                    continue
            aci_cache[rgb] = best[1]
            return best[1]

        for layer_name in link_layers.values():
            try:
                doc.Layers.Item(layer_name)
            except Exception:
                try:
                    doc.Layers.Add(layer_name)
                except Exception as exc:
                    raise CadConnectionError(f"실/벽 연동 레이어를 생성하지 못했습니다: {layer_name} / {exc}") from exc

        section_by_layer = {name.casefold(): section for section, name in link_layers.items()}

        def entity_color_aci(entity):
            try:
                value = abs(int(getattr(entity, 'Color', 7) or 7))
                return max(1, min(255, value)) if value not in (0, 256) else 7
            except Exception:
                return 7

        def seg_key(p1, p2):
            a=(round(float(p1[0]),3),round(float(p1[1]),3)); b=(round(float(p2[0]),3),round(float(p2[1]),3))
            return tuple(sorted((a,b)))

        def poly_key(points):
            q=[(round(float(x),3),round(float(y),3)) for x,y in points]
            if not q: return ()
            variants=[]
            for seq in (q,list(reversed(q))):
                for i in range(len(seq)): variants.append(tuple(seq[i:]+seq[:i]))
            return min(variants)

        existing = set()
        existing_by_object_id = {}
        try:
            entities = self._iter_entities_filtered(list(link_layers.values()), ['LINE','LWPOLYLINE','POLYLINE'])
            for entity in entities:
                layer = str(getattr(entity, 'Layer', '') or '')
                section = section_by_layer.get(layer.casefold())
                if not section:
                    continue
                kind = self._entity_kind(entity)
                metadata = get_link_xdata(entity)
                object_id = str(metadata.get('object_id', '') or '').strip()
                if object_id:
                    existing_by_object_id[object_id] = entity
                aci = entity_color_aci(entity)
                if kind == 'LINE':
                    a=self._point(entity.StartPoint); b=self._point(entity.EndPoint)
                    existing.add((section,aci,'L',seg_key(a,b)))
                elif kind in ('LWPOLYLINE','POLYLINE'):
                    pts=self._polyline_xy_points(entity,kind)
                    if pts:
                        if section == '벽':
                            closed = bool(getattr(entity, 'Closed', False))
                            limit = len(pts) if closed else max(0, len(pts)-1)
                            for i in range(limit):
                                existing.add((section,aci,'L',seg_key(pts[i],pts[(i+1) % len(pts)])))
                        else:
                            existing.add((section,aci,'P',poly_key(pts)))
        except Exception:
            existing=set()

        result={'requested':0,'created':0,'updated':0,'skipped_existing':0,'failed':0,'details':[],'layers':dict(link_layers)}
        for raw in items or []:
            item=dict(raw or {})
            section=str(item.get('section','') or '')
            layer=link_layers.get(section)
            pts=[(float(x),float(y)) for x,y in (item.get('points') or [])]
            if not layer or len(pts)<2:
                continue
            result['requested']+=1
            aci=max(1,min(255,int(item.get('aci') or nearest_aci(item.get('color','')))))
            object_id=str(item.get('object_id','') or '').strip()
            key=(section,aci,'L',seg_key(pts[0],pts[1])) if section=='벽' and len(pts)==2 and not item.get('closed') else (section,aci,'P',poly_key(pts))
            existing_entity=existing_by_object_id.get(object_id) if object_id else None
            if existing_entity is not None:
                try:
                    existing_entity.Layer=layer
                    existing_entity.Color=int(aci)
                    if key[2]=='L':
                        existing_entity.StartPoint=com_point(pts[0][0],pts[0][1])
                        existing_entity.EndPoint=com_point(pts[1][0],pts[1][1])
                    # 폴리선 좌표 재작성은 CAD별 COM 차이가 커서 ID/색상 메타데이터만 안전하게 갱신한다.
                    set_link_xdata(existing_entity,item)
                    try: existing_entity.Update()
                    except Exception: pass
                    result['updated']+=1
                    continue
                except Exception as exc:
                    if len(result['details'])<30:
                        result['details'].append(f"ID 업데이트 실패 {object_id}: {type(exc).__name__}: {exc}")
            if key in existing:
                result['skipped_existing']+=1
                continue
            try:
                if key[2]=='L':
                    entity=model.AddLine(com_point(pts[0][0],pts[0][1]),com_point(pts[1][0],pts[1][1]))
                else:
                    coords=[]
                    for x,y in pts: coords.extend((x,y))
                    entity=model.AddLightWeightPolyline(com_array(coords))
                    try: entity.Closed=bool(item.get('closed',True))
                    except Exception: pass
                entity.Layer=layer
                try: entity.Color=int(aci)
                except Exception: pass
                set_link_xdata(entity,item)
                try: entity.Update()
                except Exception: pass
                existing.add(key)
                if object_id:
                    existing_by_object_id[object_id]=entity
                result['created']+=1
            except Exception as exc:
                result['failed']+=1
                if len(result['details'])<30:
                    result['details'].append(f"{section}/{item.get('group_name','')}: {type(exc).__name__}: {exc}")
        try:
            doc.Regen(1)
        except Exception:
            pass
        return result

    def read_room_wall_link_geometry(self, layers=None) -> dict:
        """연결 CAD의 설정된 실/벽 연동 레이어를 색상별 그룹 매핑용 도형으로 읽는다."""
        from app.rooms.core import (
            ROOM_WALL_LINK_LAYERS, ROOM_WALL_LINK_APPID, parse_room_wall_link_xdata_values,
        )
        from ezdxf.colors import aci2rgb
        link_layers = dict(layers or ROOM_WALL_LINK_LAYERS)
        doc=self._require_doc()
        section_by_layer={name.casefold():section for section,name in link_layers.items()}
        items=[]

        def get_link_xdata(entity):
            try:
                raw=entity.GetXData(ROOM_WALL_LINK_APPID)
            except Exception:
                return {}
            try:
                if isinstance(raw,tuple) and len(raw)>=2:
                    types,values=list(raw[0] or []),list(raw[1] or [])
                    strings=[str(value) for code,value in zip(types,values) if int(code)==1000]
                    return parse_room_wall_link_xdata_values(strings)
            except Exception:
                pass
            return {}

        try:
            entities=self._iter_entities_filtered(list(link_layers.values()), ['LINE','LWPOLYLINE','POLYLINE'])
        except Exception as exc:
            raise CadConnectionError(f"실/벽 연동 레이어를 읽지 못했습니다: {exc}") from exc

        def color_info(entity):
            # ACI 객체는 번호를 그대로 보존한다. TrueColor는 최근접 ACI로 변환한다.
            try:
                raw_aci=abs(int(getattr(entity,'Color',7) or 7))
            except Exception:
                raw_aci=7
            if raw_aci not in (0,256):
                aci=max(1,min(255,raw_aci))
                try:
                    rgb=aci2rgb(aci)
                    return f"#{int(rgb.r):02X}{int(rgb.g):02X}{int(rgb.b):02X}", aci
                except Exception:
                    return '#FFFFFF', aci
            # BYLAYER/BYBLOCK은 실/벽 연동 레이어의 ACI 색상을 우선 해석한다.
            try:
                layer_name=str(getattr(entity,'Layer','') or '')
                layer_obj=doc.Layers.Item(layer_name)
                layer_aci=abs(int(getattr(layer_obj,'Color',7) or 7))
                if layer_aci not in (0,256):
                    aci=max(1,min(255,layer_aci))
                    rgb=aci2rgb(aci)
                    return f"#{int(rgb.r):02X}{int(rgb.g):02X}{int(rgb.b):02X}", aci
            except Exception:
                pass
            try:
                tc=getattr(entity,'TrueColor',None)
                method=int(getattr(tc,'ColorMethod',-1)) if tc is not None else -1
                if tc is not None and method==194:
                    r=max(0,min(255,int(getattr(tc,'Red',0)))); g=max(0,min(255,int(getattr(tc,'Green',0)))); b=max(0,min(255,int(getattr(tc,'Blue',0))))
                    # 로컬 최근접 ACI 계산
                    best=(1e18,7)
                    for index in range(1,256):
                        try:
                            c=aci2rgb(index)
                            d=(int(c.r)-r)**2+(int(c.g)-g)**2+(int(c.b)-b)**2
                            if d<best[0]: best=(d,index)
                        except Exception: pass
                    aci=best[1]
                    rgb=aci2rgb(aci)
                    return f"#{int(rgb.r):02X}{int(rgb.g):02X}{int(rgb.b):02X}", aci
            except Exception:
                pass
            return '#FFFFFF', 7

        for entity in entities:
            try:
                layer=str(getattr(entity,'Layer','') or '')
                section=section_by_layer.get(layer.casefold())
                if not section: continue
                kind=self._entity_kind(entity)
                handle=str(getattr(entity,'Handle','') or '')
                metadata=get_link_xdata(entity)
                if kind=='LINE':
                    a=self._point(entity.StartPoint); b=self._point(entity.EndPoint)
                    color,aci=color_info(entity); items.append({'section':section,'points':[(a[0],a[1]),(b[0],b[1])],'closed':False,'color':color,'aci':aci,'handle':handle,**metadata})
                elif kind in ('LWPOLYLINE','POLYLINE'):
                    pts=self._polyline_xy_points(entity,kind)
                    if len(pts)>=2:
                        color,aci=color_info(entity); items.append({'section':section,'points':pts,'closed':bool(getattr(entity,'Closed',False)),'color':color,'aci':aci,'handle':handle,**metadata})
            except Exception:
                continue
        return {'items':items,'count':len(items),'layers':dict(link_layers)}

