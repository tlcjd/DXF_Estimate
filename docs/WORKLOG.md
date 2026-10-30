# 작업 기록 (WORKLOG)

Codex·Claude가 **교대로** 작업하면서 다음 작업자에게 넘기는 기록입니다. 최신 항목이 맨 위입니다.
작업을 마칠 때마다 아래 형식으로 한 항목을 추가하고 커밋·push 하세요.

```
## YYYY-MM-DD · 작업자(Codex/Claude) · 한 줄 제목
- 한 일:
- 바뀐 파일:
- 검증한 것:
- 검증 못 한 것 / 주의:
- 다음 작업자에게:
```

---

## 2026-10-30 · Claude · DXF 열기 버튼 하나로 통합 (업로드·경로칸 제거)

**한 일**
- 사용자 요청: "DXF 파일 선택"과 "경로 열기"가 두 개일 필요 없음, 경로칸도 필요 없음 → 버튼 하나로.
- 배경: 브라우저는 실제 파일 경로를 주지 않아 업로드(전체 복사)만 가능했고, 큰 도면 때문에 경로 직접 열기가 따로 붙어 있었음.
  로컬 앱이므로 서버가 Windows 파일 선택 창을 직접 띄워 원본 경로를 받도록 변경.
- `web_trial/file_dialog.py`(신규): `ask_dxf_path(initial_dir)` — PowerShell `OpenFileDialog`(STA, TopMost 소유자 폼으로 Chrome 앱 창 앞에 표시,
  스크립트는 `-EncodedCommand` UTF-16, 출력 UTF-8로 한글 경로 안전). Windows 외에는 tkinter. 동시에 두 개 창 열림 방지(Lock).
- `server.py`: `POST /api/dxf/browse` {force, carryChoices} — 창 띄움 → 취소 시 `{canceled:true}` → `.dxf` 검증 → `signature_for_path`로
  기존 경로 읽기와 같은 `DXF_JOBS.begin()`. 초기 폴더는 현재 캐시의 `sourcePath` 폴더.
  `carryChoices`: 업로드 캐시(원본 위치 없음)에서 저장한 도곽 선택(`frame_choices/<업로드키>.json`)을 경로 캐시 키로 복사.
- `app.js`: `openDxf()`/`openDxfPath()` 삭제 → `browseDxf({force,carryChoices})`. 도곽 선택/복귀의 `needsUpload` 분기는 안내 후 `browseDxf({force:true,carryChoices:true})`.
- `index.html`/`style.css`: 파일 입력·선택 파일명·DXF 읽기·경로칸·경로 열기 제거, `#btnBrowseDxf` 추가. 관련 CSS 정리.
- 서버의 `/api/dxf/upload`, `/api/dxf/open-path`는 호환·테스트용으로 유지(화면에서는 사용 안 함).

**검증한 것**
- `test_file_browse.py`(신규) PASS: 취소, DXF 외 파일 거절, 경로 직접 읽기·경로 캐시 생성, 직전 폴더에서 열기, 같은 파일 캐시 재사용,
  업로드 캐시 도곽 선택 이어받기(force 재분석), 중복 창 차단, 실제 PowerShell 스크립트로 한글 경로 왕복(창 표시 부분만 대체).
- 기존 테스트 7종 PASS. app.js가 쓰는 id 모두 HTML에 존재. 테스트 폴더 서버 재시작 후 화면 정상.

**검증 못 한 것 / 주의**
- 실제 파일 선택 창을 띄워 클릭하는 것은 사용자 PC 화면에 창이 뜨므로 하지 않음 → **사용자 실기 확인 필요**
  (창이 Chrome 앱 창 앞에 뜨는지, 선택 후 읽기 진행되는지).
- 파일 창이 열려 있는 동안 해당 HTTP 요청은 대기 상태(브라우저 fetch는 시간 제한 없음).
- 서버 코드가 바뀌었으므로 테스트 폴더에서 이미 실행 중인 서버는 **재시작해야** 적용됨.

**다음 작업자에게**
- DXF를 여는 새 기능은 `browseDxf()`/`/api/dxf/browse`를 사용. 브라우저 업로드 경로는 더 이상 화면에서 쓰지 않음.

---

## 2026-10-30 · Claude · Ctrl 누르는 동안 선택 레이어만 표시

**한 일**
- 사용자 요청: 객체를 선택하고 Ctrl을 누르고 있으면 그 객체의 레이어만 보이고 나머지는 숨김.
- `app.js`
  - `state.solo`(단독 표시 레이어), `state.ctrlDown` 추가.
  - 레이어 표시 판단을 `layerHidden(name)` 하나로 통일: 단독 중이면 해당 레이어만, 아니면 기존 `state.hidden`.
    WebGL/Canvas2D 선, 채움, 문자, 데모 선택, 서버 선택(`/api/pick`의 `hidden`은 `hiddenLayerNames()`)이 모두 이 함수를 사용.
    레이어 체크박스(`rebuildLayerList`)만 `state.hidden`을 직접 사용 → 단독 보기는 체크 상태를 바꾸지 않음.
  - `setSolo(layer|null)`: 배지(`#soloBadge`) 표시, 레이어 목록에서 해당 행 강조(`.soloing`, `.solo`), 다시 그리기.
  - keydown `Control`(반복 무시, 입력칸 포커스 시 무시) → 선택된 레이어로 단독. keyup `Control`·`window blur` → 해제.
    Ctrl을 누른 채 선택하면 `showSelection()`에서 바로 단독. `chooseFloor()`에서 해제.
