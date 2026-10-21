# M2 엔진 CPU 재시작 001: 실험 Job으로 부모 관계 확인

2026-10-21 KST. 이번 변경은 엔진의 자식 프로세스를 판별하던 계측 도구의 확인된 결함을 고치는 것이다. 경기 결과에 맞춘 후보 선택이나 성적 기반 재시도가 아니다. 새 도구·계획·입력을 실행 전에 동결하고, 새 사전 24판을 모두 검증한 경우에만 새 정식 768판을 시작한다. 새 실행 결과는 아직 없다.

직전 계획 `2026-10-21-m2-engine-cpu-plan.json` SHA256 `33bf24f3160be1bbd1ea86289e0e1c7485582f70333e5f0e780c5506eee0e448`를 부모 계획으로 연결한다. 최초 정식 계획과 기존 증빙 참조도 유지한다. 기존 v4 소스·감사 도구·계획·실행 자료는 덮어쓰지 않는다.

## 보존하는 이전 결과와 원인 한계

직전 `build-m2-engine-cpu/attempt-001/preflight`는 실제 24판을 완료해 제품·독립 감사가 PASS였다. 이후 main은 40/768판 완료 후 `main-0040`의 worker exit 2로 중단됐다. 이 셀은 Regicide PID 1016의 endpoint 설치 중 자식 OpenProcess 접근 오류 5가 발생했고 첫 수 검색은 0회였다. 앞선 사전 24판의 역사적 PASS를 소급 변경하지 않지만 새 v5 실행의 사전 검사를 대신하지 않는다. 중단된 main 40판과 부분 실행 4쌍은 비교 성적에서 제외하고 새 768판의 일부로 합산하지 않는다.

v4 도구는 전역 Toolhelp 목록의 숫자 `parent_pid == transport_pid`를 현재 엔진 자식으로 먼저 선택한 뒤 해당 프로세스를 열어 생성시각을 확인한다. Windows의 숫자 PID는 재사용될 수 있으므로 이 부모 숫자만으로 새 엔진과 실제 부모 관계를 증명할 수 없다. 확인 전에 접근 불가인 오래된 전역 프로세스를 열려는 경로가 코드에 존재한다.

중단 후 읽기 전용 조사에서 현재 `csrss.exe`·`wininit.exe` 두 프로세스가 숫자 부모 1016을 가리키지만 생성일은 새 Regicide보다 약 5일 앞서는 것을 확인했다. 이는 오래된 부모 숫자 관계의 실제 사례다. 그러나 실패 시점의 초기 resolver inventory와 실패한 child PID가 저장되지 않았으므로 그 두 프로세스 중 어느 것이 당시 오류를 일으켰는지, 그것이 정확한 과거 원인인지까지 단정하지 않는다. 뒤늦은 관측을 과거의 원시 기록으로 바꾸지 않는다. 독립 중단 감사는 40판 기보·PV·CPU·응답·원시 로그 일치와 중단/정리 기록을 확인했으며 전체 실행 validity는 false를 유지한다.

## 새로 확인할 소유 범위

새 resolver는 이미 실험 Job 안에 배정된 worker에서 `QueryInformationJobObject(NULL, JobObjectBasicProcessIdList)`로 호출자의 현재 Job PID 목록을 조회한다. NULL은 중첩 Job의 가장 안쪽 Job을 뜻한다. 현재 worker와 transport가 포함되고 coordinator는 제외되는지 확인한다. Job limits에서 `BREAKAWAY_OK`와 `SILENT_BREAKAWAY_OK`가 꺼져 있어 실험 자식이 이 Job에서 이탈하도록 허용되지 않았는지도 확인한다.

Job PID 목록은 모든 항목이 반환된 완전한 배열이어야 한다. assigned/list count·반환 길이·PID 중복·조회 시각을 검사하고, buffer가 부족하면 제한된 크기 증가만 허용한다. API 오류나 잘린 목록을 소유 범위 부재의 증거로 사용하지 않는다. 두 전역 Toolhelp 목록과 두 Job 목록, 사용한 범위 투영과 제외한 부모 숫자 일치 항목을 원시 증빙에 남긴다. 다른 lane의 정상적인 시작·종료로 전체 Job PID 집합은 바뀔 수 있으므로 전체 집합의 완전 동일성을 요구하지 않고 현재 엔진 subtree의 필요한 관계와 선택된 identity 안정성을 확인한다.

