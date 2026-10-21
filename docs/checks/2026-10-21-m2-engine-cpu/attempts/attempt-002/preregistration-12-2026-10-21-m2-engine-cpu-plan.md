# M2 엔진별 CPU·응답 계측 사전 계획

2026-10-21 KST. 사용자 승인에 따라 잔여 부하 20%는 경고로 기록하고, 엔진별 실제 CPU 시간과 응답 지연을 확인하는 기준으로 새 사전 24판부터 시작한다. 사전 검증을 모두 통과한 경우에만 새 정식 768판을 진행한다. 실행 전 이 문서·JSON·새 도구 소스·기존 입력을 새 실행 폴더에 동결한다. 이 문서를 작성할 때 새 대국 결과는 아직 없다.

직전 계획은 `2026-10-21-m2-job-accounting-restart-001.json`, SHA256 `1f7f1638e0c4d79c84611a4987ce2e496ffc939fe73b6e514528058e72c58f0a`다. 최초 정식 계획 SHA256 `da587c70ede95fe482f00895a5d44c63a577b36686b726225242ed16407431fb` 및 기존 사전 등록 증빙과 참조 관계를 유지한다. 기존 계획·실행 도구·실패 기록·기보는 수정하지 않는다.

## 바꾸는 판정

전체 PC 잔여 CPU는 `GetSystemTimes`의 busy 증가량에서 대국 Job의 누적 CPU와 조정기 프로세스 CPU 증가량을 뺀 비동기 추정치다. 음수 원시 값, 양자화·조회 시각 차이, 보간·0 하한 처리 내역을 남긴다. 기존 0.5초 샘플·최대 2초 gap·2초 rolling 창·20% 값은 유지한다. **20% 초과는 경고와 요약 통계로만 남긴다. 초과의 크기와 반복 횟수 자체로 idle 사전 검사나 대국을 중단하거나 비교 자격을 무효화하지 않는다.** 기존의 외부 엔진 CPU 0·identity·접근 오류·시간/카운터 단조성·샘플 gap 기준과 실패 시 중단은 유지한다.

20%는 이 PC에서 개별 탐색의 경합이 발생하는 경계라고 검증된 값이 아니다. 잔여 부하가 20% 이하인 것도 경합 부재의 증명이 아니다. OS·백그라운드 잔여를 특정 앱의 CPU라고 단정하거나, 이번 판정 변경으로 과거 중단을 소급 PASS로 바꾸지 않는다.

## 엔진별 CPU와 응답 기록

각 엔진 프로세스를 시작할 때 정확한 PID와 생성시각을 고정하고 프로세스 핸들을 유지한다. 각 실제 검색 호출의 전후에 `GetProcessTimes`의 user·kernel 누적 tick과 조회 시작/끝 monotonic 시각을 기록한다. 1 tick은 100ns이며 CPU 증가량은 user+kernel 전후 차이다. 모든 스레드 CPU가 포함되지만 별도 자식 프로세스의 CPU를 이 값에 합친다고 주장하지 않는다. 기존 두 상대와 Regicide의 실제 수 선택 프로세스를 계측한다.

Regicide와 Stockfish는 동결 실행 파일 자체를 직접 계측한다. Sunfish도 실제 실행 트리를 확인한다. 관측된 자식이 없고 transport의 실제 실행 파일 경로·SHA·생성시각이 고정 프로필과 일치하면 해당 프로세스를 직접 계측한다. 별도 자식에서 실행되는 경우에는 동결된 Sunfish 스크립트와 launcher 실행 파일, 유일한 직계 기본 Python 자식의 경로·SHA·부모 관계·생성시각을 두 번의 inventory와 유지 핸들로 확인한 뒤 실제 자식 PID+생성시각을 CPU meter에 전달하여 다시 검증한다. 이 경우 기본 Python은 이미 동결한 `worker_runtime.executable`과 일치해야 한다. 알 수 없는 여러 자식·손자·관계 변경은 허용하지 않는다. 기존 상대 프로필·실행 명령은 바꾸지 않는다. 별도 launcher가 있는 경우 그 시작·유지 CPU는 전체 실험 Job CPU에 포함되며 실제 endpoint 검색 CPU에는 포함하지 않는다. 직접 실행인 경우 해당 endpoint의 검색 CPU를 계측한다. 이 확인은 관측 시점의 관계 증거이며 이후 모든 자식 생성의 부재나 OS 전체의 원자적 관측을 보증하지 않는다.

CPU 전후 조회 구간은 callable 응답 구간보다 약간 넓고 원자적이지 않다. 응답 시간은 CPU 조회를 제외한 callable 구간으로 기록하며, 기존 deadline 판정은 CPU 전후 조회와 callable을 포함한 계측 envelope에도 그대로 적용한다. 이 envelope 이후의 JSON 직렬화·원시 로그 flush는 엔진 응답 시간에 포함하지 않는다. 기존 비교 도구의 반올림된 바깥 `elapsed_seconds`는 이 로그 처리까지 포함하므로 envelope를 포함하는지 대조하고 분포·차이를 진단 기록하되 엔진 응답 deadline 상한으로 별도 판정하지 않는다. 실제 callable과 계측 envelope의 요청 시간×2+0.25초 상한, 전체 pair/phase 시간 상한과 감시 샘플 gap 검사는 유지한다. [Microsoft GetProcessTimes 공식 문서](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)는 프로세스의 모든 스레드 CPU 합과 100ns 시간 단위를 명시하며 CPU 시간이 실제 경과시간보다 클 수 있음을 설명한다.

