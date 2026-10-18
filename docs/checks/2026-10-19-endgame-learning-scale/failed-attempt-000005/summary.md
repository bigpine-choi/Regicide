# 종반 자료 연구

- 상태: failed
- 영속 시도: 5
- 새 모델 채택: False

후보는 첫 시드의 가장 큰 사전 자료 규모로 고정한다. 개발 성적과 시험 오차로 후보를 바꾸지 않는다.


실패: source PGN is missing a game or contains illegal moves

상세 근거: cycle.json, diagnosis/, endgame-train/, endgame-train-quality/,
endgame-heldout-quality/, curve/, compute-control/, development-*/, arena/

규칙 정확성·정답 일치·개발 대국·채택 판정을 구분한다. 기존 모델은 기준 통과 때만 바꾼다.