숫자 부모가 일치해도 확인된 현재 Job에 속하지 않는 전역 항목은 실험 endpoint 후보에서 제외하고, 그 근거와 PID·부모 PID를 기록한다. 이를 확인하기 위해 관련 없는 보호 프로세스를 열거나 권한을 올리지 않는다. Job 안에서 부모 숫자가 일치하는 후보는 유지 핸들로 정확한 PID·생성시각을 먼저 확인한다. 그 프로세스가 정확한 부모보다 먼저 생겼다는 긍정 증거가 있으면 실제 자식일 수 없으므로 오래된 관계로 제외하고 원시 증거를 보존한다. 생성시각을 알 수 없거나 실제 범위 안 후보에 접근할 수 없는 경우는 계속 실패다.

실제 선택된 endpoint에는 기존 실행 파일 경로·SHA, 정확한 부모 생성시각 순서, held PID+creation, nonsignaled process liveness, 두 inventory 일치 및 CPU meter 재확인을 모두 유지한다. Sunfish는 원래 고정 명령의 직접 실행 또는 고정 기본 Python의 유일한 실제 직계 자식만 허용한다. 알 수 없는 실제 자식·손자·중복·관계 변동을 추측으로 무시하지 않는다. 고정 console helper 예외는 이번 계획에 추가하지 않으며 None을 유지한다. 새 오류 기록에는 가능한 원시 Job·inventory·후보와 정확한 실패 PID·단계를 보존하여 같은 진단 한계를 반복하지 않는다.

이 확인도 서로 다른 시점의 관측이며 OS 전체의 원자적 추적이나 미래의 모든 자식 생성 부재를 보증하지 않는다. 전체 Job CPU에는 종료된 작업도 포함된다. engine endpoint CPU·응답의 범위, 양자화, 로그 후처리 시간의 진단 지표라는 제한은 직전 계획과 같다.

## 유지하는 대국·계측 조건과 완료 기준

부모 JSON과 다음 20필드를 deep equality로 대조한다.

`candidates`, `opponents`, `openings`, `preflight_openings`, `preflight_opening_count`, `movetimes_ms`, `max_plies`, `draw_policy`, `lane_affinity_masks`, `topology`, `load_policy`, `idle_probe_seconds`, `move_deadline`, `cell_timeout_seconds`, `preflight_timeout_seconds`, `main_timeout_seconds`, `retry_policy`, `statistics`, `worker_runtime`, `accounting_policy`.

잔여 20%/2초는 경고로만 기록하고 자체 중단·비교 무효 기준으로 쓰지 않는다. 각 검색의 실제 endpoint CPU·응답·계측 envelope·원시 로그 연결을 확인하며 CPU/NPS 최소값이나 CPU/wall 비율 상한을 추가하지 않는다. 요청 시간×2+0.25초 callable/envelope deadline, 외부 엔진 CPU 0, identity/API/카운터/입력/기보 오류, 감시 gap, 전체 pair/phase 시간 상한은 유지한다. 로그 직렬화·flush가 포함되는 비교 도구 바깥 elapsed는 응답 deadline으로 판정하지 않고 진단 기록한다.

동일 M1 세 후보·두 상대·100ms/1000ms·양색·32 정식 계열과 별도 사전 첫 1계열, 네 lane과 후보 회전, 한 검색 스레드·Hash16·500ply·claim 무승부·통계 seed/20000 bootstrap을 유지한다. 이미 노출된 은행을 새 holdout으로 부르지 않으며 기본 평가기나 Huber 후보는 바꾸지 않는다.

새 사전 24판 전부 완료, 오류·미완료·미실행 0, 기보/PV/설정/원시 Job·CPU·응답·새 소유 범위 증거의 제품·독립 감사 PASS일 때만 새 본 768판을 시작한다. 정식 완료도 같은 기준으로 새 768판/384쌍 전부가 필요하다. 실제 실패는 첫 발생에 전체 대국을 중단하고 소유 작업 정리·원시 자료를 보존한다. 과거 부분 성적·사전 성적을 새 본 대국과 합치거나 CPU로 성적을 사후 보정하지 않는다. Elo·세계 순위·모델 승격은 주장하지 않는다.

공식 근거: [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject), [Job PID 목록](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list), [중첩 Job과 자식 상속](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs). 부모 숫자 재사용과 이번 오류의 정확한 과거 원인 여부는 별개다.
