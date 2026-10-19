# 입력층 정밀도 배분과 QAT — 2026-10-19

입력층의 학습 변화가 정수 모델에 남도록 고정소수점 정밀도를 높였다. 학습 자료에서 선택한 **1/32 단위 입력·편향** 모델은 같은 정밀도의 초기값 대비 입력 정수 **975개**, 편향 2개, 출력 11개가 바뀌었다. 모델을 고정한 뒤 생성한 새 **64게임·1,893개 위치**에서 기존 QAT 모델의 모방 MAE **30.62cp → 11.21cp**를 확인했다. 저장·불러오기, 전체 재계산·증분 갱신·특수 수·undo를 통과했고, 평가 비용은 같은 실행 파일의 기존 모델과 비슷했다.

이는 자체 기존 평가기의 정적 점수를 모방한 실험이다. 승패 데이터 학습, 대국 기력 향상이나 Elo의 증거로 해석하지 않는다. 기본 평가기는 계속 기존 평가기다.

## 원리와 작은 예

이전 형식의 입력 가중치는 정수 한 칸이 1이었다. 실수 가중치 4.00과 4.02는 모두 4로 저장된다. 이번에는 **S=32**를 곱해 128과 129로 저장하므로 작은 차이가 남는다. 이를 고정소수점이라고 하며, 저장한 정수가 실제로는 1/32 단위라는 뜻이다.

입력만 크게 하면 활성 함수가 일찍 포화돼 점수의 의미가 바뀐다. 따라서 입력·편향과 은닉 활성 상한을 모두 S배 하고, 마지막 출력 분모도 S배 한다. 예를 들어 기존 입력 정수만 정확히 32배, 활성 상한 127→4064, 분모 8→256으로 바꾸면 **학습 없이 원래 점수가 그대로**다. 이 정확한 배율 변환을 대조군으로 검증했다.

이번 학습에서는 원래 실수 체크포인트를 1/32 단위로 재양자화한 모델을 별도 대조군으로 둔다. 그 모델과 학습 모델의 차이가 정밀도 표현 변경 외에 실제 학습으로 생긴 변화다. 최종 입력을 이 초기값으로만 되돌리고 편향·출력을 그대로 둔 모델도 보류 자료 생성 전에 고정했다.

## 모델 형식과 정수 계산

기존 버전 1 파일의 의미를 바꾸지 않고 **버전 2**를 추가했다. 새 엔진은 v1과 v2를 모두 읽고 v1 체크섬·내장 점수를 유지한다. 이전 엔진 실행 파일은 v2를 읽지 못하므로 새 모델에는 이번에 빌드한 엔진을 사용한다.

| 항목 | 기존 v1 | 새 v2 |
|---|---|---|
| 구조 | 768입력·16은닉·두 관점 공유 | 동일 |
| 입력·편향 단위 | 1 | 1/S |
| 활성 상한 | 127 | 127×S |
| 입력 정수 범위 | ±127 | ±32767 |
| 편향 정수 범위 | ±4096 | ±1048576 |
| 출력 정수 범위 | ±8192 | 동일 |
| 허용 S | 1 | 2..256의 2의 거듭제곱 |
| 마지막 분모 | 1..65536 | 같은 범위이면서 S의 배수 |

이번 QAT의 출력 가중치는 계속 1/8 단위이므로 최종 분모는 **8S**다. 선택 모델의 헤더는 `REGICIDE_NNUE 2 768 16 4064 256 32`다. 체크섬은 버전·차원·실제 활성 상한·분모·v2의 S를 포함한 uint32 little-endian FNV-1a64다. 파일 크기·항목 수·범위·잘림·체크섬·후행 데이터와 안전한 새 파일 저장 검사는 유지한다.

원래 실수 단위의 shadow를 학습하면서 입력·편향에 S, 출력에 8을 곱해 ties-to-even 반올림한다. 순전파는 배포 정수 계산과 같고, 역전파는 반올림·절삭에 STE 근사를 쓴다. 출력층 기울기에 은닉 차/S를 사용하며 입력층에서는 S가 분모의 S와 상쇄되도록 구현했다. 숫자가 커졌다는 이유로 학습률의 실제 의미가 S배 바뀌지 않는다.

누적값은 C++ int32를 유지한다. 64칸에 모두 기물이 있어도 편향을 포함한 절댓값은 3,145,664 이하로 안전하다. 활성·출력 내적과 두 관점 차는 int64다. 최대 S의 출력 분자는 int32 범위를 넘을 수 있으므로 넓은 누적의 필요성을 별도로 검사했다. 백 관점 분자를 **한 번만** 분모로 나눠 0 방향으로 절삭하고 ±28000으로 제한한 뒤 둘 차례에 맞춰 부호를 붙인다.

