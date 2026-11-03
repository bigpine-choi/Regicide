# 코칭 Stockfish 형세 게이지 검증

선택한 코칭 보드의 형세 게이지만 로컬 Stockfish로 별도 분석한다. 루크의 추천 수·실수 분류·배지·비교 카드와 저장된 자체 엔진 점수는 유지한다. 실제 대국용 심판과 코칭 참고용 심판은 별도 인스턴스이며, 전체 기보를 포함한 위치 문맥으로 평가와 최근 32개 완료 결과를 구분한다.

## 수행한 검사

| 검사 | 결과 |
|---|---|
| Python 서비스·인증 HTTP 계약 | 12개 통과 |
| 새 브라우저 참고 게이지 검사 | 8개 통과 |
| 기존 브라우저 코칭 회귀 | 90개 통과 |
| 새 Godot 합성 검사 | 21개 통과 |
| 기존 Godot 합성 회귀 | 456개 통과 |
| 실제 Stockfish → 인증 HTTP → Godot | 26개 통과(앞의 합성 21개 포함) |
| 기존 실제 HTTP 코칭 흐름 | 35개 통과 |
| 실제 Stockfish 위치 검사 | 7개 통과: 비종료 6개 ready, 체크메이트 1개 규칙 판정 |

Python과 브라우저 검사는 백·흑 점수 방향, 메이트 승자, 전체 기보와 FEN 일치, 반복 이력별 캐시, 지난 위치의 응답 차단, 정답 숨김, 자체 보고서 불변, 외부 엔진 부재, 실패 후 제한된 재시도를 포함한다. 실패 재시도는 같은 위치에서 3초 간격 최대 두 번이며, 완료 후 계속 재시작하지 않는다. 자체 점수가 없는 변화 수순의 중간 위치도 실패를 명시한다.

실제 엔진 검사는 시작 위치, 1.e4 이후, 흑/백 각각의 메이트 1수, 3회 반복 청구 가능 위치, 50수 무승부 청구 가능 위치, 실제 체크메이트를 사용했다. 청구 가능한 무승부는 코칭의 계속된 PGN과 일치하도록 종료로 확정하지 않는다. 캐시 재사용 및 연습 숨김도 확인했다. 평가에는 기본 1초·1스레드·32MiB Hash·제한 없는 Stockfish 18을 사용했다. 수순이나 추천 수는 참고 API 응답에 포함하지 않는다.

Godot는 별도 임시 프로젝트 이름과 임시 기보 저장소에서 headless로 실행했다. 실제 사용자 게임·앱·리체스 자격 증명을 열거나 닫지 않았다. 실제 HTTP 전후에 진행 중 대국 상태, 자유 분석 상태, 원본 JSON/PGN 파일의 바이트가 동일함을 확인했다. 코칭 참고 요청만으로 자체 코칭 보고서가 바뀌지 않는 것도 별도로 확인했다. 허용된 POST는 기보 미리보기, 코칭, 코칭 참고 형세, 상시 오프닝의 읽기 조회뿐이다.

## 근거와 재현

- [실제 Stockfish 평가](stockfish-smoke.json), [새 Godot 합성](reference-godot.log), [기존 Godot 합성](coaching-regressions.log)
- [실제 게이지 연결](reference-live.log), [기존 실제 코칭](coaching-live-regressions.log), [HTTP 격리](http-isolation.json)
- [소스·엔진 해시](source-hashes.json), [검사 프로세스 정리](process-cleanup.json)
- 재현: `tests/coaching_reference_tests.py`, `tests/coaching_reference_web_tests.js`, `tests/coaching_reference_validation.py`.

실제 엔진/Godot 통합 명령:

```powershell
.venv-tests\Scripts\python.exe tests/coaching_reference_validation.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64.exe --binary build/runtime-engines/5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba/regicide.exe --stockfish build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe --output docs/checks/2026-11-04-coaching-reference
```

화면 검사는 합성 입력 및 headless 장면 검사다. 사람이 실제 앱에서 조작한 수동 검수나 전체 경기력·Elo 향상을 뜻하지 않는다. 제한 시간 Stockfish 평가도 더 깊은 탐색에서 달라질 수 있다. 초기 통합 실행은 상시 오프닝의 읽기 API가 이전 허용목록에 없어서 마지막 경로 검사만 실패했고, `/api/opening-explorer`를 명시적으로 추가한 뒤 재검증했다.
