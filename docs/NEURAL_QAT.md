# 현재 정수 형식을 유지한 QAT — 2026-10-19

양자화 인지 학습(QAT)을 구현하고 실제 가중치를 학습했다. 설정을 학습 자료에서 정한 뒤 모델을 고정하고, 그 후 생성한 **새 64게임·1,905개 위치**에서 기존 정수 모델과 비교했다. 기존 평가기 모방 MAE는 **33.85 → 30.53cp(약 9.8% 감소)**였다. 저장·불러오기, Python/C++ 점수, 전체 재계산·증분 갱신·특수 수·undo를 검증했고 평가 비용은 대체로 유지됐다. 이 결과는 정적 점수 모방 오차이며 대국 기력이나 Elo의 증거는 아니다.

## 원리와 작은 예

기존 학습은 실수 가중치로 점수를 계산한 후 마지막에 정수로 바꿨다. 예를 들어 입력 가중치가 4.00에서 4.24로 바뀌어도 저장하면 둘 다 4다. 실수 학습의 개선이 배포 모델에서 사라질 수 있다.

이번에는 매 학습 순전파에서 **실제로 배포할 정수 가중치와 같은 계산**을 사용한다. 실수 가중치는 작은 갱신을 누적하는 별도 원본(shadow)으로 유지한다. 반올림의 진짜 미분은 거의 모든 곳에서 0이므로, 역전파에는 반올림·절삭을 통과하는 기울기를 1로 근사하는 STE(straight-through estimator)를 사용한다. 이는 학습을 위한 근사이며 정수 함수의 정확한 미분이라는 뜻이 아니다. 활성·점수 제한 밖은 기울기를 차단하고 가중치 범위를 지킨다.

