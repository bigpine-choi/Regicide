# 실제 Windows owned-process lifecycle 검증

**최종 `attempt-003/execution.json`: PASS.** guard SHA `39b25242e03faef0dd65708481ea08720eadf62cf163604ba173df26725d5b13`, 9.342초, 샘플 32회(timer 8회), 최대 gap 0.51784초, nonworker 추정 최대 8.3181%. guard가 열린 상태에서 detach 후 두 샘플 모두 retired count 0이며, close 후 owned/retired도 0이다. 자체 root 전부 종료, 소스 실행 전후 SHA 동일, summary valid true/failure null을 확인했다. 20%/0.5초/2초 gap/2초 rolling/idle 5초 조건은 그대로다.

```powershell
.\.venv-tests\Scripts\python.exe docs/checks/2026-10-21-m2-formal/native-lifecycle/probe.py --expected-guard 39b25242e03faef0dd65708481ea08720eadf62cf163604ba173df26725d5b13 --output docs/checks/2026-10-21-m2-formal/native-lifecycle/attempt-003
```

같은 최종 소스의 `attempt-002`는 **FAIL 그대로 보존**했다. 자체 cohort 종료와 첫 retired count 0을 관측한 뒤, 두 번째 확인 전에 rolling nonworker CPU 23.1710%가 20%를 넘어 guard가 정상 차단했다. 핸들 retirement 오류는 아니었으나 clean summary를 얻지 못했으므로 PASS로 계산하지 않는다. 원인 프로세스는 확정하지 않았다. 병렬 검사 작업이 끝난 뒤 같은 조건으로 별도 새 `attempt-003`을 실행했으며, 실패 데이터를 삭제하거나 수치를 완화하지 않았다.

## 이전 소스의 일반 lifecycle 확인

`attempt-001/execution.json`: **PASS**, 9.331초. 검증 대상 guard SHA는 `f22ef91446b59b74db101bd7ddb96426fe8a50d52b1fdf6f76bdcbadf6873610`이며 실행 전후 같았다. 게임 엔진이나 사용자 프로세스를 시작·종료하지 않았다.

```powershell
.\.venv-tests\Scripts\python.exe docs/checks/2026-10-21-m2-formal/native-lifecycle/probe.py --output docs/checks/2026-10-21-m2-formal/native-lifecycle/attempt-001
```

검증 절차는 idle 5초 → timer 시작 → 새 Python root 4개 identity/attach → 각 root의 2.5초 자식 1개 실행 → 종료 final sample/detach → guard가 열린 상태의 후속 샘플 → close다. native identity·등록·샘플·해제는 GuardMonitor의 sampler actor를 통과한다. 프로브가 만든 Popen 참조 핸들은 해당 root가 실제 종료되고 final timer를 반영한 뒤 닫았다. 예외 시에도 자신이 만든 Job/Popen만 정리한다.

실제로 관측한 own 계보는 Python root 4개, Python child 4개, 자동 생성된 conhost 4개의 **12개 exact identity**다. 각각의 `retired_timer_closed`에는 연속 부재 스냅샷 2회가 기록됐다. detach 후 guard가 여전히 열린 상태에서 확인한 두 샘플 모두 `retired_handle_count=0`이었다. 따라서 close가 딕셔너리를 비워 만든 결과가 아니다.

- 총 샘플 32회, timer 샘플 8회, 최대 gap 0.5211초.
- nonworker CPU 추정 최대 16.96%, 정책 상한 20%. 간격 0.5초/gap 상한 2초/rolling window 2초를 바꾸지 않았다.
- summary `failure=null`, `valid_for_load_check=true`, timer 종료 확인.
- 자체 root 전부 종료, close 후 owned/retired handle 모두 0, 검사 소스 실행 전후 SHA 동일.

이 자연 실행에서 `stale_owned_entry` 복원 이벤트는 **0회**였다. 따라서 실제 Windows 일반 lifecycle의 수렴을 확인했으며, 오류 5와 종료된 stale Toolhelp 행을 강제로 재현한 검사는 아니다. 그 경계와 중복 retirement 거절/멱등성은 별도 mock 회귀의 대상이다. 실제 raw `load.jsonl`, root/child PID 파일과 실행 당시 `probe-source.py`를 각 attempt에 보존한다. 이미 존재하는 출력 폴더는 덮어쓰지 않는다.
