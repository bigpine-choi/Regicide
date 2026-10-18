# 신경망 평가 비용 최적화

v0.6.4는 모델의 점수 계산 순서를 유지하면서 첫 층의 메모리 배치를 바꾼다. 예전에는 뉴런 하나를 계산할 때마다 흩어진 기물 특징 가중치를 읽었다. 이제 모델을 읽을 때 특징 하나에 해당하는 32개 가중치를 나란히 저장하고, 보드의 기물 하나를 읽을 때 32개 뉴런을 함께 갱신한다.

예를 들어 백 폰·나이트·킹 순서로 읽었다면 각 뉴런은 여전히 `((bias + 폰) + 나이트) + 킹` 순서로 계산된다. double 정밀도, 덧셈 순서, 활성화 함수와 반올림을 유지한다. 이후 층과 V2의 기존 평가 + 최대100cp 보정은 그대로이며, V2에서는 결과에 쓰지 않는 별도 기물 합산만 생략한다. 모델 파일·가중치·학습·규칙·탐색·채택 기준은 유지한다.

`tests/reference_neural_v063.cpp`는 이전 스칼라 계산을 독립적으로 보존한다. 무작위 합법 위치·실제 모델·두 색·평가 옵션·반올림/상쇄 경계를 비교한다. 평가 함수를 반복 호출하는 비용과 전체 탐색 비용은 다른 수치다. 둘 다 기존 점수 및 고정 작업 일치를 전제로 측정하며, 속도 개선을 전체 기력이나 새 모델의 채택으로 해석하지 않는다.

## 재현

Windows 빌드:

```powershell
./scripts/build.ps1 -BuildDirectory build-neural-v4 -Test -NeuralProfile -EvaluationProbe
```

CMake의 선택 대상은 `regicide_profile_neural`이다. `scripts/profile_neural.cpp`, 동결 참조 구현, 실제 구현을 각각 컴파일하고 LTO를 사용하지 않는다. 기본9블록·블록당4쌍·모델별 준비 반복/보정을 사용하며, 모든 위치와 옵션의 점수가 일치해야 시간을 측정한다. V2 결과에는 기존 수식 평가 비용도 포함된다.

```powershell
build-neural-v4/regicide_profile_neural.exe --fen-file tests/data/evaluation_profile_2026_10_18.fens --model models/regicide-small-v1.nn --model PATH_TO_V2.nn --output build/profile.json
python scripts/profile_neural_audit.py build/profile.json --output build/profile-audit.json
```

실행 파일 비교는 두 버전에서 같은 모델과 옵션을 사용한다. `scripts/benchmark_neural_inference.py`는 파일·모델·위치·코드 해시, 전체 이력, 빈 TT, 순서와 원시 UCI 결과를 보존한다. `--model NAME PATH`를 반복할 수 있고 수식 평가 대조군이 자동 추가된다. 깊이/노드가 같은데 수·점수·PV·탐색 작업이 다르면 실패한다. 모델을 읽지 못하거나 자원 감시가 오염되면 유효한 시간 결과를 내지 않는다.

```powershell
python scripts/benchmark_neural_inference.py OLD.exe NEW.exe --baseline-commit BASE_SHA --candidate-commit NEW_SHA --model learned_v2 PATH_TO_V2.nn --output build/neural-comparison
```

[사전 계획](experiments/2026-10-19-neural-inference-opt-plan.md)에 조건과 한계를 기록한다. 기존 자동 학습/모델 채택은 [자동 학습 설명](CONTINUAL_LEARNING.md)을 따른다.
