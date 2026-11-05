# 과거 NNUE 예약의 CI 이식성 복구

2026-11-06. 이번 변경은 역사 증거 검사만 복구한다. 자체 엔진, 모델 가중치,
선택 알고리즘, 품질 기준 및 과거 예약의 실행 권한은 변경하지 않는다.

## 확인한 원인

- 기존 Linux CI의 `Frozen NNUE differs`는 과거 예약에 저장된 Windows 절대경로와
  현재 checkout의 모델 경로를 문자열 그대로 비교해서 발생한다.
- 경로만 바꾸어도 현재 `scripts/neural_training.py`는 원래 예약의 소스 SHA와
  다르다. 과거 unused 예약을 현재 코드의 실행 허가로 해석해서는 안 된다.
- Git 바이트 점검에서는 추가로 세 증거 JSON의 HEAD blob이 LF이고, 예약 당시와
  현재 Windows 원본은 CRLF인 것을 확인했다. 이 세 파일만 `-text`로 보존하고
  실제 인덱스 blob의 SHA가 원래 예약과 같은지 확인했다.
  - `docs/checks/2026-10-19-neural-incremental/search-depth4.json`
  - `docs/checks/2026-10-19-neural-incremental/uci-integration.json`
  - `docs/checks/2026-10-19-neural/integration.json`

## 변경과 보존 계약

`scripts/nnue_reservation_audit.py`는 읽기 전용이다. 원래 reservation 및
position-only audit JSON의 SHA를 고정하고, 기록된 저장소 루트에 대한 상대경로만
현재 checkout으로 옮긴다. 다른 저장소, 상대경로, `..` 및 현재 저장소 밖으로의
이탈은 거부한다. 원본 JSON의 절대경로 문자열이나 바이트는 수정하지 않는다.

원래 tactical 267개, superseded cost 292개, adopted cost 301개 입력을 전부
검증한다. 세 예약은 동일한 16개 root/594개 root·legal-child feature를 보존하며,
query/label/plan/consumed 등 추가 파일이 생기면 미사용 상태 검증을 거부한다.

이미 변경된 소스는 `(원래 상대경로, 원래 SHA)`가 명시된 경우에만 exact source
archive로 검증한다. 기존 neural-training 및 pre-ordering source archive를
재사용한다. 이번에 호출만 바뀌는 두 역사 테스트의 수정 전 바이트는
`72f765f0346c6c4de67139525ee39ad064c54d30`의 Git blob과 먼저 대조한 뒤
`ci-source-snapshot/historical-tests.zip`에 보존했다. manifest는 commit, blob,
SHA와 길이를 기록한다. ZIP과 manifest 자체도 새 auditor에서 SHA로 고정한다.
아카이브를 임의 검색하거나 줄바꿈을 정규화하여 다른 해시를 허용하지 않는다.

기존 tactical/cost/reply 실행용 gate 소스는 변경하지 않았다. 새 auditor는 역사
테스트에서만 사용하고 실행용 gate는 import하지 않는다. 현재 neural-training
소스가 원래 예약과 다르면 실행용 `authenticate_reservation()`이 계속 거부하는
회귀 검사도 추가했다. 기존 query-marker 검사도 유지한다.

## 수행한 검증

- `tests/nnue_reservation_audit_tests.py`: 9개 통과.
- `tests/nnue_tactical_search_gate_tests.py`: 9개 통과.
- `tests/nnue_qsearch_cost_gate_tests.py`: 11개 통과.
- `tests/nnue_reply_budget_gate_tests.py`: 14개 통과.
- 새 auditor 검사를 GitHub Rules workflow에 추가했다.
- 303개 distinct path/digest binding의 Git blob 또는 명시적 archive를 검사했다.
  처음 확인한 LF 3개 실패도 `ci-portability-git-bindings-before.json`에 보존했다.
- 실제 Git blob으로 별도 경로에 304개 파일을 복원하고, 새 SHA 고정 ZIP을 더해
  전체 860개 input-hash 확인과 16개 root/594개 feature 검사를 통과했다.
  `ci-portability-git-bindings.json`은 이 결과와 인덱스에 보존한 원본 3개 blob을
  기록한다. 해당 보고서 작성 시점에는 최종 commit/push 전이다.
- 모델, 원본 예약, 일반 입력, 허용 archive 및 ZIP/manifest 변조가 거부되는지,
  Windows/POSIX 기록 경로가 해석되는지 검사했다. 변경된 live source를 통한
  실행 허가는 계속 거부된다.
- `git diff --check` 통과. 새 native/teacher 질의, 학습, 엔진 성능 실험은 0회다.

이는 역사 증거와 경량 Python 검사의 복구 결과다. 최종 원격 Linux CI 결과는
상위 작업의 push 이후 확인하며, 이 문서 자체는 원격 CI 성공을 주장하지 않는다.
