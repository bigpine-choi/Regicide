# 2026-11-04 강제메이트 프로토타입 실행 기록

## 범위와 버전

- 개발 브랜치: `feature/forced-mate-prototype`.
- 기준 main: `059f7f70ba25ff3c616b081c99a310e1adf9e583`.
- 검사 대상 코드는 이 기록과 함께 커밋한다. [소스·실행 파일 지문](manifest.json)을 함께 남겼다.
- 기존 일반 대국과 갬빗 정책에 자동 적용하지 않는다. `go mate N`으로만 별도 경로를 실행한다.
- 외부 Stockfish 분석·기보·모델·착수 선택 코드를 사용하지 않았다. 독립 규칙 비교는 기존 테스트 의존성인 chess 1.11.2를 사용했다.

## 실행 환경

한국 시간 2026-11-04, Windows x64, Intel Core i5-1235U (10코어/12논리 프로세서).
CMake 3.31.6-msvc6, Ninja, MSVC 19.44.35229, C++20 Release /O2.
기존 /W4 /WX /permissive- /utf-8 경고 정책을 유지했다.
메이트 탐색은 단일 작업 스레드다. Python 3.14.7, chess 1.11.2,
독립 생성 국면의 시드는 `0x4D16`이다.

## 실제 수행 결과

| 검사 | 결과 | 근거 |
| --- | --- | --- |
| 전체 CTest | 14개 묶음 모두 통과 | [ctest.txt](ctest.txt) |
| 메이트 C++ | 25개 그룹 통과, CTest의 mate 대상 | [테스트 소스](../../../tests/mate_tests.cpp) |
| 메이트 UCI | 14개 메서드와 잘못된 명령 15개 하위 사례 통과 | [테스트 소스](../../../tests/mate_uci_tests.py) |
| 독립 전체 합법 수 오라클 | 27국면 + 노드·stop·일반 탐색 복귀 통과 | [oracle.txt](oracle.txt), [국면·결과](oracle.json) |
| 기존 UCI 검사 | 28개 그룹 통과; CLI 교차검사 3개는 미완료 | [최초 실행](existing-uci-partial.txt), [나머지 실행](existing-uci-remainder.txt) |
| 일반 탐색 전후 비교 | 6/6 조건에서 bestmove·점수·깊이·노드·PV 일치 | [normal-parity.json](normal-parity.json) |
| 일반 탐색 시간 진단 | 8쌍 교대 측정, 매번 결과·노드 일치 | [normal-benchmark.json](normal-benchmark.json) |
| M16 예산 제한 | 시작 국면 50,000노드에서 unknown, 메이트 점수/PV 없음 | [실제 UCI 출력](samples.txt) |

C++ 검사는 M1/M2/M3, 조용한 첫 수, 여러 방어와 서로 다른 방어 거리,
협조적인 한 수순만 성공하는 반례, 현재·예정 수의 50수/반복 청구,
5회 반복·75수·스테일메이트·기물 조합 무승부,
네 가지 승격·앙파상·캐슬링, 노드·stop·입력 오류와 호출자 보드 보존을 검사한다.
검증된 모든 PV를 합법 수로 재생해 상대 체크메이트로 끝나는지 확인한다.

