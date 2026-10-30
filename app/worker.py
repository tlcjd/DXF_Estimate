from __future__ import annotations

import traceback
from pathlib import Path
from datetime import datetime

import ezdxf
from PySide6.QtCore import QThread, Signal

from app.material_text.core import aggregate, iter_text_records
from app.rooms.background_cache import build_background_cache
from app.rooms.background_source_cache import (
    load_background_source_cache, save_background_source_cache, floor_frame_rects, plan_frame_rects,
    build_opening_candidates_by_floor,
)
from app.rooms.core import read_cad_background, read_drawing_divisions, read_rooms, validate_room_numbers


def _console_gap(lines: int = 5):
    try:
        print("\n" * max(1, int(lines)), end="", flush=True)
    except Exception:
        pass


def _console_progress(message: str, source: str = ""):
    """백그라운드 진행상황을 실행 CMD에 즉시 출력한다."""
    try:
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{stamp}] "
        if source:
            try:
                name = Path(str(source)).name
            except Exception:
                name = str(source)
            if name:
                prefix += f"[{name}] "
        print(prefix + str(message), flush=True)
    except Exception:
        pass


def _emit_progress(signal, source: str, message: str):
    _console_progress(message, source)
    signal.emit(source, message)


def _division_key(frame: dict) -> str:
    try:
        handle = str((frame or {}).get("handle", "") or "").strip().upper()
        if handle:
            return f"H:{handle}"
        pts = [(float(x), float(y)) for x, y in ((frame or {}).get("points") or [])]
        if not pts:
            return ""
        xs = [x for x, _ in pts]; ys = [y for _, y in pts]
        return f"B:{min(xs):.3f},{min(ys):.3f},{max(xs):.3f},{max(ys):.3f}"
    except Exception:
        return ""


def _merge_drawing_divisions(raw_divisions, override_divisions):
    """DXF에서 실제 검출한 도곽 전체와 프로그램 저장 지정값을 합친다.

    override가 일부 층만 가지고 있어도 나머지 실제 평면도 도곽을 버리지 않는다.
    같은 도곽은 override의 도서구분/층 지정값을 우선한다.
    """
    raw = [dict(v or {}) for v in (raw_divisions or [])]
    override = [dict(v or {}) for v in (override_divisions or [])]
    by_key = {_division_key(v): v for v in override if _division_key(v)}
    used = set()
    merged = []
    for frame in raw:
        key = _division_key(frame)
        patch = by_key.get(key) if key else None
        if patch:
            combined = dict(frame)
            for name in ("drawing_type", "assigned_input", "title", "floors", "floor"):
                value = patch.get(name)
                if value not in (None, "", []):
                    combined[name] = list(value) if name == "floors" else value
            merged.append(combined)
            used.add(key)
        else:
            merged.append(frame)
    for frame in override:
        key = _division_key(frame)
        if key and key in used:
            continue
        if key and any(_division_key(v) == key for v in merged):
            continue
        merged.append(frame)
    return merged



def _supplement_empty_xclip_blocks_from_active_cad(main_doc, bridge, cad_values: dict, room_rules: dict, progress_callback=None):
    """V24.41: 적용 DXF에 INSERT/XCLIP은 남았지만 블록 정의가 비어 있는 경우
    연결 CAD의 실제 블록 정의를 임시 ezdxf 문서로 복사해 현재 DXF 문서에 보충한다.

    원본 CAD/DXF는 수정하지 않는다. 기존 INSERT의 위치/회전/축척/XCLIP 확장사전은
    그대로 두고 dxf.name만 보충 블록명으로 바꾼다.
    """
    stats = {
        "targets": [], "cad_doc_built": False, "imported": {}, "failed": [],
        "before_empty": 0, "after_filled": 0, "retargeted_inserts": 0,
    }
    try:
        from ezdxf.xclip import XClip
    except Exception as exc:
        stats["failed"].append(f"XClip import: {type(exc).__name__}: {exc}")
        return {}, stats

    # XCLIP을 실제 소유하면서 적용 DXF의 블록 정의가 0개인 INSERT만 대상으로 한다.
    target_names = {}
    try:
        inserts = list(main_doc.modelspace().query('INSERT'))
    except Exception:
        inserts = []
    for insert in inserts:
        try:
            clip = XClip(insert)
            if not clip.has_clipping_path or not clip.is_clipping_enabled:
                continue
            raw_name = str(getattr(insert.dxf, 'name', '') or '').strip()
            if not raw_name:
                continue
            try:
                block = main_doc.blocks.get(raw_name)
                count = len(block)
            except Exception:
                count = 0
            if count > 0:
                continue
            key = raw_name.casefold()
            target_names.setdefault(key, raw_name)
        except Exception:
            continue

    stats["targets"] = list(target_names.values())
    stats["before_empty"] = len(target_names)
    if not target_names:
        return {}, stats

    def report(msg):
        if callable(progress_callback):
            try:
                progress_callback(str(msg))
            except Exception:
                pass

    report(f"빈 XCLIP 블록 CAD 보충 시작: {', '.join(target_names.values())}")

    # V0.24.7: 대형 블록을 Python/COM 객체별로 해석하지 않고 CAD CopyObjects로
    # 묶음 복사 -> 임시 DXF SaveAs -> ezdxf 파일 읽기 방식으로 보충한다.
    # 실패할 때만 기존의 느린 객체별 복사기로 폴백한다.
    try:
        cad_doc, fast_stats = bridge.build_ezdxf_supplement_blocks_fast(
            list(target_names.values()),
            progress_callback=lambda msg: report(f"CAD 보충: {msg}"),
        )
        stats["cad_doc_built"] = True
        for err in list((fast_stats or {}).get("errors") or []):
            stats["failed"].append(str(err))
        report("CAD 고속 보충 문서 생성 완료")
    except Exception as fast_exc:
        report(f"CAD 고속 보충 실패 · 기존 방식 폴백: {type(fast_exc).__name__}: {fast_exc}")
        values = dict(cad_values or {})
        values['import_background'] = True
        values['import_selected_only'] = False
        values['xref_only_background'] = False
        values['import_material_list'] = False
        values['_cad_supplement_target_blocks'] = list(target_names.values())
        try:
            cad_doc, _counts, _skipped = bridge.build_ezdxf_document(
                values, room_rules, progress_callback=lambda msg: report(f"CAD 보충(폴백): {msg}")
            )
            stats["cad_doc_built"] = True
        except Exception as exc:
            stats["failed"].append(f"CAD 임시문서 생성: {type(exc).__name__}: {exc}")
            report(stats["failed"][-1])
            return {}, stats

    source_names = {}
    try:
        for block in cad_doc.blocks:
            name = str(getattr(block, 'name', '') or '').strip()
            if name:
                source_names[name.casefold()] = name
    except Exception:
        pass

    aliases = {}
    try:
        from ezdxf.addons import Importer
        importer = Importer(cad_doc, main_doc)
        imported_roots = {}
        for key, original_name in target_names.items():
            source_name = source_names.get(key)
            if not source_name:
                stats["failed"].append(f"{original_name}: 연결 CAD 임시문서에 동일 블록 정의 없음")
                continue
            try:
                source_block = cad_doc.blocks.get(source_name)
                source_count = len(source_block)
            except Exception as exc:
                stats["failed"].append(f"{original_name}: CAD 블록 조회 실패: {type(exc).__name__}: {exc}")
                continue
            if source_count <= 0:
                stats["failed"].append(f"{original_name}: 연결 CAD 블록 정의도 비어 있음")
                continue
            try:
                # target_doc에 같은 이름의 빈 정의가 있으므로 rename=True로 독립 보충 블록을 만든다.
                imported_name = importer.import_block(source_name, rename=True)
                imported_roots[key] = str(imported_name)
                stats["imported"][original_name] = {
                    "source_count": int(source_count), "target_name": str(imported_name),
                }
                aliases[str(imported_name).casefold()] = original_name
            except Exception as exc:
                stats["failed"].append(f"{original_name}: 블록 import 실패: {type(exc).__name__}: {exc}")
        try:
            importer.finalize()
        except Exception as exc:
            stats["failed"].append(f"Importer finalize: {type(exc).__name__}: {exc}")

        # 원래 빈 블록을 참조하던 모든 INSERT를 보충 정의로 연결한다.
        # INSERT 자체는 그대로이므로 handle/transform/XCLIP은 보존된다.
        if imported_roots:
            for insert in list(main_doc.modelspace().query('INSERT')):
                try:
                    raw_name = str(getattr(insert.dxf, 'name', '') or '').strip()
                    imported_name = imported_roots.get(raw_name.casefold())
                    if not imported_name:
                        continue
                    insert.dxf.name = imported_name
                    stats["retargeted_inserts"] += 1
                except Exception as exc:
                    stats["failed"].append(f"INSERT 재연결 실패: {type(exc).__name__}: {exc}")

            # 실제 보충 후 정의 수를 확인한다.
            for original_name, info in list(stats["imported"].items()):
                try:
                    target_block = main_doc.blocks.get(str(info.get('target_name') or ''))
                    target_count = len(target_block)
                    info["target_count"] = int(target_count)
                    if target_count > 0:
                        stats["after_filled"] += 1
                except Exception as exc:
                    info["target_count"] = -1
                    stats["failed"].append(f"{original_name}: 보충 결과 확인 실패: {type(exc).__name__}: {exc}")
    except Exception as exc:
        stats["failed"].append(f"CAD 블록 보충 처리: {type(exc).__name__}: {exc}")

    report(
        f"빈 XCLIP 블록 CAD 보충 완료: 대상 {stats['before_empty']} · 성공 {stats['after_filled']} · "
        f"INSERT 재연결 {stats['retargeted_inserts']} · 오류 {len(stats['failed'])}"
    )
    return aliases, stats

