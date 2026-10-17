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

C++20 규칙 코어에 자체 기물 위치 평가와 안정화 탐색을 추가한 v0.3 기준 구현입니다. Regicide가 직접 수를 선택하고 UCI로 대국하며, 별도 Python 도구가 합법성·종료를 검증해 PGN/JSON을 저장합니다. 기존 규칙 검증은 그대로 유지합니다. 이전 v0.2와 같은 1,000노드/수 조건에서 13승 3무 0패, 40ms/수 조건에서 10승 5무 1패를 기록했습니다. 각 조건 8개 고정 시작 포지션·선후공 교대 16판의 결과이며 Elo나 다른 상대에게 일반화하지 않습니다.

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
```

첫 실행은 초기 FEN과 합법 수 20개, `make_undo: OK`를 출력합니다. `perft 5`의 기대값은 **4,865,609**입니다. GUI가 없는 콘솔 프로그램입니다.

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
- UCI 클라이언트 연결, 탐색 중 준비 확인·중지·종료, 깊이·노드·시간·시계 제한.
- 초기 배치 자기대국의 실제 종료와 PGN/JSON 전체 수순 재검증; 자세한 조건은 실험 기록 참조.

**제한:** 폰 벽 등 복잡한 죽은 포지션의 일반 판정은 미구현입니다. FEN만 불러오면 이전 반복 이력은 알 수 없습니다. 안정화 탐색은 조용한 포크·메이트 위협 전체를 읽지 못하며 연장 한계가 있습니다. 같은 노드 수에서 새 버전의 시간 비용은 늘었습니다. 안정화 탐색만 켠 8판은 전부 무승부였으므로, 각 기능의 일반적인 경기력 향상까지 입증한 결과는 아닙니다.

## 문서

- [프로젝트 지침](docs/PROJECT_INSTRUCTIONS.md)
- [개발 로드맵](docs/ROADMAP.md)
- [규칙 코어 구조·실행·검증 방법](docs/RULES_CORE.md)
- [첫 대국 엔진의 원리·사용법·제한](docs/ENGINE.md)
- [2026-10-17 검증 기록](docs/experiments/2026-10-17-rules-core.md)
- [첫 엔진 검증과 실제 대국 기보](docs/experiments/2026-10-17-first-engine.md)
- [위치 평가·안정화 탐색 원리와 사용법](docs/SEARCH_IMPROVEMENTS.md)
- [v0.2 대비 64판 비교 결과와 기보](docs/experiments/2026-10-17-position-quiescence.md)
