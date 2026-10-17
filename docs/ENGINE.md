# 자체 대국 엔진 v0.2 → v0.5.2

v0.3은 아래 첫 대국 엔진의 구조에 자체 위치 평가와 안정화 탐색을 추가한다. [추가 원리·옵션](SEARCH_IMPROVEMENTS.md)과 [고정 조건 비교 결과](experiments/2026-10-17-position-quiescence.md)를 함께 읽는다. 아래 기물 가치 예시는 기본 원리이며 현재 기본 평가는 위치 점수도 포함한다.

v0.4는 평가를 유지하고 [프로파일링 기반 최적화·전이표](PERFORMANCE.md)를 추가한다. 이전 버전과 [속도·같은 시간 대국 결과](experiments/2026-10-17-profile-transposition.md)를 따로 측정했다.

v0.4.1은 [전이표 메모리를 재사용](HASH_MEMORY_REUSE.md)하고 검색별 기록을 세대로 격리한다. 옵션·이력이 달라져도 이전 결과를 읽지 않으며, [반복 검색 비용과 같은 시간 대국](experiments/2026-10-17-hash-memory.md)을 별도로 검증했다.

v0.5.0은 [패배 분석 기반 탐색 개선](LOSS_GUIDED_SEARCH.md)으로 비캡처 체크를 제한적으로 연장하고 killer/history 일반 수 정렬을 추가한다. [검증·같은 시간 비교](experiments/2026-10-17-loss-guided.md)를 개발/보류 표본으로 나눠 기록한다.

v0.5.1 개발 후보는 [PVS](PVS.md)를 추가한다. 후속 후보를 좁은 범위로 먼저 검사하고 개선 후보를 다시 탐색한다. UCI `PVS` 옵션으로 켜고 끌 수 있으며 평가·안정화·규칙·전이표 격리 정책은 유지한다. [계획](experiments/2026-10-18-pvs-plan.md)은 구현 전에 고정했고 [비용·100ms·1초 결과](experiments/2026-10-18-pvs.md)를 구분해 기록했다. 1초 경기력 향상은 확인하지 못해 현재 안정 설치본은 보존한다.

v0.5.2 개발 후보는 [SEE 교환 손익 추정](SEE.md)을 수 정렬에만 추가한다. 전이표/직전 PV 다음으로 손해 없는 교환을 우선하고 불리한 교환도 읽는다. UCI `UseSEE`, 콘솔 `--no-see`로 끌 수 있다. [비교 계획](experiments/2026-10-18-see-plan.md)을 구현 전에 고정했고, [첫 비용·대국 결과](experiments/2026-10-18-see.md)를 보존했다. 이후 [새 위치·외부 CPU 증가 0의 재현 비교](experiments/2026-10-18-see-replication.md)는 100ms 7승 5무 4패, 1초 3승 6무 7패여서 경기력 개선을 일관되게 재현하지 못했다. 계산 비용과 일반 강도를 구분한다.

## 원리와 작은 체스 예

**Regicide가 자체 규칙으로 합법 수를 만들고, 기물 가치와 상대의 응수를 계산해 수를 고른다.** Python 도구는 입출력·검증·기보 저장을 담당한다.

기본 점수는 폰 100, 나이트 320, 비숍 330, 룩 500, 퀸 900이다. 킹의 가치는 기물 합산에 넣지 않고 체크메이트를 별도로 처리한다. 단위는 센티폰이며 100이 폰 하나다. 이 값들은 첫 기준 구현의 설계값으로, 통계적으로 최적화한 결과가 아니다.

예를 들어 백 퀸 d1, 흑 룩 d5, 흑 폰 e6이 있으면 퀸으로 룩을 잡아 당장 500점을 얻을 수 있다. 하지만 흑 폰이 d5의 퀸을 되잡으면 손해다. 깊이 1은 백의 수 뒤의 기물만 세므로 이 함정을 놓칠 수 있다. 깊이 2는 흑의 재캡처까지 읽어 그 수를 피한다. 이 예는 자동 테스트에 들어 있다.

