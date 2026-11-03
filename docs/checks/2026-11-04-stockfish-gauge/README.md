# 공통 Stockfish 형세 게이지 검증

기본·갬빗·스파링의 실시간 형세와 분석 화면은 별도 `StockfishGauge`를 사용한다. 코칭은 독립 `CoachingReference`를 유지한다. 실제 착수, 추천 수, 수 분류, 시간 배분에 쓰는 Regicide 자체 평가는 별도로 보존한다. 형세 참고 서비스는 추천 수/PV를 공개하지 않는다. Stockfish가 계산 중이거나 사용할 수 없으면 표시 점수는 비어 있으며 자체 점수로 대신 채우지 않는다. 종료 결과는 체스 규칙과 실제 대국 결과를 우선한다.

`StockfishGauge`는 `SparringReferee`의 기존 비동기 UCI 경로를 독립 소유한다. 스파링 후보 검증 서비스와 인스턴스를 공유하지 않는다. 소유자별 비공개 세션·세대 번호와 공개 대국 ID·수정 번호·FEN·차례를 분리하여 이전 요청, 무르기, 같은 FEN으로 돌아오기, 새 대국, 종료 후 결과가 현재 위치를 덮지 못하게 했다. 전체 기보도 복사한다. 다음 수를 두면 가능한 무승부 청구권만으로 참고 계산을 중단하지 않는다.

## 실제 실행: 최종 성공은 native-v3

- [최종 원본 결과](native-v3/result.json): 모든 검사 통과. Python 3.13.16, Windows 11, 2026-11-04 실행.
- 자체 엔진: `Regicide 0.7.0-dev`, SHA-256 `5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba`.
- 참고 엔진: `Stockfish 18`, SHA-256 `9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6`. 약화 옵션 없이 1스레드, Hash 32MiB. 실시간/분석 참고 탐색은 500ms, 코칭 참고는 1000ms이며 기동 시간은 별도 기록한다.
- 실제 HTTP API와 로컬 엔진을 사용했다. 사용자의 대국이나 실행 중인 게임은 조작하지 않고 검증 전용 저장소에 대국을 생성했다.
- 시작 FEN: `6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30`. 빠른 명백한 수를 확인하기 위한 작성 국면이다.

| 모드 | Stockfish 착수 전 평가/깊이 | Regicide 실제 착수 | HTTP 착수 경과 | 생각 중 시계 수정 | 종료 후 평점 저장 |
| --- | --- | --- | ---: | --- | --- |
| 기본 | +3.60 / 20 | Rxd4 (`d2d4`) | 54.009ms | 통과 | 1500 |
| 갬빗 | +3.60 / 20 | Rxd4 (`d2d4`) | 67.366ms | 통과 | 1500 |
| 스파링 | +3.60 / 20 | Rxd4 (`d2d4`) | 1546.060ms | 통과 | 1500 |

각 모드에서 사람/엔진 양쪽 시계의 +10초·-10초를 실제 API로 적용하고, 엔진 검색 또는 대기 활성 중 엔진 시계를 다시 +10초·-10초 수정했다. 모두 합법 수 하나로 완료됐으며 선택 수는 원본 Regicide UCI 반환에 존재한다. 스파링에는 기존 별도 후보 검증 비용도 포함된다. 이 표는 세 번의 기능 검증이며 일반 평균 착수 시간, 모드별 속도 비교나 경기력 증거가 아니다.

분석 화면에서도 실제 Regicide 추천과 별도 Stockfish +3.60/깊이 20을 확인했다. 코칭 참고는 실제 Stockfish +0.35/깊이 18을 반환했다. 분석·코칭 조회 중 실시간 대국 ID·수정 번호·FEN·저장된 대국 JSON이 변하지 않았다. 3모드 착수 뒤 새 위치의 Stockfish 평가도 각각 확인했다. 모든 소유 서비스를 닫았고, 실행 전후 모든 `scripts/*.py`, 하네스 및 양쪽 엔진 바이너리 해시가 같았다. 원본 요청·완성 평가·시계 변경·평점·소스 해시는 최종 JSON에, Regicide 검색/반환은 각 모드의 `native-searches.jsonl`에 남겼다.

앞선 `native-v1`과 `native-v2`는 하네스가 분석용 Regicide 실행에 필요한 `uci` 인자를 빠뜨려 분석 추천이 비어 있는 상태에서 중단했다. `native-v2/basic/native-turn.json`은 그 전에 통과한 실시간 참고/Native 착수/시계 변경의 부분 기록이다. 잘못된 하네스를 수정한 뒤 새 경로에서 전체를 다시 수행한 `native-v3`만 최종 성공 결과로 사용한다. 두 중단을 제품의 실패 또는 성공으로 세지 않는다.

재현:

```powershell
.venv-tests\Scripts\python.exe tests\shared_stockfish_gauge_validation.py --stockfish build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe --output <새 결과 경로>
```

## 결정적 회귀 검사

| 검사 | 결과 | 원본 로그 |
| --- | --- | --- |
| 공통 게이지/분석 격리 | 13 통과 | [gauge-tests.log](gauge-tests.log) |
| 기존 분석 검사 | 56 실행, 50 통과·6 실제 엔진 선택 검사 제외 | [analysis-regressions.log](analysis-regressions.log) |
| 기존 심판/후보 검증/호스트 | 113 통과 | [referee-regressions.log](referee-regressions.log) |
| 코칭 참고 서비스 | 12 통과 | [coaching-reference-tests.log](coaching-reference-tests.log) |

게이지의 13개 검사는 제어된 독립 참고 서비스와 자체 엔진 모형을 사용한다. 서로 반대인 Native/SF 점수, 백/흑 기준, 메이트, 누락/상한 점수, 미사용 중립, 무르기/새 대국의 늦은 패킷, 취소·종료, 소유자별 격리, 전체 기보, 예정 수로만 가능한 3회 반복/50수 청구를 검사했다. 기존 호스트 검사에서는 표시 참고 -555, 후보 검증 +777, 자체 평가 -40을 분리하여 표시 서비스가 스파링의 검증 근거를 덮지 않는지 확인했다. 이 결정적 검사와 위의 실제 엔진 실행을 구분한다.

UI 검증은 부모 작업에서 별도로 수행했다: `web_analysis_tests.js` 45개, `coaching_reference_web_tests.js` 8개, `web_coaching_tests.js` 95개 통과. Godot 및 전체 UI 근거는 해당 검증 기록을 함께 참조한다.

Stockfish의 제한된 시간 평가는 더 깊은 탐색에서 달라질 수 있다. 이번 결과는 표시 출처, 위치 일치, 취소와 기능 동작의 검증이며 자체 엔진의 경기력 향상이나 모든 국면의 정답을 입증하지 않는다.
