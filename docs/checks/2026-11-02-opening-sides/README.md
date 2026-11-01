# 백·흑 오프닝 표시와 Chess.com 명칭 — 2026-11-02

일반 대국·기보 재생의 오프닝 안내를 **백의 오프닝 / 흑의 대응 / 전체 세부 변형**으로 나눴다. 확인된 이름은 Chess.com 표기를 우선한다. 기준 코드는 `db225ff2ff4f8426671ce09389e4f704ed23b80e`이며, 이번 변경은 자체 엔진의 수 선택·평가와 별도인 안내 기능이다.

## 확인한 문제와 변경

기존 위치 판별은 이미 전체 수순 일치를 우선하고 다른 수순으로 도달한 같은 위치를 구분하고 있었다. 일반적인 수순 매칭 오류라고 주장하지 않는다. 개선 대상은 한 개의 합쳐진 이름만 보이는 화면, 서로 다른 자료의 명칭, 알려진 수순의 누락, 그리고 백·흑의 역할 구분이다.

- `1.e4 c5`: 백 King's Pawn Opening / 흑 Sicilian Defense.
- `1.e4 c5 2.c3`: 백 Alapin Variation (Sicilian) / 흑 Sicilian Defense.
- `1.e4 e5 2.Nf3 Nc6 3.Bb5 a6`: 백 Ruy Lopez Opening / 흑 Morphy Defense.
- `1.d4 Nf6 2.c4 g6`: 원자료의 West Indian 대신 공식 King's Indian Defense 표기. 이후 `…d5`로 Grünfeld 위치에 도달하면 새 분류를 따른다.
- `1.d4 d6 2.c4 g6`: Modern Defense 이름을 표시용 위치 색인에 보완했다. 확인하지 않은 ECO는 비워 둔다.

공식 이름을 특정 진영의 역할에 연결할 때는 명시적인 계열·변형 규칙을 사용한다. `Sicilian Defense: French Variation`을 French Defense로 오인하지 않고, English의 `…c5`를 Sicilian Defense로 단정하지 않는다. 확인된 별도 이름이 없으면 실제 흑의 대응 수와 미확정 상태를 보여 준다. 검수 중 발견한 Portuguese Opening의 백·흑 역할 구분과 Accelerated London 누락도 수정했다.

FEN만 있는 위치는 **현재 배치**, 목록에서 벗어난 뒤 남기는 이름은 **마지막 확인**으로 표시한다. 과거 첫 수를 추측하지 않는다. 이름이 길어져도 두 행의 높이를 유지하고 전체 이름은 툴팁에 보존한다.

## Chess.com 우선 정책의 범위

[역할·시작 수순 출처](source-verification.json)와 [명칭 대조 출처](terminology-verification.json)는 공식 Chess.com 페이지와 원자료 저장소를 기록한다.

- 9개 계열 이름을 통일했다: King's Pawn Opening, Queen's Pawn Opening, Ruy Lopez Opening, Alekhine's Defense, Owen's Defense, Bird's Opening, Nimzowitsch-Larsen Attack, King's Fianchetto Opening, Indian Game.
- 이 계열에 속한 원자료 538행 중 정확한 수순 보정이 우선하는 4행을 제외한 **534행**에 계열명 보정이 적용된다.
- 전체 이름을 정확한 수순·위치로 확인한 보정은 기존 35개에서 **38개**로 늘었다. King's Indian 시작, Réti의 Tennison Gambit과 Santasiere's Folly를 추가했다.
- 이름 있는 끝 위치가 누락된 Modern은 **표시 전용 1행**이다. 원자료나 엔진의 후보 색인에 추가하지 않았다.

계열명 확인은 뒤의 세부 변형명까지 확인했다는 뜻이 아니다. `name_verification=family_only`와 `exact_line`, 각 출처 URL을 현재 이름·다음 한 수·도달할 변형에 구분해 기록한다. Zukertort 전체를 Réti로 일괄 치환하지 않으며 Lemberger, Tennison의 Brigg's Trap 등 미확인 후속 변형은 유지한다. 3,865개 모든 세부 변형이 Chess.com과 완전히 일치한다는 주장은 하지 않는다.

