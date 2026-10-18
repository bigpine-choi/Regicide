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

v0.6.1은 **자가대국 → 패배 분석 → 자료 누적 → 재학습 → 이전 채택 모델과 대국 검증 → 기준 통과 모델만 채택**을 자동으로 연결합니다. `Learn-Regicide.cmd`와 바탕화면 **Regicide - Collect and Learn**은 기본3회 순환을 실행합니다. 검증된 모델만 다음 기본 대국에 적용하며, 후보가 실패하면 기존 모델을 유지합니다. [원리·실행·채택 기준](docs/CONTINUAL_LEARNING.md), [사전 계획](docs/experiments/2026-10-18-learning-cycle-plan.md)을 참고합니다. 채택 신경망이 없으면 기존 수식 평가로 시작하며, 개인 대국 기보를 자동으로 학습에 넣지는 않습니다.

v0.6.0은 **자가대국 자료 수집과 작은 자체 신경망 평가의 첫 연구 버전**입니다. 자체32판에서588위치를 모아 학습하고 C++ 추론과 연결했습니다. 첫 모델은 최종 보류 시험 오차가 악화돼 **실험용 선택 기능**으로 설치하며 기본 대국은 기존 수식 평가를 유지합니다. [원리·학습과 실행 방법](docs/NEURAL_EVALUATION.md), [자료·검증·한계](docs/checks/2026-10-18-neural-v1/README.md)를 참고합니다. `Learn-Regicide.cmd`는 새 자료 수집/학습을, `Play-Regicide-Learned.cmd`는 포함된 실험 모델과 대국을 실행합니다. 자동으로 계속 강해지는 운영 시스템은 후속 과제입니다.

브라우저 부하를 제거한 [v0.5.7/v0.5.8 재대국](docs/experiments/2026-10-18-lmr-idle-rematch.md)은 같은200ms16판에서 **v0.5.8 4승7무5패(46.875%)**였습니다. 일반 브라우저 프로세스·CPU 증가0을 관측했지만, 이번 소표본에서도 LMR의 기력 향상은 확인하지 못했습니다. 도중 브라우저가 실행된 첫 시도는 사전 기준대로 제외하고 별도 보존했습니다.

v0.5.8에서는 **보수적인 LMR**로 뒤쪽 일반 기물 수를 한 반수 얕게 먼저 읽고, 유망하면 원래 깊이로 다시 확인합니다. 체크·포획·승격·폰/킹 이동과 주요 후보는 보호합니다. [원리·작은 예·설정](docs/LMR.md), [사전 계획](docs/experiments/2026-10-18-lmr-plan.md)과 [검증·비용·대국 기록](docs/experiments/2026-10-18-lmr.md)을 참고합니다. LMR OFF90쌍은 기존 검색과 같았고 깊이6 표본의 시간은 약18% 줄었습니다. 200ms16판은 **4승6무6패**로 기력 향상을 확인하지 못했습니다. 소표본과 대국 중 큰 브라우저 부하의 한계를 함께 기록합니다.

v0.5.7은 **통과폰·폰 구조의 점수를 유지하면서 반복 계산을 줄였습니다.** 파일별 감점·보너스와 전방 조건을 압축된 작은 표에서 조회하고, 기물의 색·실제 칸에 따른 위치 점수도 미리 계산합니다. [최적화 계획](docs/experiments/2026-10-18-pawn-opt-plan.md)과 [동값·비용·검증 기록](docs/experiments/2026-10-18-pawn-opt.md)에 평가 함수와 전체 탐색의 효과를 구분합니다.

v0.5.6은 **통과폰·겹친 폰·고립된 폰**을 평가에 추가했습니다. 통과폰은 승격에 가까울수록, 특히 종반에 더 높게 평가하고 바로 앞이 막히면 보너스를 줄입니다. [원리·설정](docs/PAWN_STRUCTURE.md), [개발 계획](docs/experiments/2026-10-18-pawn-structure-plan.md)과 [검증·비용 기록](docs/experiments/2026-10-18-pawn-structure.md)을 참고합니다. UCI `UsePawnStructure` 또는 콘솔 `--no-pawn-structure`로 새 항목만 끌 수 있으며 일반 기력 향상은 아직 측정하지 않았습니다.