양자화를 학습 중 모사한다는 원리는 [Jacob 등의 QAT 원논문](https://arxiv.org/html/1712.05877v1)을 참고했다. Regicide의 고정 정수 형식과 두 관점 공유망에 맞춘 NumPy 구현이며 논문의 코드나 외부 가중치를 가져오지 않았다.

## 형식과 구현

- `REGICIDE_NNUE` 버전 **1**, 입력 **768**, 은닉 **16**, 활성 **0..127**, 출력 배율 **8**을 유지한다. C++ 평가기·로더·탐색 코드는 바꾸지 않았다.
- 입력·편향은 ties-to-even 정수 반올림, 출력은 실수 가중치에 8을 곱한 후 같은 반올림을 쓴다. 누적·내적은 int64로 계산한다.
- 백·흑 관점의 출력 차를 **한 번** 8로 나누고 0 방향으로 절삭한 뒤 ±28000으로 제한한다. 마지막에 둘 차례에 맞춰 부호를 변환한다.
- `scripts/neural_qat.py`는 정확한 정수 순전파, STE 역전파, float64 shadow와 Adam 상태, 후보 학습·정수 모델 저장·설정 고정을 제공한다. 손실은 폰 단위 MSE, gradient norm 상한은 10이다.
- `scripts/calibrate_neural_qat.py`는 명시적 JSON 후보 계획을 같은 학습기에 전달하고 계획·실행 드라이버 해시를 추가 기록한다.
- `scripts/neural_qat_experiment.py`는 고정 기록을 검사한 후 새 게임을 만들고, 두 모델을 동일 자료에서 평가한다. 설정·가중치·교사 엔진·자료·실험 소스의 해시가 달라지면 비교를 거부한다.

## 설정 선택과 새 자료의 경계

초기값은 [첫 학습](NEURAL_TRAINING.md)의 실수 체크포인트다. 그 체크포인트가 정수화하면 비교 기준 모델과 같음을 먼저 검사했다. 후보 선택에는 원래 **학습 1,498개**만 사용했다. 초기값 자체가 이 학습 전체를 이미 보았으므로 이번 선택은 학습 내 보정이며 독립 내부 검증으로 부르지 않는다. 기존 검증 390개의 정답은 설정 선택에 쓰지 않았다.

처음 고정한 4개 설정은 모두 학습 MAE가 악화됐다. 입력·편향 학습률 0.005/0.02, 출력 0.01, 60/120 epochs의 최선도 79.23cp로 기존 30.77cp보다 나빴다. 큰 정수 변화에서 STE 학습이 불안정해진 결과를 보존했다. 학습 곡선만 확인한 뒤 **새 보류 자료를 만들기 전에** 보정 계획을 기록하고 5개 후보를 추가했다.

| 단계 | 입력·편향 학습률 | 출력 학습률 | epochs | 학습 MAE 최선 |
|---|---|---|---|---:|
| 초기 계획 4개 | 0.005 / 0.02 | 0.01 | 60 / 120 | 79.23cp |
| 보정 계획 4개 | 0.00002 / 0.0001 | 0.005 | 60 / 120 | 26.16cp |
| 보정 계획 1개 | 0.005 | 0.01 | 5 | 28.85cp |

모든 후보는 batch 128, 시드 `20261021`이다. 최종 정수 학습 MAE → RMSE → 계획 순서로 선택했다. 최종 설정은 **입력·편향 0.00002, 출력 0.005, 120 epochs**다. 입력·편향 0.0001의 120 epochs도 같은 정수 모델이어서 계획 순서로 동점을 해소했다. 전체 9개가 처음부터 한 번에 사전 등록됐다고 주장하지 않는다.

원래 네 후보와 실패 결과는 `selection/`, 추가 다섯 후보와 최종 모델은 `selection-final/`에 남아 있다. 최종 모델·설정·학습 자료·소스의 SHA256을 `freeze.json`에 묶고, 보정 계획·드라이버 기록을 추가 검사했다. `final-freeze-receipt.json`은 새 자료 디렉터리가 아직 없는 상태를 기록했다. 당시 초기 실험 보조 스크립트는 개발 중 수정됐으며, 최초 protocol의 소스 해시와 실제 실행 버전은 구분한다. 최종 receipt와 보류 매니페스트가 실제 생성·평가한 소스를 식별한다.

그 후 시드 **20261022**, 게임별 독립 난수 스트림으로 무작위 합법 게임 64개를 최대 64ply까지 생성했다. 6ply부터 2ply마다 채집하며 종료·무승부 청구 가능 위치는 제외한다. 기존 학습·검증의 기물 배치, 색 교환·랭크 반사까지 정규화한 입력과 새 게임 ID의 중복을 검사한다. 실제 후보 1,905개에서 중복·기존 자료 제외 건수는 모두 0이었다. 정답은 변경 없는 자체 C++ 기존 평가기의 백 관점 점수다. 외부 기보·교사·모델은 사용하지 않았다.

보류 결과를 보고 설정을 바꾸거나 다시 선택하지 않았다. 이후 동일 고정 설정을 다시 학습한 재현 검사는 정수 배열과 shadow 실수 배열까지 모두 같았다.

## 새 보류 자료의 오차

| 같은 1,905개 위치 | 기존 정수 모델 | QAT 정수 모델 |
|---|---:|---:|
| MAE | 33.8462cp | 30.5339cp |
| RMSE | 43.4372cp | 38.8388cp |

MAE는 3.3123cp, 약 9.8% 감소했다. 64게임의 인접 표본이므로 1,905개의 독립 대국 결과로 해석하지 않는다. 원자료·위치별 예측·게임별 지표를 [비교 기록](checks/2026-10-19-neural-qat/holdout-comparison.json)에 보존했다.

중요한 한계가 남았다. 최종 모델의 **정수 입력·편향은 그대로이고 출력 가중치 13개만 바뀌었다**. 모든 층의 shadow를 학습하는 QAT 연결은 작동하지만, 현재 거친 입력 단위에서 입력층 학습 변화가 배포 가중치에 보존됐다는 증거는 없다. 첫 실수 모델의 7.18cp는 다른 자료·실수 계산의 결과이며 이번 정수 모델의 성과로 가져오지 않는다. 다음 가설은 더 많은 학습 자료 또는 정밀도 배분이 입력층 변화와 새로운 보류 자료의 오차를 개선하는지다.

## 정확성과 저장·불러오기 검증

실제로 실행한 검증은 다음과 같다.

- QAT 단위 테스트 **21개 통과**: 수작업 정수 계산, 음수 절삭, 배포 순전파, STE 마스크·공유 sparse gradient, 작은 shadow 변화 누적, 결정적 학습, 검증 정답 미접근, 설정·가중치 변조와 덮어쓰기 거부.
- 별도 작은 자료의 통합 테스트: 학습 60·기존 검증 20·새 보류 40개, Python/C++ 전체/증분 **126개 위치**, UCI 탐색 **4쌍** 일치. 기존 검증 파일을 열지 않고 선택하며, 보류 정답 변경은 비교 지표만 바꾼다. 잘못된 교사 정답·기존 반전 입력 중복·고정 모델 변조를 거부했다. 실제 보류 시드와 다른 테스트 전용 자료다.
- 실제 두 모델 각각 새 보류 **1,905개 전부**에서 Python 정수 점수 = C++ 전체 재계산 = 증분 점수, 교사 레이블 재검사 불일치 **0**.
- 최종 QAT 모델을 실제 로드한 C++ 모델 테스트: Windows와 Linux ASan+UBSan에서 각각 **6개 그룹·21,538개 검사, 실패 0**. 저장·다시 읽기, 손상·범위·체크섬 검사, 캐슬링·앙파상·4종 승격·잡으며 승격, 형제 분기·undo·랜덤 수순 전체 복원, 모델·탐색 컨텍스트 전환을 포함한다.
- Linux에서는 런타임 소스가 같은 이전 학습 단계의 ASan+UBSan 실행 파일을 재사용했다. 이전 매니페스트와 실행 파일 SHA256 일치를 확인하고 **새 QAT 파일을 인수로** 전달했다. 이번에 Linux 재컴파일을 수행했다고 주장하지 않는다.
- Windows 전체 빌드와 기존 규칙·평가·탐색 회귀 검증, 내장 신경망 **51,153개 검사**, 엔진 통합 19개 그룹, 내장 모델 UCI 40쌍·CLI 8쌍도 통과했다.
- 읽기 전용 독립 감사에서 후보 순위·모델·학습 지표·고정 해시를 재계산하고, 64게임 난수 수순과 1,905개 표본을 재생했다. 실제 C++ 점수와 벤치마크 96행의 중앙값도 다시 확인했다.

CI에는 QAT 단위·소규모 통합 테스트를 Linux/Windows 경로에 추가했다. 원격 CI는 이 작업에서 실행하지 않았다.

## 평가 비용

AMD Ryzen AI 7 350, Windows 11, MSVC 14.51.36231의 C++20 `/O2 /DNDEBUG /W4 /WX` 빌드다. 두 모델을 같은 실행 파일에서 미리 불러온 뒤 측정했다. 특수 수를 포함한 **163개 합법 전이**, 작업별 1,000회 준비, **500,000회 × 8라운드**를 사용했다. 매 라운드 모델·작업 순서를 교대하고 모델별 전체/증분 누적값·점수·undo를 먼저 대조했다. 서로 다른 모델의 점수까지 같을 필요는 없다.

| 작업, 8회 중앙값 | 기존 정수 | QAT 정수 |
|---|---:|---:|
| 수를 둔 뒤 전체 신경망 재계산 | 630.85ns | 631.49ns |
| 증분 갱신 + 누적값 저장·복원 | 64.19ns | 62.08ns |
| 보드 수 적용 + 증분 평가 + undo | 108.30ns | 109.95ns |

QAT 모델 내 전체 재계산/증분 갱신의 쌍별 비율 중앙값은 약 **10.13배**, 보드 적용·undo 포함은 약 **6.23배**였다. 이 비율은 해당 작업만 비교한 값이며 전체 탐색 속도 향상률이 아니다. 기존 모델 대비 비용의 작은 차이는 작업별로 방향이 섞여 대체로 유지됐다고 해석한다. CPU 고정·독점 실행·외부 프로세스 부하 표본 수집은 하지 않았다. 새로운 동일 시간 대국이나 기력 실험은 수행하지 않았다.

[원시 CSV·96개 측정·환경·해시·중앙값](checks/2026-10-19-neural-qat/evaluation-benchmark.json)을 보존했다. 학습·재현 시 Python 3.13.16, NumPy 2.3.4, chess 1.11.2와 BLAS 스레드 1개를 사용했다.

## 실행과 재현

저장소 루트에서 실행한다. 새 출력 디렉터리를 사용하며 기존 결과는 덮어쓰지 않는다. 아래 보정 계획 명령이 이번 **최종 다섯 후보**를 재현한다. `neural_qat.py select`는 기록된 **최초 네 후보**를 실행하므로 이번 자료의 권장 최종 설정과 다르다.

```powershell
./.venv-tests/Scripts/python.exe -m pip install -r requirements-training.txt
./scripts/build.ps1 -BuildDirectory build-neural-qat -Test
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
$old='docs/checks/2026-10-19-neural-training/first-run'
$run='build-neural-qat/my-qat-run'
./.venv-tests/Scripts/python.exe scripts/calibrate_neural_qat.py --plan docs/checks/2026-10-19-neural-qat/calibration-plan.json --data "$old/data" --baseline-model "$old/training/model.nnue" --initial-floats "$old/training/float_weights.npz" --output "$run/selection"
./.venv-tests/Scripts/python.exe scripts/neural_qat.py check-freeze "$run/selection/freeze.json"
./.venv-tests/Scripts/python.exe scripts/neural_qat_experiment.py generate --freeze "$run/selection" --original-data "$old/data" --engine build-neural-qat/regicide.exe --output "$run/holdout" --games 64 --seed 20261022
./.venv-tests/Scripts/python.exe scripts/neural_qat_experiment.py evaluate --freeze "$run/selection" --holdout "$run/holdout" --output "$run/comparison.json" --engine build-neural-qat/regicide.exe
./.venv-tests/Scripts/python.exe scripts/benchmark_qat_models.py build-neural-qat/regicide_neural_incremental_benchmark.exe "$old/training/model.nnue" "$run/selection/model.nnue" --output "$run/benchmark.json" --iterations 500000 --repeats 8
./.venv-tests/Scripts/python.exe tests/neural_qat_tests.py
./.venv-tests/Scripts/python.exe tests/neural_qat_integration.py --engine build-neural-qat/regicide.exe --model-tests build-neural-qat/regicide_neural_model_tests.exe
```

같은 시드로 다시 생성하는 것은 이번 실험의 재현이며, 앞으로 설정을 조정할 때 쓸 새로운 미관측 평가 자료가 되지는 않는다.

이번 [저장 모델](checks/2026-10-19-neural-qat/selection-final/model.nnue)은 기존 CLI에서 사용할 수 있다. 기본은 기존 평가기이며 모델을 지정해 전환한다.

```powershell
./build-neural-qat/regicide.exe search 3 --neural --neural-model docs/checks/2026-10-19-neural-qat/selection-final/model.nnue
./build-neural-qat/regicide.exe search 3 --neural --neural-model docs/checks/2026-10-19-neural-qat/selection-final/model.nnue --neural-full-refresh
```

모델 SHA256: `cdbaca3d047ef3d4c5615ce691b4b99fb798c422d9a4bda30a47fa8395f29a13`. 모델 내부 체크섬: `4169634320372998457`.

## 검증 기록

원자료는 [checks/2026-10-19-neural-qat/](checks/2026-10-19-neural-qat/)에 보존했다. `protocol.json` → `calibration-plan.json` → `selection-final/freeze.json`·`calibration_driver.json` → `final-freeze-receipt.json` → `holdout/holdout_manifest.json` → `holdout-comparison.json`이 설정·모델 고정과 평가 순서를 보여 준다. 최종 `manifest.json`은 소스·모델·자료·실행 파일·기록의 SHA256을 묶는다. 측정 기준 코드는 `4f27bce9a510937143fd9ded62a6b42438638909`다. 측정 당시에는 작업 트리 상태였으며, QAT 코드·최종 모델·후보와 검증 기록은 이번 변경과 함께 커밋해 보존한다.
