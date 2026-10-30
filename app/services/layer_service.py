from __future__ import annotations

from app.cad.bridge import CadBridge


class LayerService:
    """설정된 CAD 업무 레이어 관련 기능."""

    def __init__(self, cad_bridge: CadBridge):
        self.cad_bridge = cad_bridge

    def ensure_configured_layers(self, room_rules: dict) -> dict:
        return self.cad_bridge.ensure_configured_layers(room_rules)
