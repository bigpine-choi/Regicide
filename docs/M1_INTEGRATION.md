# M1: 두 개발 경로의 성과 통합

기준일: 2026-10-21, 한국 시간. 통합 버전: **0.7.0-dev**.

목적은 기존 대국·학습 기능을 유지하면서 정수·증분 NNUE 연구를 같은 자체 엔진에서 사용할 수 있게 연결하는 것이다. 모델 변환이나 성능 승격 없이 두 학습 방식을 보존한다. 기력 향상을 판정하는 대국 실험은 이번 범위에 포함하지 않는다.

## 무엇을 함께 살렸는가

| 설치·자동 학습 경로 | NNUE 연구 경로 |
| --- | --- |
| 수식 평가·폰 방패·폰 구조·LMR·root searchmoves | 정수 NNUE format1/2·S32·QAT |
| 실수 보정망 V1/V2·엄격한 모델 로드 | 전체/증분 평가·모델 체크섬·특수 수/undo 검증 |
| 자체 자료 수집·정답 정제·재학습·조건부 채택 | 정적/탐색 정답 자료 생성·학습·오차/대국 진단 |
| Godot/브라우저·추천·평가 스트림·기보·시간 변경 | 독립적인 모델 내보내기·검색 자료 재현·감사 |

두 경로는 같은 규칙·전이표·SEE 기반을 공유한다. 자동 학습 순환은 기존 보정망을 계속 다루고, 정수 NNUE 학습 도구는 별도로 유지한다. 두 계열에서 만든 모델을 같은 통합 엔진으로 검증하고 선택할 수 있다.

## 실제 호환성 문제와 해결

| 문제 | 통합 결과 |
| --- | --- |
| 같은 `src/neural.cpp`가 서로 다른 모델을 구현 | 보정망은 기존 `neural.hpp/cpp`, 정수 연구는 `nnue.hpp/cpp`로 분리 |
| `SearchOptions.neural`의 타입·의미 충돌 | 기존 포인터 유지, NNUE 선택·모델·증분 필드 추가 |
| 같은 UCI 활성화 옵션이 서로 다른 모델을 의미 | 기존 `UseNeuralEvaluation`/`EvalFile` 유지, 정수는 `UseNNUEEvaluation`/`NeuralModelFile` |
| 모델 헤더·정밀도·점수 관점이 다름 | 각자의 엄격한 로더 유지. 잘못된 계열·손상 파일 거절, 이전 정상 상태 보존 |
| 두 평가기를 함께 활성화하면 의미가 모호 | 두 모드의 동시 활성화를 입력 단계와 탐색 API에서 거절 |
| 폰 평가·LMR 기본값으로 옛 연구 교사가 달라질 수 있음 | 연구 검색에서 신규 옵션을 명시적으로 끄고 실제 옵션 기록. eval-batch 첫 열은 옛 수식 정의 유지 |
| NNUE 갱신이 설치본 LMR·검색 제한과 연결되지 않음 | 수 적용·복원 범위에 누산 상태를 연결하고 LMR 재검색·취소·root 제한·모드 전환 검사 |
| 같은 이름의 테스트가 한쪽 검증을 덮어씀 | 기존 residual 테스트 유지, 연구는 nnue 테스트로 분리. 양쪽 빌드·CI 연결 |
| 기존 자료 감사기가 새 모드 이름을 모름 | 과거 기록과 새 NNUE 기록을 각각 검증. 원래 동결 증거 파일은 보존 |
| 재현용 소스 목록이 하위 자료 생성 모듈을 빠뜨림 | scripts/tests 하위 Python도 수집. 추가·삭제·변조 및 원격 경로 검사 유지 |
| 시간 변경에 따른 응답 순서 보완과 옛 UI fixture 충돌 | 실제 UI의 오래된 응답 거절을 유지하고, 테스트의 게임/응답 번호를 현실적인 순서로 수정 |

NNUE 파일 경로의 빈 값·`<builtin>` 복원, 연속 공백이 있는 경로, 검색 중 모델 변경도 검증한다. 파일을 읽는 것과 활성화하는 것은 분리돼 있으며, 사용하지 않는 계열의 파일을 준비할 수 있다.

## 사용 방법

기본 대국은 기존 수식 평가다. 기존 `Play-Regicide-Godot.cmd`, 브라우저 실행기와 자동 학습 실행기는 같은 계약을 유지한다.

```powershell
# Windows 빌드와 두 계열의 C++ 회귀 검사
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test

# 수식 평가
.\build\regicide.exe search 4

# 기존 실수 신경망: 파일에 V1/V2 형식이 명시됨
.\build\regicide.exe search 4 --eval-file models/regicide-small-v1.nn

# 정수 NNUE: builtin은 학습 전 시험용 초기값
.\build\regicide.exe search 4 --nnue
.\build\regicide.exe search 4 --nnue-model PATH_TO_MODEL
.\build\regicide.exe search 4 --nnue-model PATH_TO_MODEL --nnue-full-refresh
```

