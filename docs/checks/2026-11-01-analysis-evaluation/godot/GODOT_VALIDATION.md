# 분석 형세 표시의 Godot 검증

2026-11-01, Godot 4.7.2·Regicide 0.7.0-dev `build/regicide.exe`를 사용했다. 분석 합성 검사 67개와 실제 관리기·엔진 연동 35개, 기존 형세 막대 43개, 기존 추천 합성 41개·실제 연동 35개가 통과했다. 검증 기록은 아래 하위 폴더에 있다.

```powershell
.\.venv-tests\Scripts\python.exe tests/analysis_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/analysis-evaluation-godot
.\.venv-tests\Scripts\python.exe tests/hint_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/analysis-evaluation-regression-hints
& C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --headless --path godot --script res://tests/evaluation_bar_tests.gd
```

양수는 백·음수는 흑 우세, 실제 0점과 미계산 구분, 탐색 중·완료 구분, 상한·하한, 메이트·무승부, 시점 전환, 기물 편집·착수·중지·연결 오류 시 이전 평가 제거를 확인했다. 평가의 game_id·revision·FEN·차례가 현재 분석 위치와 하나라도 다르면 표시하지 않는다. 실제 엔진의 동일 추천 탐색에서 계산 중 평가가 먼저 도착하고 종료 시 ready로 바뀌는 것도 확인했다. 형세 막대는 추정 평가의 시각화이며 승률이 아니다.

OS 포인터로 처음 시험한 끌기는 적용되지 않았다. 이후 실제 Viewport 입력 디스패처에 마우스 누름→이동→뗌을 넣어, 상대 이동량이 0인 포인터 이벤트에서는 기본 끌기 임계값이 시작되지 않는 것을 재현했다. 편집 전용 보드·기물 목록이 눌렀던 위치와 현재 위치의 차이로도 네이티브 끌기를 시작하도록 보강했다. 원래 경로와 상대 이동량 0인 경로를 모두 검증했다. 이 단계의 디스패처 기록은 38개 검사이며, 후속 캐슬링 단계에는 Esc·창 밖 취소를 포함한 최종 44개 기록을 보관했다.

모든 이 문서의 자동 검사는 headless·합성 입력이고 실제 OS 포인터의 수정 후 재검증은 포함하지 않는다. 사용 중인 사용자 창은 조작하지 않았다. 첫 병렬 실행은 검사 통과 후 Python 엔진 종료 future 경고가 한 번 있었고, 최종 직렬 재실행의 stdout/stderr인 `analysis/runner.txt`에는 경고가 없었다. 원본과 사본의 바이트 일치는 각 `sha256.json`에 기록했으며 `.log`는 `.txt`로 확장자만 바꿨다. 후속 캐슬링 검사는 이 형세·끌기 검사 전체를 포함한다.
