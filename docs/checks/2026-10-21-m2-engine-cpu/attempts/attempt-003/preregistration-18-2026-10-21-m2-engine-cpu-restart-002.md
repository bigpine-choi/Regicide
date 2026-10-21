# M2 엔진 CPU 재시작 002: 감시 provider의 소유 Job 범위

2026-10-21 KST. 새 v6 도구의 변경은 감시 provider에 남아 있던 전역 숫자 부모 관계 선택을 바로잡는 것이다. 기존 실행 도구·계획·실패 증빙은 그대로 보존한다. 새 사전 24판 전부와 독립 검증을 통과한 경우에만 새 본 대국 768판을 시작한다. 이번 변경은 성적 기반 재시도나 후보 선택이 아니며 새 실행 결과는 아직 없다.

부모 계획 `2026-10-21-m2-engine-cpu-restart-001.json` SHA256 `e933ec3c2149f4c62d4f91216afb0a7061ef6afc060bc1f936f14b9f768e78a9` 및 최초 정식 계획의 참조 관계를 유지한다. 이전 attempt002 사전 24판은 제품·독립 감사 PASS였다. main은 194/768판(97쌍) 완료 후 4쌍 진행 중 감시 process-access 오류로 중단됐다. 과거 사전 PASS는 그대로 보존하지만 새 v6 gate에 재사용하지 않는다. 중단 main의 모든 성적은 비교에서 제외하며 새 768판에 합산하지 않는다.

## 확인한 결함과 보존 증거

endpoint를 고른 새 v5 resolver의 Job 범위 검증은 정상 작동했다. 별도로 0.5초마다 작업 목록을 읽는 이전 감시 provider는 전역 숫자 부모 PID로 가능한 후손을 확장한 후 CPU를 조회하는 경로를 여전히 사용했다. 서로 다른 두 실행 경로 중 감시 쪽에 같은 종류의 범위 결함이 남아 있었다.

실제 실패 원시 샘플은 worker 15796 → 새 Sunfish wrapper 3144(생성 tick `134370584605527954`)를 기록하고, 숫자 부모 3144를 가진 `csrss.exe` PID 34924·`winlogon.exe` PID 11368 및 그 아래 `fontdrvhost.exe` PID 4380·`dwm.exe` PID 25884를 가능한 후손으로 조회하다 error 5를 기록했다. 이 네 행에는 생성시각·CPU가 null이며 실제 실험 소유라는 증거는 없다. 실패 샘플은 12:14:20.590573 UTC에 시작했고 process-access 오류 시각은 같은 샘플 안의 12:14:20.603518 UTC다. 이 차이를 없애거나 같은 시각이라고 꾸미지 않는다.

두 실패 샘플 원문·줄 번호·원본 SHA는 `v5-provider-failure-evidence.json`에 보존한다. 이전 Job 원시 기록은 누적 CPU/프로세스 수를 담지만 개별 Job PID 목록은 담지 않았으므로 그 목록을 소급 복원했다고 주장하지 않는다. 특정 보호 메커니즘이나 DACL 내용은 error 5만으로 단정하지 않는다. 새 범위 수정은 전역 부모 숫자만으로 CPU 조회 대상을 정하던 확인된 결함을 고친다.

중단 독립 감사의 첫 도구가 샘플 시작 시각과 오류 시각의 완전 일치를 잘못 요구한 FAIL은 원래 script·결과로 남겼다. 새 r2 도구는 시각 순서를 확인하고 완료 194판 기보·PV·검색 CPU/응답·endpoint Job 증거와 중단/정리를 검증했다. 이 무결성 PASS는 전체 main의 invalid를 바꾸지 않는다.

## 새 감시 범위와 그대로 유지하는 검사

새 provider는 coordinator가 직접 소유한 실험 Job 핸들로 `JobObjectBasicProcessIdList`를 먼저 조회한다. caller의 암묵적 Job이나 전역 부모 숫자를 실험 소유권 대신 쓰지 않는다. no-breakaway limits, coordinator의 Job 밖 상태, 완전한 PID 배열·반환 길이·중복·조회 시각을 확인하고 원시 자료를 남긴다. Job PID 조회가 불완전하거나 실패하면 범위 밖이라고 추측하지 않고 중단한다.

