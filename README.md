# Regicide

독학으로 체스와 인공지능의 원리를 익히고, 자체 체스 엔진을 개발하는 장기 연구개발 프로젝트입니다.

## 목표

- 체스 규칙을 정확히 처리하고 끝까지 대국할 수 있는 자체 엔진을 구현합니다.
- 탐색·평가·학습 기법을 연구하며 세계 정상급 경기력에 도전합니다.
- 엔진의 동작을 이해하고, 변경 효과를 재현 가능한 실험으로 검증합니다.

## 개발 원칙

- 실제 수를 선택하는 핵심 엔진을 자체 개발합니다.
- 외부 자료의 출처와 이용 조건, 주요 설계 결정, 실험 조건과 결과를 기록합니다.
- 성능은 통제된 대국 조건과 충분한 표본을 바탕으로 평가합니다.

## 현재 상태

C++20 자체 엔진 v0.5.0입니다. 이전 100ms 패배 기보를 분석해 **비캡처 체크의 제한적 안정화 연장과 일반 수의 killer/history 정렬**을 추가했습니다. [원리·작은 체스 예·옵션](docs/LOSS_GUIDED_SEARCH.md)을 설명합니다. 규칙 코어와 전이표 메모리 재사용·검색별 기록 격리를 유지하며 Regicide가 직접 수를 고릅니다.

[사전 계획](docs/experiments/2026-10-17-loss-guided-plan.md)에 따라 v0.4.1과 같은 100ms/수로 비교했습니다. **새 시작 위치 16판은 10승 3무 3패**, 기존 개발 위치 16판은 **4승 4무 8패**였습니다. [검증·기보·결과](docs/experiments/2026-10-17-loss-guided.md)에 계산 비용 증가와 상반된 성적을 함께 남겼습니다. 주 비교의 이 표본에서는 개선됐으나 일반적인 우위·Elo를 확정하지 않습니다.

이전 [v0.4.1 실험](docs/experiments/2026-10-17-hash-memory.md)의 100ms 전체 재측정은 4승 5무 7패였습니다. 전이표 메모리 재사용의 비용 감소와 경기력 결과, 유휴 절전으로 중단된 원본·재측정 근거도 보존합니다.

## 최신 엔진과 직접 대국하기

Windows에서는 **`Play-Regicide-Godot.cmd`를 더블클릭**하면 Godot 대국 창이 열립니다. **시점 뒤집기** 버튼 또는 **F** 키로 언제든 백·흑 시점을 바꿀 수 있습니다. 내가 두는 색과 시점은 독립적이며, 엔진이 생각하는 중에도 전환됩니다. 클릭·드래그 이동, 백·흑 선택, 생각 시간(0.1·1·3·5초), 기보 저장·**무르기**를 지원합니다. **Regicide 수 바꾸기**로 과거 엔진 수를 사용자가 지정한 합법 수로 바꾸고 새 수순을 이어가는 핸디캡 연습도 가능합니다. [Godot 실행·검증 안내](docs/GODOT_PLAY.md)를 참고합니다.

기존 **`Play-Regicide.cmd`**의 브라우저 대국도 계속 사용할 수 있습니다. [브라우저 실행 안내](docs/HUMAN_PLAY.md)에 설명합니다.

Windows 바탕화면 바로가기는 `scripts/install_desktop_shortcut.ps1`로 만들 수 있습니다. 자체 Regicide 왕 아이콘을 사용하며 같은 최신 Godot 대국을 엽니다.

## 바로 실행하기

Windows에서 Visual Studio의 **C++를 사용한 데스크톱 개발** 구성 요소가 필요합니다. 저장소 폴더에서 실행합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\build\regicide.exe
.\build\regicide.exe perft 5
.\build\regicide.exe inspect --play e2e4 e7e5 g1f3
.\build\regicide.exe search 3
.\build\regicide.exe search 64 --movetime 200
.\build\regicide.exe search 2 --no-position --no-quiescence
.\build\regicide.exe search 4 --no-tt
```

첫 실행은 초기 FEN과 합법 수 20개, `make_undo: OK`를 출력합니다. `perft 5`의 기대값은 **4,865,609**입니다. 핵심 엔진은 콘솔 프로그램이며, 위의 로컬 체스판으로 직접 대국할 수 있습니다.

직접 대국을 진행하고 기보를 저장하려면 선택적 Python 도구 의존성을 설치합니다. `python`은 3.11 이상이 필요합니다.

```powershell
python -m venv .venv-tests
.\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv-tests\Scripts\python.exe scripts/play_game.py build/regicide.exe --depth 2 --pgn games/selfplay.pgn
```

백·흑 모두 별도의 Regicide 프로세스가 수를 고릅니다. 기본 정책은 가능한 3회 반복·50수 무승부를 청구하는 것입니다. 종료 판정에 도달하면 PGN과 재현용 JSON을 저장하며, 수 제한 때문에 중단되면 미완료 `*`로 기록합니다. 기존 출력은 `--overwrite` 없이는 덮어쓰지 않습니다.

UCI를 지원하는 GUI에는 실행 파일과 인수 `uci`를 등록합니다. 명령줄에서는 `.\build\regicide.exe uci`로 시작할 수 있습니다. 지원 명령·제한은 [엔진 설명](docs/ENGINE.md)을 참고합니다.

CMake 3.20 이상과 C++20 컴파일러가 있으면 Linux에서도 빌드할 수 있습니다.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
./build/regicide
```