기존 연구 CLI의 `--neural`, `--neural-model`, `--neural-full-refresh` 별칭도 유지한다. 새 도구는 명시적인 `--nnue` 이름을 사용한다. UCI의 옛 연구 `UseNeuralEvaluation`은 통합 후 기존 설치 기능을 보존하기 위해 보정망 의미를 유지하므로, 연구 도구는 `UseNNUEEvaluation`을 사용하도록 갱신했다. 과거 실험은 그때의 동결 엔진으로 재현한다.

| UCI 선택 | 보정망 | 정수 NNUE |
| --- | --- | --- |
| 파일 | `EvalFile` | `NeuralModelFile` |
| 활성화 | `UseNeuralEvaluation` | `UseNNUEEvaluation` |
| 전체/증분 | 기존 실수 계산 | `UseIncrementalNeuralEvaluation` |

다른 계열로 바꿀 때 현재 활성화 옵션을 먼저 끈다. 기존 positional=false의 재료 평가 계약도 유지한다. NNUE는 명시적인 독립 평가 모드다.

## 검증과 증거

실행 로그·원본 보존·참조 모델 및 실행 파일 해시는 [M1 검증 폴더](checks/2026-10-21-m1-integration/)에 기록한다. 초기 실패 로그는 발견·수정 이력이며 최종 결과와 분리한다.

- Windows MSVC 최적화·엄격 경고 빌드의 C++ **174그룹·617,469검사, 실패0**. 규칙·탐색·평가·상태·전이표·SEE·기존 보정망·NNUE·모델 포함.
- 기존 직접 빌드 외에 CMake4.4.4·NMake Release의 전체 빌드와 CTest9/9도 통과해 새 대상과 기존 대상의 빌드 연결을 확인했다.
- Ubuntu 24.04의 GCC에서 같은 9개 C++ 모음을 AddressSanitizer·UndefinedBehaviorSanitizer로 빌드·실행해 모두 통과했다. 메모리 오류와 정의되지 않은 동작을 검사한 결과다.
- 원본 두 경로를 고정 Git 소스에서 새로 빌드해 8개 위치·이력, 7개 평가 조건의 **56쌍** 비교. 시간 외 공통 수·점수·노드·PV·작업 계수 일치.
- raw UCI **23사례**: 계열 오류·손상 파일 복원, 양방향 동시 활성 거절, 공백 경로, 모드 순환과 builtin 복원.
- 두 보정망과 세 NNUE 내보내기 경로를 실제 소규모 학습. NNUE는 각126위치에서 Python·C++ 전체·증분 점수 정확 일치.
- 별도 C++ 검증에서 LMR의 실제 감축·재검색, 특수 수·undo·중단·재개·root 제한·재사용 탐색 컨텍스트와 모델 전환 일치.
- 기존 학습·종반·원격 검증·등록부와 대국 도구 **29개 회귀 실행 전부 통과**. 외부 기준 규칙 차등 2,997사례와 기존 UCI 대국 검증도 포함한다. 새 재귀 소스 목록의 5검사와 관련 기존 회귀도 통과했다.
- UI는 실제 엔진 연결과 자동 장면·합성 입력으로 검증한다. 이 결과를 사람이 직접 클릭한 수동 GUI 검증으로 표현하지 않는다.

전체 비교를 다시 실행하려면 두 원본 부모에서 빌드한 실행 파일을 전달한다. 참조 인수를 생략하면 현재 실행 파일의 UCI 호환성 23사례만 실행한다.

```powershell
python tests/m1_engine_parity.py --binary build/regicide.exe --installed-reference build-m1-references/installed/build-ref/regicide.exe --research-reference build-m1-references/research/build-ref/regicide.exe --output build/m1-engine-parity.json
python tests/neural_coexistence_integration.py --binary build/regicide.exe
python tests/m1_source_inventory_tests.py
```

원본 두 작업 폴더의 수정·미추적 파일은 통합 폴더로 필요한 내용만 복사하고 원래 위치의 바이트를 보존한다. 설치본의 시간 표시·선택 유지·평가 스트림·watchdog 보완도 포함한다.

## 기준점과 다음 단계

- 설치 경로 부모: `3c3d4b4526e1fa0c927347d9974534e4385ea601`.
- NNUE 연구 부모: `0f7783bf8212b86be0fbd74e425c55c4ffc61a06`.
- 공통 조상: `acf2a18e4dad01015d671e895d8ffdc61193cb18`.
- 과거 학습 환경 지정은 `requirements-learning.txt`와 `requirements-training.txt`로 각각 유지한다. 하나의 NumPy 버전으로 과거 실험 수치까지 동일하다고 가정하지 않는다.

M1은 두 경로를 한 엔진에서 검증·개발할 기반을 만든다. 다음 M2에서는 통합 기준을 고정하고 외부 상대군의 성적을 측정한다. 새 신경망의 강도 승격이나 세계 순위는 별도 대국 근거가 필요하다.
