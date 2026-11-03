# 3회 반복 자동 무승부가 반 수 일찍 발생하는 문제

확인일: 2026-11-04. 사용자 실행 중 대국이나 프로세스에는 접근하지 않았다. 규칙 재현과 호스트/보관함 회귀는 새 임시 대국에서 수행했다.

## 원인과 최소 재현

기존 `HumanGame._outcome()`과 `GameArchive._prepare()`는 `board.outcome(claim_draw=True)`를 자동 종료 판정에 사용했다. 이 API는 현재 세 번째 위치뿐 아니라 **다음에 선택할 수 있는 수 하나가 세 번째 위치를 만드는 경우**도 무승부로 반환한다. 반복 횟수 자체를 잘못 세는 문제가 아니라, 예정수로 청구할 수 있는 권리를 착수 전 자동 종료에 적용한 정책 차이다.

최소 기보: `1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1`.

| 시점 | 현재 위치 3회 | 예정수 청구 가능 | 기존 호스트 | 수정 호스트 |
| --- | --- | --- | --- | --- |
| 4.Ng1 직후, 7 ply | 아니오 | 흑이 ...Ng8를 선언하면 가능 | 무승부 | 계속 진행 |
| 실제 4...Ng8 직후, 8 ply | 예 | 가능 | 무승부 | 무승부 |
| 대신 실제 4...e5 직후 | 아니오 | 아니오 | 이미 대국이 끝났음 | 계속 진행 |

같은 문제는 50수 규칙에서도 확인됐다. 비가역 수 없이 99 halfmove가 지난 위치에서 다음 조용한 수가 100을 만들 수 있다는 이유로 착수 전 종료했다. 수정 후 실제 100 halfmove에서 종료하며, 폰 이동이나 포획을 선택하면 카운터를 0으로 재설정한다.

## 공식 규칙과 비교

- [Chess.com: 반복 무승부](https://support.chess.com/en/articles/8573042-i-got-a-draw-by-repetition-how-did-that-happen): 동일 위치가 실제 세 번째로 나타나면 자동 무승부다. 기물 배치, 차례, 캐슬링 권리와 합법적인 앙파상 가능성이 같아야 한다. 연속 반복일 필요는 없다.
- [Chess.com: 무승부 종류](https://support.chess.com/en/articles/8572743-how-do-draws-work): Live와 Daily에서 3회 반복 및 50수 조건이 충족된 후 자동으로 종료한다.
- [FIDE Laws, Article 9](https://handbook.fide.com/chapter/e012023): 9.2.1은 변경할 수 없는 예정수를 적고 선언하는 청구, 9.2.2는 이미 생긴 세 번째 위치의 청구를 구별한다. 9.2.3은 동일 위치의 조건, 9.3은 50수 청구를 규정한다. 9.6은 실제 5회 반복과 75수의 강제 무승부이며, 마지막 수가 메이트면 메이트가 우선한다. 따라서 예정수 청구 자체는 규칙상 가능하지만, 어떤 수를 둘지 선택하지 않은 앱 대국을 자동으로 끝낼 근거와는 구별해야 한다.
- [python-chess 공식 API](https://python-chess.readthedocs.io/en/stable/core.html#chess.Board.can_claim_threefold_repetition): `can_claim_threefold_repetition()`은 다음 합법수의 반복도 포함한다. `is_repetition(3)`은 현재 위치만 검사한다. `can_claim_fifty_moves()`와 `is_fifty_moves()`도 같은 구별이 있다.

## 변경 범위

`scripts/game_outcome.py`의 `automatic_outcome(board)`를 자동 종료의 공통 함수로 추가했다. `outcome(claim_draw=False)`를 먼저 적용해 메이트/스테일메이트/기물 부족/5회 반복/75수 우선순위를 보존하고, 현재의 `is_fifty_moves()`와 `is_repetition(3)`만 추가 적용한다. 수 선택 후보의 청구 위험 필터는 보수적으로 유지한다. 대국 코칭의 분석 연속성 정책도 별개다.

새 기록의 `DrawPolicy`는 `automatic draw on completed third repetition or 50-move condition`이다. 보관함은 미완료 기록을 예정수 청구권만으로 종료 처리하지 않는다. 이전 정책으로 명시적으로 저장된 완료 무승부는 실제 청구 가능 근거를 확인한 뒤 과거 결과와 정책 표시를 보존한다. 기보 파일 원본을 수정하거나 가상의 수를 추가하지 않는다.

## 재현·검증

변경 전 `before-fix-diagnosis.json`은 실제 호스트/보관함 함수로 7 ply 조기 종료를 확인한 135개 검사의 증거다. 소스 함수와 SHA-256을 함께 기록했다. 수정 후 `reproduce.py`는 146개 검사를 실행하고 `after-fix-diagnosis.json`에 실제 호스트, 기존 청구 정책, 현재 위치 기준 비교 결과와 소스 지문을 기록한다.

```powershell
.\.venv-tests\Scripts\python.exe docs/checks/2026-11-04-repetition-draw/reproduce.py
.\.venv-tests\Scripts\python.exe -m unittest discover -s tests -p game_outcome_tests.py
```

검증 범위: 7→8 ply, 반복 회피, 캐슬링 권리 소실, 합법/불가/핀으로 금지된 앙파상, 차례 차이, 전체 수 이력 보존, FEN만으로 반복을 판정할 수 없음, 50/75수, 5회 반복, 메이트 우선, 폰/포획 초기화, 이전 저장 기록 호환, PGN 결과·정책·실제 수 일치. 엔진이 어떤 수를 선택해야 하는지에 대한 경기력 실험은 아니다.

`regression.log`/`regression.json`: 공통 결과 판정·대국 호스트·보관함 69개 실행, 실패 0, 오류 0, 3개 스킵. 스킵은 실제 엔진 경로를 받지 않은 기존 스모크 2개와 계정의 심볼릭 링크 권한 제한 1개다. 이후 추가한 PGN 2개를 포함한 `game_outcome_tests.py` 19개도 모두 통과했다. 중복을 제외하면 71개 중 68개 통과, 3개 스킵이다. GUI 육안 검증은 이 기록의 범위에 포함하지 않는다.
