from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from threading import Lock
from typing import Any


class ColorDiagnosticLog:
    """CAD 배경 색상 경로를 단계별로 기록하는 진단 전용 로그.

    정상 실행 경로의 색상 계산에는 관여하지 않고, 전달받은 값을 텍스트로만 기록한다.
    대량 객체에서도 로그 손실을 줄이기 위해 파일을 열린 상태로 유지하고 주기적으로 flush한다.
    """

    def __init__(self, path: str | Path, source_path: str = "", mode: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")
        self._counts = Counter()
        self._fallback_counts = Counter()
        self._fallback_handles: dict[str, set[str]] = defaultdict(set)
        self._stage_rgb: dict[str, dict[str, tuple[int, int, int]]] = defaultdict(dict)
        self._core_override_missing: set[str] = set()
        self._temp_handle_mismatch: set[str] = set()
        self._png_color_transform_mismatch: set[str] = set()
        self._line_count = 0
        self._closed = False
        self._summary_written = False
        self._write_raw("CAD 배경 색상 단계별 진단 로그")
        self._write_raw("기준: V18.4 색상 처리 + V18.5 배경 OFF 업무레이어 직접읽기 최적화")
        self._write_raw(f"원본: {source_path}")
        self._write_raw(f"실행: {mode}")
        self._write_raw("단계: ZWCAD_SOURCE -> TEMP_DXF -> CORE_COLOR -> PNG_RENDER")
        self._write_raw("※ 이 진단판은 색상 계산을 고치지 않고 각 단계의 실제 값을 기록합니다.")
        self._write_raw("")

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "Y" if value else "N"
        if isinstance(value, (tuple, list)):
            return "(" + ",".join(str(x) for x in value) + ")"
        text = str(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", " ")
        return text

    def _write_raw(self, text: str) -> None:
        if self._closed:
            return
        self._fh.write(text + "\n")
        self._line_count += 1
        if self._line_count % 100 == 0:
            self._fh.flush()

    def log(self, stage: str, **fields: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self._counts[stage] += 1
            parts = [f"[{stage}]", f"N={self._counts[stage]}"]
            for key, value in fields.items():
                parts.append(f"{key}={self._fmt(value)}")
            self._write_raw(" | ".join(parts))

            handle = str(fields.get("handle") or fields.get("source_handle") or "").strip().upper()
            if handle:
                rgb_value = None
                if stage == "ZWCAD_SOURCE":
                    rgb_value = fields.get("display_rgb")
                elif stage == "CORE_COLOR":
                    rgb_value = fields.get("final_rgb")
                    if fields.get("override_found") is False:
                        self._core_override_missing.add(handle)
                elif stage == "PNG_RENDER":
                    rgb_value = fields.get("input_rgb")
                    try:
                        final_rgb = fields.get("final_rgb")
                        if str(fields.get("style") or "") == "cad" and isinstance(final_rgb, (tuple, list)):
                            if tuple(int(x) for x in rgb_value[:3]) != tuple(int(x) for x in final_rgb[:3]):
                                self._png_color_transform_mismatch.add(handle)
                    except Exception:
                        pass
                if isinstance(rgb_value, (tuple, list)) and len(rgb_value) >= 3:
                    try:
                        self._stage_rgb[stage].setdefault(handle, tuple(int(x) for x in rgb_value[:3]))
                    except Exception:
                        pass
                if stage == "TEMP_DXF" and fields.get("handle_match") is False:
                    self._temp_handle_mismatch.add(handle)

    def mark_fallback(self, stage: str, handle: str = "", reason: str = "") -> None:
        with self._lock:
            if self._closed:
                return
            self._fallback_counts[stage] += 1
            key = str(handle or "").strip().upper() or "(HANDLE없음)"
            self._fallback_handles[stage].add(key)
            self._write_raw(
                f"[FALLBACK_190] | STAGE={stage} | H={key} | REASON={self._fmt(reason)} | RGB=(190,190,190)"
            )

    def write_summary(self, **extra: Any) -> None:
        with self._lock:
            if self._closed or self._summary_written:
                return
            self._summary_written = True
            self._write_raw("")
            self._write_raw("[SUMMARY]")
            for stage in ("ZWCAD_SOURCE", "TEMP_DXF", "CORE_COLOR", "PNG_RENDER", "PNG_TEXT"):
                self._write_raw(f"{stage}: {self._counts.get(stage, 0)}건")
            total_fallback = sum(self._fallback_counts.values())
            self._write_raw(f"회색 fallback (190,190,190): 총 {total_fallback}건")
            for stage, count in sorted(self._fallback_counts.items()):
                handles = sorted(self._fallback_handles.get(stage, set()))
                self._write_raw(f"  - {stage}: {count}건 / Handle {len(handles)}개")
                if handles:
                    self._write_raw("    " + ", ".join(handles))

            if self._temp_handle_mismatch:
                handles = sorted(self._temp_handle_mismatch)
                self._write_raw(f"임시 DXF Handle 불일치: {len(handles)}개")
                self._write_raw("    " + ", ".join(handles))
            else:
                self._write_raw("임시 DXF Handle 불일치: 0개")

            if self._core_override_missing:
                handles = sorted(self._core_override_missing)
                self._write_raw(f"CORE color override 미발견: {len(handles)}개")
                self._write_raw("    " + ", ".join(handles))
            else:
                self._write_raw("CORE color override 미발견: 0개")

            source_rgb = self._stage_rgb.get("ZWCAD_SOURCE", {})
            core_rgb = self._stage_rgb.get("CORE_COLOR", {})
            png_rgb = self._stage_rgb.get("PNG_RENDER", {})
            source_core_mismatch = []
            core_png_mismatch = []
            for handle in sorted(set(source_rgb) & set(core_rgb)):
                if source_rgb[handle] != core_rgb[handle]:
                    source_core_mismatch.append((handle, source_rgb[handle], core_rgb[handle]))
            for handle in sorted(set(core_rgb) & set(png_rgb)):
                if core_rgb[handle] != png_rgb[handle]:
                    core_png_mismatch.append((handle, core_rgb[handle], png_rgb[handle]))
            self._write_raw(f"ZWCAD display RGB -> CORE 최종 RGB 불일치: {len(source_core_mismatch)}개")
            for handle, before, after in source_core_mismatch[:200]:
                self._write_raw(f"    H={handle} | ZWCAD={before} | CORE={after}")
            if len(source_core_mismatch) > 200:
                self._write_raw(f"    ... 나머지 {len(source_core_mismatch)-200}개 생략")
            self._write_raw(f"CORE 최종 RGB -> PNG 입력 RGB 불일치: {len(core_png_mismatch)}개")
            for handle, before, after in core_png_mismatch[:200]:
                self._write_raw(f"    H={handle} | CORE={before} | PNG={after}")
            if len(core_png_mismatch) > 200:
                self._write_raw(f"    ... 나머지 {len(core_png_mismatch)-200}개 생략")
            self._write_raw(f"CAD 원래색 모드에서 PNG QColor 변환 불일치: {len(self._png_color_transform_mismatch)}개")
            if self._png_color_transform_mismatch:
                self._write_raw("    " + ", ".join(sorted(self._png_color_transform_mismatch)))

            for key, value in extra.items():
                self._write_raw(f"{key}: {self._fmt(value)}")
            self._fh.flush()

    def close(self, **summary_extra: Any) -> None:
        with self._lock:
            if self._closed:
                return
        self.write_summary(**summary_extra)
        with self._lock:
            if not self._closed:
                self._fh.flush()
                self._fh.close()
                self._closed = True

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def fallback_total(self) -> int:
        return int(sum(self._fallback_counts.values()))
