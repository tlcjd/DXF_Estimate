from __future__ import annotations

import json
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from app.core.paths import DRAWINGS_DIR, PROJECTS_DIR, PROJECT_LIST_DIR


PROJECT_SCHEMA_VERSION = 4
PROJECT_METADATA_NAME = "project.json"
PROJECT_SUBDIRECTORIES = ("캐쉬", "내부실", "재료마감", "도면", "동명목록")
INVALID_PROJECT_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ProjectError(RuntimeError):
    pass


@dataclass
class ProjectRecord:
    project_id: str
    name: str
    overview: str = ""
    client: str = ""
    location: str = ""
    building_area_m2: float = 0.0
    gross_area_m2: float = 0.0
    height_m: float = 0.0
    depth_m: float = 0.0
    construction_months: int = 0
    construction_type: str = ""
    exterior_material: str = ""
    building_structure: str = ""
    insulation_type: str = ""
    created_date: str = ""
    base_drawing: str = ""
    base_drawing_source: str = ""
    cad_link_handle: str = ""
    cad_link_token: str = ""
    cad_link_document: str = ""
    cad_link_updated_at: str = ""
    building_groups: list[dict] = field(default_factory=list)
    overview_fields: dict[str, str] = field(default_factory=dict)
    path: Path = field(default=Path(), repr=False, compare=False)

    @property
    def base_drawing_path(self) -> Path | None:
        if not self.base_drawing or not self.path:
            return None
        candidate = (self.path / self.base_drawing).resolve()
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