가중치와 누적값의 C++ 저장 자료형 자체를 작게 압축한 작업은 아니다. 기존 int 배열을 유지하고 수치 범위와 계산 단위를 재배분했다. 모델 메타데이터에 버전·S가 추가됐으며 텍스트 파일에는 더 큰 정수가 들어간다.

## 학습 자료에서 설정 선택

기준 코드는 `1cd4de87eec36e75cdfc321a1e1845245aaa53b8`이다. 기존 학습 **1,498개**만 읽고, 이전 검증 390개와 이전 QAT 보류 1,905개의 정답은 설정 선택에 쓰지 않았다. 초기값은 첫 학습의 실수 체크포인트이며 이것이 원래 학습 전체를 이미 보았으므로 독립 내부 검증이 아닌 학습 내 보정이다. 비교 기준은 직전 QAT의 정수 모델이다.

`protocol.json`을 학습 전에 작성했다. S=8/16/32와 입력·편향 학습률 0.00002/0.0001의 **6개 후보**를 실행했다. 모두 출력 학습률 0.005, 120 epochs, batch 128, 시드 20261024, Adam·float64 shadow/moments, 폰 단위 MSE, gradient norm 상한 10이다. 최종 정수 학습 MAE → RMSE → 계획 순서로 선택했다.

| S | 입력·편향 학습률 | 학습 MAE | 같은 S 초기값 대비 입력 변화 |
|---:|---:|---:|---:|
| 8 | 0.00002 | 25.0280cp | 120 |
| 8 | 0.0001 | 24.5007cp | 1066 |
| 16 | 0.00002 | 14.0240cp | 771 |
| 16 | 0.0001 | 18.7717cp | 1362 |
| **32** | **0.00002** | **8.6829cp** | **975** |
| 32 | 0.0001 | 14.9706cp | 1391 |

기존 QAT의 학습 MAE는 26.1582cp였다. 같은 S32로 원래 실수 체크포인트를 재양자화만 한 모델은 21.1142cp다. 선택 모델에서 입력만 초기값으로 복원하면 1,490/1,498개 위치의 점수가 바뀌고 MAE는 21.0781cp가 된다. 입력 정수 975개는 단순 배율 변환이나 이전 실수 체크포인트의 표현 변경만을 센 값이 아니다.

학습기·기존 학습 의존성·자료·계획·6개 후보·최종 모델을 저장하고 `selection/freeze.json`으로 고정했다. 같은 설정을 반복 학습한 결과 정수 배열과 shadow 실수 배열까지 모두 같았다. 읽기 전용 감사로 순위·후보·120epoch·체크섬·오차·변화량과 입력 복원 결과를 재계산했다.

## 새 보류 자료와 실제 오차

원래 학습·검증과 이전 QAT 보류의 정규화 입력·게임 ID를 제외 대상으로 묶었다. 색 교환·랭크 반사도 같은 입력으로 처리하며, 이 단계에서는 이전 자료의 정답을 설정 결정에 사용하지 않는다. 최종 모델·대조군 2개·교사 엔진·런타임 소스·실험 소스와 이전 자료의 해시를 `experiment-freeze/freeze_receipt.json`에 묶고 새 보류 디렉터리가 없는 상태를 기록했다.

그 뒤 새 시드 **20261025**로 게임별 독립 난수 스트림의 합법 무작위 대국 **64개**를 생성했다. 최대 64ply, 6ply부터 2ply마다 표본을 채집하며 종료·무승부 청구 가능 위치를 제외한다. 후보 1,894개 중 중복 1개를 제거해 **1,893개**가 남았다. 과거 자료와 정규화·반전 입력 및 게임 ID 교집합은 모두 0이다. 정답은 변경 없는 자체 C++ 기존 평가기의 백 관점 점수다.

| 같은 새 1,893개 위치 | MAE | RMSE |
|---|---:|---:|
| 직전 QAT 정수 v1 | 30.6218cp | 38.1932cp |
| 같은 S32의 무학습 실수 재양자화 | 20.3714cp | 25.8755cp |
| **입력 정밀도 개선 QAT v2** | **11.2102cp** | **14.6568cp** |
| 최종 입력만 같은 S32 초기값으로 복원 | 20.5959cp | 25.9337cp |

직전 QAT 대비 MAE는 19.4115cp, 약 **63.4% 감소**했다. 입력 복원 대조군과 최종 모델은 **1,861개 위치**에서 예측이 달랐다. 이 대조군은 그 밖의 최종 편향·출력을 유지하므로 학습된 입력이 실제 새 자료의 점수에 영향을 준다는 직접 검사다. 어느 대조군도 보류 결과를 보고 새로 조정하지 않았다.

