# Windows Job CPU 독립 대조

2026-10-21, 실제 체스 엔진 대국 없이 자체 생성한 Python 작업만 측정했다. 제품 guard와 원래 20% 기준, 004/005의 무효 판정은 바꾸지 않았다.

Microsoft 공식 문서상 [Job 기본 회계 구조](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)의 `TotalUserTime + TotalKernelTime`은 활성·종료된 소속 프로세스의 CPU를 100ns 단위로 누적한다. [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)의 class 1을 별도로 구현해 읽었다. [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)의 자식 상속 규칙을 사용했으며 breakaway를 허용하지 않았다. 새 Job에는 직접 생성한 gated root만 넣었다.

| 관측 | 오래 실행되는 root 4개 | 짧은 자식 32개를 생성하는 root 4개 |
|---|---:|---:|
| 종료 후 Job 총 CPU | 16.453125초 | 8.140625초 |
| guard 전체 관측 구간의 owned 차감 합 | 16.453125초 | 0.937500초 |
| Job CPU − guard 차감 | **0초** | **7.203125초** |
| 표본에서 보이지 않은 자식 | 0개 | **32개 전부** |
| 해당 자식의 held final CPU 합 | 0초 | **7.250000초** |
| guard 차감과 관측 root 최종 CPU 합 | 정확히 일치 | 정확히 일치 |

장수명 control에서는 Job과 guard 및 root 최종 타이머가 정확히 일치했다. churn에서는 root의 CPU는 모두 차감했지만, 표본 사이에서 생성·종료된 자식은 놓쳤다. 자식의 종료 핸들에서 별도로 읽은 CPU와 독립 Job 누적값 모두 큰 누락을 보여준다.

churn의 전체 root+leaf 타이머 합은 8.187500초로, Job 합보다 **0.046875초 크다**. 이 잔차를 그대로 남겼다. 프로세스 생성과 Job 할당 사이의 경계나 native 계수 정밀도가 가능한 설명이지만 원인을 입증하지 않았다. 따라서 샘플 전후 Job query를 서로 다른 native counter의 엄밀한 순간 상한·하한으로 취급하지 않는다. `analysis.json`은 전체 lifetime 비교와 이 잔차를 함께 보고한다. 처음의 exact-additivity 분석 가정은 assertion으로 거절되었으며, raw 결과를 고치지 않고 분석 범위를 수정했다.

첫 `attempt-001`은 원래 20% 기준을 그대로 사용했다. 장수명 workload는 완료했지만 비소유 CPU 최대 34.8373%로 guard FAIL을 기록했다. 뒤의 churn은 workload를 띄우기 전 idle 31.0587%에서 거절됐다. 이 기록은 그대로 보존했다.

그 다음 **별도 attribution-only 실험**에서만 diagnostic threshold 1.0을 사용해 회계 누락을 측정했다. 이 실행의 `formal_load_validation=false`, `would_violate_formal20=true`이며 관측 최대는 34.4096%였다. 그 진단 설정에서의 guard `valid_for_load_check=true`는 원래 20% 기준의 통과를 뜻하지 않는다. 두 CPU 실행은 각각 9.29초·6.69초였고 종료 후 Job active process는 모두 0이었다. 사용자 앱·Teams·시스템 설정은 조작하지 않았다.

재현 명령:

```text
.venv-tests/Scripts/python.exe -B docs/checks/2026-10-21-m2-formal/cpu-attribution-audit/native-job-probe/probe.py --output <새 strict20 결과 폴더>
.venv-tests/Scripts/python.exe -B docs/checks/2026-10-21-m2-formal/cpu-attribution-audit/native-job-probe/attribution_only.py --output <새 attribution-only 결과 폴더>
.venv-tests/Scripts/python.exe -B docs/checks/2026-10-21-m2-formal/cpu-attribution-audit/native-job-probe/analyze.py
```

마지막 분석은 이 디렉터리의 보존된 두 결과를 읽고 새 `analysis.json`만 작성한다. 이미 존재하는 출력은 덮어쓰지 않는다. 이 합성 실험은 측정 결함의 한 경로를 입증하며, 실제 004/005 중단 원인이 이 경로였다는 소급 결론이나 임계치 변경·재대국·기력 승격을 허용하지 않는다.
