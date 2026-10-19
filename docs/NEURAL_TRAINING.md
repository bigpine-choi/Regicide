# 작은 신경망 첫 학습 파이프라인 — 2026-10-19

자체 데이터 생성 → 학습·검증 분리 → 학습 → 가중치 저장 → 엔진 불러오기 → 전체/증분 일치를 실행했다. 기본 평가기는 기존 위치 평가기이며 내장 bootstrap도 남아 있다. 이번 모델은 **기존 평가기의 정적 점수를 모방하는 첫 학습 실험**이다. 승패 학습이나 기력 개선을 입증한 결과는 아니다.

## 데이터와 분리

시드 `20261019`로 자체 무작위 합법 대국 64개를 최대 64ply까지 생성했다. 각 게임은 별도 난수 스트림을 사용하며, 6ply부터 2ply마다 위치를 채집했다. Python `chess`는 독립 합법 수 생성 도구이고, 목표 점수는 C++ Regicide의 기존 위치 평가기가 `eval-batch`로 제공했다. 외부 기보·교사 엔진·가중치는 사용하지 않았다. 끝난 대국과 무승부 주장 가능 위치는 표본에서 제외한다. 이 게임들은 학습 원자료이며 엔진 비교 대국이 아니다.

게임을 생성하기 전에 ID 단위로 학습 51개·검증 13개에 배정했다. 조기 종료한 학습 게임 하나는 표본이 없어 실제 표본 출처는 학습 50게임·검증 13게임이다. 같은 게임의 인접 위치를 양쪽에 나누지 않는다.

현재 신경망 입력은 기물·칸뿐이므로 중복 키는 차례·캐슬링 권리·앙파상 칸·수 카운터를 제외한 기물 배치다. 색 교환과 랭크 반사로 같은 정보가 되는 배치까지 정규화했다. 후보 1,890개에서 전역 중복 2개를 제거해 **학습 1,498개·검증 390개**가 남았다. 양쪽의 게임·정규화 입력 교집합은 모두 0이다.

`raw_games.jsonl`은 원본 게임 수순을 저장한다. 분할 JSONL에는 FEN·원본 게임 ID·ply·입력 해시·기존 평가기 점수를 저장했다. 목표는 백 관점의 centipawn이다. 로더는 파일 해시, 게임의 분할 배정, 실제 입력 키와 교집합, 합법 상태, 정수 목표 점수와 차례 변환을 다시 검사한다. 매니페스트에 적힌 교집합 0이라는 수치를 그대로 믿지 않는다.

## 학습과 정수 변환

구조는 **768입력 → 16은닉(clipped ReLU 0..127) → 백/흑 관점의 출력 차**다. 두 관점은 입력·편향·출력 가중치를 공유한다. 모델 파일을 위해 은닉 편향 16개와 출력 배율을 추가했다. 기존 내장 모델은 편향 0·배율 1로 이전 점수를 유지한다.

초기값은 자체 bootstrap의 기물 수 채널 입력을 4배, 출력 가중치를 1/4로 조정해 초기 기물 점수를 보존했다. 입력 가중치·은닉 편향·출력 가중치를 모두 실수로 학습했다. Adam, 시드 `20261020`, **60 epochs·batch 128**을 사전에 고정했다. 입력/편향 학습률 0.002, 출력 0.05, MSE(점수/100 단위), gradient norm 상한 10을 사용했다.

검증 데이터는 시작과 끝에만 측정하며 gradient와 최종 epoch 선택에 쓰지 않는다. 중간 이력은 학습 오차만 기록한다. 검증 정답만 바꿔도 모델과 학습 이력이 그대로라는 자동 검사도 추가했다.

학습 후 입력·편향은 정수로 반올림하고 출력 가중치는 8배 후 반올림한다(ties-to-even). 엔진은 두 관점의 정수 출력 차를 한 번 8로 나누고 0 방향으로 절삭한다. 그 뒤 ±28000으로 제한하고 차례에 맞춰 부호를 붙인다. 전체·증분 경로와 Python 정수 평가가 같은 규칙을 쓴다.