- `index.html`: 단독 배지, 하단 조작 안내와 사용 안내 창에 Ctrl 설명 추가. `style.css`: 배지·레이어 강조 스타일.

**검증한 것**
- 테스트 폴더 서버 + 효제동 B2F 캐시에서 스크립트로 확인: WAL 선택 후 Ctrl → 보이는 레이어 WAL 1개, 배지·목록 강조 표시,
  해제 시 23개 복귀, 체크 23개 유지 / 단독 중 다른 레이어(DIM) 위치 클릭 시 선택 안 됨 / Ctrl 먼저 누르고 클릭해도 단독 /
  사용자가 끈 레이어(TEX)는 해제 후에도 꺼진 상태 유지 / 창 포커스 잃으면 해제. 스크린샷으로 화면 확인.
- 테스트 7종 PASS, app.js가 쓰는 id 모두 HTML에 존재.

**검증 못 한 것 / 주의**
- 실제 키보드로 누르는 동작은 스크립트 이벤트로 대신 확인함(사용자 실기 확인 필요).
- Ctrl+휠은 이 앱의 휠 확대·축소가 가로채므로 브라우저 확대는 일어나지 않음(기존 동작과 동일).
- 레이어가 수백 개인 도면에서 단독 중 클릭 선택 시 `/api/pick` 쿼리에 숨김 레이어 목록이 길게 붙음(기존 `전체 숨김`과 같은 방식). 문제 되면 서버에 `only=` 파라미터 추가 검토.

**다음 작업자에게**
- 레이어 표시 조건을 새로 쓰는 코드는 `state.hidden.has()` 대신 `layerHidden()`을 사용할 것.

---

## 2026-10-30 · Claude · UI 2차 정리 + 테스트 폴더 반영 bat

**한 일**
- 사용자가 실제 실행·테스트하는 폴더가 `D:\P\Estimate\DXF_견적프로그램 작성\@@테스트 웹`임을 확인(저장소와 별도, git 관리 안 함).
  저장소 최신본을 그 폴더로 복사하는 `테스트폴더로_반영.bat` 추가(캐시·설정·zip·데스크톱 파일은 건드리지 않음). AGENTS.md에 규칙 명시.
- 실제 현장 도면(효제동 평면변경안1.dxf 캐시)을 띄워 UI 2차 정리. **기능 추가·삭제 없음.**
  - `#dxfJobStatus`, `#dxfElapsed`를 상단 DXF 바에서 하단 상태바(`.jobStatus`)로 이동. DXF 바는 경로 입력칸이 넓어지고 진단 버튼은 오른쪽 정렬.
  - 1680px 이하에서는 고정 문구 `#modeStatus`와 중복 표시인 `#renderTime`을 숨겨 상태 문구 공간 확보(`#performance`에 같은 ms 포함).
  - `.gridHint`(CAD VECTORS · LOCAL DXF)가 도면 제목을 가려 `display:none`(요소와 app.js 코드는 유지).
  - 도곽 판정 창: `showFrameInfo()`에서 요약을 칩(`.frameChip`)으로, 목록에 그룹 제목(`.frameSection`) 추가. `row()`의 상태가 `추정|확인|보류`를 포함하면 주황 배지.
  - 캐시 폴더 접기 화살표를 CSS 셰브런으로 교체.

**바뀐 파일**: `web_trial/static/index.html`, `style.css`, `app.js`(showFrameInfo 표시 부분만), `README.md`, `AGENTS.md`, `테스트폴더로_반영.bat`

**검증한 것**
- 테스트 7종 PASS. 단 `test_dxf_import`가 연속 실행 중 한 번 Windows `PermissionError [WinError 5]`(임시 파일 잠김)로 실패 → 단독 재실행 시 PASS. UI 변경과 무관해 보이나 반복되면 테스트의 임시 폴더 정리 부분 확인 필요.
- 테스트 폴더에서 서버 실행: 1440px·1100px 화면, 도곽 판정 창(층 도곽 8 / 다른 도면 후보 47), 흰색 배경, 선분 선택, 콘솔 오류 없음. app.js가 쓰는 id 모두 HTML에 존재.
- `테스트폴더로_반영.bat` 실행 후 테스트 폴더와 저장소 `app/`·`web_trial/`·README 27개 파일 일치 확인.

**검증 못 한 것 / 주의**
- 실제 DXF 새로 읽기(진행률 막대, 하단 상태 문구 갱신)와 `이 도곽 선택`·`자동으로 복귀` 클릭 후 재분석 흐름은 실행하지 않음.
- WebGL 모드는 여전히 미확인(확인 환경에서 Canvas2D만 가능).

