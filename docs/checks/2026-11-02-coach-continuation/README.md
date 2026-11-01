# 추천 수의 6수 해설 재생 검증 — 2026-11-02

기준 커밋은 `6321e1c813e94cdff5d808cfd342f079362aff7a`다. 복기에서 실수 대신 추천하는 수를 시작으로 양쪽이 번갈아 두는 최대 여섯 번의 착수를 보드와 영어 설명으로 재생한다. 엔진의 수 선택·평가·분류·탐색 예산은 변경하지 않았다. [사용 방법](../../COACHING.md)을 참고한다.

## 동작과 근거

- 루크 카드의 **왜 더 좋은지 · 6수 보기**에서 시작한다. 추천 이유 → 착수별 효과와 상대 응수 → 해당 수순의 결과 및 체스 원리 순서다.
- 원래 분석의 합법적인 `best.pv_uci`와 `best.pv_frames`를 그대로 사용한다. 정확한 점수 경계, 실제 수와 다른 추천, 시작 위치와 각 프레임의 일치를 확인한다. 짧은 PV를 새로운 수로 채우거나 추가 탐색하지 않는다.
- `coach.continuation`에는 실제 길이, 조기 종료 여부, 도입·마무리 및 1..N 단계별 UCI/SAN/색/설명/강조 칸을 담는다. 백·흑 사용자 관점은 같은 사실을 다른 인칭으로 읽는다.
- 단일 예상 수순을 강제 미래나 강제 메이트의 증명으로 표현하지 않는다. 기물 비교는 같은 출발점과 같은 착수 길이에서만 하고, 실제 수순이 먼저 메이트로 끝난 경우는 별도로 설명한다. 조용한 수에 확인되지 않은 목적을 붙이지 않는다.
- 음성이 끝난 뒤 다음 장면으로 이동한다. 음성이 없으면 문장 길이에 비례해 2.2~9초를 둔다. 일시정지는 음성도 멈추며, 재개는 현재 문장부터 다시 읽는다. 수동 이전/다음은 해당 설명을 한 번 읽고 자동 진행을 멈춘다.
- 마지막 설명을 읽는 중에도 일시정지할 수 있다. 마지막 수에 도착한 상태와 설명을 모두 마친 상태를 구분한다. 다른 국면·색·기보·재도전·총평으로 이동하거나 화면을 숨기면 이전 재생을 취소한다.
- 이전 코칭 캐시를 열면 검증한 수순으로 해설 v5를 재구성한다. 원본 PGN/JSON이나 현재 대국은 변경하지 않는다.

## 실행한 검사

| 검사 | 결과 |
| --- | --- |
| `tests/coach_continuation_tests.py` | 신규 19개 통과 |
| `tests/coaching_tests.py` | 25개 통과 |
| `tests/character_coach_tests.py` | 21개 통과 |
| `tests/coach_explanations_tests.py` | 19개 통과 |
| `tests/coach_teaching_tests.py` | 16개 통과 |
| `tests/coaching_http_tests.py --binary build/regicide.exe` | 실제 엔진 HTTP 흐름·취소·캐시·원본 격리 통과 |
| `tests/web_coaching_tests.js` | 75개 통과 |
| `tests/web_board_motion_tests.js` | 21개 통과 |
| Python에서 생성한 패킷 → 웹 VM | 보고서 2개 × 사용자 관점 2개 = 4회 통과 |
| Godot `coaching_continuation_tests.gd` | 신규 79개 통과 |
| Godot 기존 `coaching_tests.gd` | 기존 합성 검사 434개 통과 |
| 실제 렌더링 1240×860 / 900×680 | 시작·3수·6수 화면 6장, 크기/캡처 검사 12개 통과 |

신규 검사는 양색·길이 제한·잘못된 근거 거절·짧은 수순·메이트/스테일메이트/자동 무승부·특수수·캐시 재구성을 포함한다. UI 검사는 음성 종료 대기, 마지막 설명 중 일시정지, 수동 수 넘김의 음성, 이전 작업의 지연 콜백 차단, 실제 기보 복귀와 연습 정답 격리를 포함한다. 별도 재생 창을 임시 프로젝트 복사본으로 열었고 사용자 게임이나 서버를 중지하지 않았다.

Godot 화면은 합성 입력을 사용하는 독립 렌더링 장면이다. 스크린샷을 직접 열어 가독성과 배치를 확인했지만 사람이 실제 대국을 수동 조작했다는 의미는 아니다. 웹은 DOM/음성 모의 환경이며 실제 브라우저 화면 검수는 수행하지 않았다. 기존 434개 Godot 회귀 후 발견한 음성 일시정지 문제는 신규 회귀를 추가하여 최종 79개 검사로 확인했다.

## 실제 Regicide 연결

`scripts/check_coach_continuation.py`는 외부 엔진 없이 실제 Regicide로 아래 합성 국면과 색을 뒤집은 국면을 각각 1.5초씩 비교했다. 각 기보에는 한 수만 기록했다.

```text
3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1
실제 수: Qd4 / 추천 수: Qxd8+
```

두 경우 모두 실제 엔진이 실수로 분류하고 6착수 이상인 합법 추천 PV를 반환했다. 모든 6단계를 원래 프레임과 대조했으며 두 사용자 관점에서도 일치했다. 캐시의 해설을 의도적으로 변조한 뒤 다시 열어도 새 엔진 실행 없이 올바른 해설이 재생성됐고, 원본 PGN 바이트는 같았다. 이 결과와 전체 보고서는 `real-engine-integration.json`, `white-exchange-report.json`, `black-exchange-report.json`에 있다.

Windows, Python 3.13.16, python-chess 1.11.2, Godot 4.7.2. 엔진은 Regicide 0.7.0-dev의 기존 수식 평가이며 실행 파일 SHA-256은 `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`다. 기능 연결 검증이며 경기력·Elo·최선수의 객관적 정답을 증명하는 실험은 아니다.

## 재현

```text
.venv-tests/Scripts/python.exe -X utf8 tests/coach_continuation_tests.py
.venv-tests/Scripts/python.exe -X utf8 scripts/check_coach_continuation.py
node tests/web_coaching_tests.js
node tests/web_board_motion_tests.js
```

Godot은 `godot/`을 `.godot/` 제외 임시 폴더로 복사하고 프로젝트 이름을 `Regicide Continuation QA`로 바꾼 뒤 import한다. 해당 폴더를 `--path`로 하여 `--headless --script res://tests/coaching_continuation_tests.gd`를 실행한다. 렌더링은 headless를 빼고 같은 스크립트에 `-- --screenshots <출력폴더> --report <white-exchange-report.json 절대경로>`를 전달한다. 스크립트는 검사 후 스스로 종료한다.

`source-manifest.json`은 이번 생산 코드·검사 코드의 CRLF→LF 정규화 SHA-256을 기록한다. `screenshots/`는 최종 문구와 마지막 설명 일시정지 수정 후 다시 캡처했다.
