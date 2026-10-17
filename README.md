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

C++20으로 규칙 코어의 첫 기준 버전을 구현했습니다. 초기 합법 수 20개, FEN 입출력, 수 적용·되돌리기, 체크와 특수 수를 처리합니다. 자체 탐색·평가·시간 관리·UCI 대국 연결은 다음 단계입니다. Python은 독립적인 검증 도구에만 사용합니다.

## 바로 실행하기

Windows에서 Visual Studio의 **C++를 사용한 데스크톱 개발** 구성 요소가 필요합니다. 저장소 폴더에서 실행합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\build\regicide.exe
.\build\regicide.exe perft 5
.\build\regicide.exe inspect --play e2e4 e7e5 g1f3
```

첫 실행은 초기 FEN과 합법 수 20개, `make_undo: OK`를 출력합니다. `perft 5`의 기대값은 **4,865,609**입니다. GUI가 없는 콘솔 프로그램입니다.

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

**제한:** 킹만 남음, 킹과 단일 비숍/나이트 대 킹, 모든 비숍이 같은 색 칸에 있는 비숍 전용 배치의 죽은 포지션을 판정합니다. 폰 벽 등 복잡한 죽은 포지션의 일반 판정은 미구현입니다. FEN만 불러오면 이전 반복 이력은 알 수 없습니다. 현재 결과는 규칙 검증이며 경기력이나 Elo 측정 결과가 아닙니다.

## 문서

- [프로젝트 지침](docs/PROJECT_INSTRUCTIONS.md)
- [개발 로드맵](docs/ROADMAP.md)
- [규칙 코어 구조·실행·검증 방법](docs/RULES_CORE.md)
- [2026-10-17 검증 기록](docs/experiments/2026-10-17-rules-core.md)
