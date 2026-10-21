# M2 엔진 CPU 계측 v4: 사전 통과, 본 실행 중단

2026-10-21 KST. 코드 `730faca565c477751fe9a61237990867269d8ef4`의 새 v4 경로를 실행했다. 기존 엔진·모델·대국 조건을 유지하고 잔여 CPU 20%를 경고로만 처리했다. 이전 무효 실행과 성적을 합치지 않았다.

## 실제 완료한 사전 검증

오프라인 100검사와 별도 Windows CPU·endpoint 검증 후 새 사전 24판을 실행했다. 492.039초에 24판/12쌍이 모두 완료됐고 제품 감사·독립 감사가 통과했다. 검색 2,042회의 원시 CPU·응답 기록, mainline 2,330ply와 PV 15,296ply를 검증했다.

잔여 부하 경고는 7개 창·1구간, 최대 28.793057%였다. **실제 20% 초과가 있어도 중단하지 않고 유효한 사전 실행을 완료했다.** 최대 샘플 간격은 0.5146314초, 알려진 외부 엔진 CPU 증가는 0이었다. 검색 CPU 합은 990.5초, 전체 소유 Job CPU는 1,014.109375초, 조정기 CPU는 25.765625초였다. 병렬 작업이므로 누적 CPU 합이 벽시계 시간보다 클 수 있다. 최종 Job 총 42프로세스·active 0 및 계측 핸들 정리를 확인했다.

## 실제 본 실행

사전 두 감사 PASS 후 본 768판을 시작했다. 83.035초 뒤 완료 40판/20쌍에서 `main-0040` 작업의 시작 오류로 중단했다. 해당 쌍은 첫 검색을 시작하기 전이었다. 실패 하나를 비롯해 총 24쌍을 시작했고, 미완료 예정 728판·미시작 360쌍이다. 3쌍은 중단 시 활성 상태였다. 잔여 부하 초과가 중단 원인은 아니다.

오류는 candidate transport PID 1016의 자식 identity를 열려는 `OpenProcess`에서 Windows error 5가 발생한 것이다. 당시 resolver는 전역 Toolhelp의 숫자 parent PID가 같다는 이유만으로 자식 후보를 고른 뒤 생성 시각을 확인했다. 이후 읽기 전용 진단에서 PID 1016을 과거 부모로 가리키는 `csrss.exe`와 `wininit.exe`가 실제 Regicide보다 약 5일 먼저 생성됐음을 확인했다. 이들은 새 Regicide의 자식일 수 없다. 이는 PID 재사용에 취약한 검사 순서의 결함이다.

**실패 직전의 전체 자식 목록과 offending child PID가 기록되지 않아 어느 프로세스가 당시 error 5를 일으켰는지는 확정하지 못한다.** 현재 시스템의 정황과 결함은 확인했지만 이 한계를 제거해 과거 원인을 단정하지 않는다. [Microsoft의 같은 PID 재사용 사례와 Job 목록 권고](https://devblogs.microsoft.com/oldnewthing/20150403-00/?p=44313)를 참고했다.

완료된 40판의 mainline 3,506ply·PV 19,062ply·검색 2,970회의 CPU/응답 기록을 독립 검증했다. 실패한 1쌍과 중단된 3쌍을 완료 성적으로 세지 않았다. 최종 Job 총 85프로세스·active 0을 확인했다. 활성 작업을 중단한 뒤 guard를 닫는 과정의 `active_workers_on_close`도 원시 실패 기록에 남아 있다.

이 실행은 `INTEGRITY_PASS_STOPPED_INVALID`다. 보존 감사 PASS가 정식 768판 완료나 유효한 기력 비교를 뜻하지 않는다. CPU로 성적을 보정하거나 모델을 승격하지 않았다. 같은 후보·시간·은행을 유지한 계측 수정판은 새 소스·계획·시도에 동결하고 새 사전 24판부터 검증한다.

## 증빙

- [사전 독립 감사](../checks/2026-10-21-m2-engine-cpu/peer-attempt-001-preflight.json)
- [본 중단 보존 감사](../checks/2026-10-21-m2-engine-cpu/peer-attempt-001-main-stopped.json)
- [PID 관계 진단과 한계](../checks/2026-10-21-m2-engine-cpu/pid-parent-audit/README.md)
- [원시 보관 매니페스트](../checks/2026-10-21-m2-engine-cpu/attempts/attempt-001/archive.json)