def _inspect_xrefs_only(doc):
    """DXF 안의 XREF를 판정만 한다. 외부 파일 자동복구/포함은 하지 않는다.

    DXF 단독 작업에서는 XREF 내용이 원본 DXF 안에 없을 수 있으므로 누락 경고만 남긴다.
    XREF까지 포함한 벡터는 연결된 CAD에서 읽는 경로가 담당한다.
    """
    stats = {"found": 0, "embedded": 0, "skipped": 0, "errors": [], "details": []}
    try:
        blocks = list(doc.blocks)
    except Exception as exc:
        stats["errors"].append(f"XREF 판정 실패: {type(exc).__name__}: {exc}")
        return stats
    for block in blocks:
        try:
            block_entity = getattr(block, "block", None)
            if block_entity is None:
                continue
            xref_path = str(block_entity.dxf.get("xref_path", "") or "")
            is_xref = bool(getattr(block_entity, "is_xref", False) or getattr(block_entity, "is_xref_overlay", False) or xref_path)
            if not is_xref:
                continue
            name = str(getattr(block, "name", "?") or "?")
            stats["found"] += 1
            stats["skipped"] += 1
            msg = "DXF 단독 읽기에서는 XREF 내용을 포함하지 않음 · CAD 연동 벡터 읽기 사용"
            stats["details"].append({"name": name, "path": xref_path, "result": msg})
            stats["errors"].append(f"{name} ({xref_path or '경로없음'}): XREF 누락")
        except Exception as exc:
            stats["errors"].append(f"XREF 판정 실패: {type(exc).__name__}: {exc}")
    return stats


def _write_block_vector_diagnostic(cache_root, source_path, stats, xref_stats=None):
    """DXF 블록 벡터 전개 상태를 사용자가 그대로 복붙할 수 있는 TXT로 남긴다."""
    try:
        root = Path(cache_root)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "블록_벡터진단.txt"
        block = dict((stats or {}).get("block_diagnostic") or {})
        lines = [
            "DXF 블록 벡터 진단",
            "",
            f"원본 DXF: {source_path}",
            f"ModelSpace 객체수: {sum(dict((stats or {}).get('modelspace_type_counts') or {}).values())}",
            f"ModelSpace 종류별: {dict((stats or {}).get('modelspace_type_counts') or {})}",
            f"최종 일반 벡터 선분수: {(stats or {}).get('background_segment_count', 0)}",
            f"최종 주석 벡터 선분수: {len((stats or {}).get('annotation_segments') or [])}",
            f"문자수: {len((stats or {}).get('texts') or [])}",
            "",
            "[INSERT 요약]",
            f"INSERT 수: {block.get('insert_count', 0)}",
            f"recursive_decompose 성공: {block.get('recursive_ok', 0)}",
            f"recursive_decompose 실패: {block.get('recursive_failed', 0)}",
            f"virtual_entities 대체 성공: {block.get('virtual_fallback_ok', 0)}",
            f"virtual_entities 대체 실패: {block.get('virtual_fallback_failed', 0)}",
            f"ATTRIB 수: {block.get('attrib_count', 0)}",
            f"전개 자식 종류별: {dict(block.get('child_type_counts') or {})}",
        ]
        if xref_stats is not None:
            lines += [
                "", "[XREF 요약]",
                f"XREF 발견: {xref_stats.get('found', 0)}",
                f"XREF 포함 성공: {xref_stats.get('embedded', 0)}",
                f"XREF 건너뜀: {xref_stats.get('skipped', 0)}",
                f"XREF 오류: {xref_stats.get('errors', [])}",
                f"XREF 상세: {xref_stats.get('details', [])}",
            ]
        lines += ["", "[INSERT 상세]"]
        details = list(block.get("details") or [])
        if not details:
            lines.append("INSERT 상세 없음")
        for idx, item in enumerate(details, 1):
            lines += [
                f"#{idx} handle={item.get('handle','')} name={item.get('name','')} depth={item.get('depth')} layer={item.get('layer','')}",
                f"  insert={item.get('insert')} rotation={item.get('rotation')} scale=({item.get('xscale')},{item.get('yscale')}) attribs={item.get('attribs')}",
                f"  block_def_count={item.get('block_def_count')} block_def_types={item.get('block_def_types')}",
                f"  method={item.get('method')} virtual_count={item.get('virtual_count')} virtual_types={item.get('virtual_types')} segments_added={item.get('segments_added')}",
            ]
            if item.get('block_def_error'):
                lines.append(f"  block_def_error={item.get('block_def_error')}")
            if item.get('recursive_error'):
                lines.append(f"  recursive_error={item.get('recursive_error')}")
            if item.get('fallback_error'):
                lines.append(f"  fallback_error={item.get('fallback_error')}")
        if block.get("modelspace_error"):
            lines += ["", "[ModelSpace 처리 오류]", str(block.get("modelspace_error"))]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


