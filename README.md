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

C++20 자체 엔진 v0.4.1입니다. 위치 평가·안정화 탐색과 전이표에 **세션별 전이표 메모리 재사용**을 추가했습니다. 각 검색은 새 세대로 기록을 격리하므로 옵션·반복 이력이 달라져도 이전 점수가 섞이지 않습니다. Regicide가 직접 수를 선택하고 Python 도구가 합법성·종료·기보를 검증합니다. 기존 규칙 검증을 유지합니다.

이전 v0.4.0과 10개 위치를 비교해 준비 검색 후 깊이 1 요청 시간은 중앙 **3.72배**, 깊이 3은 **1.37배** 빨랐습니다. 전체 250쌍에서 수·점수·노드·PV·TT 작업량이 같았고 준비 후 재할당은 없었습니다. 속도와 같은 시간 대국 성적은 [새 실험 기록](docs/experiments/2026-10-17-hash-memory.md)에서 구분합니다. 작은 개발 표본을 Elo나 외부 상대 경기력으로 일반화하지 않습니다. 앞선 [v0.4.0 최적화 실험](docs/experiments/2026-10-17-profile-transposition.md)도 보존합니다.

같은 시간 대국은 40ms 16판 **5승 6무 5패**, 100ms 전체 재측정 16판 **4승 5무 7패**로 경기력 향상은 확인하지 못했습니다. 첫 100ms 묶음의 유휴 절전 중단·미완료 기록과 사전 재측정 계획도 함께 공개합니다.

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
- 위치·전체 반복 맥락·50수 카운터를 구분하는 전이표, 경계값·메이트 거리·충돌·중단·검색 격리와 TT 켬/끔 점수 일치.
- 메모리 재사용·세대 순환·크기 변경·옵션·이력 전환 후 새 컨텍스트와 수·점수·전체 탐색 동작 일치.
- UCI 클라이언트 연결, 탐색 중 준비 확인·중지·종료, 깊이·노드·시간·시계 제한.
- 초기 배치 자기대국의 실제 종료와 PGN/JSON 전체 수순 재검증; 자세한 조건은 실험 기록 참조.

**제한:** 폰 벽 등 복잡한 죽은 포지션의 일반 판정은 미구현입니다. FEN만 불러오면 이전 반복 이력은 알 수 없습니다. 안정화 탐색은 조용한 포크·메이트 위협 전체를 읽지 못하며 연장 한계가 있습니다. 전이표의 점수는 검색 사이에 공유하지 않고 안정화 점수는 캐시하지 않습니다. 최초 사용·Hash 크기 변경에는 할당이 필요합니다. 속도 개선이 일반적인 경기력 우위를 보장하지 않으며, 더 넓은 보류 표본·강한 외부 상대 검증은 남아 있습니다.

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
