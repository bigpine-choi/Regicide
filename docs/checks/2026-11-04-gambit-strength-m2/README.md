# 갬빗 M2: 후보 비교와 실제 평가 호출 진단

2026-11-04, 한국 시간. **진단 기능 구현과 4국면·84회 탐색 완료. 핵심 탐색/평가 수정과 기력 검증은 아직 남아 있다.** 왕 안전 항목을 추가할 직접 근거는 얻지 못했다. 다음 가설은 체크가 아닌 퀸 위협이 있는 위치에서 정적 평가를 그대로 사용하는 정지 탐색의 한계다. 이번 결과로 기력 향상이나 패인의 확정 원인을 주장하지 않는다.

## 구현

- `scripts/probe_loss_search.py --root-candidates ...`: 전체 수 이력을 보존한 같은 루트에서 자유 탐색과 지정 후보를 각각 계산한다. 후보를 먼저 둔 뒤 깊이가 달라진 자식 위치를 비교하지 않는다.
- `scripts/gambit_candidate_probe.py`: 깊이·노드 상한·설정을 고정하고, 새 프로세스에서 LMR만 켜고 끈다. LMR은 뒤늦게 조사하는 일부 수를 얕게 읽고 필요하면 재탐색하는 방법이다. 반환 수와 합법 예상 수순(PV), 점수의 관점, 완료 깊이, 원시 UCI와 소스 해시를 기록한다.
- 요청 깊이에 미달하거나 제한 시간 초과·비정상 종료가 발생하면 순위 비교를 통과시키지 않는다. 종료 코드는 0만 정상으로 인정한다.
- 정적 평가 도구의 기존 13열과 활동성 포함 17열을 지원한다. 루트·후보 직후·PV 끝 위치의 설정별 평가를 기록한다. 이 차이는 **가산 가능한 평가 항목별 기여도나 실제 탐색 리프 증거가 아니다.**
- `scripts/trace_gambit_leaf.cpp`: 기존 observer를 사용해 요청 깊이와 정확한 경로에서 실제 발생한 평가 호출만 기록한다. 관측 없음과 탐색 미완료를 구분하며, 해당 호출이 루트 점수를 결정했다는 인과 관계는 주장하지 않는다. CMake 선택 대상은 `regicide_trace_gambit_leaf`다.

핵심 엔진 소스와 대국 정책은 수정하지 않았다. 외부 엔진을 실행하거나 실제 착수 선택에 연결하지 않았다. 두 실패 국면의 세 번째 후보는 이전 외부 감사에서 얻은 수를 진단 대상으로 명시한 것이다.

## 실제 실행

기준 소스 `8f5e1b0506e616e8d9a3a8bb1384f98a70c0e9b4`에서 MSVC 14.51.36231 x64, C++20, `/O2 /DNDEBUG /W4 /WX`로 별도 빌드했다. 실행 환경은 Windows 11, 논리 CPU 16개, Python 3.13.16, python-chess 1.11.2이며 라이브러리 버전이다. 엔진은 단일 탐색, Hash 16MiB, classical + piece activity, PVS/SEE/기존 정지 탐색을 사용한다. 추가 위협 탐색·위협 캐시는 비활성이다.

- 새 진단 엔진 SHA256: `64f6eda3c5bad1aba07d37e207472aca26583aa8c9815d4a1ef3c71b4f22d941`.
- 대국용 기준 SHA256: `5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba`.
- 소스와 빌드 해시: [build-manifest.json](build-manifest.json). 빌드 명령은 [build-engine.cmd](build-engine.cmd), [build-trace.cmd](build-trace.cmd).
- 네 국면의 깊이 5에서 기존/새 엔진의 수·점수·PV·노드 수가 모두 같았다. 이는 관측된 동작 일치이며 파일이나 모든 국면의 동등성 증명은 아니다. [runtime-parity.json](runtime-parity.json).
- [fixtures.json](fixtures.json)은 이전 개발자료의 King's 두 위치, Morra 한 위치, Evans 대조군을 비교 전에 지정했다. 미학습 평가 표본이 아니다.
- 깊이 4·5·6, 호출당 최대 5,000,000노드·안전 제한 30초, 설정/후보마다 새 프로세스. 실제 본 실행은 21:10:34~21:11:28 KST, 약 54초였다. 모든 84회가 요청 깊이를 완료했고 기록된 시간 초과는 없었다.