| 검증 위치의 기존 평가기 모방 오차 | 초기 MAE | 학습 후 MAE |
|---|---:|---:|
| 실수 모델 | 34.16cp | 7.18cp |
| 배포용 정수 모델 | 34.16cp | 31.20cp |

실수 입력 **8,982개**, 편향 **14개**, 출력 **15개**가 바뀌었다. 정수 변환 후에는 입력·편향 변화가 반올림으로 사라지고 **출력 15개만 바뀌었다**. 실수 오차 개선을 배포 모델의 개선으로 제시하지 않는다. 이 큰 정밀도 손실은 다음 단계에서 학습 데이터만 사용해 검토할 양자화 인지 학습·정수 스케일 조정의 과제다. 이번 검증 결과를 보고 학습 설정이나 epoch를 바꾸지는 않았다.

## 가중치 저장·불러오기

Python/C++ 공통 `REGICIDE_NNUE` 버전 1 텍스트를 사용한다. 버전·입력/은닉 차원·활성 한계·출력 배율, 편향·출력·입력 가중치, `checksum <uint64>`를 순서대로 저장한다. 체크섬은 정수들을 uint32 little-endian으로 처리한 FNV-1a64이며, 실험 파일 자체는 별도 SHA256으로 식별한다.

파일 256KiB, 토큰 64바이트 한도, 정확한 항목 수·버전·차원·범위·체크섬·마지막 데이터를 검사한다. 입력 ±127, 편향 ±4096, 출력 ±8192, 배율 1..65536을 허용한다. 정수 출력 내적에는 int64를 쓴다. 저장은 검증 후 새 경로에 게시하고 기존 파일을 덮어쓰지 않는다. 손상·범위 오류·잘못된 배율을 Python/C++에서 거부한다.

탐색은 `shared_ptr<const NeuralModel>`로 가중치 수명을 보장하고 누적값은 그 모델을 참조한다. 모델과 undo snapshot을 사용하는 worker가 종료되기 전에 가중치가 사라지지 않는다. UCI는 진행 중 탐색을 종료·join한 뒤 새 파일을 완전히 검증해 교체한다. 불러오기 실패 시 이전 모델을 유지한다. 전이표의 이전 점수는 다음 탐색으로 넘기지 않는다.

## 실행 방법

```powershell
./.venv-tests/Scripts/python.exe -m pip install -r requirements-training.txt
./scripts/build.ps1 -BuildDirectory build-neural-training -Test
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
./.venv-tests/Scripts/python.exe scripts/neural_training.py run --engine build-neural-training/regicide.exe --output build-neural-training/my-first-run --games 64 --max-plies 64 --sample-every 2 --epochs 60 --batch-size 128
./build-neural-training/regicide.exe search 3 --neural --neural-model build-neural-training/my-first-run/training/model.nnue
./build-neural-training/regicide.exe search 3 --neural --neural-model build-neural-training/my-first-run/training/model.nnue --neural-full-refresh
./.venv-tests/Scripts/python.exe tests/neural_training_unit_tests.py
./.venv-tests/Scripts/python.exe tests/neural_training_integration.py --engine build-neural-training/regicide.exe
```

출력은 매번 새 폴더를 지정한다. `generate`와 `train`으로 데이터 생성과 학습을 따로 실행할 수도 있다. 실수 가중치 `float_weights.npz`는 피클 없이 저장하며, 엔진은 정수 `model.nnue`를 읽는다. optimizer 상태를 저장해 중단된 학습을 이어가는 기능은 이번 범위에 없다.

UCI의 `NeuralModelFile`에 파일 경로를 지정하고 `UseNeuralEvaluation=true`로 신경망을 켠다. `UseIncrementalNeuralEvaluation=false`는 같은 모델의 전체 재계산이다. `NeuralModelFile=<builtin>` 또는 빈 문자열로 내장 모델에 복귀한다. 파일 선택만으로 기본 평가기 종류가 바뀌지는 않는다.