각 위치에서는 **현재 둘 차례인 쪽에 유리하면 양수**를 준다. 내 수를 둔 다음에는 상대 차례이므로 상대가 받은 점수의 부호를 뒤집는다. 이 방식이 negamax이며, 양쪽 모두 자기에게 가장 좋은 수를 고른다고 가정한다.

Alpha-beta는 현재 최선의 선택을 바꿀 수 없는 응수들을 건너뛴다. 이미 백에게 +100을 보장하는 후보가 있는데 다른 후보에서 흑이 백을 -500으로 만드는 응수를 찾았다면, 흑은 그 응수를 선택할 수 있다. 그러면 그 후보는 기존 +100을 개선할 수 없으므로 나머지 흑 응수 계산을 생략한다. 작은 포지션에서는 가지치기 없는 완전 탐색과 최종 점수가 같은지 비교한다.

## 구현의 선택과 경계

| 구성 | 구현 |
| --- | --- |
| 평가 | 기물 가치 + 중앙화·전개·국면별 위치 점수, 현재 둘 차례 기준의 부호 |
| 탐색 | negamax와 alpha-beta, 깊이 1..64 |
| 중단 복구 | 깊이 1부터 반복 심화; 마지막으로 끝난 깊이의 수·점수·주요 수순 유지 |
| 안정화 탐색 | 깊이 끝의 잡기·모든 승격 및 경로당 비캡처 체크 1회 연장; 체크 중에는 모든 회피 수를 탐색 |
| 수 순서 | 전이표·직전 완료 최선 수, SEE 0 이상 캡처·승격, killer/history 일반 수, SEE 음수 캡처·승격; 동점은 좌표 순서로 고정 |
| 재사용 | 기본 16 MiB 전이표 버퍼를 세션에서 유지, 검색별 새 기록; 위치·반복 맥락·50수 카운터와 동일 남은 깊이를 확인 |
| 종료 | 메이트와 무승부를 깊이 끝의 정적 평가보다 먼저 확인; 빠른 메이트를 선호 |
| 자원 | 탐색 노드 수, 밀리초 시간, 원자적 중지 신호; 단일 탐색 스레드 |
| 상태 | 원본 보드와 전체 반복 이력의 복사본에서 탐색; 정상·중단 경로 모두 수를 되돌림 |

메이트의 내부 점수는 `30000 - 메이트까지의 반수`다. 일반 기물 점수와 구별하며 UCI/콘솔에는 메이트까지의 수로 표시한다. 메이트가 탐색 끝의 마지막 수에서 일어나도 기물 이득보다 우선한다.

노드 수에는 반복 심화의 각 루트와 미완료 깊이에서 계산한 노드도 포함한다. 노드 제한에 걸리면 그 한도를 넘어서 새 탐색 노드에 들어가지 않는다. 깊이 1도 끝내기 전에 중단되면 미리 확보한 합법 수를 반환하고 완료 깊이를 0으로 표시한다. 이 경우 점수는 검색으로 입증된 최선 수 점수가 아니다.

3회 반복·50수 청구는 선택 가능한 0점으로 반영한다. 이길 수 있는 수가 있으면 탐색에서 승리를 계속 고려한다. UCI에는 무승부를 청구하는 표준 출력 명령이 없으므로 **청구와 최종 대국 판정은 대국 관리자의 책임**이다. 기본 Python 실행 도구는 양측이 가능한 청구를 항상 하는 정책이다. 따라서 이길 기회가 있어도 청구 정책 때문에 무승부가 될 수 있다.

신경망과 학습은 아직 없다. 안정화 탐색은 잡기·승격·제한된 비캡처 체크와 체크 회피를 연장하지만 조용한 전술 전체와 수평선 문제를 해결하지는 못한다. 위치 평가·안정화·전이표·검색 전용 빠른 수 적용은 각각 꺼서 비교할 수 있다. 규칙 코어와 그 검증을 유지하며 외부 엔진의 수를 가져오지 않았다. 대국 완주와 이전 버전 대비 통제된 비교는 별도의 검증이다.

