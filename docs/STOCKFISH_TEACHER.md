# Stockfish 교사로 현재 NNUE 학습하기

2026-10-21 사용자 요청에 따라 Stockfish가 오프라인에서 만든 평가 점수로 Regicide의
기존 NNUE를 추가 학습한다. 실제 대국의 수 선택·탐색·평가망 구현은 Regicide가 담당한다.
Stockfish 실행 파일이나 신경망을 Regicide 엔진에 편입하지 않는다.

## 10/21 최초 교사 실행의 범위

- 기존 구조: 기물768+상대 손번1 입력, 공유 은닉16, v3 정수 모델·S32.
- 손실: 기존 MSE. 현재 손번 모델의 실수 가중치에서 이어서 학습한다.
- 자료: 기존 원본 분할에서 seed20261021로 학습512·검증128위치를 먼저 고정한다.
  원본 기보 계열과 기물 특징이 두 분할에 겹치지 않는지 확인한다. 제외 자료는 채우지 않는다.
- 교사: 기존에 확보한 공식 Stockfish18 generic, Threads1·Hash16·Skill20·강도 제한 꺼짐,
  MultiPV1·테이블베이스 경로 비움. 위치마다 해시를 비우고 원본 시작 위치와 수순을 재생한다.
- 정답: 위치당10,000노드 요청, 마지막 점수 패킷의 백 관점 UCI cp.
  실제 노드는 요청값을 넘을 수 있으며 원시 기록에 남긴다. 메이트를 임의의 cp로 치환하지 않는다.
- 사전 제외: 종료 위치·메이트·상하한만 제시된 점수·잘못된 점수/PV·절대1500cp초과.
- 학습: seed20261020·batch128·500epoch, 입력/편향/손번 학습률0.0002·출력0.005,
  기존 Adam과 기울기 norm상한10. 마지막 epoch 하나를 보존한다.
- 비교: 기본 수식 Regicide 상대, 같은8개시의 선후공 교대16판·100ms.

기존120epoch보다 횟수와 입력 학습률을 늘린 것은 작은 자료에서도 실제 가중치 이동을
허용하려는 사전 결정이다. 정답 변화만 분리한 인과 실험으로 해석하지 않는다.
검증 점수로 epoch·시드·설정을 다시 고르거나 이번 후보를 재학습하지 않는다.

## 코드와 기록

`scripts/stockfish_teacher.py`는 원본 이력과 FEN을 확인하고 각 Stockfish 질의의 원시 점수,
합법 PV, 실제 탐색량, 옵션, 실행 파일 해시와 채택 여부를 저장한다. 최종 점수 패킷의 bound를
검사해 이전 패킷에 남아 있는 bound 표시를 잘못 사용하지 않도록 했다.

`scripts/train_stockfish_teacher.py`는 선정·교사 생성·MSE 학습·새 가중치의 native 검증을
한 번 실행한다. 출력 폴더는 새 경로여야 한다. 기존 NNUE 계산 코드와 기존 자료는 변경하지 않는다.

```powershell
.venv-tests/Scripts/python.exe -B -s tests/stockfish_teacher_tests.py
.venv-tests/Scripts/python.exe -B -s scripts/train_stockfish_teacher.py <new-output>
```

교사 출력의 `labels.jsonl`은 원본 필드와 `source_target_white_cp`를 보존하고 새
`target_white_cp`·`teacher_white_cp`·`teacher_query_id`를 추가한다.
기존 `depth`·`nodes`는 원본 Regicide 기록이다. 새 교사의 탐색 정보는 연결된
`teacher-queries.jsonl`을 사용한다. 기존 자체 교사 자료의 manifest로 이 파일을 해석하지 않는다.

## 이번 소규모 실행

640질의에서 메이트24건·bound185건을 제외해 학습350·검증81개를 사용했다.
교사 생성10.21초, 학습2.66초. 학습 MAE205.48→78.22cp지만 검증 MAE158.06→221.73cp로
악화돼 과적합 징후를 확인했다. 실제 기력 판단은 별도16판을 따른다.
새 모델477위치에서 Python/전체/증분 점수가 정확히 일치했고 실제 가중치로 모델 회귀도 통과했다.
변경 없는 native 소스와 기존 모델 호환성 검사는 실행 파일 해시로 연결해 재사용했다.

기본 수식 Regicide와의16판은 **0승1무15패·111.06초**였다. 기보·PV·교사 자료 독립 감사는
통과했지만 경기력 개선에는 실패했다. 이번 후보는 채택하지 않으며 기본 엔진을 유지한다.
추가 대국·재학습 없이 [이번 결과와 한계](experiments/2026-10-21-m3-stockfish-teacher.md)를 기록했다.

실제 실행 소스는 결과 폴더의 `source-snapshot`에 고정한다. 실행 후 학습 요약의 교사 시간
조회 키를 한 줄 수정했으며 실제 시간은 `teacher/teacher-report.json`의 `elapsed_seconds`를
따른다. 이 기록 수정으로 교사 생성·학습·대국을 반복하지 않았다.

