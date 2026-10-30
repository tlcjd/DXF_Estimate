from __future__ import annotations

import json

DEFAULT_WINDOW_CATEGORIES = ["금속창호", "AL창호", "PL창호", "문", "그릴"]


def normalize_window_categories(values=None) -> list[str]:
    if isinstance(values, str):
        text = values.strip()
        if not text:
            values = []
        else:
            try:
                parsed = json.loads(text)
                values = parsed if isinstance(parsed, list) else text.splitlines()
            except Exception:
                values = [part.strip() for part in text.replace(",", "\n").splitlines()]
    result: list[str] = []
    for raw in values or []:
        name = str(raw or "").strip()
        if name and name not in result:
            result.append(name)
    return result or list(DEFAULT_WINDOW_CATEGORIES)


def serialize_window_categories(values=None) -> str:
    return json.dumps(normalize_window_categories(values), ensure_ascii=False)
