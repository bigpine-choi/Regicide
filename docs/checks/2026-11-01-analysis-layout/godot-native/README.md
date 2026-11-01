# 화면 배치 수정 후 기존 기능 검증

2026-11-01, Godot 4.7.2와 Regicide 0.7.0-dev `build/regicide.exe`를 사용했다. 분석 합성 88개, Viewport 입력 디스패처 44개, 실제 엔진 연동 71개, 정상 대국 흐름 캐슬링 입력 114개가 모두 통과했다. 캐시 없는 새 복사본 실행도 통과했다.

```powershell
.\.venv-tests\Scripts\python.exe tests/analysis_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/analysis-layout-native
```

보드 왼쪽 열은 `AnalysisBoardToolbar`의 한 줄 차례·시점 전환, `AnalysisEvaluationSummary`의 한 줄 점수, 늘어나는 `AnalysisBoardRow`만 가진다. 차례 줄의 최소 높이는 36, 점수 줄은 26이며 줄바꿈과 여러 줄 표시를 막고 넘치는 글자를 생략한다. 캐슬링 버튼·불가 사유·평가 상세는 `AnalysisSidebarScroll` 안으로 옮겼다. 오른쪽 영역이 콘텐츠 넘침을 처리하므로 긴 텍스트가 판의 가로 공간을 가져가지 않는다. 판의 크기는 고정 픽셀로 지정하지 않고 창 크기에 따라 남은 영역을 사용한다.

이 파일들은 headless·합성 입력으로 기존 기능을 확인한 기록이다. 화면 좌표 불변식과 실제 OS 창 조작의 증거는 이 폴더 밖의 별도 배치 검증 기록과 구분한다. 실행 후 마지막으로 중복된 도움말 줄을 없애고 판의 툴팁으로 옮겼으며, 현재 소스의 Viewport 입력 44개를 다시 확인해 통과했다. 개인 대국 대신 임시 관리기·저장 폴더를 사용했고 기존 대국 상태와 저장 바이트가 바뀌지 않았다.

원본 `.log`의 확장자만 `.txt`로 바꿨으며 내용의 SHA-256 일치를 `sha256.json`에 기록했다. 엔진 해시·HTTP 경로 기록은 `analysis-integration.json`에 있다.
