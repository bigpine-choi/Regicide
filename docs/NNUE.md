# NNUE 증분 평가 — v0.6.6

기존 768→32→16→1 신경망의 첫 층에 증분 계산을 연결했습니다. 모델을 새로 학습하지 않고 V1/V2 파일에서 첫 층 고정소수 가중치를 파생합니다. 신경망 구조·학습 자료·모델 채택 기준은 그대로입니다.

## 계산 방식

- 첫 층의 활성화 전 합을 백·흑 관점별로 보관합니다. 흑 관점은 기물의 자기편/상대편 채널과 랭크 방향을 함께 바꿉니다.
- 루트에서 한 번 전체 계산하고, 자식 국면은 부모 값을 복사한 뒤 실제로 바뀐 칸의 기물만 빼고 더합니다. 수를 되돌리면 보존된 부모 값을 다시 사용합니다.
- 캐슬링·앙파상·승격도 이동 전후 칸 비교로 처리합니다. 현재 입력에는 킹 위치별 버킷이 없어 킹 이동도 일반 증분 갱신입니다.
- 탐색과 quiescence 모두 같은 경로를 사용합니다. 누산기는 탐색별로 소유하며 NNUE 활성 시에만 할당합니다. 가중치는 대국 중 학습하지 않습니다.

첫 층의 가중치와 편향은 `llround(value * 2^24)`로 int32에 저장하고 int64로 누적합니다. 최대 허용 가중치 ±100, 최대 64칸과 편향에서도 누적 합과 double 변환은 안전합니다. ReLU는 누적 원본을 보존한 평가용 복사본에 적용합니다. 뒤층은 기존 double 계산입니다.

이 정수 경로는 **동일한 양자화 모델을 매번 전부 계산한 결과와 정확히 일치**합니다. 기존 double 경로와는 별도 계산 규약입니다. 첫 층 각 값의 양자화 오차는 `(기물 수 + 1) / 2^25` 이하이나 뒤층이 이를 증폭할 수 있으므로 모든 허용 모델의 최종 cp·탐색 결과가 같다고 보장하지 않습니다. 기존 double 경로는 보존합니다.

## 실행

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
build/regicide search 8 --eval-file MODEL.nn --nnue
build/regicide eval --eval-file MODEL.nn --nnue --fen "FEN"
```

Windows의 다중 구성 빌드에서는 실행 파일이 `build/Release/regicide.exe`에 있습니다. 실제 경로에 맞춰 실행합니다.

UCI:

```text
setoption name EvalFile value MODEL.nn
setoption name UseNeuralEvaluation value true
setoption name UseNNUE value true
position startpos
go nodes 10000
```

`UseNeuralEvaluation=false`, `UseNNUE=false`가 기본입니다. `UseNNUE`는 활성화된 신경망의 계산 방식을 선택합니다. 단독으로 신경망을 켜지 않으며, 신경망을 껐다 켜도 계산 방식 선택은 유지됩니다. CLI `--nnue`에는 `--eval-file`이 필요합니다. `UsePositionalEvaluation=false`는 기존과 같이 기물 점수 평가를 선택합니다.

기존 학습/arena/사람 대국 실행기는 자동으로 `UseNNUE`를 켜지 않습니다. NNUE의 기력을 비교할 때는 실제 대국 양쪽의 옵션과 모델 해시를 기록하고, NNUE 후보에 `UseNNUE=true`를 명시해야 합니다. 이번 변경으로 이전에 탈락한 후보를 채택하지 않습니다.

## 검증과 측정

`tests/nnue_tests.cpp`는 파일 순서에서 직접 계산하는 독립 정수/신경망 기준, 모든 특수 수, 무작위 이동·되돌리기·형제 가지, 극한 가중치, 독립 minimax/quiescence, 중단과 모델 교체를 검사합니다. `tests/nnue_integration_tests.py`는 실제 UCI/CLI와 기본값·옵션 전환을 검사합니다. 두 검사는 Linux/Windows CI에 포함됩니다.

```sh
python tests/nnue_integration_tests.py --binary build/regicide
cmake --build build --config Release --target regicide_profile_nnue
build/regicide_profile_nnue MODEL.nn 500000
python scripts/benchmark_nnue_search.py --binary build/regicide --model MODEL.nn --nodes 10000 --repeats 5 --output search.json
```

정적 profiler는 수 생성·모델 로딩을 제외하고 부모 복사·양 관점 갱신·자식 평가를 포함합니다. 전체 탐색 비교는 사전에 존재한 10개 국면을 사용하며 같은 모델의 원래 경로와 NNUE 경로를 순서를 섞어 비교합니다. 고정 노드 비용 검사는 기력 대국이 아닙니다. 실제 결과와 한계는 [검증 기록](checks/2026-10-19-nnue-incremental/README.md)에 기록합니다.

현재 단계는 증분 추론 기반입니다. 더 강한 평가를 얻으려면 학습 정답·특징·모델 용량을 검증하고 실제 시간제 대국에서 채택 기준을 통과해야 합니다. 첫 층 정밀도·정수 폭·SIMD·지연 갱신의 후속 변경도 정확성·속도·대국 성적을 각각 확인합니다.
