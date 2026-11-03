# 코칭 기호 통일과 착수 대기 추가 0.5초 단축

2026-11-04. 분류 계산과 탐색 예산은 그대로 두고 표시 기호와 계산 후 대기만 수정했다.

## 코칭 배지

Chess.com의 [공식 분류 설명](https://support.chess.com/en/articles/8572705-how-are-moves-classified-what-is-a-blunder-or-brilliant-etc)과 [Game Review 안내의 분류 이미지](https://www.chess.com/terms/game-review)를 확인했다. 공식 이미지의 기호와 바탕색에 맞춰 Godot·웹 표시를 통일했다. 기존 이미지 파일을 제품에 복사하지 않고, 원과 문자로 직접 그린다.

| 기존 Regicide 분류 | 표시 기호 | 바탕색 |
| --- | --- | --- |
| Great / 탁월 후보 | `!` | `#749bbf` |
| 추천 수 | `★` | `#81b64c` |
| 좋은 수 | `✓` | `#95b776` |
| 아쉬운 수 | `?!` | `#f7c631` |
| 실수 | `?` | `#ffa459` |
| 큰 실수 / 메이트 허용 | `??` | `#fa412d` |
| 메이트 기회 놓침 | `×` | `#ff7769` |

기호는 흰색이다. `…` 판단 보류는 기존 기능으로 유지한다. 기존 Great 후보의 `!!` 표기는 `!`로, missed_mate의 `?`는 `×`로 바로잡았다. `!!`는 Chess.com의 Brilliant(적절한 희생)에 해당하므로 기존 Great 후보에 붙이지 않는다. Brilliant·Excellent·Book 같은 새 분류를 만들어내거나 자체 점수·분류 임계값을 변경하지 않았다. 같은 기호를 쓰는 것이 두 서비스의 판정이 같다는 뜻은 아니다.

저장된 보고서는 검증된 탐색 근거에서 배지를 다시 만들어 옛 `!!`를 `!`로 바꾼다. 잘못된 FEN·착수·도착 칸·기호 조합을 숨기는 기존 검증, 이동 중·변화 수순·재도전의 배지 숨김은 유지했다.

## 착수 시간

정책은 `regicide-engine-pacing-v11`, `post_search_reduction_ms`는 기존 1,000ms에서 1,500ms로 변경했다. 같은 위치·시계·시드에서 검색용 목표와 기본 검색·접전 회복·기물 안전 검사 예산을 유지한다. 같은 게임의 리듬 난수 이름 공간도 유지했다. 과거 저장 기록의 자체 차감값도 그대로 읽는다.

현재 남아 있는 계산 후 대기를 **최대 0.5초 더** 줄인다. 계산이 이미 짧아진 목표를 넘었거나 회복·안전 검사·형세 재검사로 대기가 없는 착수는 추가로 줄이지 않는다. 실제 대국 전체 평균이 정확히 0.5초 감소했다고 주장하지 않는다. 다음 착수의 잔여 시계가 많아지는 효과는 생길 수 있다.

`pacing-receipts.json`은 통제된 호스트와 가상 시계로 생성한 실제 착수 처리 기록이다. 검색 시간·선택 수를 고정한 0.4초 검색 사례에서 착수까지 시계 차감은 3.0초에서 2.5초로 줄었고, 대기도 500ms 줄었다. 3.5초 검색 사례는 검색을 끝까지 수행하고 양쪽 모두 대기 0으로 착수했다. 새 실제 대국 평균이나 경기력 측정은 아니다.

## 수행한 검사

| 검사 | 결과 |
| --- | --- |
| Python 시간 배분·초반 상한·리듬·회복·호스트 시계 | 89개 통과 |
| Python 코칭·백/흑 Great 후보·저장 보고서 기호 재생성 | 28개 통과 |
| 브라우저 코칭·배지·옛/위조 기호 거부·특수 수·뒤집기 | 90개 통과 |
| Godot 합성 코칭 화면·배지·특수 수·재도전 | 456개, 실패 0 |
| Godot 렌더 | 1240×860 / 900×680에서 20장 생성 |

렌더는 11월 1일 저장한 자체 엔진 평가 자료의 기호만 다시 생성한 결과다. 새 엔진 분석으로 표현하지 않는다. 생성한 장면 중 큰 화면·작은 화면의 `!`와 작은 화면 `??`를 직접 이미지로 확인했다. 실제 기물 위 배지와 안내 문구가 일치하고 작은 화면에서도 원 밖으로 넘치지 않았다. 원본 근거 해시는 `render-provenance.json`에 기록했다. 사용자 대국·설정은 사용하지 않았고 별도 임시 프로젝트를 화면 밖에서 실행했다.

이 단계의 기호 검증에는 Stockfish 코칭 게이지를 포함하지 않았다. 이후 [게이지 실제 연결 검증](../2026-11-04-coaching-reference/README.md)과 [최종 통합 검사](../2026-11-04-integration/README.md)를 완료했다.

재현 명령:

```powershell
.venv-tests/Scripts/python.exe -m unittest discover -s tests -p '*pacing*tests.py'
.venv-tests/Scripts/python.exe -m unittest discover -s tests -p coaching_tests.py
node --test tests/web_coaching_tests.js
.venv-tests/Scripts/python.exe docs/checks/2026-11-04-coaching-symbols-pacing/capture_pacing_receipts.py
.venv-tests/Scripts/python.exe docs/checks/2026-11-04-coaching-symbols-pacing/validate_move_symbols.py
```
