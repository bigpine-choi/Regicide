# M2 Job 계측 재시작 001 사전 계획

2026-10-21 KST. 새 사전 대국 전에 이 문서·JSON·실행 도구·원래 입력을 새 실행 폴더에 동결한다. 이번 변경은 측정 중 조정기가 생성하던 짧은 Git 보조 작업의 계측 경로를 바로잡는 것이다. 앞선 경기 결과에 맞춘 후보 선택이나 성적 기반 재시도가 아니다. 새 결과는 아직 수행하지 않았다.

직전 계획은 `2026-10-21-m2-job-accounting-plan.json`, SHA256 `9ea1427dbb59c3f394ebd3622f91ac1f12ecc4e2828f4e15bcee73c953460c14`다. 직전 문서 SHA256은 `28b40d5bad2c11e137039b11b1a956491ca8a34ba537e736a4fe933592eb416b`다. 원래 정식 계획 `2026-10-21-m2-formal-plan.json`의 SHA256 `da587c70ede95fe482f00895a5d44c63a577b36686b726225242ed16407431fb`와 참조 관계도 유지한다. 기존 문서·v2 실행 도구·동결 자료·실패 증빙을 수정하지 않는다.

## 확인된 문제와 과거 실패의 범위

직전 `build-m2-job-accounting/attempt-001/preflight`는 예정 24판 중 16판/8쌍 완료 후 잔여 CPU 기준 초과로 `STOPPED`였다. 보고된 경과시간은 230.103초, 원시 계측은 899샘플이며 제품 원시 기록 감사와 계산이 일치했다. 종료 Job 기록은 총 35프로세스, active 0이다. `preflight-audit.json`의 감사 PASS는 보존된 자료의 일치를 뜻하며 `valid_for_comparison=false`를 뒤집지 않는다. 본 대국은 시작하지 않았다.

직전 실패 보고서 SHA256은 `511a6a150f3466cb9ace1798d772db343471abf31c29c66beac224c04b72f562`, 부하 요약은 `a85e8db5fb83f119cd5928bfd755e4d30b8abc2469bdffd0e2f7d3953326d1bd`, 최종 Job 기록은 `f0a4712b67ea6a9524c85c144b4ab7a9471204bafde99eceeb46ccbf0768d35d`다. 원문과 별도 보존본을 유지하고 새 실행의 일부로 합산하지 않는다.

후속 코드 검토에서 조정기의 60초 주기 `verify_frozen → load_profile → verified_commit` 경로가 실제 Git 자식을 생성하는 것을 확인했다. 이 자식은 대국 Job 밖에 있고 조정기 프로세스 자체의 CPU에도 포함되지 않아 잔여 CPU에 남을 수 있다. 이 경로의 존재는 계측 범위의 확인된 누락이다. **이 누락이 직전 약 230초의 중단 원인이었다고 단정하지 않는다.** 앞선 정기 검증 시점 약 180초와 중단 시점의 차이만으로 인과관계를 입증할 수 없다. 과거 실패를 소급 PASS로 바꾸지 않는다.

## 변경하는 실행 경로

새 v3 실행 도구에서 측정 시작 전 실제 Git 명령으로 필요한 commit 객체·HEAD·작업 상태를 검증하고, 성공한 정확한 명령 인자와 출력·시각을 동결한다. 측정 중 worker뿐 아니라 조정기의 정기 입력 검증도 이 동결된 exact-query cache를 사용한다. 동결하지 않은 Git 조회, 실패한 사전 조회, 변조된 cache는 허용하지 않는다.

실행 파일·모델·프로필·runtime·도구·개시 은행의 실제 파일 hash 재검증은 계속한다. Git subprocess를 생략한다는 이유로 원래 commit 검증기나 주기적 파일 무결성 검사를 끄지 않는다. 새 runner·요약 도구·Job/guard/대기 제한/원시 감사 및 공통 의존 도구의 정확한 소스와 SHA는 새 runner가 실행 폴더에 함께 동결한다. v2 원문을 덮어쓰지 않는다.

