# M2 Job CPU 계측 재실행 사전 계획

2026-10-21 KST. 이번 재실행의 첫 새 대국 전에 새 JSON·문서·도구·입력을 동결한다. 목적은 기존 M1 세 평가 경로의 **고정 외부 상대 기준선**을 계측 수정판에서 다시 측정하는 것이다. 평가기·탐색·학습 모델·상대·개시군을 선택하는 실험이 아니다.

원본은 `docs/experiments/2026-10-21-m2-formal-plan.json`, SHA256 `da587c70ede95fe482f00895a5d44c63a577b36686b726225242ed16407431fb`다. 원문과 이전 동결·실패·부분 완료 기록을 그대로 보존한다. 새 계획은 `schema_version=2`, `phase=formal-m2-job-accounting`으로 구분한다. 새 runner가 새 Job API·guard·runner와 사용한 공통 도구의 원문·SHA를 새 실행 폴더에 동결한다. 옛 runner/guard를 수정해 기존 실행의 의미를 바꾸지 않는다.

## 유지하는 비교 조건

- M1 수식 평가·보정망 V2·정수 NNUE S32의 기존 세 프로필과 native 실행 파일·모델, Stockfish 18·고정 Sunfish의 기존 두 프로필·소스를 그대로 쓴다. Huber 후보는 넣지 않는다.
- **사전 24판**: 별도 사전 은행의 첫 1계열 × 세 평가 × 두 상대 × 두 시간 × 양색. 두 번째 사전 계열로 교체하지 않는다. 연결·자원·합법성/기보/설정 검사를 위한 단계이며 본 성적에 합치지 않는다.
- **본 768판/384쌍**: 기존 정식 32계열 × 세 평가 × 두 상대 × 100ms/1000ms × 양색을 전부 새로 실행한다. 앞선 부분 완료분으로 새 실행의 일부를 채우거나 성적 좋은 부분만 합치지 않는다.
- lane masks `[15,240,3840,61440]`, 8물리/16논리 CPU, lane당 2물리 코어를 유지한다. 같은 family/opponent/time의 세 후보가 같은 lane을 쓰고 순서가 회전하는 기존 배정식도 유지한다. 작업 훔치기나 실행 중 재배분은 없다.
- 기존 프로필의 한 검색 스레드·Hash16, 최대 500ply, 청구 가능한 반복/50수 무승부 청구 정책을 유지한다. ply cap은 미완료 `*`다.
- 외부 응답 상한은 요청시간×2+0.25초, pair 1500초, 사전 1800초, 본 14400초다. 초과를 패배나 무승부로 바꾸지 않는다. UCI movetime은 전체 대국 시계 심판이 아니다.

숫자·시드·코어·프로필·상대·은행·실행 시간·순서·runtime 참조는 원본 JSON을 복사해 유지한다. Ryzen AI 7 350의 Zen 5/Zen 5c 혼합 구성에서 물리 mask 분리만으로 lane의 동일 계산 성능까지 입증한 것은 아니다. 기존 동일 조건/후보별 lane 통제와 이 한계를 함께 기록한다.

## 기존에 노출된 은행

정식 은행 SHA는 `1994d67709942de642b0e577ded78e382195df260b806117edc8aa774e454edd`, 사전 은행 SHA는 `1e6b08b689701e95c34adc8809cf1a104d12413c8ad02c02461de1ef2a6817b8`로 유지한다. 앞선 사전·부분 본 대국에서 이미 노출된 **고정 기준선 은행**이며, 이번에 처음 보는 holdout/독립 확인 자료라고 부르지 않는다.

개시 수순·평가 프로필·상대·시간을 앞선 결과에 맞춰 바꾸지 않는다. 오프닝의 최초 작성/배제 범위·한계·7개 불균형 상위 묶음은 원래 은행 증빙을 따른다. 이번 변경은 계측 가설에 근거한 재실행이며, 개선된 모델이나 새로운 표본을 확인하는 실험은 별도 계획으로 둔다.

## 새 CPU 계측의 정의

```text
전체 capacity T = Δ(GetSystemTimes.kernel + user)
전체 busy B     = T - Δidle
대국 Job J     = Δ(Job.TotalUserTime + Job.TotalKernelTime)
조정기 C       = Δ(GetProcessTimes(coordinator).kernel + user)
부호 있는 잔여 R = B - J - C
```

Windows Job의 TotalUserTime/TotalKernelTime은 Job에 소속된 활성 프로세스와 이미 종료한 프로세스의 CPU를 누적한다. 이 집계로 짧게 실행된 worker 자식의 CPU도 Job 단위로 기록한다. `ThisPeriod*`를 사용하거나 누적 CPU를 주기적으로 리셋하지 않는다. `TotalTerminatedProcesses`는 정상 종료한 전체 수가 아니라 limit 위반으로 종료한 수다. [Microsoft Job 기본 accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information), [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)