class ProjectManager:
    def __init__(self, root: Path = PROJECTS_DIR, drawings_dir: Path = DRAWINGS_DIR, legacy_root: Path | None = None):
        self.root = Path(root)
        self.drawings_dir = Path(drawings_dir)
        self.legacy_root = Path(PROJECT_LIST_DIR if legacy_root is None else legacy_root)
        self.ensure_storage()

    def ensure_storage(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.drawings_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_project_folders()

    def _migrate_legacy_project_folders(self):
        """구형 프로젝트/프로젝트목록/<ID> 구조를 프로젝트/<프로젝트명>으로 옮긴다.

        프로젝트 ID는 project.json 내부 식별값으로만 유지하고 실제 사용자 폴더명에는
        프로젝트명을 사용한다. 충돌이 있으면 원본을 건드리지 않아 데이터 손실을 피한다.
        """
        legacy = self.legacy_root
        if not legacy.is_dir() or legacy.resolve() == self.root.resolve():
            return
        for old_path in list(legacy.iterdir()):
            if not old_path.is_dir() or old_path.name.startswith('.'):
                continue
            metadata = old_path / PROJECT_METADATA_NAME
            if not metadata.is_file():
                continue
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
                name = self.validate_name(data.get("name", ""))
            except Exception:
                continue
            destination = self.root / name
            if destination.exists():
                # 이미 같은 프로젝트가 새 위치에 있으면 구형 폴더는 남겨 두고 사용하지 않는다.
                try:
                    new_data = json.loads((destination / PROJECT_METADATA_NAME).read_text(encoding="utf-8"))
                    if str(new_data.get("project_id", "")).upper() == str(data.get("project_id", "")).upper():
                        shutil.rmtree(old_path)
                except Exception:
                    pass
                continue
            try:
                old_path.replace(destination)
            except OSError:
                try:
                    shutil.copytree(old_path, destination)
                    shutil.rmtree(old_path)
                except Exception:
                    if destination.exists():
                        shutil.rmtree(destination, ignore_errors=True)
        try:
            if legacy.is_dir() and not any(legacy.iterdir()):
                legacy.rmdir()
        except Exception:
            pass

    @staticmethod
    def normalize_name(name: str) -> str:
        return " ".join(unicodedata.normalize("NFC", str(name or "")).strip().split())

    @classmethod
    def validate_name(cls, name: str) -> str:
        value = cls.normalize_name(name)
        if not value:
            raise ProjectError("프로젝트 이름을 입력해 주세요.")
        if len(value) > 100:
            raise ProjectError("프로젝트 이름은 100자 이내로 입력해 주세요.")
        if INVALID_PROJECT_NAME.search(value) or value.endswith((".", " ")):
            raise ProjectError("프로젝트 이름에 사용할 수 없는 문자가 포함되어 있습니다.")
        if value.casefold() in {"임시프로젝트".casefold(), "프로젝트목록".casefold()}:
            raise ProjectError("프로그램에서 사용하는 예약 폴더명은 프로젝트 이름으로 사용할 수 없습니다.")
        return value

    @staticmethod
    def safe_library_name(name: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "").strip())
        return value.rstrip(". ") or "새 도면"

    @staticmethod
    def _nonnegative_float(value, default: float = 0.0) -> float:
        try:
            number = float(str(value if value is not None else "").replace(",", "").strip() or default)
        except (TypeError, ValueError):
            return float(default)
        return max(0.0, number)

    @staticmethod
    def _nonnegative_int(value, default: int = 0) -> int:
        try:
            number = int(float(str(value if value is not None else "").replace(",", "").strip() or default))
        except (TypeError, ValueError):
            return int(default)
        return max(0, number)

    @staticmethod
    def _overview_number(value, default: float = 0.0) -> float:
        text = str(value if value is not None else "").replace(",", "").strip()
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
        if not match:
            return float(default)
        try:
            return max(0.0, float(match.group(0)))
        except ValueError:
            return float(default)

    @classmethod
    def normalize_building_groups(cls, groups, strict: bool = False) -> list[dict]:
        """동명 데이터를 저장 가능한 공통 구조로 정규화한다.

        schema 1의 자리표시용 동명 데이터도 번호를 자동 부여해 계속 읽을 수 있게 한다.
        """
        result: list[dict] = []
        seen: set[str] = set()

        def next_code() -> str:
            number = 1
            while f"A{number:02d}".casefold() in seen:
                number += 1
            return f"A{number:02d}"

        for index, raw in enumerate(list(groups or [])):
            data = dict(raw or {})
            code = " ".join(str(data.get("code", data.get("number", "")) or "").strip().split())
            marker = code.casefold()
            if not code or marker in seen:
                if strict:
                    reason = "번호를 입력해 주세요." if not code else f"동 번호 '{code}'가 중복됩니다."
                    raise ProjectError(reason)
                code = next_code()
                marker = code.casefold()
            if len(code) > 30:
                raise ProjectError("동 번호는 30자 이내로 입력해 주세요.")
            seen.add(marker)

            name = " ".join(str(data.get("name", "") or "").strip().split())
            if len(name) > 100:
                raise ProjectError("동 이름은 100자 이내로 입력해 주세요.")

            basement = cls._nonnegative_int(data.get("basement", 0))
            ground = cls._nonnegative_int(data.get("ground", 0))
            roof = cls._nonnegative_int(data.get("roof", 0))
            area_m2 = cls._nonnegative_float(data.get("area_m2", 0.0))
            result.append({
                "code": code,
                "name": name,
                "basement": basement,
                "ground": ground,
                "roof": roof,
                "area_m2": round(area_m2, 6),
                "area_py": round(area_m2 / 3.305785, 6) if area_m2 else 0.0,
            })
        return result

    def _project_folder_for_name(self, name: str) -> Path:
        value = self.validate_name(name)
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ProjectError("프로젝트 저장 경로가 올바르지 않습니다.") from exc
        return candidate

    def _project_path(self, project_id: str) -> Path:
        marker = str(project_id or "").strip().upper()
        if not re.fullmatch(r"[0-9A-F]{12}", marker):
            raise ProjectError("올바르지 않은 프로젝트 ID입니다.")
        for path in self.root.iterdir():
            if not path.is_dir() or path.name.startswith(".") or path == self.legacy_root:
                continue
            metadata = path / PROJECT_METADATA_NAME
            if not metadata.is_file():
                continue
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("project_id", "")).strip().upper() == marker:
                return path
        # 마이그레이션 실패 시 구형 위치도 읽을 수 있게 마지막으로 확인한다.
        legacy = self.legacy_root / marker
        if legacy.is_dir() and (legacy / PROJECT_METADATA_NAME).is_file():
            return legacy
        return self.root / f".__missing_{marker}"

    def _metadata_path(self, project_id: str) -> Path:
        return self._project_path(project_id) / PROJECT_METADATA_NAME

    def _record_from_data(self, data: dict, path: Path) -> ProjectRecord:
        project_id = str(data.get("project_id", path.name) or path.name).upper()
        name = self.validate_name(data.get("name", ""))
        created = str(data.get("created_date", "") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
            created = date.today().isoformat()
        return ProjectRecord(
            project_id=project_id,
            name=name,
            overview=str(data.get("overview", "") or ""),
            client=str(data.get("client", "") or ""),
            location=str(data.get("location", "") or ""),
            building_area_m2=self._nonnegative_float(data.get("building_area_m2", 0.0)),
            gross_area_m2=self._nonnegative_float(data.get("gross_area_m2", 0.0)),
            height_m=self._nonnegative_float(data.get("height_m", 0.0)),
            depth_m=self._nonnegative_float(data.get("depth_m", 0.0)),
            construction_months=self._nonnegative_int(data.get("construction_months", 0)),
            construction_type=str(data.get("construction_type", "") or ""),
            exterior_material=str(data.get("exterior_material", "") or ""),
            building_structure=str(data.get("building_structure", "") or ""),
            insulation_type=str(data.get("insulation_type", "") or "") if str(data.get("insulation_type", "") or "") in {"", "내단열", "외단열", "복합"} else "",
            created_date=created,
            base_drawing=str(data.get("base_drawing", "") or ""),
            base_drawing_source=str(data.get("base_drawing_source", "") or ""),
            cad_link_handle=str(data.get("cad_link_handle", "") or "").strip().upper(),
            cad_link_token=str(data.get("cad_link_token", "") or "").strip(),
            cad_link_document=str(data.get("cad_link_document", "") or ""),
            cad_link_updated_at=str(data.get("cad_link_updated_at", "") or ""),
            building_groups=self.normalize_building_groups(data.get("building_groups"), strict=False),
            overview_fields={str(k): str(v or "") for k, v in dict(data.get("overview_fields") or {}).items()},
            path=path,
        )

    def _read_path(self, path: Path) -> ProjectRecord:
        metadata = path / PROJECT_METADATA_NAME
        data = json.loads(metadata.read_text(encoding="utf-8"))
        return self._record_from_data(data, path)

    def list_projects(self) -> list[ProjectRecord]:
        self.ensure_storage()
        records: list[ProjectRecord] = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.name.startswith(".") or path == self.legacy_root:
                continue
            if not (path / PROJECT_METADATA_NAME).is_file():
                continue
            try:
                records.append(self._read_path(path))
            except Exception:
                continue
        return sorted(records, key=lambda item: (item.name.casefold(), item.created_date, item.project_id))

    def get(self, project_id: str) -> ProjectRecord:
        path = self._project_path(project_id)
        if not path.is_dir():
            raise ProjectError("프로젝트를 찾을 수 없습니다.")
        try:
            return self._read_path(path)
        except Exception as exc:
            raise ProjectError("프로젝트 정보를 읽을 수 없습니다.") from exc

    def _ensure_unique_name(self, name: str, exclude_id: str = "") -> str:
        value = self.validate_name(name)
        marker = value.casefold()
        for record in self.list_projects():
            if record.project_id != exclude_id and record.name.casefold() == marker:
                raise ProjectError("같은 이름의 프로젝트가 이미 있습니다.")
        return value

    def _write(self, record: ProjectRecord):
        target = self._project_folder_for_name(record.name)
        current = Path(record.path) if record.path else None
        if current and current.is_dir() and current.resolve() != target.resolve():
            if target.exists():
                raise ProjectError("같은 이름의 프로젝트 폴더가 이미 있습니다.")
            try:
                current.replace(target)
            except OSError:
                shutil.copytree(current, target)
                shutil.rmtree(current)
        path = target
        path.mkdir(parents=True, exist_ok=True)
        for name in PROJECT_SUBDIRECTORIES:
            (path / name).mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": record.project_id,
            "name": record.name,
            "overview": record.overview,
            "client": record.client,
            "location": record.location,
            "building_area_m2": record.building_area_m2,
            "gross_area_m2": record.gross_area_m2,
            "height_m": record.height_m,
            "depth_m": record.depth_m,
            "construction_months": record.construction_months,
            "construction_type": record.construction_type,
            "exterior_material": record.exterior_material,
            "building_structure": record.building_structure,
            "insulation_type": record.insulation_type,
            "created_date": record.created_date,
            "base_drawing": record.base_drawing,
            "base_drawing_source": record.base_drawing_source,
            "cad_link_handle": record.cad_link_handle,
            "cad_link_token": record.cad_link_token,
            "cad_link_document": record.cad_link_document,
            "cad_link_updated_at": record.cad_link_updated_at,
            "building_groups": list(record.building_groups or []),
            "overview_fields": dict(record.overview_fields or {}),
        }
        temporary = path / f"{PROJECT_METADATA_NAME}.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path / PROJECT_METADATA_NAME)
        record.path = path

    def create(self, name: str) -> ProjectRecord:
        value = self._ensure_unique_name(name)
        project_id = uuid.uuid4().hex[:12].upper()
        record = ProjectRecord(project_id, value, created_date=date.today().isoformat(), path=self._project_folder_for_name(value))
        self._write(record)
        return record

    def update_overview(self, project_id: str, overview: str) -> ProjectRecord:
        record = self.get(project_id)
        record.overview = str(overview or "")
        self._write(record)
        return record

    def update_overview_fields(self, project_id: str, fields) -> ProjectRecord:
        record = self.get(project_id)
        source = {str(key): str(value or "") for key, value in dict(fields or {}).items()}

        # 개요 입력모드의 공사명은 프로젝트명과 같은 값으로 관리한다.
        # 빈 이름/중복 이름은 기존 프로젝트명 변경 규칙과 동일하게 차단한다.
        project_name = source.pop("project_name", None)
        if project_name is not None and self.normalize_name(project_name) != self.normalize_name(record.name):
            record.name = self._ensure_unique_name(project_name, exclude_id=record.project_id)

        record.overview_fields = source

        # 프로젝트 목록에서 처음 입력하는 기본 개요 항목과 양방향으로 맞춘다.
        if "location" in source:
            record.location = source["location"]
        if "construction_period" in source:
            record.construction_months = int(self._overview_number(source["construction_period"], 0.0))
        if "use_type" in source:
            record.construction_type = source["use_type"]
        if "building_area" in source:
            record.building_area_m2 = self._overview_number(source["building_area"], 0.0)
        if "gross_area" in source:
            record.gross_area_m2 = self._overview_number(source["gross_area"], 0.0)
        if "height" in source:
            record.height_m = self._overview_number(source["height"], 0.0)
        if "excavation_depth" in source:
            record.depth_m = self._overview_number(source["excavation_depth"], 0.0)
        if "exterior_finish" in source:
            record.exterior_material = source["exterior_finish"]
        if "structure_type" in source:
            record.building_structure = source["structure_type"]
        if source.get("insulation", "") in {"", "내단열", "외단열", "복합"}:
            record.insulation_type = source.get("insulation", "")
        self._write(record)
        return record

    def update_name(self, project_id: str, name: str) -> ProjectRecord:
        record = self.get(project_id)
        record.name = self._ensure_unique_name(name, exclude_id=record.project_id)
        self._write(record)
        return record

    def update_details(self, project_id: str, values: dict) -> ProjectRecord:
        record = self.get(project_id)
        text_fields = (
            "overview", "client", "location", "construction_type",
            "exterior_material", "building_structure",
        )
        for key in text_fields:
            if key in values:
                setattr(record, key, str(values.get(key, "") or ""))
        for key in ("building_area_m2", "gross_area_m2", "height_m", "depth_m"):
            if key in values:
                setattr(record, key, self._nonnegative_float(values.get(key)))
        if "construction_months" in values:
            record.construction_months = self._nonnegative_int(values.get("construction_months"))
        if "insulation_type" in values:
            insulation = str(values.get("insulation_type", "") or "")
            if insulation not in {"", "내단열", "외단열", "복합"}:
                raise ProjectError("단열구분을 목록에서 선택해 주세요.")
            record.insulation_type = insulation

        # 프로젝트 정보 창에서 수정한 값도 개요 탭의 대응 항목에 즉시 이어지게 한다.
        overview = dict(record.overview_fields or {})
        sync = {
            "location": record.location,
            "construction_period": f"{record.construction_months}개월" if record.construction_months else "",
            "use_type": record.construction_type,
            "building_area": str(record.building_area_m2) if record.building_area_m2 else "",
            "gross_area": str(record.gross_area_m2) if record.gross_area_m2 else "",
            "height": str(record.height_m) if record.height_m else "",
            "excavation_depth": str(record.depth_m) if record.depth_m else "",
            "exterior_finish": record.exterior_material,
            "structure_type": record.building_structure,
            "insulation": record.insulation_type,
        }
        for key, value in sync.items():
            overview[key] = str(value or "")
        record.overview_fields = overview
        self._write(record)
        return record

    def update_building_groups(self, project_id: str, groups) -> ProjectRecord:
        record = self.get(project_id)
        record.building_groups = self.normalize_building_groups(groups, strict=True)
        overview = dict(record.overview_fields or {})
        ground = max((int(group.get("ground", 0) or 0) for group in record.building_groups), default=0)
        basement = max((int(group.get("basement", 0) or 0) for group in record.building_groups), default=0)
        overview["ground_floors"] = str(ground) if ground else ""
        overview["basement_floors"] = str(basement) if basement else ""
        overview["total_floors"] = str(ground + basement) if ground or basement else ""
        record.overview_fields = overview
        self._write(record)
        return record

    def update_cad_link(self, project_id: str, handle: str, token: str, document: str = "") -> ProjectRecord:
        """프로젝트와 CAD 도면을 연결하는 단일 마커 정보를 저장한다."""
        record = self.get(project_id)
        record.cad_link_handle = str(handle or "").strip().upper()
        record.cad_link_token = str(token or "").strip()
        record.cad_link_document = str(document or "")
        record.cad_link_updated_at = datetime.now().isoformat(timespec="seconds")
        self._write(record)
        return record

    def delete(self, project_id: str):
        record = self.get(project_id)
        path = record.path
        if not path.is_dir():
            raise ProjectError("삭제할 프로젝트를 찾을 수 없습니다.")
        shutil.rmtree(path)

    def duplicate(self, project_id: str, new_name: str) -> ProjectRecord:
        source = self.get(project_id)
        value = self._ensure_unique_name(new_name)
        new_id = uuid.uuid4().hex[:12].upper()
        destination = self._project_folder_for_name(value)
        shutil.copytree(source.path, destination)
        try:
            record = self._read_path(destination)
            record.project_id = new_id
            record.name = value
            record.created_date = date.today().isoformat()
            record.cad_link_handle = ""
            record.cad_link_token = ""
            record.cad_link_document = ""
            record.cad_link_updated_at = ""
            record.path = destination
            self._write(record)
            return record
        except Exception:
            if destination.is_dir():
                shutil.rmtree(destination)
            raise

    def assign_base_drawing(self, project_id: str, source_path: str | Path) -> ProjectRecord:
        source = Path(source_path)
        if not source.is_file() or source.suffix.lower() != ".dxf":
            raise ProjectError("적용할 DXF 도면 파일을 찾을 수 없습니다.")
        record = self.get(project_id)
        drawing_dir = record.path / "도면"
        drawing_dir.mkdir(parents=True, exist_ok=True)
        destination = drawing_dir / source.name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        record.base_drawing = destination.relative_to(record.path).as_posix()
        record.base_drawing_source = str(source.resolve())
        # 베이스 DXF 지정은 CAD 연동 등록과 별개의 작업이다.
        # 동일 프로젝트에서 배경 도면을 적용/교체해도 기존 CAD 등록정보를 유지한다.
        self._write(record)
        return record

    def export_project(self, project_id: str, output_path: str | Path) -> Path:
        record = self.get(project_id)
        output = Path(output_path)
        if output.suffix.lower() != ".zip":
            output = output.with_suffix(".zip")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(record.path.rglob("*")):
                if path.is_file() and path.name != f"{PROJECT_METADATA_NAME}.tmp":
                    archive.write(path, path.relative_to(record.path).as_posix())
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ProjectError("프로젝트 ZIP 검사에 실패했습니다.")
        return output

    @staticmethod
    def _validate_zip_members(archive: zipfile.ZipFile):
        infos = archive.infolist()
        if len(infos) > 50_000:
            raise ProjectError("ZIP 안의 파일 개수가 지나치게 많습니다.")
        if sum(info.file_size for info in infos) > 2 * 1024 * 1024 * 1024:
            raise ProjectError("ZIP의 압축 해제 크기가 2GB를 초과합니다.")
        seen: set[str] = set()
        for info in infos:
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            marker = name.casefold()
            if marker in seen:
                raise ProjectError("ZIP에 중복된 파일 경로가 포함되어 있습니다.")
            seen.add(marker)
            if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
                raise ProjectError("ZIP에 안전하지 않은 경로가 포함되어 있습니다.")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise ProjectError("ZIP의 심볼릭 링크는 가져올 수 없습니다.")

    def inspect_import(self, zip_path: str | Path) -> ProjectRecord:
        source = Path(zip_path)
        if not source.is_file():
            raise ProjectError("가져올 ZIP 파일을 찾을 수 없습니다.")
        try:
            with zipfile.ZipFile(source) as archive:
                self._validate_zip_members(archive)
                if PROJECT_METADATA_NAME not in archive.namelist():
                    raise ProjectError("프로젝트 정보(project.json)가 없는 ZIP입니다.")
                data = json.loads(archive.read(PROJECT_METADATA_NAME).decode("utf-8"))
        except ProjectError:
            raise
        except Exception as exc:
            raise ProjectError("프로젝트 ZIP을 읽을 수 없습니다.") from exc
        return self._record_from_data(data, Path())

    def import_project(self, zip_path: str | Path, new_name: str) -> ProjectRecord:
        source = Path(zip_path)
        inspected = self.inspect_import(source)
        value = self._ensure_unique_name(new_name)
        new_id = uuid.uuid4().hex[:12].upper()
        temporary = self.root / f".import_{new_id}"
        destination = self._project_folder_for_name(value)
        if temporary.exists() or destination.exists():
            raise ProjectError("프로젝트 가져오기 임시 경로가 이미 존재합니다.")
        try:
            temporary.mkdir(parents=True)
            with zipfile.ZipFile(source) as archive:
                self._validate_zip_members(archive)
                archive.extractall(temporary)
            record = self._read_path(temporary)
            record.project_id = new_id
            record.name = value
            record.created_date = inspected.created_date
            record.cad_link_handle = ""
            record.cad_link_token = ""
            record.cad_link_document = ""
            record.cad_link_updated_at = ""
            temporary.replace(destination)
            record.path = destination
            if record.base_drawing and record.base_drawing_path is None:
                record.base_drawing = ""
            self._write(record)
            return record
        except Exception:
            if temporary.is_dir():
                shutil.rmtree(temporary)
            if destination.is_dir():
                shutil.rmtree(destination)
            raise
