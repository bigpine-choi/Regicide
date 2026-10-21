# NNUE 손번 입력과 MSE 학습

기존 정수 NNUE는 기물 종류와 위치만 입력받았다. 같은 배치에서 백 차례와 흑 차례를
서로 다르게 평가하는 표현이 없었다. v3는 각 관점에 **그 관점의 색이 지금 둘 차례인지**를
뜻하는 입력 하나를 추가한다. 공유 은닉층16개에 연결하므로 새 학습 가중치는16개다.

예를 들어 같은 기물 배치라도 먼저 공격하거나 위협을 피할 수 있는 쪽에 따라 평가가
달라질 수 있다. 손번 입력은 이런 차이를 기존 기물 특징과 함께 학습할 수 있게 한다.
정확히 어떤 판단을 배우는지와 경기력 향상 여부는 별도 검증 대상이다.

## 계산과 호환성

기존768개 기물 입력의 누산과 수 적용·되돌리기는 유지한다. 평가할 때 실제 둘 차례의
관점에만 `turn_weights`를 더한 뒤 clipped ReLU와 출력 계산을 적용한다.
증분 누산에는 손번을 따로 저장하지 않고 보드의 현재 차례를 사용한다.
전체 재계산을 기준으로 두 계산의 점수가 정확히 일치하는지 확인한다.

v1/v2 파일의 배열·저장 바이트·체크섬·평가를 보존한다. 기존 모델에는 손번 가중치0을
사용한다. v3 형식은 다음과 같다.

```text
REGICIDE_NNUE 3 769 16 4064 256 32
hidden_bias[16]
output_weights[16]
piece_input_weights[768][16]
turn_weights[16]
checksum <FNV64>
```

체크섬 순서는 버전·총입력769·은닉16·clip·출력분모·입력배율, 편향·출력·기물·손번
가중치다. v3의 범위와 배율 제한은 v2와 같다. v1/v2에0이 아닌 손번 가중치를 넣으면
저장 시 거부해 새 가중치가 조용히 사라지지 않도록 한다.

## 이번 후보 학습

MSE를 유지하고 준비된 B-MSE20261020을 기준 모델로 사용했다. 기준 모델은 재학습하지
않았다. 새 후보의 공통 기물·편향·출력 초기 배열은 원래 배열과 같고 손번 가중치만0이다.
같은 학습2,209행·검증723행, B 정답, 시드20261020, 배치순서, S32, 120epoch,
batch128, 입력·편향·손번 학습률0.00002와 출력0.005를 사용했다.
검증 자료는 optimizer와 epoch 선택에 사용하지 않았다. 후보는 마지막 epoch 하나다.

정답 B는 기존 Regicide가 위치마다10,000노드를 탐색해 만든 백 관점 추정 점수다.
이번에는 정답을 새로 생성하지 않았다. 학습 오차는 교사 점수 모방 정도이며 기력 증거가 아니다.
NumPy는 기존 준비 기록2.3.4, 이번 실행2.3.5로 달라 실행환경 전체가 같다는 주장은 하지 않는다.

실제 학습3.76초, 검증 MAE89.4550→89.1272cp였다. 양자화된 손번 가중치16개 중1개가
0에서1로 변했다. 손번을 지운 동일 후보와의 추론 검사에서는 최대3cp 차이가 났다.
기력 판단은 별도16판 결과를 따른다. 기본 평가기와 설치 게임은 이번 연구 실행으로 바꾸지 않는다.

## 재현과 검증

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -BuildDirectory build-m3-stm
.venv-tests/Scripts/python.exe -B -s tests/neural_stm_tests.py
# 출력 폴더는 존재하지 않는 새 경로여야 한다.
.venv-tests/Scripts/python.exe -B -s scripts/train_neural_stm.py <new-output>
.venv-tests/Scripts/python.exe -B -s tests/neural_stm_integration.py build-m3-stm/regicide.exe build/regicide.exe <new-output>
```

Python 단위 검사는 손번을 끈 작은 학습에서 기존 MSE의 가중치·학습 기록까지 정확히
일치하는지 확인한다. 실제 후보는3,078위치의 Python/전체/증분 점수와 기존 모델의
구·신 실행 파일 점수가 정확히 일치했다. 특수 수·되돌리기·탐색 회귀도 통과했다.

[학습 조건과 소스](checks/2026-10-21-m3-nnue-stm/attempt-001/protocol.json),
[학습 결과](checks/2026-10-21-m3-nnue-stm/attempt-001/training.json),
[실제 후보 검증](checks/2026-10-21-m3-nnue-stm/attempt-001/validation.json)을 보존한다.