이는 한 번 고정한 무작위 게임 분포의 결과이며, 같은 게임의 인접 위치가 포함돼 있다. 1,893개의 독립 대국으로 해석하지 않는다. 이전 QAT 문서의 30.53cp는 다른 보류 자료의 값이므로 직접 비교하지 않고 이번 자료에서 같은 기준 모델을 다시 평가했다. 보류 결과를 보고 설정을 바꾸거나 재선택하지 않았다.

## 정확성 검증

- 새 정밀도 단위 검사 **20개 통과**: 수작업 부호 절삭·활성 제한·넓은 분자, v1 호환, v2 저장·손상 거부, 독립 sparse STE 계산·S 상쇄, 결정성, 검증 자료 미접근, 실제 입력 변화와 고정 자료 변조 거부.
- 작은 학습 통합 검사: 학습 60·기존 검증 20·새 보류 40개, **166개 위치** Python/C++ 전체/증분 일치, UCI 탐색 4쌍 일치, v1·기존 평가기 동작 보존. 실제 연구와 다른 시드다.
- 실험 절차 별도 검사: 테스트 전용 학습 60·이전 보류 40·새 보류 40개로 4모델을 비교하고 합법 수순을 독립 재생했다. 계획·원래 자료·모델 불일치, 재해시한 수순·정답 위조, 생성·보고서 덮어쓰기 **8가지 거부 검사**를 통과했다.
- 실제 선택 v2 모델을 인수로 전달한 Windows 및 **새로 빌드한 Linux ASan+UBSan**에서 각각 **9그룹·26,566개 검사, 실패 0**. 저장·로드·범위·체크섬, v1 정확 배율 변환, 캐슬링·앙파상·승격·형제 분기·undo·전체 수순 복원·탐색 중단·모델 전환을 포함한다. 이전 런타임의 sanitizer 실행 파일을 재사용하지 않았다.
- 실제 새 자료의 **4모델 각각 1,893개 전부**에서 Python 정수 점수 = C++ 전체 재계산 = 증분 점수. 교사 정답 재검사 불일치 0. 원본 수순·시드·FEN·ply·입력 해시를 다시 검사한다.
- 기존 C++ 규칙·평가·탐색·SEE·전이표·상태 복원과 내장 신경망 51,153개 검사, Python 규칙 교차 검사 2,997사례, 엔진 통합 19개 그룹 통과. 이전 학습 7개·QAT 21개 단위 검사와 해당 통합 검사도 통과했다.
- 직전 QAT v1 파일을 이전·새 런타임에서 탐색한 **8쌍**이 시간 외 출력까지 같았다. 내장 UCI 전체/증분 40쌍·CLI 8쌍도 일치했다.
- 학습기를 import하지 않는 독립 토큰 파서·FNV·scalar 정수 계산으로 v1/v2 각각 학습 1,498개를 C++와 대조하고 재직렬화·정확 배율 변환을 검사했다. 별도 읽기 전용 감사에서 새 64게임 수순·1,893개 표본·4모델 오차·벤치마크 96행을 재계산해 모두 일치함을 확인했다. 감사 코드와 결과도 보존했다.

초기 새 C++ 테스트 1개는 초기 배치의 대칭인 흑 폰을 수작업 기대값에서 빠뜨린 fixture 오류였다. 테스트 위치를 수정한 뒤 통과했으며 원래 실패 로그와 수정 설명을 함께 보존했다. 구현 오류나 실패 실험을 삭제한 기록으로 만들지 않았다.

CI에는 새 단위·통합·실험 절차 검사를 Linux/Windows 경로에 추가했다. 원격 CI는 이 단계에서 실행하지 않았다.

## 평가 비용

AMD Ryzen AI 7 350, Windows 11, MSVC 14.51.36231의 C++20 `/O2 /DNDEBUG /W4 /WX` 빌드다. 같은 새 실행 파일에 기존 v1 QAT와 선택 v2를 미리 불러온다. 특수 수를 포함한 **163개 전이**, 작업별 준비 1,000회, **500,000회×8라운드**로 모델·작업 순서를 교대했다. 6개 작업×2모델×8회=**96개 표본**이며 정수 전체/증분·보드 undo를 사전·사후 검사했다.

| 작업, 8회 중앙값 | 직전 QAT v1 | 새 QAT v2 |
|---|---:|---:|
| 전체 신경망 재계산 | 594.06ns | 607.14ns |
| 증분 갱신 + 누적값 저장·복원 | 66.15ns | 66.56ns |
| 보드 적용 + 증분 평가 + undo | 111.90ns | 112.30ns |

