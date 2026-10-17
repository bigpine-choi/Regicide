# 규칙 코어 첫 기준 버전

## 원리 → 작은 예 → 구현 → 검증

**합법 수는 기물의 이동 방식과 자기 킹의 안전성을 모두 만족하는 수다.**

예를 들어 e1에 백 킹, e2에 백 룩, e8에 흑 룩이 있으면 e2 룩은 가로로 움직일 수 있어 보인다. 그러나 움직이면 e파일이 열려 백 킹이 체크에 놓인다. 따라서 그런 수는 합법 수가 아니다.

`src/board.cpp`는 먼저 기물별 이동 후보를 만든다. 후보를 임시로 적용하고 자기 킹이 공격받는지 검사한 다음, 원래 상태로 되돌린다. 통과한 후보만 `legal_moves()`가 반환한다. 공격받는 칸은 기물의 기하학적 공격으로 계산한다. 핀에 걸린 상대 기물도 킹 이동과 캐슬링의 공격 판정에 포함한다.

`perft(depth)`는 가능한 합법 수를 depth번 이어 간 **경로 수**다. 백의 첫 수 20개와 흑의 응수 각 20개로 깊이 2는 400이다. 깊이가 깊어지면 캡처·체크·특수 수가 섞이므로 기준값과의 일치는 더 많은 규칙을 검증한다. 좋은 수를 고르는 능력이나 경기력은 측정하지 않는다.

## 자료와 파일 구조

| 파일 | 역할 |
| --- | --- |
| `include/regicide/board.hpp` | `Move`, `Position`, `Board`, 종료/무승부 조회 API |
| `src/board.cpp` | 자체 FEN 처리, 이동·공격·합법성·특수 규칙·상태 이력 |
| `src/main.cpp` | 콘솔 실행, perft/divide, 검사 도구용 batch 입출력 |
| `tests/rules_tests.cpp` | 외부 의존성 없는 C++ 검증 |
| `tests/differential.py` | 외부 기준과 비교하는 선택적 Python 검증 |

보드는 64칸 배열이다. a1=0, h1=7, a8=56, h8=63이며 백은 대문자, 흑은 소문자, 빈칸은 `.`이다. 첫 버전은 비트 연산 최적화보다 상태를 직접 읽고 디버깅하기 쉬운 구성을 택했다. 비트보드는 향후 프로파일링 근거가 있을 때 검토한다.

수는 `e2e4`, `a7a8q`처럼 출발·도착 칸과 선택적 승격 기물을 기록한다. 캐슬링은 `e1g1`/`e1c1` 또는 흑의 대응 좌표다. SAN 기보 표기와 UCI 대국 프로토콜 전체는 아직 지원하지 않는다.

FEN은 판 배치뿐 아니라 둘 차례, 캐슬링 권리, 앙파상 칸, 반수 카운터, 수 번호를 기록하는 6개 필드의 표기다. 반수는 한쪽의 수 1번이며, 폰 이동이나 캡처가 발생하면 반수 카운터가 0이 된다. FEN 출력은 실제 앙파상 캡처 가능성과 관계없이 직전 두 칸 폰 이동의 통과 칸을 남기는 원래 FEN 관례를 사용한다.

`play()`는 합법 수인지 확인하고 변경 전 `Position` 전체를 저장한다. `undo()`는 저장된 배치·차례·캐슬링·앙파상·두 카운터와 반복 이력을 복원한다. 작은 상태의 전체 복사로 시작해, 추후 탐색 측정에서 병목이 확인되면 변경분만 저장하는 방식을 비교한다. 테스트는 FEN뿐 아니라 내부 이력까지 `Board` 전체 동등성으로 검사한다.

## 종료와 무승부 API의 범위

| API | 의미 |
| --- | --- |
| `outcome()` | 체크메이트/스테일메이트, 인식한 죽은 기물 구성, 75수/5회 반복 자동 무승부 |
| `claims_now()` | 현재 위치에서 50수 또는 3회 반복을 청구할 수 있는지 |
| `claims_after(move)` | 지정한 합법 수를 두겠다고 선언할 때 성립하는 청구 가능 여부; 원본 상태 유지 |
| `repetition_count()` | 불러온 FEN부터 기록한 현재 위치의 등장 횟수 |

현재 차례·기물 배치·캐슬링 권리·**합법적인** 앙파상 가능성이 같아야 같은 위치로 센다. 앙파상 칸이 있어도 캡처가 불가능하거나 핀 때문에 불법이라면 반복 위치 구분에 사용하지 않는다. 반수/수 번호는 위치 구분에서 제외한다.