조정기 PID와 생성시각을 고정하고 **조정기가 측정 Job 밖에 있는지 매 query 검증**해 이중 차감을 막는다. worker는 직접 만든 객체만 Job에 할당하며, startup gate가 열리기 전에 Job 소속과 identity/affinity를 확인한다. Job 밖 사용자 프로세스를 소유로 편입하거나 제외 목록에 넣지 않는다. Job CPU는 handle이 열린 동안 최종 기록을 확보하고 정상 종료/cleanup 상태와 함께 남긴다. 확인할 수 없는 종료는 clean 완료로 간주하지 않는다.

Job 할당 경계 전후에 읽은 CPU와 read 시작/끝 시각은 진단으로 보존한다. boundary 값에 lower/upper라는 필드명이 있어도 이를 엄밀한 잔여 CPU bound로 해석하지 않으며, Job CPU에 pre-assignment CPU를 무가정으로 더하거나 offset으로 보정하지 않는다. gate는 엔진 검색의 Job 밖 실행을 막는 장치이며 Python bootstrap 전체의 계측 경계를 원자적으로 만드는 것은 아니다.

전체 busy·Job CPU·조정기 CPU·잔여 OS/백그라운드 **추정치**를 별도 값으로 보존한다. 잔여에는 OS·DPC/ISR·다른 작업·비동기 측정 오차가 포함될 수 있다. 이 집계만으로 앱별 CPU를 분리하지 않는다. 전체 프로세스 attribution은 추가 진단 범위이며, 각 프로세스의 명령줄·창 내용·자격증명을 수집하는 절차가 아니다.

GetSystemTimes의 kernel에는 idle이 포함되고 이 호스트의 모든 논리 CPU 시간이 합산된다. T에 idle을 더하거나 ratio를 다시 16으로 나누지 않는다. [Microsoft GetSystemTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes)

## 동일 숫자의 정책과 측정 한계

샘플 0.5초, 최대 gap/샘플 소요시간 2초, 시작 idle 5초를 유지한다. **전체 PC capacity 대비 잔여 CPU의 2초 rolling 20% 상한**을 유지한다. 이는 개별 lane/프로세스의 문턱도, 새로 도입한 total-busy 20% 상한도 아니다. 전체 busy 자체는 별도 공개한다.

옛 지표는 조정기·감시 CPU도 잔여에 포함했고, 새 지표는 이를 C로 분리한다. 따라서 숫자가 같아도 지표 정의가 바뀐 새 프로토콜이다. 과거 실패를 소급 PASS로 바꾸거나 옛 지표와 새 지표의 유효성을 동일시하지 않는다.

system/Job/coordinator 카운터는 원자적 한 순간의 측정이 아니다. 원래 카운터, read bracket·UTC·monotonic 시각, delta·signed residual과 rounding/clipping 조정이 있다면 그 값도 남긴다. 양자화·비동기·window 경계 보간의 근사 오차를 명시하며 **엄밀한 upper/lower bound나 오차가 0이라는 주장**을 하지 않는다. 음의 잔여를 숨기지 않고 진단 값으로 보존한다.

20% 차단은 사전 환경 기준 초과를 뜻하며 특정 100ms 탐색의 경합·지연·경기력 손실을 직접 증명하지 않는다. 문턱 미만도 모든 간섭이 제거됐다는 보장은 아니다. affinity는 다른 worker lane을 분리하지만 OS/다른 프로세스의 배타적 코어 예약은 아니다. 100ms·1000ms 및 이 로컬 병렬 구성의 결과 범위를 유지한다.

## 중단·보존·완료 기준

known non-Job 엔진의 PID+생성시각 집합과 CPU 증가 0 검사, clock/counter/identity/접근/receipt 오류의 fail-stop을 유지한다. 측정 불가·샘플 지연·변조·오류·미완료·부하 초과에서 네 lane의 새 대국을 모두 중단한다. 소유 Job만 정리하고 기존 사용자 프로세스는 종료하거나 설정을 바꾸지 않는다. 운영/API 오류를 성적에 유리한 패배/무승부로 변환하지 않는다.

Job API·새 guard·새 runner의 mock/실제 Windows 검증을 완료하고, 새 입력/소스 snapshot을 동결한 뒤 사전 24판을 실행한다. 사전 전부 완료·부하/identity/affinity/기보/설정 감사 PASS 후 본 768판을 시작한다. 본 완료는 768판·384쌍·오류/미완료/미실행 0, 부하 정책 충족, 원시 JSON/PGN/자원·입력 hash·독립 감사 일치다. 실제 실행과 이 계획의 제안을 구분해 기록한다.

첫 실패를 보존하며 같은 출력 경로로 덮어쓰지 않는다. 무관한 사용자 작업을 종료해 결과를 만드는 절차나 결과 기반 후보/수순/시간 변경은 없다. 새 결함 때문에 재실행이 필요하면 별도 식별자·새 동결·이전 실패 자료와 변경 이유를 남기고 소급 합산하지 않는다.

각 후보×상대×시간의 64판 W/D/L·점수·노드/응답시간·계측 자원을 분리한다. 상위 7묶음 percentile cluster bootstrap 20000회/seed 2026102101, 기존 해석 제한을 유지한다. 노출된 고정 은행의 서술적 로컬 기준선이며 보편적 Elo·세계 순위·평가 경로 승격의 증명이 아니다. 기본 평가기 교체와 Huber 후보 확인은 수행하지 않는다.