새 모델 내 전체/증분 갱신의 쌍별 비율 중앙값은 **9.05배**, 보드 적용·undo 포함은 **5.82배**다. 기존 모델 대비 쌍별 비용은 전체 재계산 약 1.35% 증가, 증분 갱신 약 0.05% 감소, 보드 적용·증분·undo 약 0.27% 증가로 작은 차이가 섞였다. 중앙값의 비율과 쌍별 비율 중앙값은 서로 다른 계산이다. 이 측정에서는 정밀도 증가의 비용이 작았다고 판단한다.

새 런타임에서 두 모델을 비교한 값이므로 이전 런타임 도입 자체의 비용을 독립 분리한 실험은 아니다. CPU 고정·독점 실행·외부 부하 표본 수집은 하지 않았다. 성분 벤치마크를 전체 탐색 속도나 기력 향상률로 해석하지 않는다. 이전 날짜 측정과 절대 시간 차이도 직접 성능 개선으로 부르지 않는다.

## 실행과 재현

Python 3.13.16, NumPy 2.3.4, chess 1.11.2, BLAS 스레드 1개를 사용했다. 저장소 루트에서 새 출력 경로로 실행한다.

```powershell
./.venv-tests/Scripts/python.exe -m pip install -r requirements-training.txt
./scripts/build.ps1 -BuildDirectory build-neural-precision-runtime -Test
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
$old='docs/checks/2026-10-19-neural-training/first-run'
$qat='docs/checks/2026-10-19-neural-qat'
$evidence='docs/checks/2026-10-19-neural-precision'
$run='build-neural-precision-runtime/my-run'
./.venv-tests/Scripts/python.exe scripts/neural_precision.py select --data "$old/data" --baseline-model "$qat/selection-final/model.nnue" --initial-floats "$old/training/float_weights.npz" --plan "$evidence/protocol.json" --output "$run/selection"
./.venv-tests/Scripts/python.exe scripts/neural_precision.py check-freeze "$run/selection/freeze.json"
./.venv-tests/Scripts/python.exe scripts/neural_precision_experiment.py freeze --selection "$run/selection" --original-data "$old/data" --prior-holdout "$qat/holdout" --engine build-neural-precision-runtime/regicide.exe --protocol "$evidence/protocol.json" --output "$run/experiment-freeze" --holdout "$run/holdout"
./.venv-tests/Scripts/python.exe scripts/neural_precision_experiment.py generate --freeze "$run/experiment-freeze"
./.venv-tests/Scripts/python.exe scripts/neural_precision_experiment.py evaluate --freeze "$run/experiment-freeze" --output "$run/comparison.json"
./.venv-tests/Scripts/python.exe scripts/benchmark_qat_models.py build-neural-precision-runtime/regicide_neural_incremental_benchmark.exe "$qat/selection-final/model.nnue" "$run/selection/model.nnue" --output "$run/benchmark.json" --iterations 500000 --repeats 8
./.venv-tests/Scripts/python.exe tests/neural_precision_tests.py
./.venv-tests/Scripts/python.exe tests/neural_precision_integration.py --engine build-neural-precision-runtime/regicide.exe
./.venv-tests/Scripts/python.exe tests/neural_precision_experiment_tests.py --engine build-neural-precision-runtime/regicide.exe
```

같은 시드로 생성하는 것은 이번 실험의 재현이며, 앞으로 설정을 고를 때 쓸 새로운 미관측 자료가 되지는 않는다.

이번 [저장 모델](checks/2026-10-19-neural-precision/selection/model.nnue)은 다음처럼 사용할 수 있다.

```powershell
./build-neural-precision-runtime/regicide.exe search 3 --neural --neural-model docs/checks/2026-10-19-neural-precision/selection/model.nnue
./build-neural-precision-runtime/regicide.exe search 3 --neural --neural-model docs/checks/2026-10-19-neural-precision/selection/model.nnue --neural-full-refresh
```

모델 SHA256: `c9ca5f35bb0ef0f61fca9b7eba10cd01d4320cf6c2a1dd4863630b7f87baaa92`. 체크섬: `12850805878070247189`.

원자료·계획·6개 후보·대조군·모델·검증 출력·독립 감사·실행 환경과 해시는 [검증 기록](checks/2026-10-19-neural-precision/)에 보존했다. 측정 기준 커밋은 위에 적었다. 측정 당시에는 작업 트리 상태였으며, 구현·최종 모델·후보·대조군과 검증 기록은 이번 변경과 함께 커밋해 보존한다. 다음에 확인할 가설은 더 실전에 가까운 자체 자료에서도 개선이 재현되는지, 같은 시간의 대국에서 실제로 강해지는지다.
