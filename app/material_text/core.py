from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from app.common.models import ResultRow, TextRecord

THICKNESS_PATTERNS = [
    re.compile(r"(?i)\bTHK\s*[=:]?\s*(\d+(?:\.\d+)?)\s*(?:MM|㎜)?\b"),
    re.compile(r"(?i)\bT\s*[=:]?\s*(\d+(?:\.\d+)?)\s*(?:MM|㎜)?\b"),
    re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*T\b"),
    re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*(?:MM|㎜)\b"),
    re.compile(r"두께\s*[=:]?\s*(\d+(?:\.\d+)?)"),
]
MTEXT_CODES = [
    re.compile(r"\\[ACFHQTW][^;]*;"), re.compile(r"\\[LlOoKk]"),
    re.compile(r"\\S([^;]+);"), re.compile(r"[{}]"),
]

def clean_mtext(text: str) -> str:
    value = str(text or "").replace("\\P", " ").replace("\\~", " ")
    value = value.replace("\n", " ").replace("\r", " ")
    for pattern in MTEXT_CODES:
        value = pattern.sub(r"\1" if pattern.pattern.startswith(r"\\S") else "", value)
    return re.sub(r"\s+", " ", value).strip()

def normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", clean_mtext(text).replace("㎜", "mm")).strip()

def get_insert_xy(entity) -> tuple[float, float]:
    try:
        p = entity.dxf.insert
        return float(p.x), float(p.y)
    except Exception:
        return 0.0, 0.0

def iter_text_records(doc) -> Iterable[TextRecord]:
    layouts = [doc.modelspace()] + [x for x in doc.layouts if x.name.lower() != "model"]
    seen = set()
    for layout in layouts:
        for entity in layout:
            etype = entity.dxftype()
            if etype not in ("TEXT", "MTEXT", "INSERT"):
                continue
            entities = getattr(entity, "attribs", []) if etype == "INSERT" else [entity]
            for item in entities:
                item_type = "ATTRIB" if etype == "INSERT" else etype
                if item_type == "MTEXT":
                    try: raw = item.plain_text()
                    except Exception: raw = item.text
                else:
                    raw = item.dxf.text
                x, y = get_insert_xy(item)
                rec = TextRecord(raw, normalize_key(raw), str(item.dxf.layer), item_type,
                                 str(item.dxf.handle or ""), x, y)
                key = (rec.handle, rec.entity_type, rec.raw)
                if key not in seen:
                    seen.add(key); yield rec

def extract_thickness(text: str) -> list[str]:
    values=[]
    for pattern in THICKNESS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                num=float(match.group(1))
                if 0 < num <= 2000:
                    values.append(str(int(num)) if num.is_integer() else str(num))
            except (TypeError, ValueError):
                continue
    return list(dict.fromkeys(values))

class MaterialClassifier:
    def __init__(self, path: Path):
        data=json.loads(path.read_text(encoding="utf-8"))
        self.exclude_keywords=[x.casefold() for x in data.get("exclude_keywords", [])]
        aliases=[]
        for item in data.get("materials", []):
            standard=item["standard"]
            for alias in item.get("aliases", []):
                raw=alias.casefold(); compact=re.sub(r"[\s·ㆍ._,/()\-]+", "", raw)
                aliases.append((raw, compact, standard))
        aliases.sort(key=lambda x:max(len(x[0]),len(x[1])), reverse=True)
        self.aliases=aliases
    def find_materials(self,text):
        low=text.casefold(); compact=re.sub(r"[\s·ㆍ._,/()\-]+", "", low); found=[]
        for raw, comp, standard in self.aliases:
            if ((raw and raw in low) or (comp and comp in compact)) and standard not in found: found.append(standard)
        if any(x in found for x in ["강화유리","복층유리","로이유리"]): found=[x for x in found if x!="유리"]
        if any(x in found for x in ["화강석","대리석","라임스톤","인조석"]): found=[x for x in found if x!="석재"]
        if any(x in found for x in ["포세린타일","세라믹타일","데코타일"]): found=[x for x in found if x!="타일"]
        if "보호몰탈" in found: found=[x for x in found if x!="시멘트 모르타르"]
        return found
    def classify(self,text):
        materials=self.find_materials(text); thicknesses=extract_thickness(text)
        if materials: return "재료 후보", materials, thicknesses
        if thicknesses: return "보류", materials, thicknesses
        return "제외", materials, thicknesses

def aggregate(records, classifier):
    grouped=defaultdict(list)
    for rec in records:
        if rec.normalized: grouped[rec.normalized].append(rec)
    result={"재료 후보":[],"보류":[],"제외":[]}
    for normalized,items in grouped.items():
        category,materials,thicknesses=classifier.classify(normalized)
        result[category].append(ResultRow(normalized,items[0].raw,", ".join(materials),", ".join(thicknesses),len(items),
            ", ".join(sorted({x.layer for x in items})),category,", ".join(sorted({x.entity_type for x in items})),
            ", ".join(x.handle for x in items[:20] if x.handle)))
    result["재료 후보"].sort(key=lambda r:(r.material.casefold(),r.normalized.casefold()))
    result["보류"].sort(key=lambda r:r.normalized.casefold()); result["제외"].sort(key=lambda r:r.normalized.casefold())
    return result
