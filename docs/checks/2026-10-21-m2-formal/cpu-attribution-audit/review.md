# CPU 차단 기준과 경기력 비교의 독립 검토

검토일: 2026-10-21. 기존 코드·사전 계획·보존된 계측 자료와 공식 API 문서만 읽었다. 이 검토에서는 새 CPU 샘플링·대국·벤치·프로세스 제어를 실행하지 않았고, 원본 소스·계획·증빙도 바꾸지 않았다. 기보 성적은 판단 근거로 사용하지 않았다.

## 판단

**20%/2초 차단은 사전 약속한 환경 검사 실패를 뜻한다. 특정 엔진의 특정 100ms 탐색이 실제로 CPU를 빼앗겼다는 증명은 아니다.** 총량 정규화의 기본 공식은 Windows API 의미에 맞는다. 작업 관리자의 숫자와 동일하다는 검증, lane별 경합량, 이 문턱과 경기력 편향 사이의 실증적 연결은 현재 없다.

| 주장 | 현재 근거로 가능한 판단 |
|---|---|
| 기존 실행이 등록한 부하 정책을 넘었는가 | 예. 보존한 카운터의 산술은 재현되고 정책상 STOP이다. |
| 누가 부하를 만들었는가 | 중단 당시 nonworker별 CPU를 수집하지 않아 미확정이다. 이후 Teams WebView 계보 관측을 과거에 소급할 수 없다. |
| 개별 100ms 탐색이 그 부하 때문에 늦어졌는가 | 미입증이다. 엔진 스레드의 실행/Ready 대기와 lane별 동시성 기록이 없다. |
| 문턱 미만이면 경합이 없는가 | 아니다. 작은 전체 부하도 특정 lane/SMT 형제에 집중될 수 있다. |
| 4개 lane이 같은 계산 성능인가 | 미입증이다. 물리 코어 분리와 동일 계산 성능은 다른 조건이다. |

사용자가 새 앱 창을 열지 않아도 서비스·이미 실행 중인 앱·OS·실험 제어기의 CPU는 존재할 수 있다. 이를 사용자의 작업 탓으로 단정할 근거는 없다. 원인 attribution과 정책 준수 판정을 분리해야 한다.

## 1. 현재 공식의 의미와 정규화

코드 `scripts/m2_load_guard.py`의 `SystemTimes.total_ticks`, `WindowsLoadProvider.snapshot`, `M2LoadGuard.sample/_host_window`는 다음 양을 사용한다.

```text
T = Δ(kernel + user)                       # idle을 포함한 전체 논리 CPU 시간
B = T - Δidle                             # 전체 busy CPU 시간
O = 합(검증한 worker 계보의 Δprocess CPU)   # owned process의 kernel+user
R = max(0, B - O)                          # interval별 추정 잔여 CPU
판정 = 최근 약 2초의 가중 R 합 / 가중 T 합 > 0.20
```

`GetSystemTimes`의 kernel에는 idle이 포함되고, 이 PC처럼 64개 이하인 시스템에서는 모든 프로세서의 시간이 합산된다. 따라서 T에 idle을 한 번 더 더하거나, 이 비율을 다시 16으로 나누면 잘못이다. **현재 코드에는 그 오류가 없다.** 공식 문서의 64개 초과 processor-group 제한도 16 logical CPU인 이 실행의 직접 문제는 아니다. [Microsoft GetSystemTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes)

