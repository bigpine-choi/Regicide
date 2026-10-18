# 자체 학습 순환 4

상태: complete. 새 모델 채택: 아니오.

- 이번 자체 대국: 완결 32판, 표본 498개
- 패배 분석: 192위치, 학습용 교정 36개
- 누적 학습/검증/시험 표본: 1681 / 96 / 125
- 교사 안정성 검사를 통과한 학습 위치: 1071개
- 평가 모델: V2 — classical_cp_stm + 100*tanh(z)
- 후보 초기화: seeded_random
- 대국 검증: 완료 32쌍, 새 모델 점수 0.375
- 채택 기준의 단측 점수 하한: 0.06903164616489793 (0.5 초과 필요)

기준을 통과하지 못하면 기존 채택 모델을 유지한다. 완료한 학습 자료는 다음 순환에 누적한다.
평가 오차 감소나 이 예약 대국의 통과가 모든 상대·시간 조건에서의 기력 향상을 보장하지는 않는다.

상세 기록: cycle.json, loss-analysis/loss-report.json, corpus/manifest.json, quality/manifest.json, quality/quality-report.json, model/report.json, arena/arena.json.