독립 오라클은 Regicide 평가나 탐색 결과를 정답으로 사용하지 않는다.
python-chess의 합법 수와 규칙으로 별도 AND/OR 완전 탐색을 수행한다.
생성 국면 12개 외에 M3 루트 후보 제한, 가짜 M2 반례, 특수 수,
무승부 경계와 이력을 포함했다.
오라클 출처는 [python-chess](https://github.com/niklasf/python-chess),
사용한 upstream 소스 ref는 `0c6bdaccfcb3b09cdbe94b0f81e948cb2a356015`이다.
외부 코드는 테스트 환경에만 두었으며 자체 C++ 엔진에 포함하지 않았다.

## 실제 증명과 자원 소진

| 요청 | 결과 | 방문 노드 | 엔진 보고 시간 |
| --- | --- | ---: | ---: |
| M1, 모든 루트 수 | proven, 1플라이 | 3 | 0ms |
| Légal M2, 모든 루트 수 | proven, 3플라이 | 45 | 0ms |
| 조용한 a2g2로 제한한 M3 | proven, 5플라이 | 813 | 1ms |
| 시작 국면 M16, 50,000노드 | unknown, 완료 범위 M3까지 | 50,000 | 221ms |

0ms는 밀리초 단위의 해상도 한계다. 시간 수치를 일반적인 속도나 기력으로 환산하지 않는다.
M3의 검증된 최장 방어 PV는 `a2g2 h7h8 f5f6 h8h7 g2g7`이다.
같은 시작 위치에서 a2g2 이후 상대가 h7h6으로 협조하면 3플라이에 메이트지만,
h7h8로 방어할 수 있어 M2 요청은 거절한다.
루트 후보를 제한했으므로 이 사례의 최단 거리는 a2g2 전략에 대한 거리다.

M16 예산 소진은 M16의 부재를 뜻하지 않는다.
진정한 M16 문제를 해결하거나 새 M16 문제를 자동 발굴했다는 실증은 없다.

## 일반 기능 보존과 남은 검사 제한

`search.cpp`, 보드·평가기·TT·NNUE, 일반 CLI와 갬빗 정책은 변경하지 않았다.
UCI의 기존 go 경로를 유지하고 mate 요청만 별도 라이브러리로 보낸다.
6개 비교 조건은 시작 위치, 루이 로페즈 전개, 캐슬링 권리와 전술이 있는 위치,
폰 종반, 고정 2,000노드, 메이트 탐색 뒤 일반 탐색 재사용이다.
모든 조건에서 결과와 탐색 노드가 같았다.

시간 진단은 캐슬링 위치 깊이 3에서 각 실행 파일 두 번 예열 후,
실행 순서를 바꿔 가며 8쌍을 측정했다. 중앙값은 기준본 380.528ms,
프로토타입 382.935ms로 약 +0.63%였다.
운영체제 부하를 통제한 통계적 성능 검정이나 Elo 대국 시험은 아니다.
[재현 스크립트](normal_benchmark.py)를 제공한다.

기존 engine_integration.py는 20개 그룹을 통과한 뒤
`test_pawn_shield_protocol_and_cli`에서 CLI 출력의 bestmove를 찾지 못해 중단됐다.
같은 환경에서 기준본과 프로토타입 모두 `search 2`가 exit 0,
stdout 0바이트로 캡처되었다. 원인은 확정하지 않았다.
이 때문에 pawn shield·pawn structure·LMR의 CLI 교차검사 3개를 통과로 세지 않는다.
해당 CLI 검사를 제외한 나머지 8개 UCI 그룹은 별도로 실행해 모두 통과했다.
전체 31개 그룹 통과로 표시하지 않는다. 원격 CI 완료 여부도 이 로컬 검사와 별개다.

## 컴파일 호환 수정과 초기 실패

원본 main의 `src/see.cpp`는 현재 MSVC에서
`for (const auto& step : knight_steps)`의 constexpr 평가가 C2131로 실패했다.
메이트 추가 전 기준본도 같은 오류였다.
같은 원소를 같은 순서로 읽는 인덱스 반복으로 바꾸자 컴파일됐다.
이 두 줄의 호환 수정은 비교용 기준본과 프로토타입 양쪽에 동일하게 적용했다.
따라서 비교 기준본은 **main + 동일 SEE 컴파일 호환 수정**이며, 무수정 main 실행 파일이라고 표시하지 않는다.
[오류 기록](compiler-portability.txt)을 남겼고, 기존 SEE 회귀도 통과했다.

최초 CTest에서는 로컬 소스 준비 시 누락된 기존 학습 모델 fixture 때문에 neural이 실패했다.
기준 커밋의 모델 두 개를 그대로 복원한 뒤 전체 14개를 재실행해 통과했다.
최초 MSVC constexpr 단계 제한 변경 시도는 효과가 없어 최종 빌드에서 제거했다.
일반 CLI의 빈 출력 현상을 숨기려고 일반 CLI 코드를 변경하지 않았다.

## 재현과 후속 과제

```text
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
python -m pip install -r requirements-test.txt
python tests/mate_oracle_tests.py build/Release/regicide.exe --output build/mate-oracle.json
python tests/mate_normal_parity.py BASELINE_EXE PROTOTYPE_EXE parity.json
```

Linux에서는 실행 파일 경로를 build/regicide로 바꾼다.
Linux 및 GitHub Actions는 이번 로컬 실행 결과에 포함하지 않는다.
CI에는 기존 CTest 외에 독립 오라클을 Windows/Linux에 등록했다.

다음 개발은 전체 방어를 재생하는 증명 인증서와,
50수·정확한 반복 이력을 보존하는 전치/DFPN 탐색이 우선이다.
현재는 전치 재사용·증명수 탐색·테이블베이스·병렬화·국면 자동 채굴이 없다.
31플라이까지 일반적인 M16을 해결한다는 성능 보장은 없다.