`GetProcessTimes`는 해당 프로세스의 스레드별 kernel/user 시간을 합한다. CPU 누적 시간은 생성·종료 UTC 시각과 다른 양이며, 여러 코어를 쓰면 벽시계 시간보다 클 수 있다. 현재의 `kernel+user` 누적 차이를 owned로 빼는 개념도 맞다. 100ns는 반환값의 단위이며, 이 단위만으로 측정 정확도가 100ns라고 보증할 수는 없다. [Microsoft GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

16 logical CPU에서 20%는 대략 초당 **3.2 logical-CPU seconds**다. 물리 코어 3.2개나 엔진 처리량의 20%가 아니다. SMT·주파수·캐시·전력 상태의 차이를 이 시간 비율이 계산 성능으로 환산해 주지 않는다.

잔여 R에는 사용자 앱뿐 아니라 OS/DPC/ISR 등 시스템 busy 시간과 worker 계보 밖의 실험 controller·감시·진단도 들어갈 수 있다. 이 값의 이름을 “다른 앱 CPU”나 “외부 경쟁 엔진 CPU”로 바꾸면 범위를 과장한다. 별도 known-engine exact-identity 감지는 또 다른 검사다.

## 2. 타임스탬프와 측정 정밀도

프로세스별 `GetProcessTimes`는 차례로 읽고 `GetSystemTimes`는 그 뒤에 읽는다. `frame.wall_ticks`는 snapshot 시작 쪽 UTC이며, rolling window 경계는 snapshot과 분류가 끝난 `time.monotonic()`이다. 따라서 모든 프로세스/호스트 카운터가 한 순간에 원자적으로 채취된 것은 아니다. `_host_window`가 경계 interval을 시간 비례로 잘라 쓰는 계산도 그 짧은 interval 안에서 CPU 소모가 균등했다는 근사다.

0.5초 timer와 2초 rolling은 100ms 검색 하나의 실행 구간을 직접 관측하지 않는다. 같은 합계는 짧은 집중 burst와 길고 낮은 부하에서 모두 나올 수 있다. UTC와 monotonic을 나눠 사용하는 방향은 적절하지만, 현 로그의 작은 산술 재현 오차를 물리 측정의 같은 정확도로 해석할 수는 없다.

기존 `main-stop-diagnosis/resource-diagnosis-final.json`에 따르면 004의 **첫** 초과는 20.8299%이고, 종료 정리 뒤 summary 최대가 21.4382%다. 첫 창은 전체 capacity 32.02895 CPU초, busy 11.67108초, owned 4.99948초, 잔여 6.67159초이며 문턱 초과분은 0.26580 CPU초다. 005의 첫 초과는 23.1156%, 종료 후 최대는 23.6769%다. 첫 trigger와 나중 최대를 구분해야 한다.

두 기존 진단은 누락된 owned final timer·startup baseline·clipping underflow로 초과가 생겼다는 기록을 찾지 못했다. 이 점은 측정의 신뢰도를 보강한다. 다만 비동기 카운터·양자화·window 경계 오차의 상한을 그 초과분보다 작다고 검증한 것은 아니다. 그 오차가 실제 초과를 전부 설명한다고 주장할 근거도 없다. **“기록된 총량 잔여가 문턱을 넘었다”까지가 확인된 사실**이다.

## 3. 작업 관리자와의 관계

현재 guard는 시간 기반 busy 비율이다. CPU 주파수로 보정한 작업량 지표를 직접 수집하지 않는다. Microsoft는 과거 Windows 8 계열 작업 관리자의 Utility와 시간 기반 Time 지표가 다르다고 설명한다. [Microsoft CPU time/utility 설명](https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/cpu-usage-exceeds-100)

그러나 그 설명을 모든 2026년 Windows 11에 그대로 적용해서도 안 된다. Microsoft의 2025년 배포 문서는 작업 관리자의 기본 CPU 표시를 표준 지표로 통일하고, 이전 값을 별도 CPU Utility 열에 남겼다고 밝힌다. 그러므로 현재 OS 빌드·배포 상태·선택한 페이지/열·같은 갱신 창을 확인하지 않고 “작업 관리자는 반드시 Utility라 다르다” 또는 “이 코드와 반드시 일치한다”고 말할 수 없다. 이번 검토는 작업 관리자 UI와 동시 대조를 하지 않았다. [Microsoft Windows 11 변경 공지](https://blogs.windows.com/windowsexperience/2025/10/16/new-experiences-currently-rolling-out-for-windows-11/), [Microsoft KB5064081](https://support.microsoft.com/en-us/servicing/os/windows-11/2025/08/august-29-2025-kb5064081-os-build-26100-5074-preview)

별도 현재-host PowerShell 진단은 process CPU / 실제초 / 16을 사용하고, guard는 process 잔여 / aggregate CPU 시간을 쓴다. 대략 같은 time 기반 규모지만 동일한 카운터 경계·갱신 주기·프로세스 범위가 아니다. null/종료 프로세스 누락 때문에 process 합계와 전체 busy도 같지 않다. 기존 `current-host/attempt-001-coverage-note.json`의 첫 진단 한계를 유지해야 한다.

## 4. 경합과 고정 시간 경기력 비교

004 첫 초과 창의 전체 busy는 약 **36.44%**, owned는 15.61%, 잔여는 20.83%다. 전체가 포화됐다는 기록은 아니다. 배경 스레드가 남은 코어에서 실행됐을 수도 있고, 특정 엔진과 같은 코어 또는 SMT 형제에서 실행됐을 수도 있다. 집계만으로 어느 쪽인지 결정할 수 없다. 반대로 전체 6.25%인 스레드 하나도 특정 lane의 중요한 100ms 검색과 겹치면 영향을 줄 수 있다.

Windows의 CPU affinity는 해당 프로세스가 실행될 수 있는 프로세서를 제한한다. 다른 프로세스를 그 코어에서 배제하는 예약 기능은 아니다. 따라서 네 worker lane이 서로 겹치지 않아도 OS·다른 프로세스와의 공유, 캐시·메모리·전력·온도 간섭은 남는다. [Microsoft SetProcessAffinityMask](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setprocessaffinitymask)

추가로 `hardware.json`의 CPU는 Ryzen AI 7 350이다. AMD 공식 사양은 **4×Zen 5 + 4×Zen 5c**, 16 threads를 명시한다. 현 `topology`와 mask `[15,240,3840,61440]`는 물리 코어 분리를 확인하지만 각 mask의 core class·주파수 또는 같은 계산 성능을 기록하지 않는다. logical ID 순서만으로 어느 lane이 어떤 class인지 추정하면 안 된다. [AMD 공식 사양](https://www.amd.com/en/products/processors/laptop/ryzen/ai-300-series/amd-ryzen-ai-7-350.html)

현재 설계의 장점도 있다. 같은 family/opponent/time의 세 평가 경로는 같은 lane을 공유하고 순서가 회전하며, 각 시작 위치의 선후공도 바뀐다. 따라서 후보별 계산 조건을 통제하려는 구조는 존재한다. 그 구조와 20% 문턱만으로 4-lane 100ms 결과가 외부 경합 없는 강도 비교라고 입증되지는 않는다. 특히 100ms에서는 상대적으로 짧은 스케줄링·프로토콜 지연도 사용할 탐색량에 영향을 줄 수 있다. 이 영향의 크기는 현재 자료로 수치화하지 않았다.

실제 스레드 경합을 입증하려면 검색 스레드가 실행 가능했지만 실행되지 못한 Ready 시간, context switch, CPU/lane 위치를 같은 구간에서 봐야 한다. Microsoft WPA 지침도 Running/Ready/Waiting을 구분해 간섭 원인을 분석한다. process 합계는 실행한 CPU 시간은 보여도 “언제 어디서 실행을 못 했는가”를 복원하지 않는다. [Microsoft WPA wait/critical-path 분석](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/optimizing-performance-and-responsiveness-exercise-3)

## 권고와 적용 범위

1. **현재 STOP 판정을 유지한다.** 실패한 실행을 새 해석으로 소급 COMPLETE 처리하거나 20%를 결과에 맞춰 높이지 않는다. 완료된 부분은 중단 사실·계열/조건의 불완전성을 붙인 서술 자료다.
2. 다음 실행 전에 새 자원 프로토콜을 정한다. 현재 문턱을 “총잔여 CPU 선별 기준”으로 정확히 부르고, 알려진 동시 엔진 검사는 별도로 유지한다. 개별 검색 지연·노드량/CPU 사용·core-class/lane 분포와의 관계를 작은 자원 점검으로 먼저 검증하는 것이 근거를 보강한다. 이 문서는 그 점검을 실행하지 않았다.
3. 공유 PC에서 짧은 시간 비교가 계속 막히면, 우선 병렬 lane 수를 줄이고 class가 확인된 비교 코어와 OS/controller 여유를 정하는 대안을 검토한다. 조용한 전용 환경 또는 더 긴 시간 조건도 별도 사전 계획의 대안이다. 사용자 앱 종료를 자동 실행하는 해법은 아니다.

20%/2초는 이번 실험에서 지켜야 할 사전 약속으로는 명확하다. 하지만 선택한 숫자가 경기력 편향을 배제하는 충분조건이라는 근거는 아직 없다. 향후 프로토콜의 목표는 작업 관리자 숫자와 단순히 맞추는 것이 아니라, **같은 후보 비교에 실제로 같은 계산 기회가 주어졌는지 확인하는 것**이어야 한다.
