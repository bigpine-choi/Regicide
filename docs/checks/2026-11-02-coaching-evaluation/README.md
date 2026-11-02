# 코칭 화면의 현재 위치 평가 표시

2026-11-02. 코칭에서 분석된 위치를 보고 있어도 Godot의 ‘둔 뒤’·기보 따라가기 화면은 평가 게이지에 값을 전달하지 않았다. 웹은 선택한 해설 행의 값을 사용해 실제 재생 위치와 점수가 어긋날 수 있었고, 예상 수순의 후속 위치에도 시작점 점수를 재사용했다.

두 화면 모두 **현재 표시한 위치와 일치하는 최선수 탐색의 점수**를 사용하도록 연결했다. 현재 위치의 반수·FEN, 해당 분석 행의 `fen_before`, 탐색 수순 첫 프레임의 FEN을 대조한다. 실제로 둔 수의 제한 탐색 점수는 비교 카드에 그대로 남기며, 이를 착수 후 위치를 새로 탐색한 값으로 표시하지 않는다. 점수는 백 기준이며 판을 뒤집어도 부호를 다시 바꾸지 않는다.

예상 수순은 평가된 시작 위치에서만 점수를 표시한다. 후속 위치나 다음 분석이 없는 마지막 비종국 위치는 별도 평가 없음으로 표시한다. 실제 종국 프레임은 승패·무승부를 표시한다. 재도전의 정답 공개 전에는 점수와 게이지를 숨긴다. 코칭 프레임 생성도 `claim_draw=False`로 바꿔, 아직 청구하지 않은 3회 반복·50수 무승부 가능성을 이미 끝난 대국으로 표시하지 않게 했다. 캐시의 예상 수순 프레임은 재생성되므로 과거 프레임의 잘못된 종국 값도 그대로 재사용하지 않는다.

## 검증

| 실행한 검사 | 결과 |
| --- | --- |
| `node tests/web_coaching_tests.js` | 90개 통과 |
| `tests/coaching_tests.py` 백엔드 회귀 검사 | 28개 통과 |
| Godot 4.7.2 headless 합성 장면 검사 | 455개 통과 |
| 자체 엔진·HTTP를 사용한 격리 Godot 통합 검사 | 35개 통과 |
| 편집기 캐시 없는 새 체크아웃 실행 | 통과 |
| 통합 검사의 원래 대국 상태·저장 기보 보존 | 변경 없음 |

최종 통합 출력은 `build/coaching-evaluation-fix/integration-final`이다. [합성 장면 결과](coaching-native-synthetic.json), [HTTP 장면 결과](coaching-native-live.json), [상태 보존·요청 경로 기록](coaching-integration.json), [요약](validation-summary.json)을 함께 보관했다. 수정 도중 실행한 앞선 `integration` 결과와 구분한다.

별도로 현재 `CoachingSession`과 활성 자체 엔진을 사용해 `1. f3 e5 2. g4 Qh4#`를 500ms 설정으로 실제 분석했다. [actual-report.json](actual-report.json)은 캐시 없이 완료한 4반수 보고서이며, 마지막 프레임만 흑 승리로 표시한다. 이 보고서는 합성 UI 시험 자료와 구분한다.

`godot/tests/coaching_evaluation_render.gd`로 이 보고서를 별도 오프라인 창에 표시하고 6개 화면을 직접 확인했다. [f3 이후 -0.34](native-after-f3.png), [흑 메이트 예고](native-black-mate-threat.png), [뒤집은 판](native-black-mate-threat-flipped.png), [흑 승리](native-terminal-black-win.png), [미평가 예상 위치](native-unanalysed-pv.png), [재도전 가림](native-retry-hidden.png)이 모두 의도대로 표시됐다. 별도 Windows 창에서도 메이트 예고와 종국 상태를 확인했다. 이는 보고서 재생 화면 검증이며, 실제 HTTP 연결은 위의 격리 통합 검사로 확인했다. 사용자의 진행 중인 대국이나 저장 기보를 화면 검증에 사용하지 않았다.

이번 변경은 평가 표시와 종국 프레임 판정 수정이다. 코칭의 최선수 탐색·정답 선택 정책, 보스 정책, 엔진 평가 함수는 변경하지 않았다. 추가 수읽기 없이 기존 분석 결과를 연결하므로 모든 예상 수순 위치에 평가가 새로 생기는 기능은 아니다.
