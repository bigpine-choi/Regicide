# 전략·전술과 실행 계획 코칭 검증 — 2026-11-02

기준 커밋은 `1bc0663a7d444df7946ad4bf4db4175813ea080d`다. 루크가 해당 수의 원리, 후속 실행, 상대의 대응을 근거 장면과 함께 설명하도록 확장했다. 엔진의 수 선택·평가·분류·탐색 예산은 변경하지 않았다. [사용 방법](../../COACHING.md)을 참고한다.

## 동작과 근거

- 기본 영어 설명은 짧게 유지한다. **핵심 구상 / 실행 계획 / 상대 대응**을 고르면 분석 수순의 해당 장면과 강조 칸을 표시하고 선택한 설명을 자동으로 읽는다. **기본 해설**은 실제 착수 뒤의 보드로 돌아간다.
- 포크, 왕에 대한 핀, 디스커버드 어택, 룩의 열린·반열린 파일, 수비받는 통과폰, 폰으로 지지한 거점, 엔드게임 왕의 활동, 목적 있는 전개, 폰 레버의 9종을 구분한다. 구체적인 패턴이 없으면 기존 해설을 유지한다.
- `Bc4 Nf6 d3`는 전개 이후 상대 나이트가 공격한 e4 폰에 d3가 새 수비를 더한다는 점을 연결한다. 룩이 상대 진영의 일곱 번째 줄에 들어가는 수, 폰 구조 변화로 통과폰이 생기는 응수 등도 보드로 확인한다.
- 수비는 실제로 재포획할 수 있는 기물만 센다. 핀된 공격자, 상대 진영의 시작 칸에서 나온 기물, 퀸만 교환했지만 다른 기물은 거의 모두 남은 국면을 잘못 설명하지 않도록 제한한다. 핀된 기물의 선상 이동과 디스커버드 어택의 뒤쪽 공격자 포획도 구분한다.
- 좋은 수는 실제 착수의 수순, 실수 교정은 다른 추천 수의 수순을 설명한다. 합법 수와 프레임을 대조하고, 정확한 점수 비교가 없는 수순이나 잘못된 근거는 사용하지 않는다. 현재·상대 사용자 관점은 같은 사실을 다른 인칭으로 읽는다.
- 저장된 한 가지 예상 수순의 조건부 계획이다. 공격을 기물 획득으로 단정하거나 추가 수를 만들어 붙이지 않는다. 종국이면 메이트·무승부 설명이 계획과 상대 응수보다 우선하며, 자동 무승부 뒤에 체크 대응을 요구하지 않는다.
- 기존 최대 6착수 해설의 해당 단계에도 구체적인 원리를 연결한다. 원래 UCI/SAN·프레임·길이·종국·물질 비교는 보존한다.
- 다른 수·기보·색·총평·연습·재생으로 이동하면 선택한 설명과 음성을 취소한다. 연습 정답을 가리는 동안에는 해설도 숨긴다. 총평 전환 시 총평을 읽는 회귀를 추가했다.
- 캐시를 열면 검증한 수순으로 해설 v6를 다시 구성한다. 추가 탐색이나 원본 기보 변경이 없다.

## 실행한 검사

| 검사 | 결과 |
| --- | --- |
| `tests/coach_strategy_tests.py` | 36개 통과, 9종 × 양색 × 3인칭 사례 포함 |
| `tests/coach_strategy_integration_tests.py` | 5개 통과, 기존 6수 해설 연결·원본 보존·종국 우선순위 |
| `tests/coach_continuation_tests.py` | 19개 통과 |
| 기존 coaching / character / explanations / teaching | 각각 25 / 21 / 19 / 16개 통과 |
| `tests/web_coaching_tests.js` | 80개 통과 |
| 실제 Python 보고서 → 웹 VM | 보고서 2개 × 관점 2개, 각 3항목의 문구·음성·보드 확인 |
| Godot 전략 UI | 135개 통과, 잘못된 패킷의 기존 해설 복귀 포함 |
| Godot 기존 6수 / 기존 코칭 | 79 / 434개 통과 |
| 실제 Godot 렌더링 | 1240×860·900×680에서 8장, 장면·보드 크기 38개 통과 |

일반 Godot 검사는 합성 입력과 모의 음성을 사용한다. 렌더링은 실제 Regicide가 생성한 `white-fork-report.json`을 임시 프로젝트 복사본에 넣었다. 화면 8장의 가독성과 배치를 확인했고 사용자 게임과 서버를 종료하지 않았다. 실제 사람의 수동 대국 검수나 스피커로 음성을 청취한 검수는 아니다. 웹은 DOM/음성 모의 환경이며 실제 브라우저 화면을 캡처하지 않았다.

## 실제 엔진 연결과 캐시

`scripts/check_coach_strategy.py`는 다음 합성 국면과 색을 뒤집은 국면에서, 한 수만 기록한 기보를 각각 탐색당 1.5초로 분석했다.

```text
8/3q1k2/8/8/8/5N2/7P/6K1 w - - 0 1
실제 수 Kh1 / 추천 수 Ne5+
```

두 경우 모두 Regicide가 실제 수를 큰 실수로 분류하고 나이트 포크를 추천했다. 3개 인칭의 심화 설명, 합법적인 전체 PV와 프레임, 기존 6수 해설 연결을 확인했다. 캐시의 해설을 의도적으로 변조한 뒤 다시 열어도 엔진을 새로 실행하지 않고 해설을 복원했으며, 원본 PGN 바이트는 같았다. 보고서와 실행 시간은 `real-engine-integration.json`, `white-fork-report.json`, `black-fork-report.json`에 있다.

Windows, Python 3.13.16, python-chess 1.11.2, Godot 4.7.2. 기존 Regicide 0.7.0-dev 실행 파일 SHA-256은 `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`다. 기능 연결 검사이며 경기력·Elo·객관적인 최선수의 증명은 아니다. 현재 지원하는 패턴을 넘어 모든 국면을 이해하는 범용 코치로 평가하지 않는다.

## 재현

```text
.venv-tests/Scripts/python.exe -X utf8 tests/coach_strategy_tests.py
.venv-tests/Scripts/python.exe -X utf8 tests/coach_strategy_integration_tests.py
.venv-tests/Scripts/python.exe -X utf8 tests/coach_continuation_tests.py
.venv-tests/Scripts/python.exe -X utf8 scripts/check_coach_strategy.py
node tests/web_coaching_tests.js
```

Godot은 `godot/`을 `.godot/` 제외 임시 폴더로 복사하고 프로젝트 이름을 `Regicide Strategy QA`로 바꾼 뒤 import한다. 해당 폴더를 `--path`로 하여 `--headless --script res://tests/coaching_strategy_tests.gd`를 실행한다. 렌더링은 headless를 빼고 같은 스크립트에 `-- --screenshots <출력폴더> --report <white-fork-report.json 절대경로>`를 전달한다. 검사 후 테스트 프로세스만 종료한다.

`source-manifest.json`은 생산 코드와 검사 코드의 CRLF→LF 정규화 SHA-256을 기록한다. `synthetic-examples.json`은 모의 분류를 지정한 설명 예시이며 실제 엔진의 추천 결과로 해석하면 안 된다.
