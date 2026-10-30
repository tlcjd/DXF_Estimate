from __future__ import annotations

import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]
USER_FILES_DIR = APP_DIR / "사용자파일"
MATERIAL_FINISH_DIR = USER_FILES_DIR / "재료마감"
MATERIAL_FINISH_PATH = MATERIAL_FINISH_DIR / "재료마감_리스트.json"
MATERIAL_DICTIONARY_DIR = USER_FILES_DIR / "재료사전"
MATERIAL_DICTIONARY_PATH = MATERIAL_DICTIONARY_DIR / "재료문자_사전.json"
WALL_FINISH_ASSIGNMENTS_DIR = USER_FILES_DIR / "벽마감지정"
BLOCK_LIBRARY_DIR = USER_FILES_DIR / "블록라이브러리"
PROJECTS_DIR = USER_FILES_DIR / "프로젝트"
PROJECT_LIST_DIR = PROJECTS_DIR / "프로젝트목록"
DRAWINGS_DIR = USER_FILES_DIR / "도면"
TEMP_PROJECT_DIR = PROJECTS_DIR / "임시프로젝트"
TEMP_PROJECT_CACHE_DIR = TEMP_PROJECT_DIR / "캐쉬"
PROJECT_BACKGROUND_DXF_PATH = TEMP_PROJECT_DIR / "원본CAD.dxf"
CAD_BACKGROUND_CACHE_DIR = TEMP_PROJECT_CACHE_DIR / "캐드배경"
TEMP_DXF_CACHE_DIR = TEMP_PROJECT_CACHE_DIR / "임시DXF"
TEMP_CAD_DXF_PATH = TEMP_DXF_CACHE_DIR / "현재도면_임시.dxf"
WORKING_DXF_CACHE_DIR = TEMP_PROJECT_CACHE_DIR / "작업DXF"
WORKING_DXF_PATH = WORKING_DXF_CACHE_DIR / "현재작업_임시.dxf"


def ensure_user_file_structure() -> None:
    for folder in (
        USER_FILES_DIR,
        MATERIAL_FINISH_DIR,
        MATERIAL_DICTIONARY_DIR,
        WALL_FINISH_ASSIGNMENTS_DIR,
        BLOCK_LIBRARY_DIR,
        PROJECTS_DIR,
        DRAWINGS_DIR,
        TEMP_PROJECT_DIR,
        TEMP_PROJECT_CACHE_DIR,
        CAD_BACKGROUND_CACHE_DIR,
        TEMP_DXF_CACHE_DIR,
        WORKING_DXF_CACHE_DIR,
    ):
        folder.mkdir(parents=True, exist_ok=True)

    legacy_files = {
        APP_DIR / "data" / "finish_materials.json": MATERIAL_FINISH_PATH,
        APP_DIR / "data" / "material_dictionary.json": MATERIAL_DICTIONARY_PATH,
    }
    for old_path, new_path in legacy_files.items():
        if old_path.exists() and not new_path.exists():
            shutil.move(str(old_path), str(new_path))

    legacy_assignment_dir = APP_DIR / "data" / "wall_finish_assignments"
    if legacy_assignment_dir.exists():
        for old_path in legacy_assignment_dir.glob("*.json"):
            new_path = WALL_FINISH_ASSIGNMENTS_DIR / old_path.name
            if not new_path.exists():
                shutil.move(str(old_path), str(new_path))

    legacy_block_dir = APP_DIR / "블록라이브러리"
    if legacy_block_dir.exists():
        for old_path in legacy_block_dir.iterdir():
            if old_path.is_file():
                new_path = BLOCK_LIBRARY_DIR / old_path.name
                if not new_path.exists():
                    shutil.move(str(old_path), str(new_path))


    legacy_temp_cache_dir = TEMP_PROJECT_DIR / "cache"
    if legacy_temp_cache_dir.exists() and legacy_temp_cache_dir != TEMP_PROJECT_CACHE_DIR:
        TEMP_PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for old_path in legacy_temp_cache_dir.iterdir():
            new_path = TEMP_PROJECT_CACHE_DIR / old_path.name
            if not new_path.exists():
                shutil.move(str(old_path), str(new_path))

    legacy_background_cache_dir = APP_DIR / "cache" / "cad_background"
    if legacy_background_cache_dir.exists():
        for old_path in legacy_background_cache_dir.iterdir():
            new_path = CAD_BACKGROUND_CACHE_DIR / old_path.name
            if not new_path.exists():
                shutil.move(str(old_path), str(new_path))