class ScanWorker(QThread):
    """재료문자와 실 데이터만 먼저 읽는 기본 분석 작업."""

    finished_ok = Signal(object, object, object, int)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, path, classifier, room_rules=None, divisions_override=None):
        super().__init__()
        self.path = path
        self.classifier = classifier
        self.room_rules = room_rules or {}
        self.divisions_override = list(divisions_override or [])

    def run(self):
        try:
            self.progress.emit("DXF 기본 데이터 읽는 중...")
            doc = ezdxf.readfile(self.path)

            self.progress.emit("재료문자 추출 중...")
            records = list(iter_text_records(doc))
            result = aggregate(records, self.classifier)

            self.progress.emit("실구분선 및 실명 읽는 중...")
            divisions = self.divisions_override or read_drawing_divisions(doc.modelspace(), self.room_rules)
            rooms, errors = read_rooms(doc, self.room_rules, divisions)
            errors.extend(validate_room_numbers(rooms))

            self.finished_ok.emit(result, rooms, errors, len(records))
        except Exception:
            self.failed.emit(traceback.format_exc())


class BackgroundScanWorker(QThread):
    """프로젝트 DXF 배경을 별도 스레드에서 읽고 디스크 캐시 이미지로 저장한다."""

    finished_ok = Signal(str)
    failed = Signal(str, str)
    progress = Signal(str, str)

    def __init__(self, path: str, rooms, cache_root: str, background_max_pixels: int, background_style: str, exclude_patterns=None, business_layers=None, cache_source_path: str | None = None, room_rules=None, background_line_width: float = 1.5, background_aci_widths: str = "", background_keep_red: bool = False, preserve_existing: bool = False, refresh_source: bool = False, divisions_override=None, render_mode: str = "png"):

        super().__init__()
        self.path = path
        self.cache_source_path = cache_source_path or path
        self.rooms = list(rooms or [])
        self.cache_root = cache_root
        self.background_max_pixels = background_max_pixels
        self.background_style = background_style
        self.background_line_width = float(background_line_width)
        self.background_aci_widths = str(background_aci_widths)
        self.background_keep_red = bool(background_keep_red)
        self.preserve_existing = bool(preserve_existing)
        self.refresh_source = bool(refresh_source)
        self.exclude_patterns = list(exclude_patterns or [])
        self.business_layers = list(business_layers or [])
        self.background_filter_signature = "|".join(sorted([str(x).casefold() for x in self.exclude_patterns + self.business_layers if str(x).strip()]))
        self.room_rules = dict(room_rules or {})
        self.divisions_override = list(divisions_override or [])
        self.render_mode = "vector" if str(render_mode).lower() == "vector" else "png"
        self.block_diagnostic_text = ""
        self.block_diagnostic_path = ""

    def run(self):
        _console_gap(5)
        _console_progress("DXF 벡터/배경 작업 시작", self.path)
        try:
            cached = None if self.refresh_source else load_background_source_cache(
                Path(self.cache_root), self.cache_source_path, signature_source_path=self.path
            )
            if cached:
                _emit_progress(self.progress, self.path, "진행사항 90% / 100% · 저장된 도곽 벡터 캐시 불러오는 중...")
                background = cached['segments']
                divisions = cached['divisions']
                texts = cached['texts']
                annotations = cached.get('annotation_segments', [])
                fills = cached.get('fills', [])
            else:
                _emit_progress(self.progress, self.path, "진행사항 5% / 100% · 프로젝트 DXF 여는 중...")
                doc = ezdxf.readfile(self.path)
                _emit_progress(self.progress, self.path, "진행사항 15% / 100% · 블록/XREF 확인 중...")
                xref_stats = _inspect_xrefs_only(doc)
                _emit_progress(self.progress, self.path, "진행사항 25% / 100% · 도곽/층 범위 확인 중...")
                # 도곽을 먼저 판정하고, 이후 배경 벡터는 평면 도곽과 교차하는 객체만 보관한다.
                raw_divisions = read_drawing_divisions(doc.modelspace(), self.room_rules)
                divisions = _merge_drawing_divisions(raw_divisions, self.divisions_override)
                frame_rects = plan_frame_rects(divisions)
                floor_rect_map = floor_frame_rects(divisions, self.rooms)
                _console_progress(
                    f"평면 도곽 범위 확인: 도곽 {len(frame_rects):,}개 · 층매핑 {len(floor_rect_map):,}개 "
                    f"({', '.join(sorted(floor_rect_map)) if floor_rect_map else '없음'})",
                    self.path,
                )
                if not frame_rects:
                    raise RuntimeError("평면 도곽을 찾지 못해 벡터/PNG 배경 생성을 중단했습니다. 도곽 밖 전체 도면은 읽지 않습니다.")
                _emit_progress(self.progress, self.path, "진행사항 35% / 100% · 도곽 내부 벡터 읽는 중...")
                background, stats = read_cad_background(
                    doc, self.exclude_patterns, self.business_layers, crop_rects=frame_rects
                )
                stats['background_segment_count'] = len(background)
                diag_path = _write_block_vector_diagnostic(self.cache_root, self.path, stats, xref_stats)
                if diag_path:
                    self.block_diagnostic_path = str(diag_path)
                    try:
                        self.block_diagnostic_text = diag_path.read_text(encoding="utf-8")
                    except Exception:
                        self.block_diagnostic_text = ""
                    _emit_progress(self.progress, self.path, f"진행사항 72% / 100% · 블록 진단 정리 중...")
                _emit_progress(self.progress, self.path, "진행사항 75% / 100% · 벡터/문자 캐시 정리 중...")
                texts = stats.get('texts', [])
                annotations = stats.get('annotation_segments', [])
                fills = stats.get('fills', [])
                _emit_progress(self.progress, self.path, "진행사항 82% / 100% · 창호 후보 데이터 정리 중...")
                opening_candidates = build_opening_candidates_by_floor(doc, divisions, self.rooms, (4, 5))
                save_background_source_cache(
                    Path(self.cache_root), self.cache_source_path, background, divisions, texts, annotations,
                    rooms=self.rooms, opening_candidates=opening_candidates, signature_source_path=self.path, fills=fills
                )
            if self.render_mode == "vector":
                _emit_progress(self.progress, self.path, "진행사항 100% / 100% · CAD 벡터 캐시 생성 완료 · 화면 표시 준비 중...")
            else:
                _emit_progress(self.progress, self.path, "진행사항 90% / 100% · CAD 배경 PNG 생성 중... (다른 작업 가능)")
                build_background_cache(self.cache_source_path, self.rooms, background, Path(self.cache_root), self.background_max_pixels, self.background_style, self.background_filter_signature, divisions, self.background_line_width, self.background_aci_widths, self.background_keep_red, texts, preserve_existing=self.preserve_existing, background_annotations=annotations)
            _console_progress("작업 완료", self.path)
            _console_gap(5)
            self.finished_ok.emit(self.cache_source_path)
        except Exception:
            detail = traceback.format_exc()
            _console_progress("작업 실패\n" + detail, self.path)
            self.failed.emit(self.cache_source_path, detail)


