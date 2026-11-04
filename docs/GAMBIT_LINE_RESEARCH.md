# 갬빗 라인 지정과 중단 후 이어 연구

2026-11-05. `scripts/train_gambit_operations.py`에서 연구할 갬빗 계열 또는 정확한 시작 라인을 지정할 수 있다. 완료한 자체 엔진 분석을 건별로 저장하며, 중단 후 같은 설정으로 실행하면 이미 완료한 분석을 재사용한다. 현재는 명령행 연구 도구이며 대국 화면의 버튼은 아니다.

## 시작 라인 선택

저장소 루트에서 실행한다. 목록 조회에는 엔진이 필요 없다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/train_gambit_operations.py --list-lines --family kings
```

`case_id`는 시작 라인의 식별자, `moves`는 그 위치까지의 전체 수순이다. `--family`를 반복하면 여러 계열을 고를 수 있다. `--case`를 반복하면 목록에서 고른 정확한 라인만 연구한다. 두 조건을 함께 사용하면 지정한 라인이 모두 선택 계열에 속해야 한다. `--case` 사용 시 계열별 표본 수 `--per-family`는 라인 선택에 적용하지 않는다.

예시는 King's Gambit의 `kings-b0c8d3df038a`에서 시작한다. 기존 대국용 실행 파일의 확인된 경로를 사용한다.

```powershell
$researchPython = ".\.venv-tests\Scripts\python.exe"
$researchEngine = ".\build\runtime-engines\5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba\regicide.exe"
$researchArgs = @(
    "scripts/train_gambit_operations.py",
    "--binary", $researchEngine,
    "--output", "build/gambit-research/kings-01",
    "--case", "kings-b0c8d3df038a",
    "--nodes", "100000",
    "--owner-turns", "8",
    "--candidates", "3"
)
& $researchPython @researchArgs --max-new-queries 20
```

이 예시는 새로운 분석 20회 뒤 일시정지한다. 먼저 연구가 끝나면 `complete`를 반환한다. `owner-turns=8`은 갬빗을 둔 쪽의 실제 연습 착수 최대 8회이며 탐색 깊이 8이라는 뜻은 아니다. 각 차례에 자체 최선 수와 후보들을 분석하고, 실제 연습 수순은 자체 최선 수와 상대의 자체 최선 응수로 이어진다. 후보별 예상 수순은 실제로 여러 대국을 수행한 결과와 구분해서 저장한다.

## 중단 후 재개

같은 셸에서는 위의 인자를 유지하고 `--resume`을 붙인다. 새 셸에서는 변수 선언도 다시 실행한다.

```powershell
& $researchPython @researchArgs --resume
```

재개할 때도 `--max-new-queries 20`을 붙이면 새 분석 20회까지만 추가한다. 전체 시간 상한 `--wall-seconds`와 개별 검색 제한 `--search-timeout`도 바꿀 수 있다. 기본값은 각각 1200초와 12초다. 시간 초과나 Ctrl+C로 중단하면 완료된 검색은 보존하고 진행 중이던 미완료 검색만 다시 계산한다. 프로세스를 강제 종료했을 때도 저장을 끝낸 검색만 재사용한다. 엔진 내부의 미완료 탐색 트리를 이어 계산하는 기능은 아니다.

다음 정보는 처음 실행과 일치해야 한다.

- 엔진 파일, 평가 설정과 실제 엔진 옵션, 관련 소스 코드
- 전체 교육 자료, 선택한 계열·라인, 시드, 후보 수와 노드 수, 연습 길이
- Python 및 python-chess 버전, 시작 위치와 그 위치까지의 전체 수 이력

변경된 설정으로 더 깊게 연구하려면 새 출력 폴더를 사용한다. 잘못된 조합을 재사용하지 않도록 동일성 검사를 엄격하게 적용한다. 같은 출력 폴더의 동시 실행은 거부한다. 세션 폴더의 로그나 체크포인트를 따로 옮기거나 수정하지 말고 함께 보존한다.

## 저장 자료와 기존 기억 보존

| 자료 | 역할 |
| --- | --- |
| `session.json`, `engine.json` | 변경할 수 없는 연구 설정과 실제 엔진 설정 |
| `completed-queries/` | 완료된 검색별 결과·전체 기보 이력·검증 해시 |
| `native-uci-*.jsonl` | 시도별 원시 엔진 입출력. 재사용 결과는 최초 로그를 계속 참조 |
| `native-uci-*-run.json`, `run.json` | 각 시도 및 최근 실행의 상태·신규/재사용 횟수·엔진 종료 상태 |
| `practice.json`, `records.json`, `queries.json` | 저장 결과를 재생해서 구성한 연습 수순·후보 분석·질의 |
| `operations_memory.json`, `audit.json` | 완주 후 자격을 통과한 별도 연구 기억과 검증 요약 |

`new_queries`는 이번 시도에서 시작한 엔진 질의 수이며 중단된 질의도 포함한다. `reused_queries`는 검증을 거쳐 재사용한 완료 질의 수다. 최종 `counts.native_queries`는 재사용을 포함한 논리적 완료 질의 수이므로 실제 추가 계산량과 구분한다. 요청 노드 수는 상한이며 CPU 시간이나 실제 방문 노드 수와 같지 않다.

기존 활성 기억 `data/gambits/operations_memory.json`은 읽기 기준으로 유지한다. 연구 결과는 새 폴더에 저장하며 자동으로 덮어쓰거나 합치거나 대국에 활성화하지 않는다. 선택한 계열에 검증 보류 계열이 없더라도 전체 교육 자료의 보류 계열과 시작 국면·직후 국면을 계속 제외한다. 보류 계열만 연구하면 연구는 완료할 수 있지만 대국용 기억 파일은 만들지 않는다.

선택 사항인 `--memory-path`도 **새 파일 경로**만 허용한다. 활성 기억 또는 이미 존재하는 파일 경로는 거부하며, 다른 작업이 먼저 같은 파일을 만들면 그 파일을 보존하고 내보내기를 중단한다. 완주·입력 불변·정상 엔진 종료를 확인한 결과만 내보낸다. 저장 결과의 `approved`는 출처·형식 검증을 통과했다는 뜻이며 기력 향상이나 활성 설치를 뜻하지 않는다.

## 이번 검증의 의미

[비교 기록](checks/2026-11-05-gambit-line-research/README.md)에서 기존 방식·새 연속 실행·중단 후 재개가 같은 고정 노드 조건에서 동일한 수와 분석 결과를 만드는지 확인했다. 이는 연구 기능의 정확성과 기존 자료 보존을 검증한 결과다. 더 깊은 라인의 품질, 갬빗 기풍, Elo 및 Chess.com `!`·`!!` 증가는 별도 평가 대상이다.
