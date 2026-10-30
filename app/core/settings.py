from __future__ import annotations

import configparser
from pathlib import Path


class AppSettings:
    def __init__(self, path: Path):
        self.path = path
        # 산출식에 백분율(%) 문자를 그대로 저장할 수 있도록 INI 보간을 사용하지 않는다.
        self.cfg = configparser.ConfigParser(interpolation=None)
        if path.exists():
            self.cfg.read(path, encoding="utf-8-sig")
        for section in ("Path", "Window", "Preview", "TabNames", "CAD"):
            if not self.cfg.has_section(section):
                self.cfg.add_section(section)

    def get(self, section: str, key: str, default: str = "") -> str:
        return self.cfg.get(section, key, fallback=default)

    def set(self, section: str, key: str, value) -> None:
        if not self.cfg.has_section(section):
            self.cfg.add_section(section)
        self.cfg.set(section, key, str(value))

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8-sig") as f:
            self.cfg.write(f)
