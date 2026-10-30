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