코칭의 공식 이름 표시에도 같은 함수를 사용한다. `e4 e5`의 기본 전개 설명은 표시 이름이 Game에서 Opening으로 바뀌어도 유지된다. 전략 설명이나 형세 평가를 새 이름만으로 바꾸지 않는다.

## 검증

| 검사 | 결과 |
| --- | --- |
| 오프닝 안내·명칭 | 29 tests 통과 |
| 백·흑 역할·FEN·표시 전용 보완 | 12 tests 통과 |
| 실제 앱 상태·무르기·핸디캡·보관함 HTTP | 11 tests 통과 |
| 기존 오프닝 선택 v1/v2/v3 | 28 / 19 / 13 tests 통과 |
| 코칭 오프닝 / 코칭 연결 | 30 / 2 tests 통과 |
| 다이내믹 수 선택 정책 | 21 tests 통과 |
| 브라우저 실제 app.js | 12 합성 사례 및 실제 자료 포함 13 사례 통과 |
| Godot 실제 장면 | 51항목 통과; 작은 창에서 긴 이름에도 보드 크기 동일 |
| 생산 실행기 | 임시 HTTP 대국 생성·e4·실제 엔진 응수·무르기·정상 종료 통과 |

별도 검수자는 구현 전에 공식 수순 34개를 선정했고 코드 검토 중 Portuguese 역할 사례를 추가했다. FEN의 이력 오인, 무르기·재생, 마지막 확인과 수순 전환을 검사했다. 원자료 3,865행의 구조 검사도 수행했지만 이는 모든 이름을 외부에서 검증했다는 뜻이 아니다. 계열명 보정과 전체 변형 검증을 구분하는 별도 명칭 감사 결과도 보존했다.

원자료 TSV·LICENSE·manifest 바이트와 엔진 후보 색인을 유지했다. 원래 색인은 3,865행·7,983위치, 안내 전용 색인은 3,866행·7,984위치다. 표본 4개 위치의 `opening_variety.plan`도 표시 색인 구축 전후 같았다. 엔진의 기력이나 속도 변화 실험이 아니다.

실제 오프닝 자료를 넣은 Godot 화면 2개를 숨긴 렌더 창에서 생성하고 픽셀을 확인했다. [Morphy 화면](ui/morphy/opening-compact.png), [FEN Sicilian 화면](ui/sicilian-position/opening-compact.png). 이는 자동 렌더 검수이며 사용자의 실행 중인 게임을 조작한 수동 검수가 아니다. 개인 기보·게임은 사용하거나 종료하지 않았다.

최초 Godot 임시 프로젝트 import는 네이티브 오류로 끝났으나 명시적 종료 옵션을 준 격리 재실행과 실제 장면 검사는 통과했다. 원 로그를 남겼다. 기존 코칭 테스트의 `d4 Nf6 c4 g6` 중립 이름 기대는 공식 King's Indian 확인과 맞지 않아, 미확정 검사는 직전 `d4 Nf6 c4`에 유지하고 확인 후 이름·Grünfeld 전환 검사를 추가했다.

## 재현과 파일

저장소 루트의 `.venv-tests/Scripts/python.exe`로 `tests/opening_guide_tests.py`, `tests/opening_sides_tests.py`, `tests/opening_guide_integration_tests.py`, `tests/coach_openings_tests.py`를 실행한다. 브라우저 검사는 `node tests/opening_guide_web_tests.js`다.

`backend/`는 최종 회귀 로그, `audit/`는 독립 기대 사례·스크립트·관측 원본, `ui/`는 실제 장면/브라우저 검사와 렌더 자료, `production/`은 임시 생산 실행 검사다. `production/production_smoke.py`의 원래 실행 위치는 `build/opening-sides/production_smoke.py`다. 도우미 스크립트의 절대 경로는 당시 환경의 기록이며 다른 환경에서는 저장소·Godot 경로를 맞춘다.

`artifact-manifest.json`은 최종 제품 소스의 LF 정규화 해시와 증거의 원시 바이트 해시를 구분한다. 커밋 전 실제 Git 저장 바이트를 대조한다. 네이티브 실행 파일은 재빌드하거나 교체하지 않았다.