실험 Job의 현재 구성원만 실제 실험 CPU 조회 후보로 삼는다. Job 밖의 일반·시스템 프로세스는 전역 숫자 부모가 실험 PID와 같아도 그 이유로 열지 않는다. 제외한 일반 프로세스와 전역 부모 숫자 일치 사례, 전체 원시 inventory, 선택 이유, 실제 CPU 조회 결과, 두 Job 목록을 기록한다. 다른 lane의 정상적인 생성·종료에 따라 전체 목록은 바뀔 수 있다. Job 목록·Toolhelp는 비원자적이므로 한 번의 추가 inventory 조회 후에도 관측되지 않은 before-only 종료·after-only 출생 PID는 각각 원시 목록으로 남기고 개별 identity를 채택하지 않는다. 두 Job 목록에 계속 남은 PID나 등록 root를 held 증거 없이 확인하지 못하면 실패다. 전체 Job CPU는 이런 짧은 작업도 포함한다. 이름으로 발견한 엔진은 실제 CPU 조회와 같은 held 핸들의 명시적 Job membership 조회가 참일 때만 소유로 인정한다.

기존에 소유로 확인해 유지한 프로세스 핸들과 종료 CPU의 정확한 PID+생성시각 증거는 계속 사용한다. Job에서 종료된 프로세스를 갑자기 외부 엔진으로 오분류하지 않는다. 종료된 소유 작업의 최종 CPU와 기존 retirement/핸들 정리를 보존한다. 실제 Job 후보·알려진 소유 핸들·선언된 외부 엔진에 대한 identity/카운터/API 오류는 계속 실패다. 범위 밖 일반 보호 프로세스 제외를 이유로 실제 엔진 오류를 무시하지 않는다.

알려진 외부 엔진의 이름/지정 PID 검사와 CPU 증가 0 기준은 유지한다. 외부 일반 프로세스 CPU를 개별 앱에 완전히 귀속했다고 주장하지 않으며 시스템 busy − 실험 Job − coordinator로 구한 잔여 부하도 그대로 남긴다. 20%/2초는 경고만 기록하고 크기·반복 횟수 자체로 중단하거나 비교를 무효화하지 않는다.

raw guard는 schema4다. header/summary의 `process_ownership_method`, 매 샘플의 `process_ownership_scope`와 연속 `process_ownership_scope_index`, summary의 `process_ownership_scope_count`를 독립 재계산한다. 선별된 current-Job/retained-owned/declared-external 이유, 부모 숫자만 일치한 일반 외부 프로세스 제외, 실제 조회 결과와 최종 감시 기록의 연결을 검증한다. 새 범위도 서로 다른 시각의 관측이며 완전한 OS 추적이나 미래의 모든 자식 부재를 보증하지 않는다.

## 대국 조건과 새 완료 기준

부모 JSON과 다음 20필드는 deep equality로 대조한다.

`candidates`, `opponents`, `openings`, `preflight_openings`, `preflight_opening_count`, `movetimes_ms`, `max_plies`, `draw_policy`, `lane_affinity_masks`, `topology`, `load_policy`, `idle_probe_seconds`, `move_deadline`, `cell_timeout_seconds`, `preflight_timeout_seconds`, `main_timeout_seconds`, `retry_policy`, `statistics`, `worker_runtime`, `accounting_policy`.

동일 M1 세 후보·두 상대·고정 개시 은행·100/1000ms·양색·네 lane/후보 회전·한 검색 스레드·Hash16·500ply·claim 무승부·time cap·통계 설정을 유지한다. endpoint별 모든 검색 CPU/응답·고정 실행 이미지·held PID+creation·요청 시간×2+0.25초 callable/envelope deadline도 유지한다. CPU/NPS 하한이나 CPU/wall 상한을 추가하지 않는다. 로그 처리까지 포함하는 바깥 elapsed는 진단 지표다. Huber 후보·기본 평가기·결과에 따른 후보/개시 변경은 없다.

새 사전 24판 전부와 기보/PV/설정/endpoint/CPU/응답/새 provider scope의 제품·독립 감사 PASS가 있어야 새 본 768판을 시작한다. 정식도 새 768판/384쌍 전부 완료·오류/미완료/미실행 0·동일 감사 PASS가 완료 기준이다. 실제 실패는 첫 발생에 전체를 중단하고 소유 작업 정리·원시 증빙을 보존한다. 이전 attempt001 main40, attempt002 main194, 각 사전·부분 경기와 새 결과를 합산하지 않는다. 이미 노출된 은행을 새 holdout으로 부르거나 Elo·세계 순위·모델 승격을 주장하지 않는다.

공식 근거: [Job PID 목록](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list), [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject), [숫자 부모 PID 재사용](https://devblogs.microsoft.com/oldnewthing/20150403-00/?p=44313). 실험 대상 식별과 시스템 전체 잔여 CPU의 해석은 별도 주장이다.