3회 반복과 50수는 자동 종료가 아니다. 실제 대국 관리자는 해당 플레이어의 청구를 처리해야 한다. 5회 반복과 75수는 자동 종료이며, 150번째 반수에 체크메이트가 발생하면 체크메이트를 우선한다. FEN에는 전체 수순이 없으므로 불러오기 전의 반복 횟수는 복원할 수 없다.

`Board`는 분석용 위치 객체다. `legal_moves()`, `play()`, `perft()`는 자동 무승부 이후에도 기하학적으로 합법적인 수를 처리할 수 있다. **실제 대국 루프는 매 수 후 `outcome()`을 확인해 종료해야 한다.** perft는 무승부에서 중단하지 않는 것이 기준값 비교의 전제다.

죽은 포지션은 어느 쪽도 어떤 합법 수순으로도 체크메이트할 수 없는 위치다. 현재 확실하게 판정하는 범위는 킹 대 킹, 킹+단일 비숍/나이트 대 킹, 킹과 같은 색 칸 비숍만 있는 배치다. 2나이트 대 킹, 서로 반대색 비숍, 비숍+나이트를 잘못 자동 무승부로 만들지 않는다. **폰 벽 등 복잡한 죽은 포지션의 일반 판정은 미구현이다.** 이 부분을 포함한 FIDE 경기 운영 전체의 완성으로 해석하지 않는다. 시간패·합의 무승부·기권·터치무브 등도 대국 관리자/GUI 범위다.

FEN 파서는 구조, 킹/기물 수, 백·흑 차례, 반수/수 번호, 승격 줄의 폰, 직전 차례의 킹 체크, 캐슬링 권리에 대응하는 홈 기물, 앙파상 상태의 기본 일관성을 확인한다. 카운터 입력 범위는 반수 0..1,000,000,000, 수 번호 1..1,000,000,000이다. 초기 배치부터의 도달 가능성을 모두 증명하는 역행 분석기는 아니다. 표준 체스만 지원하며 Chess960은 제외한다.

## 실행과 검증 재현

Windows의 Visual Studio C++ 도구가 설치된 PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\build\regicide.exe
.\build\regicide.exe perft 5
.\build\regicide.exe divide 3
.\build\regicide.exe inspect --fen "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2" --play e5d6
```

선택적 외부 기준 검증은 별도 Python 환경에서 실행한다.

```powershell
python -m venv .venv-tests
.\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv-tests\Scripts\python.exe tests/differential.py build/regicide.exe
```

CMake 및 C++20 컴파일러가 설치된 Linux:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
python3 -m venv .venv-tests
.venv-tests/bin/python -m pip install -r requirements-test.txt
.venv-tests/bin/python tests/differential.py build/regicide
```

Linux에서 메모리/정의되지 않은 동작을 검사하는 재현 명령:

```sh
mkdir -p build-sanitized
g++ -std=c++20 -O1 -g -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -Iinclude src/board.cpp tests/rules_tests.cpp -o build-sanitized/rules_tests
./build-sanitized/rules_tests
```

CI는 Windows/Linux에서 CMake 빌드, C++ 테스트, Python 교차 검증을 실행하도록 구성했다. 실제 실행 결과는 각 커밋의 Actions 결과를 확인한다.

## 외부 자료와 코드 구분

확인일: 2026-10-17.

- [FIDE Laws of Chess](https://handbook.fide.com/chapter/e012023): 기물 이동·킹 안전성·특수 수(3조), 종료(5조), 반복·50수 청구와 자동 종료(9조)의 규칙 참고. 조문을 코드로 직접 구현했다.
- [Stockfish 공식 perft 테스트](https://github.com/official-stockfish/Stockfish/blob/effdc3fd3f6de6e75fe1374a51a7a7eed66f880b/tests/perft.sh): 표준 검증 포지션의 출처 추적. 해당 저장소는 GPL-3.0이며 엔진 구현 코드는 가져오지 않았다. 여기서는 포지션과 수치 검증 자료로만 참조한다. 원문은 Chessprogramming의 Perft Results를 출처로 기재한다.
- [python-chess v1.11.2](https://github.com/niklasf/python-chess/tree/v1.11.2): `chess==1.11.2`, GPL-3.0-or-later. 별도 프로세스에서 합법 수·상태의 외부 기준으로만 실행한다. C++ 코어에 코드 복사·링크·실행 의존성이 없다. 패키지의 라이선스 파일을 제거하거나 엔진에 통합하지 않는다.

규칙 코어·콘솔·테스트 드라이버는 이 저장소에서 새로 작성했다. Stockfish/Leela의 탐색·평가·학습 코드나 모델은 사용하지 않았다. 다음 단계는 기물 가치 기반 평가와 negamax/alpha-beta의 작은 탐색을 붙여, 동일 규칙 테스트를 유지하며 첫 자체 수 선택을 검증하는 것이다.