v0.5.5는 **왕 앞 폰 방패**의 점수를 유지하면서 기물 분류·위치 점수를 미리 계산한 표에서 조회해 평가 비용을 줄였습니다. [동값 최적화·비용·검증 기록](docs/experiments/2026-10-18-shield-opt.md)에 함수 단독과 전체 탐색의 측정을 구분합니다.

v0.5.4에서 도입한 폰 방패는 가까운 자기 폰이 사라지거나 멀어진 비용을 중반 평가에 반영합니다. 상대의 공격 기물이 줄면 영향도 줄이고, 폰 종반의 기존 평가는 유지합니다. [원리·설정](docs/PAWN_SHIELD.md)과 [최초 검증](docs/experiments/2026-10-18-pawn-shield.md)을 참고합니다. UCI `UsePawnShield` 또는 콘솔 `--no-pawn-shield`로 항목만 끌 수 있습니다. 일반 기력 향상과 Elo는 아직 측정하지 않았습니다.

**SEE**로 한 칸의 교환 손익을 추정해 유리한 캡처부터 탐색합니다. 수 정렬에만 적용하고 손해로 보이는 희생 수도 계속 읽습니다. [원리·작은 체스 예·옵션](docs/SEE.md)을 기록하며 UCI `UseSEE` 또는 콘솔 `--no-see`로 끌 수 있습니다.

[1초 패배 분석과 SEE 비용 개선](docs/experiments/2026-10-18-see-cost.md)에서 교환 값·수 순서를 유지하며 프로파일 표본의 함수 계산 시간을 쌍별 중앙값 기준 약 18~20% 줄였습니다. 고정 깊이 125쌍의 탐색 결과는 같았지만 전체 시간 차이는 작고 혼재됐습니다. 수정 전 SEE v0.5.2와 새 위치에서 비교한 **100ms는 5승 3무 8패(40.625%)**, **1초는 5승 7무 4패(53.125%)**였습니다. 외부 엔진 5개의 CPU 증가 0과 기존 규칙·Godot 검증, 기보 감사를 확인했으며 일관된 경기력 개선과 안정판 승격은 보류합니다.

[새 시작 위치·외부 엔진 CPU 증가 0의 재현 비교](docs/experiments/2026-10-18-see-replication.md)는 이전 v0.5.1 대비 **100ms 16판 7승 5무 4패(59.375%)**, **1초 16판 3승 6무 7패(37.5%)**였습니다. 경기력 개선은 두 조건에서 일관되게 재현되지 않았습니다. 규칙 검증과 32판 기보·CPU 기록 감사를 통과했고, SEE의 일반적인 강도 향상과 안정판 승격은 보류합니다.

[첫 SEE 개발 비교](docs/experiments/2026-10-18-see.md)에서 고정 깊이 3·4의 노드는 약 16%, 쌍별 중앙 시간은 약 3%·7% 줄었습니다. 재사용 위치의 100ms는 7승 7무 2패, 1초는 10승 4무 2패였으나 다른 사용자 엔진의 동시 부하도 관측됐습니다. 계산 비용 감소와 경기력은 구분하며, 이 기록과 원자료는 보존합니다.

v0.5.1은 **PVS**로 첫 후보를 충분히 읽고 후속 후보를 좁은 점수 범위로 검사한 뒤 필요할 때 다시 읽습니다. [원리·작은 체스 예·옵션](docs/PVS.md)을 설명합니다. PVS를 끄면 기존 탐색 경로를 사용하며 평가·안정화 범위·규칙과 전이표 격리는 유지합니다.

[사전 계획](docs/experiments/2026-10-18-pvs-plan.md)에 따른 고정 깊이 3·4의 전체 탐색 노드는 약 28%·39% 줄었고 점수 차이는 없었습니다. v0.5.0과 선후공을 바꾼 **100ms 16판은 9승 4무 3패**, **1초 16판은 6승 4무 6패**였습니다. [실험·기보·독립 감사](docs/experiments/2026-10-18-pvs.md)에 측정 초반 다른 사용자 엔진의 부하와 작은 표본의 한계도 기록했습니다. 계산 비용 감소와 일반적인 경기력 향상을 구분하며, 당시 바탕화면의 안정 대국용 v0.5.0 설치본은 유지했습니다.