class CadBackgroundWorker(QThread):
    """연결된 CAD에서 배경 원본을 읽고 캐시/PNG를 만드는 전용 백그라운드 작업.

    COM 객체는 UI 스레드에서 넘겨받지 않고 이 스레드 안에서 새로 연결한다.
    저장된 원본 도형 캐시가 있으면 CAD를 다시 읽지 않고 즉시 재렌더링한다.
    """

    finished_ok = Signal(str)
    failed = Signal(str, str)
    progress = Signal(str, str)

    def __init__(self, source_path: str, rooms, cache_root: str, background_max_pixels: int, background_style: str, cad_values: dict, room_rules: dict, exclude_patterns=None, business_layers=None, background_filter_signature: str = "", background_line_width: float = 1.5, background_aci_widths: str = "", background_keep_red: bool = False, preserve_existing: bool = False, refresh_source: bool = False, divisions_override=None, render_mode: str = "png", applied_dxf_path: str = ""):

        super().__init__()
        self.source_path = source_path
        self.rooms = list(rooms or [])
        self.cache_root = str(cache_root)
        self.background_max_pixels = int(background_max_pixels)
        self.background_style = str(background_style)
        self.cad_values = dict(cad_values or {})
        self.room_rules = dict(room_rules or {})
        self.exclude_patterns = list(exclude_patterns or [])
        self.business_layers = list(business_layers or [])
        self.background_filter_signature = str(background_filter_signature)
        self.background_line_width = float(background_line_width)
        self.background_aci_widths = str(background_aci_widths)
        self.background_keep_red = bool(background_keep_red)
        self.preserve_existing = bool(preserve_existing)
        self.refresh_source = bool(refresh_source)
        self.divisions_override = list(divisions_override or [])
        self.render_mode = "vector" if str(render_mode).lower() == "vector" else "png"
        self.applied_dxf_path = str(applied_dxf_path or "").strip()
        self.block_diagnostic_text = ""
        self.block_diagnostic_path = ""

    def run(self):
        bridge = None
        _console_gap(5)
        _console_progress("CAD 벡터/배경 작업 시작", self.source_path)
        try:
            root = Path(self.cache_root)
            applied_signature = Path(self.applied_dxf_path) if self.applied_dxf_path else None
            # V0.24.8: CAD 연동 벡터의 실제 원본은 현재 연결 CAD다.
            # 적용 DXF는 더 이상 최신 배경의 기준으로 쓰지 않으므로 캐시 서명도 CAD 경로를 사용한다.
            signature_source = self.source_path
            cached = None if self.refresh_source else load_background_source_cache(
                root, self.source_path, signature_source_path=signature_source
            )
            if cached:
                _emit_progress(self.progress, self.source_path, "진행사항 90% / 100% · 저장된 CAD 배경 원본 캐시 불러오는 중...")
                background = cached['segments']
                divisions = cached['divisions']
                texts = cached['texts']
                annotations = cached.get('annotation_segments', [])
                fills = cached.get('fills', [])
            else:
                _emit_progress(self.progress, self.source_path, "진행사항 5% / 100% · 연결된 CAD 배경 읽기 준비 중... (다른 작업 가능)")
                from app.cad.bridge import CadBridge, CadConnectionError
                bridge = CadBridge()
                info = bridge.connect_to_active(require_foreground=False)
                try:
                    expected = str(Path(self.source_path).resolve()).casefold()
                    actual = str(Path(info.document_path).resolve()).casefold()
                except Exception:
                    expected = str(self.source_path).casefold()
                    actual = str(info.document_path).casefold()
                if expected and actual and expected != actual:
                    raise CadConnectionError("현재 활성 CAD 도면이 프로그램에 연결된 도면과 다릅니다.\n배경 재생성 전에 연결된 도면을 CAD에서 활성화하십시오.")

                divisions = list(self.divisions_override or [])
                background = []
                texts = []
                annotations = []
                fills = []
                main_stats = {}
                xref_stats = {}
                cad_supplement_stats = {"targets": [], "imported": {}, "failed": [], "before_empty": 0, "after_filled": 0, "retargeted_inserts": 0}
                cad_supplement_aliases = {}

                # V24.05.1: 벡터 모드는 본도면을 COM으로 객체 하나씩 복사하지 않는다.
                # 적용 DXF가 정상이라면 본도면은 ezdxf로 직접 읽고, CAD COM은 XREF INSERT/정의만 읽는다.
                applied = Path(self.applied_dxf_path) if self.applied_dxf_path else None
                use_fast_vector = self.render_mode == "vector"
                main_source_label = "연결 CAD 직접읽기"
                live_snapshot_stats = {}
                if use_fast_vector:
                    # V0.24.8: 적용 DXF가 아니라 현재 활성 CAD의 최신 ModelSpace를
                    # 원본 수정 없이 임시 DXF로 고속 복제해서 읽는다. CAD에서 방금 추가/수정한
                    # 도곽과 객체도 CAD 도서읽기 즉시 반영된다.
                    try:
                        _emit_progress(self.progress, self.source_path, "진행사항 10% / 100% · 현재 CAD 최신 사본 생성 중...")
                        main_doc, live_snapshot_stats = bridge.build_active_modelspace_snapshot_fast(
                            progress_callback=lambda msg: _console_progress("CAD 최신사본: " + msg, self.source_path),
                        )
                        main_source_label = "연결 CAD 최신 임시 DXF 사본"
                        _emit_progress(self.progress, self.source_path, "진행사항 12% / 100% · 현재 CAD 최신 사본에서 본도면 벡터 읽는 중...")
                    except Exception as live_exc:
                        # V0.24.11: CAD 도서읽기는 반드시 '현재 활성 CAD'를 기준으로 한다.
                        # 최신 사본 생성이 실패했는데 과거 적용 DXF로 몰래 폴백하면 CAD에서 방금
                        # 추가한 층/도곽은 목록만 생기고 배경은 비는 등 잘못된 결과를 만든다.
                        # 따라서 구 DXF 폴백은 금지하고 원인을 그대로 사용자에게 알린다.
                        _console_progress(
                            f"현재 CAD 최신 사본 생성 실패 · 구 DXF 폴백 중단: {type(live_exc).__name__}: {live_exc}",
                            self.source_path,
                        )
                        raise RuntimeError(
                            f"현재 CAD 최신 사본을 만들지 못했습니다. 구 DXF는 사용하지 않습니다. "
                            f"{type(live_exc).__name__}: {live_exc}"
                        ) from live_exc
                    raw_divisions = read_drawing_divisions(main_doc.modelspace(), self.room_rules)
                    divisions = _merge_drawing_divisions(raw_divisions, divisions)
                    # V24.07.2: 배경 원본 수집 범위는 실이 있는 층이 아니라 모든 평면도 도곽이다.
                    # 층명이 빠진 도곽도 원본 geometry 수집에서 제외하지 않는다.
                    frame_rects = plan_frame_rects(divisions)
                    floor_rect_map = floor_frame_rects(divisions, self.rooms)
                    _console_progress(
                        f"평면 도곽 범위 확인: 도곽 {len(frame_rects):,}개 · 층매핑 {len(floor_rect_map):,}개 "
                        f"({', '.join(sorted(floor_rect_map)) if floor_rect_map else '없음'})",
                        self.source_path,
                    )
                    if not frame_rects:
                        raise RuntimeError("평면 도곽을 찾지 못해 CAD 벡터 배경 생성을 중단했습니다. 도곽 밖 전체 도면은 읽지 않습니다.")
                    # V24.41: 적용 DXF에 XCLIP INSERT는 있으나 블록 정의가 빈 경우
                    # 연결 CAD의 실제 블록 정의를 프로그램 내부 문서에 보충한 뒤 벡터를 전개한다.
                    cad_supplement_aliases, cad_supplement_stats = _supplement_empty_xclip_blocks_from_active_cad(
                        main_doc, bridge, self.cad_values, self.room_rules,
                        progress_callback=lambda msg: _console_progress(msg, self.source_path),
                    )
                    background, main_stats = read_cad_background(
                        main_doc, self.exclude_patterns, self.business_layers, crop_rects=frame_rects,
                        block_name_aliases=cad_supplement_aliases,
                    )
                    texts = list(main_stats.get('texts', []) or [])
                    annotations = list(main_stats.get('annotation_segments', []) or [])
                    fills = list(main_stats.get('fills', []) or [])
                    # 아래 XREF 병합 단계에서 main_doc의 ModelSpace를 정리하므로,
                    # 창호 후보는 원본 최신 사본이 온전할 때 먼저 계산한다.
                    opening_candidates = build_opening_candidates_by_floor(main_doc, divisions, self.rooms, (4, 5))
                    _emit_progress(self.progress, self.source_path, f"진행사항 45% / 100% · 본도면 벡터 완료 · 선분 {len(background):,}개")

                    values = dict(self.cad_values)
                    values['import_background'] = True
                    values['import_selected_only'] = False
                    values['xref_only_background'] = True

                    # V24.05.2: 적용 DXF에는 XREF 실제 도형은 없어도 INSERT별 XCLIP
                    # SPATIAL_FILTER 정보는 남아 있다. 핸들 기준으로 XCLIP 경계를 읽어
                    # 연결 CAD에서 가져온 XREF INSERT에 그대로 적용한다.
                    xref_xclip_by_handle = {}
                    try:
                        from ezdxf.xclip import XClip
                        for insert in main_doc.modelspace().query('INSERT'):
                            try:
                                handle = str(getattr(insert.dxf, 'handle', '') or '').strip().upper()
                                if not handle:
                                    continue
                                clip = XClip(insert)
                                if not clip.has_clipping_path or not clip.is_clipping_enabled:
                                    continue
                                path = clip.get_wcs_clipping_path()
                                vertices = [(float(v.x), float(v.y)) for v in (path.vertices or [])]
                                if len(vertices) < 3:
                                    continue
                                xref_xclip_by_handle[handle] = {
                                    'vertices': vertices,
                                    'inverted': bool(path.is_inverted_clip),
                                }
                            except Exception:
                                continue
                    except Exception:
                        xref_xclip_by_handle = {}
                    values['xref_xclip_by_handle'] = xref_xclip_by_handle
                    _console_progress(
                        f"XREF XCLIP 경계 확인: {len(xref_xclip_by_handle):,}개 INSERT",
                        self.source_path,
                    )
                    # V24.07: XREF DWG는 CAD에서 DXF 사본으로 1회 변환하고,
                    # 실제 XREF 벡터는 그 사본을 ezdxf로 직접 읽는다.
                    # 이후 CAD COM의 XREF 내부 객체 순회 경로는 사용하지 않는다.
                    xref_saveas_results = []
                    xref_direct_stats = {
                        "definitions": 0,
                        "source_files": 0,
                        "converted": 0,
                        "cache_reused": 0,
                        "insert_count": 0,
                        "xclip_insert_count": len(xref_xclip_by_handle),
                        "errors": [],
                    }
                    try:
                        import hashlib
                        from ezdxf.addons import Importer

                        xref_items = bridge.list_xref_files()
                        xref_direct_stats["definitions"] = len(xref_items)
                        _console_progress(
                            f"XREF 원본 경로 확인: {len(xref_items):,}개 정의",
                            self.source_path,
                        )

                        # V0.24.8: XREF INSERT/XCLIP도 같은 최신 CAD 사본에서 가져온다.
                        # 일반 본도면 객체는 이미 위에서 처리했으므로 여기서는 모두 제거한다.
                        xref_host_doc = main_doc
                        xref_names = {
                            str(item.get("name") or "").strip().casefold()
                            for item in xref_items if str(item.get("name") or "").strip()
                        }
                        host_msp = xref_host_doc.modelspace()
                        kept_inserts = []
                        for ent in list(host_msp):
                            keep = False
                            if ent.dxftype() == "INSERT":
                                try:
                                    keep = str(getattr(ent.dxf, "name", "") or "").strip().casefold() in xref_names
                                except Exception:
                                    keep = False
                            if keep:
                                kept_inserts.append(ent)
                            else:
                                try:
                                    host_msp.delete_entity(ent)
                                except Exception:
                                    try:
                                        ent.destroy()
                                    except Exception:
                                        pass
                        xref_direct_stats["insert_count"] = len(kept_inserts)
                        _console_progress(
                            f"XREF INSERT 보존: {len(kept_inserts):,}개 · 일반 본도면 객체 제거 완료",
                            self.source_path,
                        )

                        cache_dir = Path(self.cache_root) / "_xref_dxf_cache"
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        source_doc_by_name = {}
                        loaded_by_path = {}

                        for item_index, item in enumerate(xref_items, 1):
                            name = str(item.get("name") or "").strip()
                            source_xref = Path(str(item.get("path") or "").strip())
                            if not name or not source_xref.is_file():
                                msg = f"XREF 원본 파일 없음: {name or '(이름없음)'} · {source_xref}"
                                xref_direct_stats["errors"].append(msg)
                                _console_progress(msg, self.source_path)
                                continue

                            source_key = str(source_xref.resolve()).casefold()
                            source_doc = loaded_by_path.get(source_key)
                            actual_read_path = source_xref
                            if source_doc is None:
                                if source_xref.suffix.lower() == ".dwg":
                                    stat = source_xref.stat()
                                    digest = hashlib.sha1(source_key.encode("utf-8", "ignore")).hexdigest()[:10]
                                    safe_stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in source_xref.stem)[:70] or f"xref_{item_index}"
                                    actual_read_path = cache_dir / f"{safe_stem}__{digest}__{stat.st_size}_{stat.st_mtime_ns}.dxf"
                                    if actual_read_path.is_file() and actual_read_path.stat().st_size > 0:
                                        try:
                                            source_doc = ezdxf.readfile(str(actual_read_path))
                                            xref_direct_stats["cache_reused"] += 1
                                            _console_progress(
                                                f"XREF DXF 사본 캐시 재사용: {source_xref.name} · {actual_read_path.name}",
                                                self.source_path,
                                            )
                                        except Exception:
                                            source_doc = None
                                    if source_doc is None:
                                        _console_progress(
                                            f"XREF DWG→DXF 사본 생성: {item_index}/{len(xref_items)} · {source_xref.name}",
                                            self.source_path,
                                        )
                                        result = bridge.save_xref_dwg_as_dxf_copy(source_xref, actual_read_path)
                                        source_doc = ezdxf.readfile(str(actual_read_path))
                                        result["entity_count"] = len(list(source_doc.modelspace()))
                                        result["block_count"] = len(list(source_doc.blocks))
                                        xref_saveas_results.append(result)
                                        xref_direct_stats["converted"] += 1
                                        _console_progress(
                                            f"XREF DXF 사본 생성 완료: {source_xref.name} · "
                                            f"{result.get('size', 0):,} bytes · ModelSpace {result['entity_count']:,}개 · 블록 {result['block_count']:,}개",
                                            self.source_path,
                                        )
                                elif source_xref.suffix.lower() == ".dxf":
                                    source_doc = ezdxf.readfile(str(source_xref))
                                    _console_progress(
                                        f"XREF DXF 원본 직접 읽기: {source_xref.name} · ModelSpace {len(list(source_doc.modelspace())):,}개",
                                        self.source_path,
                                    )
                                else:
                                    msg = f"지원하지 않는 XREF 형식: {source_xref.name}"
                                    xref_direct_stats["errors"].append(msg)
                                    _console_progress(msg, self.source_path)
                                    continue
                                loaded_by_path[source_key] = source_doc
                                xref_direct_stats["source_files"] += 1

                            source_doc_by_name[name.casefold()] = (source_doc, actual_read_path)

                        # 각 XREF 정의를 로컬 블록으로 치환한다. INSERT 자체는 그대로 두므로
                        # 위치/회전/축척/XCLIP(extension dictionary)이 유지된다.
                        local_block_by_name = {}
                        for name_key, (source_doc, actual_read_path) in source_doc_by_name.items():
                            original_name = next((str(item.get("name") or "").strip() for item in xref_items
                                                  if str(item.get("name") or "").strip().casefold() == name_key), name_key)
                            safe_name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in original_name)[:80] or "XREF"
                            local_name = f"__XREFVEC_{safe_name}"
                            suffix = 1
                            while local_name in xref_host_doc.blocks:
                                local_name = f"__XREFVEC_{safe_name}_{suffix}"
                                suffix += 1

                            # 원래 XREF 블록의 기준점을 보존한다.
                            base_point = (0.0, 0.0, 0.0)
                            try:
                                original_block = xref_host_doc.blocks.get(original_name)
                                bp = original_block.block.dxf.base_point
                                base_point = (float(bp.x), float(bp.y), float(getattr(bp, 'z', 0.0)))
                            except Exception:
                                try:
                                    bp = source_doc.header.get('$INSBASE', (0.0, 0.0, 0.0))
                                    base_point = (float(bp.x), float(bp.y), float(getattr(bp, 'z', 0.0)))
                                except Exception:
                                    pass

                            target_block = xref_host_doc.blocks.new(name=local_name, base_point=base_point)
                            _console_progress(
                                f"XREF 사본 벡터 블록 구성 시작: {original_name} · {Path(actual_read_path).name}",
                                self.source_path,
                            )
                            importer = Importer(source_doc, xref_host_doc)
                            importer.import_modelspace(target_layout=target_block)
                            importer.finalize()
                            local_block_by_name[name_key] = local_name
                            _console_progress(
                                f"XREF 사본 벡터 블록 구성 완료: {original_name} · 내부 객체 {len(target_block):,}개",
                                self.source_path,
                            )

                        replaced = 0
                        for insert in kept_inserts:
                            try:
                                old_name = str(getattr(insert.dxf, "name", "") or "").strip().casefold()
                                local_name = local_block_by_name.get(old_name)
                                if not local_name:
                                    continue
                                insert.dxf.name = local_name
                                replaced += 1
                            except Exception as exc:
                                xref_direct_stats["errors"].append(f"INSERT 치환 실패: {type(exc).__name__}: {exc}")
                        _console_progress(
                            f"XREF INSERT 로컬블록 치환 완료: {replaced:,}/{len(kept_inserts):,}개",
                            self.source_path,
                        )

                        _emit_progress(self.progress, self.source_path, "진행사항 55% / 100% · XREF DXF 사본에서 벡터 직접 읽는 중...")
                        block_aliases = {}
                        for original_key, local_name in local_block_by_name.items():
                            original_name = next((str(item.get("name") or "").strip() for item in xref_items
                                                  if str(item.get("name") or "").strip().casefold() == original_key), original_key)
                            block_aliases[str(local_name).casefold()] = original_name
                        xref_background, xref_stats = read_cad_background(
                            xref_host_doc, self.exclude_patterns, self.business_layers,
                            crop_rects=frame_rects, xclip_by_handle=xref_xclip_by_handle,
                            block_name_aliases=block_aliases,
                            xref_block_names=[str(item.get("name") or "").strip() for item in xref_items]
                        )
                        background.extend(xref_background)
                        texts.extend(list(xref_stats.get('texts', []) or []))
                        annotations.extend(list(xref_stats.get('annotation_segments', []) or []))
                        fills.extend(list(xref_stats.get('fills', []) or []))
                        _emit_progress(
                            self.progress, self.source_path,
                            f"진행사항 78% / 100% · XREF 사본 벡터 병합 완료 · 추가 선분 {len(xref_background):,}개"
                        )
                    except Exception as exc:
                        xref_direct_stats["errors"].append(f"{type(exc).__name__}: {exc}")
                        _console_progress(
                            f"XREF DXF 사본 직접읽기 실패: {type(exc).__name__}: {exc}",
                            self.source_path,
                        )
                        raise

                else:
                    # PNG 또는 적용 DXF가 없는 경우 기존 CAD 직접 읽기 경로를 유지한다.
                    values = dict(self.cad_values)
                    values['import_background'] = True
                    values['import_selected_only'] = False
                    _emit_progress(self.progress, self.source_path, "진행사항 15% / 100% · CAD 객체를 벡터 데이터로 변환 중...")
                    doc, _counts, _skipped = bridge.build_ezdxf_document(
                        values, self.room_rules,
                        progress_callback=lambda msg: _console_progress(msg, self.source_path),
                    )
                    _emit_progress(self.progress, self.source_path, "진행사항 70% / 100% · 도곽별 벡터 정리 중...")
                    if not divisions:
                        divisions = list(bridge.last_selected_divisions or [])
                    if not divisions:
                        divisions = read_drawing_divisions(doc.modelspace(), self.room_rules)
                    frame_rects = plan_frame_rects(divisions)
                    if not frame_rects:
                        raise RuntimeError("평면 도곽을 찾지 못해 CAD 벡터/PNG 배경 생성을 중단했습니다. 도곽 밖 전체 도면은 읽지 않습니다.")
                    background, main_stats = read_cad_background(
                        doc, self.exclude_patterns, self.business_layers, bridge.last_background_color_overrides,
                        crop_rects=frame_rects
                    )
                    texts = list(main_stats.get('texts', []) or [])
                    annotations = list(main_stats.get('annotation_segments', []) or [])
                    fills = list(main_stats.get('fills', []) or [])
                    opening_candidates = build_opening_candidates_by_floor(doc, divisions, self.rooms, (4, 5))

                cad_diag = dict(getattr(bridge, "last_import_diagnostics", {}) or {})
                cad_blocks = dict(cad_diag.get("cad_block_stats") or {})
                lines = [
                    "CAD 연동 블록/XREF 벡터 진단", "",
                    f"연결 CAD: {self.source_path}",
                    f"본도면 벡터 원본: {main_source_label if use_fast_vector else '연결 CAD 직접읽기'}",
                    f"본도면 일반 선분: {len(main_stats.get('background_segments', []) or []) if main_stats.get('background_segments') is not None else '직접집계'}",
                    f"빈 XCLIP 블록 CAD 보충 대상: {cad_supplement_stats.get('targets', []) if use_fast_vector else '해당없음'}",
                    f"빈 XCLIP 블록 CAD 보충 성공: {cad_supplement_stats.get('after_filled', 0) if use_fast_vector else '해당없음'} / {cad_supplement_stats.get('before_empty', 0) if use_fast_vector else '해당없음'}",
                    f"빈 XCLIP 블록 INSERT 재연결: {cad_supplement_stats.get('retargeted_inserts', 0) if use_fast_vector else '해당없음'}",
                    f"빈 XCLIP 블록 CAD 보충 상세: {cad_supplement_stats.get('imported', {}) if use_fast_vector else '해당없음'}",
                    f"빈 XCLIP 블록 CAD 보충 오류: {cad_supplement_stats.get('failed', []) if use_fast_vector else '해당없음'}",
                    f"블록 정의 읽기: {xref_direct_stats.get('definitions', 0) if use_fast_vector else cad_blocks.get('definitions', 0)}",
                    f"블록 내부 객체 복사: {'DXF 사본 직접읽기' if use_fast_vector else cad_blocks.get('entities', 0)}",
                    f"XREF 정의 발견: {xref_direct_stats.get('definitions', 0) if use_fast_vector else cad_blocks.get('xrefs', 0)}",
                    f"XREF 내용 읽기 성공: {xref_direct_stats.get('source_files', 0) if use_fast_vector else cad_blocks.get('xref_loaded', 0)}",
                    f"XREF 빈 정의: {'해당없음' if use_fast_vector else cad_blocks.get('xref_empty', 0)}",
                    f"XREF INSERT 대상: {xref_direct_stats.get('insert_count', 0) if use_fast_vector else '전체경로'}",
                    f"XCLIP 경계 발견: {len(xref_xclip_by_handle) if use_fast_vector else '직접경로'}",
                    f"XCLIP 적용 INSERT: {xref_direct_stats.get('xclip_insert_count', 0) if use_fast_vector else cad_blocks.get('xclip_applied', 0)}",
                    f"XCLIP 없는 XREF INSERT: {max(0, xref_direct_stats.get('insert_count', 0)-xref_direct_stats.get('xclip_insert_count', 0)) if use_fast_vector else cad_blocks.get('xclip_missing', 0)}",
                    f"XCLIP 실제 전개 감지: {xref_stats.get('block_diagnostic', {}).get('xclip_insert_count', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('xclip_insert_count', 0)}",
                    f"XCLIP 원본 WCS 강제적용: {xref_stats.get('block_diagnostic', {}).get('xclip_forced_count', 0) if use_fast_vector else 0}",
                    f"XCLIP 선분 입력/분할출력/전부제외: {xref_stats.get('block_diagnostic', {}).get('xclip_segment_input', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('xclip_segment_input', 0)} / {xref_stats.get('block_diagnostic', {}).get('xclip_segment_parts', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('xclip_segment_parts', 0)} / {xref_stats.get('block_diagnostic', {}).get('xclip_segment_rejected', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('xclip_segment_rejected', 0)}",
                    f"도곽클립 선분 입력/출력/전부제외: {xref_stats.get('block_diagnostic', {}).get('crop_segment_input', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('crop_segment_input', 0)} / {xref_stats.get('block_diagnostic', {}).get('crop_segment_output', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('crop_segment_output', 0)} / {xref_stats.get('block_diagnostic', {}).get('crop_segment_rejected', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('crop_segment_rejected', 0)}",
                    f"채움 클립 입력/출력/오류: {xref_stats.get('block_diagnostic', {}).get('fill_clip_input', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('fill_clip_input', 0)} / {xref_stats.get('block_diagnostic', {}).get('fill_clip_output', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('fill_clip_output', 0)} / {xref_stats.get('block_diagnostic', {}).get('fill_clip_errors', 0) if use_fast_vector else main_stats.get('block_diagnostic', {}).get('fill_clip_errors', 0)}",
                    f"XREF DWG→DXF 사본 사용: {xref_saveas_results if use_fast_vector else '미실행'}",
                    f"XREF DXF 캐시 재사용: {xref_direct_stats.get('cache_reused', 0) if use_fast_vector else '미실행'}",
                    f"XREF 직접읽기 오류: {xref_direct_stats.get('errors', []) if use_fast_vector else '미실행'}",
                    f"SOLID/해치 채움 영역: {len(fills)}",
                    f"본도면 숨김 객체 제외: {dict(main_stats.get('block_diagnostic') or {}).get('invisible_entity_skipped', 0)}",
                    f"본도면 숨김 객체 예시: {dict(main_stats.get('block_diagnostic') or {}).get('invisible_entity_examples', [])}",
                    f"오류: {cad_blocks.get('errors', [])}",
                ]
                # V24.39: XREF 최상위 INSERT별로 원본 전개 -> XCLIP -> 도곽클립 -> 최종 출력 개수를 남긴다.
                diag_block = dict((xref_stats if use_fast_vector else main_stats).get('block_diagnostic') or {})
                diag_details = [d for d in list(diag_block.get('details') or []) if int(d.get('depth', -1)) == 0]
                if diag_details:
                    lines += ["", "[XREF/최상위 INSERT 단계별 진단]"]
                    for idx, d in enumerate(diag_details, 1):
                        lines.append(
                            f"#{idx} handle={d.get('handle','')} name={d.get('name','')} layer={d.get('layer','')} "
                            f"virtual={d.get('virtual_count',0)} def={d.get('block_def_count',0)} "
                            f"xclip={'Y' if d.get('xclip_applied') else 'N'}({d.get('xclip_vertices',0)}점, inverted={bool(d.get('xclip_inverted'))}) "
                            f"xclip선분={d.get('xclip_segment_input',0)}->{d.get('xclip_segment_parts',0)} "
                            f"(전부제외 {d.get('xclip_segment_rejected',0)}) "
                            f"도곽={d.get('crop_segment_input',0)}->{d.get('crop_segment_output',0)} "
                            f"(전부제외 {d.get('crop_segment_rejected',0)}) "
                            f"최종선분={d.get('segments_added',0)} 문자={d.get('texts_added',0)} 채움={d.get('fills_added',0)}"
                        )
                else:
                    lines += ["", "[XREF/최상위 INSERT 단계별 진단]", "최상위 INSERT 상세 없음"]

                # V24.40: 적용 DXF 본도면의 '일반 블록 + XCLIP'을 별도로 추적한다.
                # XREF 진단과 섞지 않고, 실제로 XCLIP을 소유한 INSERT마다 정의/전개/클립/최종 출력 수를 남긴다.
                main_diag = dict(main_stats.get('block_diagnostic') or {})
                main_details = list(main_diag.get('details') or [])
                own_xclip_details = [d for d in main_details if bool(d.get('xclip_own'))]
                lines += [
                    "",
                    "[일반 블록 XCLIP 단계별 진단]",
                    f"본도면 INSERT 전개 감지: {main_diag.get('insert_count', 0)}",
                    f"본도면 자체 XCLIP INSERT 감지: {len(own_xclip_details)}",
                    f"본도면 XCLIP 선분 합계: {main_diag.get('xclip_segment_input', 0)} -> {main_diag.get('xclip_segment_parts', 0)} (전부제외 {main_diag.get('xclip_segment_rejected', 0)})",
                ]
                if own_xclip_details:
                    for idx, d in enumerate(own_xclip_details, 1):
                        bounds = d.get('xclip_bounds')
                        if isinstance(bounds, (tuple, list)) and len(bounds) >= 4:
                            bounds_text = f"({float(bounds[0]):.3f},{float(bounds[1]):.3f})~({float(bounds[2]):.3f},{float(bounds[3]):.3f})"
                        else:
                            bounds_text = "없음"
                        insert_xy = d.get('insert')
                        if isinstance(insert_xy, (tuple, list)) and len(insert_xy) >= 2:
                            insert_text = f"({float(insert_xy[0]):.3f},{float(insert_xy[1]):.3f})"
                        else:
                            insert_text = "없음"
                        lines.append(
                            f"#{idx} depth={d.get('depth',0)} handle={d.get('handle','')} name={d.get('name','')} layer={d.get('layer','')} "
                            f"insert={insert_text} rot={float(d.get('rotation',0) or 0):.3f} "
                            f"scale=({float(d.get('xscale',1) or 1):.6g},{float(d.get('yscale',1) or 1):.6g}) "
                            f"정의객체={d.get('block_def_count',0)} 1차전개={d.get('virtual_count',0)} "
                            f"xclip={d.get('xclip_source','')} {d.get('xclip_vertices',0)}점 inverted={bool(d.get('xclip_inverted'))} "
                            f"경계={bounds_text} "
                            f"xclip선분={d.get('xclip_segment_input',0)}->{d.get('xclip_segment_parts',0)} "
                            f"(전부제외 {d.get('xclip_segment_rejected',0)}) "
                            f"도곽={d.get('crop_segment_input',0)}->{d.get('crop_segment_output',0)} "
                            f"(전부제외 {d.get('crop_segment_rejected',0)}) "
                            f"최종선분={d.get('segments_added',0)} 문자={d.get('texts_added',0)} 채움={d.get('fills_added',0)}"
                        )
                else:
                    lines.append("자체 XCLIP을 가진 일반 INSERT 상세 없음")

                self.block_diagnostic_text = "\n".join(lines)
                try:
                    diag_path = Path(self.cache_root) / "CAD_XREF_단계진단.txt"
                    diag_path.parent.mkdir(parents=True, exist_ok=True)
                    diag_path.write_text(self.block_diagnostic_text, encoding="utf-8")
                    self.block_diagnostic_path = str(diag_path)
                    # 일반 블록 XCLIP 부분만 따로 복사해 전달하기 쉽게 저장한다.
                    xclip_lines = []
                    marker = "[일반 블록 XCLIP 단계별 진단]"
                    try:
                        start = lines.index(marker)
                        xclip_lines = lines[start:]
                    except ValueError:
                        xclip_lines = [marker, "진단 데이터 없음"]
                    xclip_diag_path = Path(self.cache_root) / "CAD_XCLIP_일반블록_단계진단.txt"
                    xclip_diag_path.write_text("\n".join(xclip_lines), encoding="utf-8")
                except Exception:
                    self.block_diagnostic_path = ""
                mapped_floors = floor_frame_rects(divisions, self.rooms)
                _console_progress(
                    f"층별 벡터 캐시 생성 대상: {len(mapped_floors):,}개 층 · "
                    f"{', '.join(sorted(mapped_floors)) if mapped_floors else '없음'}",
                    self.source_path,
                )
                save_background_source_cache(
                    root, self.source_path, background, divisions, texts, annotations,
                    rooms=self.rooms, opening_candidates=opening_candidates, fills=fills,
                    signature_source_path=signature_source
                )

            if self.render_mode == "vector":
                # 벡터 표시 모드에서는 QImage/QPainter 기반 PNG 렌더링을 백그라운드 스레드에서 하지 않는다.
                # CAD 선/폴리라인/문자/색상/Handle 데이터 캐시만 만든 뒤 GUI 스레드가 직접 표시한다.
                _emit_progress(self.progress, self.source_path, "진행사항 100% / 100% · CAD 벡터 캐시 생성 완료 · 화면 표시 준비 중...")
            else:
                _emit_progress(self.progress, self.source_path, "진행사항 90% / 100% · CAD 배경 PNG 생성 중... (다른 작업 가능)")
                build_background_cache(
                    self.source_path, self.rooms, background, root,
                    self.background_max_pixels, self.background_style,
                    self.background_filter_signature, divisions,
                    self.background_line_width, self.background_aci_widths,
                    self.background_keep_red, texts,
                    preserve_existing=self.preserve_existing,
                    background_annotations=annotations,
                )
            _console_progress("작업 완료", self.source_path)
            _console_gap(5)
            self.finished_ok.emit(self.source_path)
        except Exception:
            detail = traceback.format_exc()
            _console_progress("작업 실패\n" + detail, self.source_path)
            self.failed.emit(self.source_path, detail)
        finally:
            try:
                if bridge is not None:
                    bridge.disconnect()
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