이번 산출물: [학습 모델](checks/2026-10-19-neural-training/first-run/training/model.nnue), [실수·정수 학습 수치](checks/2026-10-19-neural-training/first-run/training/metrics.json), [분할 기록](checks/2026-10-19-neural-training/first-run/data/split_manifest.json). 모델 FNV 체크섬은 `4291016961389606401`, SHA256은 `012baabebc712109b02e0f2ed55dcebf785b0f06f6e2a577d4cfebaad41a57a8`이다.

## 실행한 검증

- Windows MSVC C++20 `/O2 /DNDEBUG /W4 /WX` 표준 빌드 통과. 기존 핵심 6종과 신경망 **51,153 assertions, 실패 0**. 최종 모델 검사 파일은 추가 스트레스 사례까지 별도로 재빌드·실행했다.
- 실제 배포 학습 파일을 읽은 C++ 검사 **6그룹·21,538 assertions, 실패 0**. 원시 누적값·점수, 양색/양쪽 캐슬링·앙파상·모든 일반/캡처 승격, 형제 분기·긴 수순·undo, 탐색·중단·모델과 컨텍스트 전환을 검사했다. 비영 편향과 활성 한계를 쓰는 별도 스트레스 모델도 통과했다.
- Linux GCC13.3.0 ASan+UBSan와 debug 합법 수 검사에서 같은 학습 모델로 **21,538 assertions, 실패 0**. 명령과 sanitizer 연결 라이브러리를 기록했다.
- 배포 모델의 학습·검증 **1,888개 위치**에서 Python 정수 점수·C++ 전체·증분 초기화 점수 일치, 교사 라벨 재대조 불일치 0. 실제 이동의 증분 계산은 위의 C++ 특수 수·수순 검사로 확인했다.
- Python 단위 **7검사 통과**. 작은 실제 학습 통합 검사에서 학습60/검증20 분리, Python/C++ 126위치 일치, UCI 전체/증분 4쌍, 모델 교체·불러오기 실패 복구·진행 중 탐색 종료·새 게임을 검증했다. 같은 게임/반전 입력을 양쪽에 넣어 파일 해시까지 맞춘 경우도 거부했다. 검증 정답만 바꿔도 학습 모델은 같았다.
- 실제 64게임·60epoch 실행을 같은 환경에서 다시 실행해 데이터·분할·정수 모델 바이트와 실수 가중치 배열이 모두 동일했다. 다른 운영체제/BLAS 버전까지의 동일성은 주장하지 않는다.
- 기존 UCI 통합 19그룹과 신경망 UCI40쌍·콘솔8쌍·이전 버전 기본/기물 가치8쌍도 통과했다. CI에 새 검사를 연결했으며, 이번에 원격 CI와 Godot 화면 검증은 실행하지 않았다.

별도 에이전트가 읽기 전용으로 미분식·분리·기록을 감사하고, 원본 대국의 합법 수 **4,038개**를 재생해 데이터 위치를 전부 복원했다. 저장 가중치에서 오차를 독립 재계산하고 해시를 대조했다. [전체 원자료](checks/2026-10-19-neural-training/)에는 데이터·모델·코드·실행 파일·패키지·시드 식별 정보가 있다.

Python 3.13.16, NumPy2.3.4, chess1.11.2로 실행했다. NumPy(BSD 계열 및 배포본 번들 고지)와 chess(GPL-3.0+)는 학습·검증용 의존성이며 C++ 엔진의 수 선택에 연결하지 않았다. 외부 기보·교사 엔진·사전 학습 가중치는 사용하지 않았다.

데이터가 작고 무작위 합법 대국에서 나온 정적 점수 모방 표본이다. 독립적인 기력 평가용 테스트 세트와 통제 대국은 아직 없다. 다음 과제는 훈련 데이터만 사용해 정수 변환 손실을 줄이는 가설을 확인한 뒤 새로운 검증 자료와 대국 조건을 정하는 것이다.