Stockfish는 별도 GPL-3.0-or-later 실행 파일로 사용했다. 공식 출처·소스 revision·원본
라이선스와 바이너리 SHA는 [기존 출처 기록](checks/2026-10-21-m2-foundation/opponents/provenance.json),
[공식 배포](https://github.com/official-stockfish/Stockfish/releases/tag/sf_18),
[라이선스](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/Copying.txt)를 따른다.
라벨은 프로젝트 소유 위치에서 로컬 실행으로 새로 생성한 수치 기록이다.

[이번 고정 조건](checks/2026-10-21-m3-stockfish-teacher/attempt-001/protocol.json),
[학습 결과](checks/2026-10-21-m3-stockfish-teacher/attempt-001/training.json),
[비교 실행 계획](checks/2026-10-21-m3-stockfish-teacher/attempt-001/plan.json)을 보존한다.

## 10/22: 검증 기준으로 중단하고 최적 가중치 복원

`neural_stm_guarded.py`와 `train_nnue_guarded.py`는 기존431라벨과 학습 전 가중치를
재사용한다. MSE·구조·Adam·학습률은 유지하고 매5회 선택용81위치의 정수 MSE를 확인한다.
5번 연속 유의미한 개선이 없으면 중단하고 실제 최소 오차 가중치를 복원한다. 학습 전
가중치도 후보에 포함한다. 기존81개는 튜닝용이며 새 독립 검증 자료라고 부르지 않는다.

```powershell
.venv-tests/Scripts/python.exe -B -s tests/neural_stm_guarded_tests.py
.venv-tests/Scripts/python.exe -B -s scripts/train_nnue_guarded.py <new-output>
```

실제1회 학습은40회에서 멈춰15회 모델을 복원했고 선택용 MAE155.96cp였다.
기본 엔진과16판은2승3무11패, 정확성·독립 감사 통과다. 기본 엔진은 유지한다.
[원인 분석·실행 결과](experiments/2026-10-22-nnue-overfit.md)를 따른다.

## 10/22: 자료의 양과 초·중반 다양성 확대

`neural_diverse_bank.py`는 기존 합법 기보·질의에서 기물 수·원본 계열·손번을 나눠
2,000위치를 먼저 고정한다. 기존431정답·색 반전 중복·분할 간 공유 특징·등록 비교 개시
수순을 제외하고 새 자기대국 없이 Stockfish 정답만 추가한다. 제외한 정답을 채우려고
질의 수를 늘리지 않는다. 원래 검증 분할은 선택용(tuning)이라고 명시한다.

`prepare_nnue_diverse_data.py`로 새 정답1,195개를 얻어 학습350→1,294개,
선택81→263개·별도 점검69개로 확대했다. 학습의28기물 이상 비중은11.7→37.2%다.
기존350개와 이번1,294개를 서로 다른 정적 시험 성적으로 직접 비교하지 않는다.

`train_nnue_diverse.py`는 직전 보호 모델에서 MSE·v3 구조·학습 조건·조기 중단·최적 복원을
유지하며 한 번 추가 학습한다. 실제45회에서 중단해20회 모델을 복원했다. 모델 저장 후
한 번 확인한 별도69개 오차는 악화됐다. 원본은 과거 개발 자료이므로 새 독립 확인 자료라고
부르지 않으며, 이번 결과를 보고 후보나 설정을 다시 선택하지 않는다.

```powershell
.venv-tests/Scripts/python.exe -B -s tests/neural_diverse_bank_tests.py
.venv-tests/Scripts/python.exe -B -s tests/nnue_diverse_training_tests.py
.venv-tests/Scripts/python.exe -B -s scripts/prepare_nnue_diverse_data.py <new-data-output>
.venv-tests/Scripts/python.exe -B -s scripts/train_nnue_diverse.py <new-data-output> <new-training-output>
```

[자료·학습·16판 조건과 실제 결과](experiments/2026-10-22-nnue-diversity.md)를 따른다.
기본 엔진·기존 실험 자료·설치 모델은 유지한다.

## 10/22 후속: 실제 패배의 오판 위치 교정

자료 확대 후보의 패배10판에서30위치를 고정하고, 실제 착수 뒤·교사 추천수 뒤·NNUE가
예상한 진행 끝·상대 응수 뒤의 위치를 함께 모았다. `prepare_nnue_loss_repair.py`가 최대
150질의의50,000노드 정답을 요청한다. 실제142질의에서111개 정답을 얻었다. 교사 조건과
메이트·bound·점수 범위 제외 정책은 유지한다. 정확한 점수여도 유한 탐색의 참조다.

`train_nnue_loss_repair.py`는 기존1,626개 자료를 보존하고 새67개를 학습·다른 개시
계열44개를 선택용에 넣는다. 새 교정 자료를4회 반복하는 MSE이며 신경망 구조·Adam·시드·
학습률·조기 중단·최적 복원은 유지한다. 기존69개는 사후 점검 전용이고 새 독립 표본이 아니다.

실제1회 학습은30회에서 중단해5회 가중치를 복원했다. 교정 오차는 줄었지만 국소 수 선택
검사에서 교사1순위 일치가4/10→2/10으로 감소했다. 정적 오차가 실제 판단 개선을 보장하지
않음을 기록하고, 사후 결과로 재학습하거나 후보를 고르지 않는다.

[실제 위치·개선 내용·다른8개시의16판 결과](experiments/2026-10-22-nnue-loss-repair.md)를 따른다.
소스·자료·모델·기보는 별도 폴더에 보존하며 기본 게임 모델은 유지한다.
