# Godot 분석 편집기의 끌어놓기 검증

2026-11-01, Godot 4.7.2와 `build/regicide.exe`(Regicide 0.7.0-dev)를 사용했다. 새 프로젝트 복사본의 캐시 없는 실행, 분석 합성 입력 47개, 실제 엔진 연동 31개, 기존 보드 좌표·입력 637개, 기존 추천 표시 합성 41개·실제 연동 35개가 통과했다.

```powershell
.\.venv-tests\Scripts\python.exe tests/analysis_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/analysis-drag-godot
.\.venv-tests\Scripts\python.exe tests/hint_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/analysis-drag-regression-hint
& C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --headless --path godot --script res://tests/orientation_tests.gd
```

편집 전용 보드는 기존 `chess_board.gd`의 파생형이다. 공유 보드 파일과 일반 대국의 입력 처리는 수정하지 않았다. 합성 검사는 실제 보드 마우스 누름·뗌과 끌기 시작·허용·완료 훅을 호출한다. 누름만으로 원래 기물이 덮어써지지 않는지, 목록에서 기물을 복사하는지, 판의 기물을 옮기거나 목적 기물을 교체하는지, 버리기와 판 밖 놓기로 판의 기물만 지우는지 확인했다. Esc·앱 밖 취소와 같은 칸·동일 기물 놓기에서는 FEN 전체가 그대로인지 확인했다.

백·흑 시점의 칸 변환, 드래그 미리보기의 기물 텍스처, 편집 완료 후 캐슬링·앙파상·반수 초기화, 기본 배치·FEN·선택 기물·시점·대기 중 요청·분석 모드 변경에 따른 이전 끌기 무효화도 검사했다. 실제 엔진 연동 검사는 자유 편집을 마친 뒤 기존 합법 수 끌기 입력이 여전히 한 수만 적용하는지 확인하며, 상태 조회가 지연되는 동안의 선택·승격·착수 검증도 유지한다.

이 파일들의 실행 방식은 모두 headless와 합성 입력이다. OS 포인터로 미리보기를 보며 직접 끌어놓은 검증은 이 자동 검사와 별도로 기록한다. 임시 관리기·임시 대국 저장 폴더를 사용했고, 분석 조작이 기존 대국 상태와 저장 파일을 바꾸지 않았는지 확인했다. 엔진 해시와 HTTP 경로 기록은 하위 결과 JSON에 있다. `.log`를 `.txt`로 이름만 바꿔 복사했으며 `godot-sha256.json`으로 원본과 사본의 바이트 일치를 확인했다.