각 검색 기록은 엔진 역할·PID·생성시각, game/ply·검색 순서, 요청 시간, 기존 응답 deadline, 검색 호출 시작/끝의 monotonic 시각, 응답 wall 시간, 실제 CPU 시간, wall 대비 CPU 비율 및 요청 시간 대비 초과/여유를 서로 연결한다. 원시 전후 카운터를 보존하여 CPU와 응답 수치를 독립 재계산할 수 있게 한다. 기존 엔진 affinity 기록과 동일한 PID+생성시각으로 연결한다.

CPU 시간은 실행에 실제 소비한 프로세스 CPU이고, 응답 시간은 호출부터 결과가 돌아올 때까지의 경과시간이다. 두 값의 차이가 전부 OS 대기 시간이라고 단정하지 않는다. 엔진의 시간 관리·즉답·통신·호출 처리 및 계측 양자화 등도 영향을 줄 수 있다. CPU가 낮거나 0인 검색, CPU/wall 비율 차이, 엔진별 속도 차이에 임의의 실패 하한을 두지 않는다. NPS 하한이나 새 CPU 비율 하한을 만들지 않는다. 이 지표는 각 엔진이 받은 연산량·응답 지연을 설명하는 자료이며 동일 하드웨어 성능·OS 격리의 증명이나 성적 보정값이 아니다.

필수 CPU 기록이 누락되거나, engine PID+생성시각이 달라지거나, 접근/API 오류·카운터 회귀·음수 CPU delta·비정상 수치가 있으면 실패로 중단한다. 응답 deadline은 기존 요청 시간×2+0.25초를 유지한다. deadline 초과·엔진 오류·불법 수·입력/소스 hash 불일치·불완전 계측 등 기존 실패 조건도 중단한다. 실패한 CPU 읽기나 timeout을 정상 검색으로 꾸며 완료 처리하지 않는다. 측정 자체의 CPU는 기존 Job 및 별도 조정기 CPU에 포함하고 원래 도구를 덮어쓰지 않는 새 실행 경로로 동결한다.

## 그대로 유지하는 대국 조건

직전 JSON과 다음 20개 필드를 deep equality로 대조한다.

`candidates`, `opponents`, `openings`, `preflight_openings`, `preflight_opening_count`, `movetimes_ms`, `max_plies`, `draw_policy`, `lane_affinity_masks`, `topology`, `load_policy`, `idle_probe_seconds`, `move_deadline`, `cell_timeout_seconds`, `preflight_timeout_seconds`, `main_timeout_seconds`, `retry_policy`, `statistics`, `worker_runtime`, `accounting_policy`.

- 기존 M1 세 평가 프로필, 동일 Regicide 실행 파일·모델, Stockfish 18·고정 Sunfish를 사용한다. Huber 후보·기본 평가기 변경·결과에 따른 후보 선택은 포함하지 않는다.
- 동일 사전 은행 첫 1계열 × 세 후보 × 두 상대 × 100ms/1000ms × 양색으로 새 사전 24판/12쌍이다. 정식은 동일 32계열로 새 768판/384쌍이다.
- 네 lane `[15,240,3840,61440]`, lane당 완전한 두 물리 코어, 기존 lane 배정식·후보 회전·순차 쌍·한 검색 스레드·Hash16을 유지한다. 혼합 코어가 동일 성능이라는 주장은 하지 않는다.
- 500ply 상한·무승부 claim·미완료 판정, idle 5초, pair 1500초·사전 1800초·본 14400초, 통계 seed 2026102101·cluster bootstrap 20000회를 유지한다.
- 최초 source plan과 기존 참조 파일 SHA, native/모델·프로필·Python/chess 원문 SHA, 동결 Git receipt cache 및 측정 중 실제 파일 hash 재검증을 유지한다.

정식 은행 SHA `1994d67709942de642b0e577ded78e382195df260b806117edc8aa774e454edd`, 사전 은행 SHA `1e6b08b689701e95c34adc8809cf1a104d12413c8ad02c02461de1ef2a6817b8`는 이미 이전 실행에 노출된 고정 기준선이다. 새 holdout이나 독립 확인 표본으로 부르지 않는다. 앞선 부분 완료분과 새 사전·본 결과를 합산하지 않는다.

## 사전 통과와 정식 완료 기준

새 사전 24판이 전부 완료되고 오류·불법 수·미완료·미실행이 없어야 한다. 기보·PV·설정·개시·affinity·PID+생성시각·원시 Job/조정기/시스템 CPU·모든 실제 검색의 엔진 CPU 및 응답 기록이 동결 조건과 일치해야 한다. 엔진 CPU 전후 값과 응답 시간·요청 시간·deadline을 독립 재계산하며 누락·오류·회귀·deadline 초과가 없어야 한다. 제품 감사와 별도 읽기 전용 독립 감사가 PASS여야 정식 768판을 시작한다. 잔여 20% 경고는 통과를 막지 않는다.

정식 완료도 새 768판/384쌍 전부와 같은 검증 기준이다. 실제 중단 조건의 첫 실패 시 새 대국 전체를 중단하고 소유 작업 정리와 원시 증빙을 보존한다. 경고만 있는 경우 계속한다. 결과가 나쁠 때 임의로 재시도하거나 프로필·은행·시간을 바꾸지 않는다. 이전 중단 기록의 validity를 변경하지 않으며, 새 기준을 실행 전에 동결한다.

CPU·응답 수치를 함께 공개하여 시간 배분의 실제 상태와 한계를 해석한다. 성적을 CPU 값으로 사후 보정하지 않으며, 이것만으로 엔진의 세계 순위·보편적 Elo·인과적 평가기 개선·모델 승격을 주장하지 않는다.
