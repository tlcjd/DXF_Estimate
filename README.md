
## V0.24.12 변경
- DXF group code 60(invisible) 및 ATTRIB/ATTDEF invisible flag 객체를 블록 전개 전에 제외합니다. 동적/익명 블록의 비표시 상태가 프로그램에서 함께 겹쳐 보이는 현상을 1차 보정합니다.
- 벡터 배경 TEXT/MTEXT를 QPainter.drawText 래스터 축소 방식 대신 QPainterPath 글리프 외곽선으로 렌더링하여 확대/축소 시 흐림을 줄였습니다.
- CAD 진단에 `본도면 숨김 객체 제외` 개수와 예시를 추가했습니다.
- 원본 벡터 캐시 버전을 10으로 올려 기존 잘못 전개된 캐시를 자동 재사용하지 않습니다.

# V0.24.12

- 현재 활성 CAD 최신사본 생성 1순위를 `Document.Export(..., "DXF", SelectionSet)` 직접 Export 방식으로 변경
- DXF Export는 SelectionSet 내용과 무관하게 현재 도면 전체를 내보내므로 ZWCAD에서 실패하던 `SelectionSet.Select(5)` 경로를 우회
- 별도 `Documents.Add()` 없이 현재 CAD의 최신 상태를 임시 DXF로 생성
- 원본 DWG에는 Save/SaveAs를 수행하지 않음
- 직접 Export 실패 시에만 기존 WBlock → CopyObjects 순으로 폴백
- 최신사본 생성 전체 실패 시 과거 적용 DXF로 폴백하지 않고 오류로 중단