## 결과

아래 점수는 수를 둘 쪽의 자체 점수(cp, 폰 100 기준)다. LMR on/off 모두 같은 값이다. 메이트 점수는 별도 필드로 보존하며 cp로 변환하지 않는다.

| 국면 | 깊이 4 자유 선택 | 깊이 5 자유 선택 | 깊이 6 자유 선택 | 깊이 6 후보별 점수 |
| --- | --- | --- | --- | --- |
| King's:9 | Nc3 / -124 | Bb5 / -120 | Nc3 / -141 | Bb5 -157, Nc3 -141, Qf5 -166 |
| King's:11 | Bd3 / -157 | Nc3 / -371 | Nd4 / -373 | Nc3 -390, Nd4 -373 |
| Morra:25 | Ne6 / -34 | Kh1 / -23 | Nd5 / +6 | Ne6 -41, Kh1 -38, Nb5 -44 |
| Evans 대조군 | c3 / -80 | a3 / -52 | Bb2 / -82 | a3 -95, Bb2 -82 |

42개 LMR 비교 쌍에서 최종 점수와 선택 수가 모두 같았다. PV가 다른 쌍은 1개, 노드 수가 다른 쌍은 11개였다. 총 노드는 on 7,813,665 / off 10,067,061이다. 따라서 **이 표본에서는 LMR을 끄는 것으로 깊이에 따른 선택 변화가 해소되지 않았다.** 노드 합계 차이는 실시간 속도나 기력 향상 수치가 아니다. 같은 깊이라도 선택적 탐색의 결과이므로 정확한 미니맥스 값으로 해석하지 않는다.

전체 후보·깊이·노드·완료 상태는 [run-001/summary.json](run-001/summary.json), 원본은 [run-001/evidence.zip](run-001/evidence.zip)에 있다. ZIP은 `report.json`, 개별 query JSON, 입력 manifest, 정적 평가 입력 TSV와 `raw-uci.log`를 포함하며 압축 해제 후 바이트 일치를 검증했다.

## 실제 평가 호출에서 찾은 다음 가설

King's:9의 Nc3 후보, 깊이 6에서 자체 PV는 `5.Nc3 d5 6.Qh5 Nf6 7.Bb5+ Nc6`였다. 끝 위치는 백 차례이며 체크는 아니다. h5의 퀸은 f6 나이트에게 공격받고, 합법적인 퀸 이동 7개 모두 직후 상대가 퀸을 잡는 합법 수를 허용한다. 예를 들어 `Qxh6`에는 `Rxh6` 또는 `Bxh6`, `Qxf7+`에는 `Kxf7`이 가능하다.

observer는 정확히 이 경로의 깊이 6 반복에서 정적 평가 호출 **2회**, 각각 **raw -141cp / 사용 점수 -141cp / qply 0**, 무승부 보정 없음을 기록했다. 루트 결과도 Nc3 / -141cp / 56,624노드로 UCI 후보 결과와 일치했다. [leaf-trace-002/stdout.json](leaf-trace-002/stdout.json), [합법 퀸 이동과 응수](leaf-trace-002/report.json).

이는 위험한 퀸 형세에서도 그 위치의 정적 평가가 실제로 호출됐다는 증거다. 그 호출이 루트 오판의 확정 원인이라는 증명이나, 모든 다른 백의 수를 고려한 강제 퀸 손실·보상 부재의 증명은 아니다. 정지 탐색은 수순 끝에서 잡기·체크 등을 더 읽는 과정이며, 현재 비체크 위치에서는 현재 평가를 유지하는 선택(stand pat)을 허용한다. 따라서 다음 실험은 **퀸이 위협받을 때 이 선택과 방어 수 탐색을 어떻게 제한할지**로 좁힌다. 단순 왕 안전 점수 추가는 이번 근거로 채택하지 않는다.

