# Windows Job CPU 실제 통합 검증

최종 gate 자료는 `native-integration-attempt-002/execution.json`이다. 실제 실행은 2026-10-21 10:22:57.923210–10:23:17.097655 UTC, Python 3.13.16에서 수행했다. 정확도 진단 4개와 별도 엄격20 idle 관측이 모두 통과했고, 모든 직접 생성 프로세스의 종료와 모든 소유 Job의 active0를 확인했다. 체스 엔진 대국은 0판이며 사용자 앱이나 시스템 설정은 변경하지 않았다.

실행 명령:

```powershell
.venv-tests\Scripts\python.exe -B tests/m2_job_load_guard_integration.py --output docs/checks/2026-10-21-m2-job-accounting/native-integration-attempt-002
```

| 검증 | 최종 실측 |
|---|---|
| 장수명 root 4개 | Job / 독립 ctypes 조회 / guard 누적 / held root timer 합계 모두 16.359375 CPU초, 누적 프로세스 4개 |
| root 4개 + 짧은 자식 32개 | Job / 독립 ctypes 조회 / guard 누적 모두 7.6875 CPU초, 누적 프로세스 36개 |
| 자식 실험의 별도 held timer | root 0.484375 + leaf 7.28125 = 7.765625 CPU초. Job보다 0.078125초 크며, 이 차이는 보정하지 않았다 |
| coordinator 별도 계측 | 장수명 0.453125, 자식 실험 0.4765625 CPU초를 Job과 구분해 차감. 빈 Job에서 coordinator 연산을 해도 Job CPU는 0 |
| 자체 생성한 Job 밖 child | held timer 0.21875 CPU초, Job CPU/프로세스 수는 0. 관측 구간 전체 signed 잔여는 4.03125 CPU초 |
| 소유 Job 종료 | 정상 종료와 강제 종료 모두 Popen handle을 유지한 채 Job active0 확인. 강제 종료 root의 exact handle wait 후 최종 exit timer 확인 |
| 엄격20 별도 idle | 기준 0.2 유지, 약 2.1초 idle + timer 관측. 최대 준비된 2초 잔여 추정 2.8666264%, 최대 sample gap 0.517134초, 외부 알려진 엔진 CPU 증가 0 |

정확도 진단의 임계값 1.0은 계측 확인만을 위한 것이다. 이 진단의 `valid_for_load_check`를 정식20 대국 통과로 사용하지 않는다. 최종 엄격20 관측은 별도 파일이며, 이후 대국의 계속된 저부하나 OS 격리를 보장하지 않는다.

Job 누적 CPU는 독립 구현한 `QueryInformationJobObject(JobObjectBasicAccountingInformation)`와 일치했다. 모든 native frame의 Job·coordinator·system 원시 카운터로 signed 잔여와 2초 창 가중합을 독립 재계산했다. 음의 구간은 먼저 잘라내지 않고 창 합계를 구한 뒤 비율만 0 이상으로 제한하는 계약이다. 해당 실행에서는 음수 구간이 없었다. 잔여는 시스템 전체에서 측정한 Job와 coordinator CPU를 제외한 비동기 추정치다. 자체 child 외에 특정 앱 원인을 식별한 결과가 아니다.

held 개별 process timer 합계와 Job 누적의 0.078125초 차이는 원시 그대로 남겼다. Popen/Job 배정 경계와 Windows 카운터 양자화가 관련될 수 있으나 이 실험으로 원인을 분리하지 않았다. 배정 전 진단 offset은 Job에 더하지 않는다. Job raw API의 정확한 재현과 개별 timer 전수 합계의 엄밀한 일치는 서로 다른 주장이다. native API/lock 대기의 일반적인 상한이나 매 수의 경합 상한도 이 검증으로 입증하지 않는다.

첫 전체 실행 `native-integration-attempt-001`의 단일 termination `AssertionError`와 종료/정리 원문은 보존했다. 최초 테스트는 Job active0 직후 exact process handle wait 전에 exit timestamp를 읽었다. 중간값과 메시지 없는 원문만으로 첫 실패의 정확한 assertion은 확정할 수 없다. wait/read 순서 및 중간값 기록을 보강한 `native-termination-attempt-002`는 통과했다. 이후 guard의 실패 후 baseline 수락 처리 변경에 맞춰 **전체** 최종 실행을 새 폴더에서 다시 통과시켰으며 최초 실패를 덮어쓰지 않았다. 최초 테스트 원본 SHA에 맞는 사본은 `native-integration-attempt-001-source.py`다.

최종 source SHA256:

- accounting: `c18f74176a85e3cc0a7376568aa34bf55662e58977f068e763bdb3cfcd9f94ac`
- guard: `c4b5546d61798cd24d82dfba42d272726a84581dd42d597ffb55497482fff743`
- integration test: `419b311b51331d991c3baca0b0ac4a2c74afb31967147b814e3c9f6ae489be55`
- 최종 execution receipt: `7f4ecd091bca14c7acda3c1c13b8dd9bb423d0a7408e8ffb93451a68d6007c6f`

실행 전후 source SHA는 동일했다. 기존 guard `39b25242…`, 기존 see-clean guard `f563d701…`와 과거 실험/중단/실패 원문은 변경하지 않았다. 각 execution의 artifact SHA와 별도 native manifest로 실제 파일 바이트를 재검사할 수 있다.

Microsoft 공식 명세: [Job 누적 CPU와 필드 의미](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information), [프로세스 CPU와 종료 시간](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes), [시스템 CPU와 idle 포함 관계](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes). `TotalTerminatedProcesses`는 Job limit 위반 종료 수이므로 모든 정상 종료 수로 사용하지 않는다.