**다음 작업자에게**
- Codex는 테스트 폴더에 접근할 수 없으니, push 후 WORKLOG에 "테스트 폴더 반영 필요"를 적어 두면 사용자가 bat로 반영함.

---

## 2026-10-30 · Claude · Git 저장소 구성 + V0.5.6 UI 정리

**한 일**
- 사용자 PC의 버전별 zip(V0.3 ~ V0.5.4)과 GitHub에 올라온 V0.5.6 zip을 풀어 버전 순서대로 커밋하고 태그를 붙임.
  - `v0.3` ~ `v0.5.4`: V0.4 이후 zip은 V0.3(데스크톱 프로그램 전체 포함) 위에 덮어쓰는 패치였으므로 누적 적용.
  - `v0.5.6`: 인계서 설명대로 웹판 전용 파일만 남긴 독립 버전이라 **교체**로 커밋(데스크톱 `main.py`, PySide UI 등 삭제됨 → 필요하면 `v0.5.4` 태그에서 꺼낼 수 있음). V0.5.5 zip은 따로 없었음.
  - `v0.5.4-ui`: V0.5.4 기준 UI 정리본(사용자에게 `DXF_Estimate_Web_V0.5.4_UI.zip`으로 전달했던 것).
- V0.5.4 기준으로 했던 UI 정리를 V0.5.6 위에 다시 적용(README `V0.5.6 UI 정리` 절 참고).
  V0.5.6의 `app.js` 신규 기능(강제 재분석·캐시 삭제·도곽 선택/복귀)과 충돌 없이 병합됨.
- GPT 인계서를 `docs/DXF_Web_Claude_Handoff_V0.5.6.txt`로 옮기고, 핵심 규칙을 `AGENTS.md`에 정리. `CLAUDE.md`는 `@AGENTS.md`만 불러옴.
- `.gitignore`(캐시·로그·`root_path.txt`·`settings.ini`·dxf/dwg/zip 제외), `.gitattributes`(저장소는 LF, `.bat`만 CRLF) 추가.
- GitHub에 사용자가 직접 올린 커밋 `d40b1c3 Add files via upload`(zip+인계서)는 지우지 않고 병합 커밋으로 기록 보존. zip 파일 자체는 최신 트리에서 빠짐(소스로 풀려 있음).

**바뀐 파일**
- `web_trial/static/index.html`, `style.css`, `app.js` (UI만. 서버·캐시·DXF 해석 코드는 수정 없음)
- `README.md`, `AGENTS.md`, `CLAUDE.md`, `.gitignore`, `.gitattributes`, `docs/*`

**app.js에서 Claude가 바꾼 부분** (Codex가 app.js 수정 시 참고)
- `busy(on,msg,progress)` — 세 번째 인자로 진행률 막대(`#busyProgress`) 표시. `watchDxfJob`에서 `result.stage` 전달.
- `message()`, `dxfProgress()`, `logNotice()` — 인라인 색상 대신 `.error` 클래스 토글.
- `rebuildLayerList()` — 레이어 첫 선분의 RGB로 `.colorMark` 색 지정.
- `showSelection()/resetSelection()` — `.selCard.picked` 토글.
- `btnGray` 핸들러 버그 수정, `dxfFile` change 시 `.dxfSelected.has` 클래스, 전역 Esc로 `.modalBackdrop.visible` 닫기.
- HTML의 모든 `id`는 기존 이름 그대로 유지함(자동 대조 검사로 누락 없음 확인).

**검증한 것**
- 테스트 7종 모두 PASS: `test_auto_frames`, `test_auto_mixed_v056`, `test_cache_controls`, `test_dxf_import`, `test_large_cache`, `unittest test_browser_window test_read_log`.
- 기존 캐시 도면으로 1440px·1100px 화면 확인: 표시, 선분 선택, 원색/회색, 흰색 배경, 대화상자 열기/Esc 닫기, 콘솔 오류 없음.

**검증 못 한 것 / 주의**
- 새 DXF를 실제로 읽는 과정(진행률 막대 동작)과 도곽 결과 창의 `이 도곽 선택` 버튼 모양은 실제 DXF로 확인하지 못함.
- 확인 환경에서 WebGL2가 안 돼 Canvas2D로만 확인. Windows Chrome의 WebGL 모드 확인 필요.
- GitHub 저장소 `tlcjd/DXF_Estimate`는 현재 **Public**. 비공개 전환 권장(사용자에게 안내함).

**다음 작업자에게**
- 인계서 10장 우선순위대로 P0-A(도곽 후보를 도면 위에 사각형으로 표시·수동 선택) 또는 P0-B(최초 DXF 읽기 메모리 피크 감소)부터 진행 권장.
- UI를 바꿀 땐 `style.css` 상단 `:root` 색상 변수와 `.btn`/`.toolBtn`/`.seg` 공통 클래스를 재사용하면 디자인이 맞춰짐.