v0.5.0은 [사전 계획](docs/experiments/2026-10-17-loss-guided-plan.md)에 따라 v0.4.1과 같은 100ms/수로 비교했습니다. **새 시작 위치 16판은 10승 3무 3패**, 기존 개발 위치 16판은 **4승 4무 8패**였습니다. [검증·기보·결과](docs/experiments/2026-10-17-loss-guided.md)에 계산 비용 증가와 상반된 성적을 함께 남겼습니다. 주 비교의 이 표본에서는 개선됐으나 일반적인 우위·Elo를 확정하지 않습니다.

이전 [v0.4.1 실험](docs/experiments/2026-10-17-hash-memory.md)의 100ms 전체 재측정은 4승 5무 7패였습니다. 전이표 메모리 재사용의 비용 감소와 경기력 결과, 유휴 절전으로 중단된 원본·재측정 근거도 보존합니다.

## 세계 정상급 인간 상대 도전: 다음 개발

[개발 순서와 완료 기준](docs/HUMAN_TOP_LEVEL_PLAN.md)을 정리했습니다. 첫 과제는 [외부 UCI 엔진 기준평가](docs/EXTERNAL_BASELINE.md)로, 상대의 독립 출처·실행 인자·파일 해시와 양측 설정을 기록합니다. 실제 외부 대국 성적과 경기력 상승은 아직 측정하지 않았습니다.

## 최신 엔진과 직접 대국하기

대국 중 **추천 수: 끔/켬** 버튼으로 내 다음 수에 대한 도움을 켜고 끌 수 있습니다. 자체 Regicide의 추천을 체스판과 문구로 표시하고, 착수는 사용자가 직접 선택합니다. [추천 수 사용법](docs/MOVE_HINTS.md)에 동작과 한계를 설명합니다.

Windows에서는 **`Play-Regicide-Godot.cmd`를 더블클릭**하면 Godot 대국 창이 열립니다. **시점 뒤집기** 버튼 또는 **F** 키로 언제든 백·흑 시점을 바꿀 수 있습니다. 내가 두는 색과 시점은 독립적이며, 엔진이 생각하는 중에도 전환됩니다. 클릭·드래그 이동, 백·흑 선택, 생각 시간(0.1·1·3·5초), 기보 저장·**무르기**를 지원합니다. **Regicide 수 바꾸기**로 과거 엔진 수를 사용자가 지정한 합법 수로 바꾸고 새 수순을 이어가는 핸디캡 연습도 가능합니다. [Godot 실행·검증 안내](docs/GODOT_PLAY.md)를 참고합니다.

기존 **`Play-Regicide.cmd`**의 브라우저 대국도 계속 사용할 수 있습니다. [브라우저 실행 안내](docs/HUMAN_PLAY.md)에 설명합니다.

Windows 바탕화면 바로가기는 `scripts/install_desktop_shortcut.ps1`로 만들 수 있습니다. 자체 Regicide 왕 아이콘을 사용하며 같은 최신 Godot 대국을 엽니다.

사용자 대국은 **문서 폴더의 `Regicide/games/`**에 매 수 자동 보관합니다. **내 대국 기보**에서 완료·미완료·핸디캡 기록을 모아 보고 한 수씩 다시 재생할 수 있습니다. 기존 기보도 원본을 보존하면서 가져옵니다. [기보 보관·재생 검증](docs/experiments/2026-10-18-game-library.md)에 조건과 결과를 기록합니다.

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
.\build\regicide.exe search 4 --no-see
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
- SEE의 핀·왕 안전·공격선·앙파상·승격·독립 LVA 비교, 음수 교환의 메이트 희생 수 보존.
- 중앙화·전개·국면별 킹 위치 평가의 대칭성과 안정화 탐색의 캡처·승격·체크 회피·공유 노드 예산.
- 비캡처 체크의 제한 연장·특수 수 체크 감지·상태 보존, 일반 수 정렬 켬/끔 점수 일치와 검색별 기록 초기화.
- 위치·전체 반복 맥락·50수 카운터를 구분하는 전이표, 경계값·메이트 거리·충돌·중단·검색 격리와 TT 켬/끔 점수 일치.
- 메모리 재사용·세대 순환·크기 변경·옵션·이력 전환 후 새 컨텍스트와 수·점수·전체 탐색 동작 일치.
- LMR OFF의 기존 탐색 동등성, ON의 보호 조건·감축/정상 깊이 재검사·중단 복원과 미검증 혼합 깊이 결과의 TT 저장 방지.
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
