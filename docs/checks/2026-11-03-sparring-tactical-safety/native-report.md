# 실제 엔진·호스트 재현 검증

`native-host.json`이 최종 통과 증거다. 검증 전후 관련 코드 해시가 일치한다. 개인 대국 파일을 변경하지 않고, 이번 대국의 합법적인 16플라이 수순을 테스트에 고정하여 임시 대국에서 다음 한 수만 실행했다.

| 조건 | 실제 선택 | 별도 Regicide 자식 탐색 | 독립 Stockfish 18 자식 탐색 |
|---|---|---:|---:|
| 안전 검증 비활성 대조군 | 9.Nxa7 | -1.60, 깊이 7 | -4.51, 깊이 23 |
| 안전 검증 활성 반복 1 | 9.Qc4 | -0.30, 깊이 6 | -0.80, 깊이 21 |
| 안전 검증 활성 반복 2 | 9.Qc4 | -0.30, 깊이 6 | -0.75, 깊이 21 |

점수는 모두 백 기준이다. Stockfish는 선택이 완료된 후 독립 검증에만 사용했다. 실제 수 선택과 안전 검증에는 자체 Regicide만 사용했다. 대조군은 코드 전체의 과거 버전이 아니라 현재 호스트에서 마지막 안전 검사만 비활성화한 실험이다.

초기 탐색을 깊이 5로 제한해 기록의 `Nxa7 +17cp`와 동일한 수·점수·주요 수순을 실제 엔진으로 재현했다. 이후 탐색에는 실제 시간 예산을 사용했다. 초기 수를 허위 패킷으로 주입하지 않았다. 새 호스트의 실제 대국 반영 수가 최종 검증 결과와 일치하는지 검사했다.

실행 조건:

- Regicide 실행 파일 SHA-256: `5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba`.
- 자체 엔진 Threads 1, Hash 16MiB, UsePieceActivity true. 각 대조/반복마다 새 엔진 사용.
- 시간 설정 10+0, 양측 시작 잔여 시간 600초. 안전 검사 최대 12초, 시간 압박 없는 조건.
- 최종 자체 독립 검증 3초, Stockfish 18 검증 2초(Threads 1, Hash 32MiB, Skill Level 20, UCI_LimitStrength false).
- Stockfish SHA-256: `9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6`.
- 새 호스트 전체 한 수 처리 약 12.94초 및 12.93초. 대조군 약 14.59초로, 기존 생각 시간 연출용 대기도 포함한다. 이 수치는 엔진 속도 향상 주장이 아니다.

재실행:

```powershell
.\.venv-tests\Scripts\python.exe tests/sparring_safety_native_integration.py --binary build/runtime-engines/5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba/regicide.exe --referee build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe --output docs/checks/2026-11-03-sparring-tactical-safety/native-host.json
```

초기 9초 예산은 마지막 대안 자식 탐색에서 요구 깊이에 도달하지 못해 원래 수를 유지했다(`9000ms-failed-diagnostic.json`). 이를 통과로 세지 않았고, 최종 12초 예산에서 위 두 반복을 검증했다. `native_probe*`, `gate_budget_probe*`, `native_safety_probe*`는 설계 중 진단 기록이며, 특히 직접 모듈 진단 `native_safety_probe_12000.json`은 실행 도중 소스가 바뀌어 최종 검증 증거로 사용하지 않는다.

한 위치에서의 회귀 검증이며 경기력·Elo·항상 접전 유지의 증거가 아니다. 유한 탐색 점수는 완전한 정답이 아니고, 시간 압박이나 더 느린 하드웨어에서는 추가 검증이 미완료될 수 있다. 수동 GUI 대국은 수행하지 않았다.
