from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.cad.bridge import CadBridge


@dataclass(frozen=True)
class BlockImportResult:
    success: bool
    block_name: str
    found_tags: tuple[str, ...]
    missing_tags: tuple[str, ...]
    path: Path | None
    entity_count: int
    target_layer: str
    layer_changed: int
    layer_failed: int

    @classmethod
    def from_report(cls, report: dict[str, Any]) -> "BlockImportResult":
        raw_path = str(report.get("path", "") or "").strip()
        return cls(
            success=bool(report.get("success")),
            block_name=str(report.get("block_name", "") or ""),
            found_tags=tuple(str(x) for x in report.get("found_tags", ())),
            missing_tags=tuple(str(x) for x in report.get("missing_tags", ())),
            path=Path(raw_path) if raw_path else None,
            entity_count=int(report.get("entity_count", 0) or 0),
            target_layer=str(report.get("target_layer", "") or ""),
            layer_changed=int(report.get("layer_changed", 0) or 0),
            layer_failed=int(report.get("layer_failed", 0) or 0),
        )


class BlockService:
    """실명 블록 검사·라이브러리 저장을 담당한다.

    UI는 이 서비스를 호출하고 결과 표시만 담당한다. CAD COM 세부 구현은
    CadBridge 내부에 남겨 두어 단계적 구조개편 중에도 기존 동작을 유지한다.
    """

    def __init__(self, cad_bridge: CadBridge, library_dir: Path):
        self.cad_bridge = cad_bridge
        self.library_dir = Path(library_dir)

    def import_room_tag_block(self, room_rules: dict) -> BlockImportResult:
        report = self.cad_bridge.import_configured_blocks(room_rules, self.library_dir)
        return BlockImportResult.from_report(report)

    def list_importable_blocks(self, room_rules: dict) -> list[dict[str, Any]]:
        return self.cad_bridge.list_configured_importable_blocks(room_rules, self.library_dir)

    def list_library_blocks(self) -> list[dict[str, Any]]:
        return self.cad_bridge.list_library_blocks(self.library_dir)

    def apply_library_blocks(self, block_names: list[str], overwrite: bool = False) -> dict[str, Any]:
        return self.cad_bridge.apply_library_blocks_to_cad(block_names, self.library_dir, overwrite)

    def import_blocks(self, block_names: list[str], room_rules: dict) -> list[BlockImportResult]:
        results: list[BlockImportResult] = []
        target_layer = str(room_rules.get("tag_layer", "0") or "0").strip() or "0"
        for block_name in block_names:
            report = self.cad_bridge.import_block_to_library(block_name, self.library_dir, target_layer)
            results.append(BlockImportResult.from_report(report))
        return results
