# 갬빗 보스 Stockfish 18 대국 원자료

2026-11-02에 실행한 20판이다. [결과와 해석](../../experiments/2026-11-02-boss-strength.md), [실행 전 계획](../../experiments/2026-11-02-boss-strength-plan.md)을 함께 읽는다.

- `all20.pgn`: 20판 합본. 12승 4무 4패이며 모든 수와 종국을 재생 검증했다.
- `all20-summary-independent.json`: 전체 집계와 탐색적 보조 추정. `phase1-summary-independent.json`, `phase2-summary-independent.json`은 단계별 집계다.
- `all20-pgn-audit-independent.json`, `phase1-pgn-audit-independent.json`: 독립적인 기보 재생·결과·설정·파일 해시 감사.
- `phase1-boss-decision-audit.json`, `phase2-boss-decision-audit.json`: 보스의 실제 선택·미끼·갬빗 성립·상대 관측 감사.
- `timing-and-independent-fit.json`: 실제 호출 경과시간 및 별도 계산한 보조 추정.
- `preservation-audit.json`: 기존 수정파일 16개와 바로가기 3개의 보존 확인.
- 각 `phase*-<설정>` 폴더: 읽기 쉬운 `match.json`, `match.pgn`과 30개 원본 파일을 담은 `raw-records.zip`. ZIP 항목은 실행 폴더 상대 경로를 유지하며 `raw-records-manifest.json`의 모든 SHA256과 대조했다. 실행 파일은 포함하지 않는다.
- `sha256.json`: 이 폴더의 보관 파일 해시(목록 자체 제외).

실제 대국 코드 동결 커밋은 `245231e33e250f7486133c169ae160894ff056f1`이다. 기록 속 절대 경로는 실험 당시 로컬 경로이며, 보관된 ZIP의 상대 경로로 원본을 식별할 수 있다. 전체 845회의 보스 착수에서 갬빗 희생 성립은 0회, 검증 미끼 선택은 3회, 미끼 포획 성공은 0회였다. 보스 모드의 전체 기력과 함정의 효과를 구분한다.