Visual Studio CMake 생성기를 사용한 Windows 빌드는 실행 파일이 `build/Release/`에 생성됩니다.

## 검증된 범위

- 초기 배치와 5개 추가 기준 포지션의 perft 및 전체 상태 복원 검증.
- 캐슬링의 경로·공격·권리 상실, 앙파상의 킹 노출·유효 기간, 4종 승격과 잡으며 승격.
- 체크메이트·스테일메이트, 현재/예정 수에 대한 3회 반복·50수 청구 가능 여부, 5회 반복·75수 자동 무승부.
- 테스트 전용 외부 라이브러리 `chess==1.11.2`와 2,997개 사례 교차 검증.
- 기물 획득·메이트·한 수 뒤 재캡처, alpha-beta와 작은 완전 탐색의 점수 일치, 중단 후 상태 복원.
- 중앙화·전개·국면별 킹 위치 평가의 대칭성과 안정화 탐색의 캡처·승격·체크 회피·공유 노드 예산.
- 비캡처 체크의 제한 연장·특수 수 체크 감지·상태 보존, 일반 수 정렬 켬/끔 점수 일치와 검색별 기록 초기화.
- 위치·전체 반복 맥락·50수 카운터를 구분하는 전이표, 경계값·메이트 거리·충돌·중단·검색 격리와 TT 켬/끔 점수 일치.
- 메모리 재사용·세대 순환·크기 변경·옵션·이력 전환 후 새 컨텍스트와 수·점수·전체 탐색 동작 일치.
- UCI 클라이언트 연결, 탐색 중 준비 확인·중지·종료, 깊이·노드·시간·시계 제한.
- 초기 배치 자기대국의 실제 종료와 PGN/JSON 전체 수순 재검증; 자세한 조건은 실험 기록 참조.

**제한:** 폰 벽 등 복잡한 죽은 포지션의 일반 판정은 미구현입니다. FEN만 불러오면 이전 반복 이력은 알 수 없습니다. 안정화 탐색은 비캡처 체크를 경로당 한 번 추가로 읽으며, 체크가 아닌 조용한 포크·메이트 위협 전체를 읽지 못합니다. 전이표의 점수는 검색 사이에 공유하지 않고 안정화 점수는 캐시하지 않습니다. 최초 사용·Hash 크기 변경에는 할당이 필요합니다. 속도 개선이 일반적인 경기력 우위를 보장하지 않으며, 더 넓은 보류 표본·강한 외부 상대 검증은 남아 있습니다.

## 문서

- [프로젝트 지침](docs/PROJECT_INSTRUCTIONS.md)
- [개발 로드맵](docs/ROADMAP.md)
- [규칙 코어 구조·실행·검증 방법](docs/RULES_CORE.md)
- [첫 대국 엔진의 원리·사용법·제한](docs/ENGINE.md)
- [2026-10-17 검증 기록](docs/experiments/2026-10-17-rules-core.md)
- [첫 엔진 검증과 실제 대국 기보](docs/experiments/2026-10-17-first-engine.md)
- [위치 평가·안정화 탐색 원리와 사용법](docs/SEARCH_IMPROVEMENTS.md)
- [v0.2 대비 64판 비교 결과와 기보](docs/experiments/2026-10-17-position-quiescence.md)
- [프로파일링·전이표 원리와 사용법](docs/PERFORMANCE.md)
- [v0.3 대비 속도 비교·같은 시간 32판 결과](docs/experiments/2026-10-17-profile-transposition.md)
- [전이표 메모리 재사용과 세대별 기록 격리](docs/HASH_MEMORY_REUSE.md)
- [v0.4.0 대비 짧은 검색 비용·같은 시간 대국 결과](docs/experiments/2026-10-17-hash-memory.md)

- [패배 분석 기반 탐색 개선의 원리·옵션](docs/LOSS_GUIDED_SEARCH.md)
- [100ms 패배 분석·기존/새 시작 위치 비교](docs/experiments/2026-10-17-loss-guided.md)

- [최신 Regicide와 직접 대국하기](docs/HUMAN_PLAY.md)
- [Godot 대국과 자유로운 백·흑 시점 전환](docs/GODOT_PLAY.md)
- [Godot 화면·입력·실제 엔진 연결 검증](docs/experiments/2026-10-17-godot-play.md)
- [무르기·핸디캡 수 교체와 새 수순 탐색 검증](docs/experiments/2026-10-18-handicap-play.md)
