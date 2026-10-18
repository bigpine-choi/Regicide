# 종반 자료 연구

- 상태: complete
- 영속 시도: 6
- 새 모델 채택: False

후보는 첫 시드의 가장 큰 사전 자료 규모로 고정한다. 개발 성적과 시험 오차로 후보를 바꾸지 않는다.

- 새 학습 위치 추가량: 1024
- 후보 시드: 2026101901
- 후보 SHA256: f4484072465f1c44dc41c1215704125a9bb61d5cfa3bf76f2496bd4d00af74a7
- 개발 대국, 추가 0위치: {'candidate_wins': 4, 'candidate_draws': 19, 'candidate_losses': 9, 'candidate_points': 13.5, 'candidate_score': 0.421875, 'complete_games': 32, 'complete_pairs': 16, 'pair_scores': [0.5, 0.5, 0.5, 0.25, 0.5, 0.5, 0.25, 0.5, 0.25, 0.5, 0.25, 0.5, 0.5, 0.0, 0.5, 0.75], 'scope': 'fixed-node synthetic endgame development games; wall time uncontrolled, no equal-time strength inference; small dependent sample, no Elo or universal strength claim; cannot authorize adoption'}
- 개발 대국, 추가 128위치: {'candidate_wins': 4, 'candidate_draws': 19, 'candidate_losses': 9, 'candidate_points': 13.5, 'candidate_score': 0.421875, 'complete_games': 32, 'complete_pairs': 16, 'pair_scores': [0.5, 0.25, 0.75, 0.25, 0.5, 0.5, 0.5, 0.5, 0.25, 0.25, 0.5, 0.5, 0.25, 0.25, 0.5, 0.5], 'scope': 'fixed-node synthetic endgame development games; wall time uncontrolled, no equal-time strength inference; small dependent sample, no Elo or universal strength claim; cannot authorize adoption'}
- 개발 대국, 추가 512위치: {'candidate_wins': 3, 'candidate_draws': 24, 'candidate_losses': 5, 'candidate_points': 15.0, 'candidate_score': 0.46875, 'complete_games': 32, 'complete_pairs': 16, 'pair_scores': [0.5, 0.5, 0.25, 0.5, 0.5, 0.5, 0.5, 0.5, 0.25, 0.5, 0.5, 0.5, 0.25, 0.75, 0.5, 0.5], 'scope': 'fixed-node synthetic endgame development games; wall time uncontrolled, no equal-time strength inference; small dependent sample, no Elo or universal strength claim; cannot authorize adoption'}
- 개발 대국, 추가 1024위치: {'candidate_wins': 2, 'candidate_draws': 14, 'candidate_losses': 16, 'candidate_points': 9.0, 'candidate_score': 0.28125, 'complete_games': 32, 'complete_pairs': 16, 'pair_scores': [0.25, 0.0, 0.25, 0.25, 0.25, 0.5, 0.25, 0.5, 0.5, 0.25, 0.25, 0.25, 0.0, 0.0, 0.5, 0.5], 'scope': 'fixed-node synthetic endgame development games; wall time uncontrolled, no equal-time strength inference; small dependent sample, no Elo or universal strength claim; cannot authorize adoption'}
- 조기 기각 전 관측 점수율(전체 계획의 기력 추정 아님): 0.28125
- 실제 완료 쌍: 8
- 채택 하한: 0.0
- 채택 기준 통과: False
- 남은 모든 쌍에서 만점을 얻어도 기존 최종 기준에 미달하므로 미채택했다.

상세 근거: cycle.json, diagnosis/, endgame-train/, endgame-train-quality/,
endgame-heldout-quality/, curve/, compute-control/, development-*/, arena/

규칙 정확성·정답 일치·개발 대국·채택 판정을 구분한다. 기존 모델은 기준 통과 때만 바꾼다.