조정기 PID+생성시각과 대국 Job의 구분, Job 밖 사용자 프로세스 불편입, Job 최종 CPU 확인, 실패 시 소유 작업 정리와 불확실한 cleanup의 invalid 판정은 유지한다. 잔여 CPU는 여전히 비동기·양자화 계측의 추정치다. 원시 기록의 idle 시간 대조에 쓰는 0.05초 reconciliation slack는 receipt 쓰기 전후 시각의 검증 조정값이며 CPU 오차나 개별 탐색 경합의 엄밀한 bound가 아니다.

## 그대로 유지하는 조건

새 JSON은 직전 계획을 복사한다. 식별자·사전 등록 시각·설명·추가 증빙 참조와 보조 작업 정책만 새로 기록한다. 다음 20개 조건 필드는 직전 JSON과 deep equality로 대조한다.

`candidates`, `opponents`, `openings`, `preflight_openings`, `preflight_opening_count`, `movetimes_ms`, `max_plies`, `draw_policy`, `lane_affinity_masks`, `topology`, `load_policy`, `idle_probe_seconds`, `move_deadline`, `cell_timeout_seconds`, `preflight_timeout_seconds`, `main_timeout_seconds`, `retry_policy`, `statistics`, `worker_runtime`, `accounting_policy`.

- 기존 M1 세 평가 프로필·native 실행 파일·모델, Stockfish 18·고정 Sunfish 프로필과 모든 참조 SHA를 유지한다. Huber 후보와 기본 평가기 교체는 포함하지 않는다.
- 동일 사전 은행의 첫 1계열로 새 24판을 실행한다. 사전 24판 전부와 부하·identity·affinity·기보·설정·원시 계측 감사가 통과한 경우에만 동일 정식 32계열로 새 본 768판/384쌍을 시작한다.
- 100ms/1000ms, 양색, 네 lane `[15,240,3840,61440]`, lane당 2물리 코어, 후보 순서 회전과 기존 lane 배정식을 유지한다. 한 검색 스레드·Hash16, 500ply, 무승부 청구·미완료 처리도 유지한다.
- 샘플 0.5초·최대 gap/샘플 소요시간 2초·idle 5초·전체 capacity 대비 잔여 CPU의 2초 rolling 20% 기준을 유지한다. 외부 응답 요청시간×2+0.25초, pair 1500초, 사전 1800초, 본 14400초를 유지한다. 기준을 완화하지 않는다.
- 통계 seed 2026102101·상위 7묶음 cluster bootstrap 20000회와 기존 해석 제한을 유지한다. 혼합 코어의 lane별 같은 계산 성능이나 완전한 OS·전력·열 격리를 주장하지 않는다.

정식 은행 SHA `1994d67709942de642b0e577ded78e382195df260b806117edc8aa774e454edd`, 사전 은행 SHA `1e6b08b689701e95c34adc8809cf1a104d12413c8ad02c02461de1ef2a6817b8`를 유지한다. 이미 여러 번 노출된 고정 기준선이며 새 holdout이나 독립 확인 표본으로 부르지 않는다. 앞선 결과에 따른 후보·개시·시간·상대 변경은 없다.

## 보존과 완료 기준

이번 재시작 사유는 성적이 아니라 발견된 측정 보조 작업 경로 수정이다. `no-result-based-retries-stop-on-first-failure` 정책과 기존 `exposure_policy`를 그대로 유지한다. 새 사전 또는 본에서 첫 부하·계측·입력·identity·오류·미완료가 발생하면 네 lane의 새 대국을 중단하고 실패를 보존한다. 같은 조건에서 통과할 때까지 무조건 재시도하지 않는다. 추가 결함이 확인되면 다시 별도 식별자·변경 이유·새 사전 계획을 남긴다.

기존 16판이나 다른 부분 완료분은 새 사전 24판·새 본 768판의 완료 수에 넣지 않는다. 사전과 본 성적도 합치지 않는다. 전체 완료는 새 본 768판·384쌍, 오류·미완료·미실행 0, 동일 조건·기보·PV·자원 및 원시 CPU 감사 일치다. 20% 기준의 통과/실패는 사전 환경 조건의 충족 여부이며 개별 100ms 탐색의 실제 경합이나 경기력 손실을 직접 증명하지 않는다. 보편적 Elo·세계 순위·모델 승격의 근거로 확대하지 않는다.
