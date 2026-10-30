# DXF 견적프로그램 (로컬 웹판) — 작업 규칙 (Codex · Claude 공용)

이 파일은 Codex와 Claude가 함께 따르는 규칙입니다. `CLAUDE.md`는 이 파일을 그대로 불러옵니다.
규칙을 바꿀 때는 이 파일만 수정하세요.
배경·구조·캐시 포맷·API·다음 작업 우선순위의 상세 설명은
`docs/DXF_Web_Claude_Handoff_V0.5.6.txt`(인계서)에 있습니다. 큰 작업 전에 먼저 읽으세요.

## 프로젝트 개요

- Windows PC에서 Python 로컬 서버(127.0.0.1)를 띄우고 Chrome/Edge `--app` 독립창으로 여는
  **완전 오프라인 로컬 앱**입니다. 인터넷 서비스가 아닙니다.
- 현재 범위: DXF 직접 읽기 → 도곽·층 자동인식 → 층별 디스크 캐시 → WebGL2/Canvas2D 읽기 전용 뷰어.
  실/벽/창호 편집, 견적 산출, CAD COM 연동은 아직 없습니다.
- `app/` — 기존 데스크톱 프로그램에서 가져온 DXF 해석 엔진
  (`app/rooms/core.py`의 `read_cad_background()`, `read_drawing_divisions()`,
  `app/rooms/background_source_cache.py`의 도곽/클리핑 보조).
- `web_trial/` — `server.py`(HTTP API·DXF 작업), `disk_cache.py`(층별 캐시, `CACHE_VERSION`),
  `auto_frames.py`(도곽 자동인식), `read_log.py`(읽기 로그), `browser_window.py`(앱 창), `file_dialog.py`(DXF 열기 창),
  `static/`(`index.html`, `style.css`, `app.js`).
- 버전별 변경 내역은 `README.md`에 누적 기록합니다.

## 실행

```sh
python -m pip install ezdxf
python -m web_trial.server --no-browser --port 8765   # http://127.0.0.1:8765/
```

Windows 사용자는 `웹_시험판_실행.bat`로 실행합니다.

## 실제 실행·테스트 폴더

- 사용자가 프로그램을 실제로 실행해 확인하는 곳은 저장소가 아니라 **`D:\P\Estimate\DXF_견적프로그램 작성\@@테스트 웹`** 입니다.
  (저장소 `DXF_Estimate_Web`와 같은 상위 폴더에 있음. 그 폴더에는 캐시·설정·이전 zip·데스크톱 파일이 함께 있어 git으로 관리하지 않음.)
- 저장소의 최신본을 테스트 폴더에 반영하는 방법: 저장소 폴더의 **`테스트폴더로_반영.bat`** 실행
  (`git pull` 후 `app/`, `web_trial/`, `README.md`, `웹_시험판_실행.bat`만 복사. `web_trial/data/` 캐시·`root_path.txt`는 건드리지 않음).
- 코드는 항상 저장소에서 고치고, 테스트 폴더의 파일을 직접 고치지 않습니다(다음 반영 때 덮어써짐).
- 로컬 PC에서 작업한 경우 끝날 때 반영 bat까지 실행하고 WORKLOG에 적습니다. Codex(클라우드)는 push까지만 하고,
  WORKLOG에 "테스트 폴더 반영 필요"라고 적어 둡니다.

## 테스트 (파일 하나만 고쳐도 변경 후 반드시 실행)

```sh
python web_trial/test_auto_frames.py
python web_trial/test_auto_mixed_v056.py
python web_trial/test_cache_controls.py
python web_trial/test_dxf_import.py
python web_trial/test_large_cache.py
python web_trial/test_file_browse.py
python -m unittest web_trial.test_browser_window web_trial.test_read_log
```

## 반드시 지킬 것

- **원본 DXF/DWG와 기존 프로젝트 폴더는 절대 수정하지 않습니다.** 생성물은 `web_trial/data/`에만 씁니다.
- **외부 네트워크/CDN 금지.** 폰트·라이브러리도 외부에서 불러오지 않습니다.
- **선분 수·파일 크기에 임의의 하드 제한을 걸지 않습니다**(과거 200만 선분 제한 제거 이력 있음).
  큰 파일은 스트리밍/분할 처리, 메모리 경고·취소로 대응합니다.
- 캐시 형식이 바뀌면 `disk_cache.py`의 `CACHE_VERSION`(현재 7)을 올리고
  README에 "기존 캐시 1회 재분석" 안내를 적습니다. 형식이 같으면 올리지 않습니다.
- 강제 재분석이 실패해도 이전 정상 캐시는 보존되어야 합니다.
- 도곽: 기존 지정 도곽 > 사용자 선택 > 자동 후보 순. 불확실하면 임의 배정하지 말고 "판정 보류".
  가상·추정 도곽을 견적 확정 경계로 쓰지 않습니다. 자동인식을 바꾸면 기존 지정 도곽을
  덮어쓰지 않는지 회귀 테스트를 추가합니다.
- 기존 기능(빠른 렌더링, 흰색/검정 배경, 층별 캐시 재사용, 강제 재분석, 진단 로그)을 훼손하지 않습니다.
- UI 문구는 한국어. 특정 CAD 제품명 대신 일반 용어 "CAD"를 씁니다.
  `index.html`의 `id`는 `app.js`가 참조하므로 바꿀 때 두 파일을 함께 수정합니다.
- `.bat`는 CRLF 유지, 사용자에게 주는 TXT는 UTF-8 BOM.
- 편집/견적 UI를 한 번에 전부 재작성하지 않고, 작은 변경 단위로 진행합니다.
- 주석 밀도·이름 짓기·코드 스타일은 주변 코드에 맞춥니다.
- 실제 현장 도면(`*.dxf`, `*.dwg`), `web_trial/data/`, `root_path.txt`, zip 파일은 커밋하지 않습니다.

## 협업 방식 (Git) — 교대 작업

Codex와 Claude는 **동시에 작업하지 않고 교대로** 작업합니다. 그래서 브랜치/PR 없이 `main` 하나로 진행합니다.

1. **시작할 때**: `git pull`로 최신 `main`을 받고, `docs/WORKLOG.md` 맨 위 항목(직전 작업자의 기록)을 먼저 읽습니다.
2. **작업 중**: 의미 있는 단위로 나눠 커밋합니다. 테스트가 통과한 상태로만 커밋합니다.
3. **끝낼 때**: `docs/WORKLOG.md` 맨 위에 이번 작업 기록을 추가합니다
   (한 일 / 바뀐 파일 / 검증한 것 / 검증 못 한 것·주의 / 다음 작업자에게). 그다음 커밋하고 `git push`.
   다음 작업자는 대화 내용을 볼 수 없으므로, **기록에 없는 내용은 전달되지 않는다**고 생각하고 적습니다.
- 소스는 **zip이 아니라 파일 그대로** 커밋합니다(zip은 변경 비교가 불가능).
- 커밋 메시지는 한국어로, 무엇을 왜 바꿨는지 한 줄 요약 + 필요하면 본문.
- 새 버전 배포 시 README 맨 위에 `## V0.x.y` 절 추가, `index.html` 상단 버전 표기 갱신, `v0.x.y` 태그.
- `git push --force` 등 기록을 지우는 명령은 쓰지 않습니다.