기존 `quiescence_threats`는 주로 나이트 포크 위협과 별도 SEE 가지치기를 함께 다룬다. 이를 켜는 것을 퀸 위협 대응의 단일 변경으로 간주하지 않는다. 다음 핵심 수정에서는 한 가지 가설만 켜고, 해당 개발 위치의 깊이/동일 시간 비교, 별도 국면의 퇴행 검사, 이후 선후공 대국을 거쳐야 한다.

## 검증과 기록의 한계

- 후보 도구 단위 검사 11개 통과. 이력 손상·불법 후보·점수 관점·미완료 깊이·시간 초과 경합·비정상 종료를 포함한다.
- 기존 loss diagnosis 검사 17개 통과, 별도 구버전 바이너리가 필요한 4개는 생략했다.
- 새 C++ observer 실제 CLI 검사 4개 통과: 정상 호출, 잘못된 루트/경로, 노드 상한의 미완료 처리.
- 본 실행 뒤 독립 리뷰에서 watchdog 경합과 종료 코드 검사를 보완했다. [run-001/executed-source.zip](run-001/executed-source.zip)은 **본 실행 당시 코드**다. 당시 84회는 complete/timeout false/closed true이지만 종료 코드 숫자는 수집하지 않았다. 현재 엄격한 정상 종료 계약을 과거 결과에 소급 적용하지 않는다.
- 수정된 최종 도구로 King's:9 깊이 6의 자유/Nc3 × LMR on/off **4회 smoke**를 실행했다. 모두 요청 깊이 완료·정상 종료 0이며 본 실행의 점수/수/노드와 같다. [final-smoke/evidence.zip](final-smoke/evidence.zip)의 `report.json`과 [validation.json](validation.json)에 기록했다.
- 실제 observer 재현 스크립트의 관측 필수 조건을 보강한 뒤 1회 재실행했다. `leaf-trace-001`은 보강 전 기록과 실행 소스, `leaf-trace-002`가 최종 재현 기록이다.
- 대국·Elo·앱 GUI 검사는 수행하지 않았다. 실제 대국용 실행 파일과 정책은 그대로다. M2의 진단 부분이 완료됐으며, 탐색 수정 및 같은 시간 비교는 다음 작업이다.

## 재현

저장소 루트에서 다음과 같이 실행한다. 결과 디렉토리는 기존 자료를 덮어쓰지 않도록 새 이름이어야 한다. 빌드는 CMake로 `regicide`, `regicide_evaluation_probe`, `regicide_trace_gambit_leaf`를 만들거나 보존한 MSVC 명령을 사용한다. observer의 입력은 `initial_fen<TAB>전체 루트 수 이력<TAB>루트 후보 또는 -<TAB>목표 PV` 한 줄이다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/probe_loss_search.py --root-candidates docs/checks/2026-11-04-gambit-strength-m2/fixtures.json --binary build-gambit-m2/regicide.exe --probe build-gambit-m2/regicide_evaluation_probe.exe --output-dir build-gambit-m2/replay-candidates --depths 4 5 6 --nodes 5000000 --timeout 30
.\.venv-tests\Scripts\python.exe docs/checks/2026-11-04-gambit-strength-m2/trace_evidence.py --binary build-gambit-m2/regicide_trace_gambit_leaf.exe --output build-gambit-m2/replay-leaf
$env:REGICIDE_TRACE_BINARY = (Resolve-Path build-gambit-m2/regicide_trace_gambit_leaf.exe).Path
.\.venv-tests\Scripts\python.exe tests/trace_gambit_leaf_tests.py -v
```
