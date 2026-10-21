# 중단 후 현재 호스트 CPU 진단

이 기록은 정식 main 176판 후 중단된 **과거 09:27:01 UTC의 원인 분석을 확정하지 않는다.** 이후의 현재 상태만 읽기 측정했다. 프로세스 종료·설정 변경·명령줄/창 내용/환경/자격증명 수집은 하지 않았다. 다른 담당자의 중단 기보·자원 감사가 병행됐을 수 있으며, 진단 자체의 PowerShell CPU도 포함된다.

## 현재 관측

| 실행/구간 | UTC | 전체 호스트 busy CPU | 관측 상위 프로세스, 논리 CPU 16개 전체 대비 |
|---|---|---:|---|
| 첫 실행 1 | 09:30:04.66–06.72 | 2.28% | EpicGamesLauncher 7684: 0.48% |
| 첫 실행 2 | 09:30:06.72–08.86 | 4.33% | 진단 pwsh 19200: 2.05% |
| 첫 실행 3 | 09:30:08.86–10.91 | 22.07% | msedgewebview2 11552: 16.42%; 진단 pwsh 19200: 2.78% |
| 보정 실행 1 | 09:31:16.15–18.22 | 2.93% | 진단 pwsh 33396: 0.24%; ChatGPT 38944: 0.14%; codex 12008: 0.14% |
| 보정 실행 2 | 09:31:18.22–20.36 | 3.88% | 진단 pwsh 33396: 2.15%; EpicGamesLauncher 7684: 0.18% |
| 보정 실행 3 | 09:31:20.36–22.40 | 2.88% | 진단 pwsh 33396: 1.48%; ChatGPT 29364: 0.24% |

계속 높은 부하가 유지된 모습은 아니었다. 첫 세 구간 중 한 번 WebView burst를 관측했고, 후속 세 구간에서 같은 PID는 0.048%/0%/0%였다. 첫 전체 CPU 22.07%에는 진단 프로세스 2.78%도 포함되므로 WebView 단독 20% 초과로 해석하지 않는다. 이 측정은 정식 대국 중 sampled nonworker CPU와 조건이 같지 않으며, 실패 후 잠시 낮았다는 사실도 긴 768판 실행의 격리를 보장하지 않는다.

## 현재 보고된 부모 계보

`parent-chain.json`, 09:31:50.67–51.25 UTC:

```text
msedgewebview2.exe PID 11552
  → msedgewebview2.exe PID 24064
    → ms-teams.exe PID 19508
      → explorer.exe PID 2224
```

따라서 현재 관측된 PID 11552는 **Teams 계보의 WebView**다. `ProcessId`, `ParentProcessId`, `Name` 세 필드만 요청했다. 생성 시각을 추가 수집하지 않았으므로 부모 PID 재사용을 완전히 배제하는 exact-identity 증명은 아니다. 다음 상위 PID 30152는 조회 당시 존재하지 않아 중단했다. 과거 중단 시점의 앱 계보를 복원한 결과가 아니다.

## 측정 방법과 한계

`probe.ps1`은 약 2초 간격 세 창에서 모든 `Get-Process`의 누적 CPU 초 차이를 계산하고, 실제 측정 간격×논리 CPU 16으로 나눴다. 한 프로세스가 논리 CPU 하나를 계속 사용하면 약 6.25%다. PID 0 Idle은 busy 합계에서 제외했다. 별도의 읽기 전용 Windows `GetSystemTimes`로 전체 호스트 busy CPU도 보존했다. 종료한 프로세스의 최종 CPU, 접근할 수 없는 카운터, 샘플 사이에 생겼다 사라진 프로세스는 프로세스 합계에서 빠지므로 이 합계는 전체 호스트 CPU와 같지 않다.

첫 실행은 null CPU를 double로 바꾸면서 0이 된 계측 coverage 문제를 발견했다. 원문 `attempt-001/receipt.json`은 그대로 보존하고 `attempt-001-coverage-note.json`에 해석 제한을 추가했다. 첫 실행의 `unmeasured=0`을 접근 가능한 전체 프로세스가 관측됐다는 뜻으로 쓰지 않는다. 양의 CPU delta와 별도 native aggregate는 그대로 관측 자료다.

보정 실행 `attempt-002/receipt.json`은 null을 유지하며 구간마다 **165개** 접근 불가 카운터를 명시했다. 각 행의 before/after 누적 CPU·delta·정규화 fraction과 top 15, 전체 행, 사라진 PID를 보존한다. 고정 threshold나 대국 계획은 바꾸지 않았다.

```powershell
& docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/current-host/probe.ps1 -OutputDirectory docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/current-host/attempt-002
& docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/current-host/parent-chain.ps1 -TargetPid 11552 -OutputPath docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/current-host/parent-chain.json
```

각 실행의 `probe-source.ps1`·SHA·UTC·실제 elapsed와 원문 JSON을 보존했다. 이미 존재하는 출력은 덮어쓰지 않는다. 이 결과로 특정 앱의 과거 책임을 확정하거나 사용자 프로세스 종료를 자동 실행하지 않는다.
