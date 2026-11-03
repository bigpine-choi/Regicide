# 스파링 평가 게이지 척도 수정

- 원인: Stockfish 심판의 WDL 기대 점수 `(wins + draws / 2) / 1000`을 `white_share`로 전달하여, 일반 cp 게이지와 달리 큰 우세에서 끝까지 포화했다.
- 변경: 일반 게이지와 동일한 `0.5 + 0.5 * cp / (abs(cp) + 400)`을 사용한다. +400cp는 75%, -400cp는 25%다. 메이트는 부호에 따라 100%/0%로 표시하며 WDL은 별도 메타데이터로 유지한다.
- 범위: 표시용 비율만 변경했다. 원래 평가값과 수 선택 정책은 변경하지 않았다.
- 자동 검증: `sparring_referee_tests.py` 27개, `sparring_evaluation_state_tests.py` 8개, Godot `evaluation_bar_tests.gd` 58개, `web_hint_tests.js` 29개 모두 통과.
- 회귀 검증: WDL 포화/부재에 무관한 cp 척도, 양수/음수, 균형, 단조 증가, 메이트 구분을 확인했다.
- 실제 GUI 대국을 통한 수동 검증은 수행하지 않았다. 실행 중인 앱에는 백엔드 재시작이 필요하다.