## 콘솔에서 수 고르기

Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\build\regicide.exe search 3
.\build\regicide.exe search 64 --nodes 10000
.\build\regicide.exe search 64 --movetime 200
.\build\regicide.exe search 2 --fen "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1"
```

`bestmove`가 선택한 수다. `depth`는 완전히 끝낸 탐색 깊이이며 `pv`는 그 수부터 이어지는 주요 예상 수순이다. 기본 제한은 깊이 4다. 지정한 깊이·노드·시간 중 먼저 도달하는 조건에서 탐색을 끝낸다. 종료 포지션은 `bestmove: 0000`을 출력한다.

## UCI로 대국 도구에 연결

실행 명령은 `regicide uci`다. 기존 인수 없는 실행은 규칙 코어의 초기 20수 출력을 유지한다. GUI에서 실행 파일과 함께 **인수 `uci`**를 지정한다.

지원 명령:

```text
uci
isready
setoption name Move Overhead value 20
setoption name UsePositionalEvaluation value true
setoption name UseQuiescence value true
setoption name UseTranspositionTable value true
setoption name Hash value 16
setoption name Clear Hash
setoption name UseFastMoves value true
setoption name UseQuietMoveOrdering value true
setoption name UseSEE value true
setoption name UseQuiescenceChecks value true
ucinewgame
position startpos moves e2e4 e7e5
position fen <6개 FEN 필드> moves <좌표 수순>
go depth 3
go nodes 10000
go movetime 200
go wtime 10000 btime 10000 winc 100 binc 100 movestogo 30
go infinite
stop
quit
```

탐색은 복사한 위치에서 별도 작업 스레드로 진행하며 입력은 계속 처리한다. `isready`는 탐색을 멈추지 않고 응답한다. `stop`은 합법 수를 반환하고 작업을 합류시킨다. `go infinite`는 내부 탐색이 끝나거나 루트가 종료 포지션이어도 중지 요청 전까지 최종 응답을 보류한다. 새 위치/탐색 명령은 이전 탐색을 중지하고 합류시켜 이전 작업의 출력이 새 탐색에 섞이지 않게 한다. `quit`과 입력 종료도 작업을 중지하고 프로세스를 정리한다.

깊이·노드·시간을 함께 지정하면 먼저 걸린 제한을 따른다. UCI `go`에 아무 제한이 없으면 깊이 4다. 시간 또는 노드만 지정하면 최대 깊이 64를 사용한다. 시계가 주어지면 `남은 시간 / 남은 예상 수 + 증가 시간의 75%`를 기본 할당량으로 계산하고 현재 남은 시간을 넘지 않도록 제한한다. 남은 예상 수는 `movestogo`가 없으면 30이다. `Move Overhead`(기본 20ms, 범위 0..5000ms)를 남겨 입출력 여유를 둔다. 최소 탐색 할당은 1ms이며 운영체제 스케줄링·입출력 지연까지 보장하는 실시간 타이머는 아니다.

`Hash`는 1..256 MiB이며 기본 16이다. `UseTranspositionTable`·`UseFastMoves`는 기본 true다. 같은 크기의 테이블 메모리는 세션에서 재사용하며 매 검색마다 세대를 바꿔 기록을 무효화한다. 최초 할당·크기 변경 비용도 검색 예산에 포함한다. `Clear Hash`는 값 없는 버튼이며 새 대국과 마찬가지로 버퍼를 보유한 채 기록만 무효화한다. 캐시로 차단한 하위 트리의 PV는 짧아질 수 있다.

`go searchmoves`, `go ponder`, `go mate`는 아직 지원하지 않으며 오류 설명과 `bestmove 0000`을 반환한다. Chess960, 여러 PV, Threads 옵션도 지원하지 않는다. 알 수 없는 설정은 무시한다. 잘못된 `position`은 기존 위치를 유지한다. 종료 판정에 도달한 위치는 최종 수 없이 `0000`을 반환하므로 대국 관리자는 먼저 종료 여부를 확인해야 한다.

## 실제 대국과 기보

Python 3.11 이상에서 선택적 도구 의존성을 설치한다.

```powershell
python -m venv .venv-tests
.\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv-tests\Scripts\python.exe scripts/play_game.py build/regicide.exe --depth 2 --games 2 --pgn games/selfplay.pgn
```

시간을 사용하는 대국은 `--depth 2` 대신 `--movetime 100`을 지정한다. 노드를 고정하려면 `--nodes 1000`을 쓴다. `--draw-policy automatic`은 청구 가능한 무승부를 청구하지 않고 자동 종료 규칙까지 진행하는 정책이다. `--fen`으로 시작 위치를 지정할 수 있다.

백과 흑은 별도의 Regicide 프로세스다. Python의 `chess==1.11.2`는 전체 수순을 UCI로 전달하고 반환된 수를 독립적으로 검사한다. Python은 어느 수를 둘지 결정하지 않는다. 각 수 전후에 종료를 확인하고 PGN과 JSON에 수순·시작/최종 FEN·결과·종료 이유·실행 제한·실행 파일 SHA256·소스 체크아웃 버전·환경을 저장한다. 파일은 엔진 종료 후 게시하며, 기존 파일은 `--overwrite`가 있어야 바꾼다.

`--max-plies`는 안전장치다. 규칙에 따른 종료 전에 이 한도에 도달하면 결과를 `*`로 저장하고 종료 코드 2를 반환한다. 엔진 오류·불법 수는 오류와 미완료 증거를 남기고 실패한다. 무승부로 바꿔 완주 처리하지 않는다.

깊이/노드 제한과 동일 실행 파일은 결정적 수순 비교에 적합하다. 시간 제한은 실행 환경에 따라 수순이 달라질 수 있다. JSON의 소스 버전은 실행 도구가 있는 체크아웃을 가리킨다. 임의의 외부 실행 파일이 그 소스에서 빌드됐음을 증명하지 않으므로, 저장하는 기준 실험은 소스를 커밋한 뒤 해당 소스에서 재빌드하고 실행 파일 해시와 함께 기록한다.

## 검증 재현과 외부 자료

기존 perft/특수 규칙 검증을 수정하지 않고 C++ 탐색 테스트와 실제 프로세스 검증을 추가했다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\.venv-tests\Scripts\python.exe tests/differential.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/engine_integration.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/game_runner_tests.py build/regicide.exe
```

Linux에서는 기존 CMake 빌드 후 같은 세 Python 검증에 `build/regicide`를 전달한다. CI는 Windows/Linux에서 기존 규칙 테스트와 새 탐색·UCI·대국 검증을 함께 실행한다. 실제 실행 결과는 `experiments/`와 커밋의 Actions 결과를 확인한다.

확인일: 2026-10-17. 프로토콜은 원저자 Shredder의 [UCI 안내](https://www.shredderchess.com/chess-features/uci-universal-chess-interface.html)에서 제공하는 [원본 명세](https://www.shredderchess.com/download/div/uci.zip)를 참고했다. 연결·PGN 도구는 [python-chess 1.11.2 엔진 문서](https://python-chess.readthedocs.io/en/v1.11.2/engine.html)와 해당 패키지 API를 사용한다. Python 패키지는 GPL-3.0-or-later이며 C++ 코어에 복사·링크하지 않는다. 수 선택·평가·탐색 구현은 자체 코드다. 다른 엔진의 코드·모델·기보 학습 데이터는 사용하지 않았다. v0.5.0 패배 분석에는 별도 Stockfish 16을 진단 도구로 사용했으며 출처·이용 조건과 한계는 해당 실험 기록에 남겼다.
