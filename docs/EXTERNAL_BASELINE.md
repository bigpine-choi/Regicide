# 외부 엔진과의 재현 가능한 기준 대국

이 문서는 외부 UCI 상대를 실행하는 도구 사용법이다. 도구 구현 자체는 경기력이나 Elo 검증 결과가 아니다. 핵심 C++ 엔진은 변경하지 않는다. 2026-10-21에는 세 평가 프로필의 동결과 두 외부 상대의 파일럿을 연결했다. 조건·실제 결과·한계는 [M2 기준선 기록](checks/2026-10-21-m2-foundation/README.md)을 따른다.

## 출처 모드

### 세 평가 경로의 동결 프로필과 사전 계획

`config/engine-profiles/`의 수식·Residual V2·NNUE S32는 같은 통합 엔진의 평가 모드와 모델을 명시한다. 두 신경망 모델은 M1에서 이미 검증한 대표 파일이며 새로 가장 강한 모델로 고른 것이 아니다. 기본 대국 평가기를 바꾸지 않는다.

`scripts/engine_profile.py`는 프로필·실행 파일·모델·출처 영수증·선언한 외부 소스/런타임 설정의 SHA256을 검증하고 새 폴더로 복사한다. 파일 경로를 복사본에 다시 연결한다. 잘못된 모델 계열, 모드 모순, UCI 옵션의 대소문자 중복, 변경된 입력과 기존 출력 덮어쓰기를 거절한다. Git 객체 존재와 빌드 영수증을 별도로 기록하며, 해시만으로 빌드 출처가 증명된다고 표현하지 않는다. 미선언 파일·OS·Python 표준 환경까지 봉인하는 도구는 아니다.

`scripts/external_baseline_matrix.py`는 프로필/개시군 해시·시간 조건·색 교환·전체 판수·중단 정책을 결과 전에 동결한다. 기존 `compare_engines.py`의 UCI·합법 수·종료 판정을 재사용하고, 각 조건 뒤 PGN과 JSON을 다시 재생한다. 후보 순서는 블록마다 회전한다. 실패·시간 상한·중단·미실행을 별도로 남기며, 미완료를 무승부나 완료 성적으로 바꾸지 않는다. 이번 구현은 **공유 PC의 기술 파일럿 전용**이다. 정식 시계 심판·부하 격리·모델 자동 승격을 제공하지 않는다.

```powershell
# 입력 프로필과 외부 실행 파일을 먼저 준비한 뒤, 새 출력 폴더 사용
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py freeze --plan docs/experiments/2026-10-21-m2-pilot-plan.json --output build-m2-matrix/new-pilot
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py run --directory build-m2-matrix/new-pilot
.venv-tests/Scripts/python.exe scripts/external_baseline_matrix.py audit --directory build-m2-matrix/new-pilot
```

현재 고정 프로필은 이 Windows 빌드의 SHA를 가리킨다. 다른 PC에서 새로 빌드하면 새 실행 파일과 빌드 출처를 기록한 프로필 및 계획을 만들어야 한다. 이전 해시 검사를 끄거나 조용히 다른 파일로 대체하지 않는다. 외부 준비 파일과 재현 절차는 위 M2 기록에 남긴다.

### 기존 단일 상대 도구

내부 기준 엔진은 기존 `--baseline-commit`을 사용한다. 외부 기준 엔진은 `--baseline-source` JSON 파일을 사용한다. 둘 중 정확히 하나를 지정한다. 후보 Regicide의 `--candidate-commit`은 현재 체크아웃에서 접근할 수 있는 전체 커밋 ID여야 한다.

외부 manifest 예시(자리표시는 실제 값으로 교체):

```json
{
  "name": "External UCI engine",
  "version": "EXACT_VERSION",
  "source_url": "https://github.com/OWNER/REPOSITORY",
  "revision": "EXACT_RELEASE_TAG_OR_FULL_COMMIT",
  "binary_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "args": []
}
```

`args`는 실행 파일 다음에 붙일 문자열 배열이다. 일반적인 외부 UCI 실행 파일은 빈 배열을 사용하고 Regicide CLI는 `["uci"]`를 사용한다. 쉘을 거치지 않고 각 인자를 그대로 전달한다. URL은 기록만 하며 내려받거나 실행하지 않는다. 엔진 바이너리는 별도로 준비하고 출처·라이선스·배포 조건을 확인한다.

SHA256은 선택한 실행 파일에서 직접 계산한다. Windows PowerShell:

```powershell
(Get-FileHash -Algorithm SHA256 .\opponents\engine.exe).Hash.ToLowerInvariant()
```

manifest는 UTF-8(선택적 BOM 허용)로 저장한다. 해시가 맞지 않으면 대국 전에 실패한다. 파일 해시 일치는 출처와 빌드 관계의 증명이 아니다.

## 실행

이미 빌드한 후보와 외부 실행 파일을 다음처럼 연결한다. 경로, 커밋, 시작 위치 목록은 실제 고정한 값으로 교체한다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/compare_engines.py .\opponents\engine.exe .\build\regicide.exe --baseline-source .\opponents\source.json --candidate-commit FULL_REGICIDE_COMMIT --baseline-options '{"Hash":16,"Threads":1}' --candidate-options '{"Hash":16}' --movetime 1000 --openings .\tests\data\comparison_openings.json --pgn .\games\external-baseline.pgn
```

상대가 광고하는 옵션만 지정할 수 있다. Regicide는 현재 `Threads` 옵션을 제공하지 않는 단일 탐색 스레드 엔진이다. 외부 엔진의 약화 기능을 사용할 경우 정확한 옵션과 값을 기록하고 해당 조건의 성적으로 해석한다. 외부 엔진의 `UCI_Elo` 같은 설정값을 Regicide의 인간 Elo로 읽지 않는다.

기본 시작 위치 목록은 이미 개발에 쓰였으므로 연결 점검용이다. 경기력 주 비교에는 별도로 고정한 새 목록을 사용한다. 각 시작 위치에서 후보가 백인 판, 흑인 판을 연속 실행한다. `--opening-count`는 결과를 보기 전에 정한 목록 앞부분을 선택한다.

## 기록과 한계

JSON에는 실행 명령·파일 해시·외부 manifest와 원본 해시, 선언된 출처, 엔진의 UCI 이름과 광고 옵션, 요청/검사 후 전송한 설정, 시간·노드·깊이, 전체 수순을 저장한다. 외부 출처에는 `source_commit_object_verified: false`와 `provided_source_commit: null`을 기록한다. 실제 커밋이 Regicide에 존재하는 것처럼 꾸미지 않는다. PGN에도 외부 출처 URL/revision을 별도로 남긴다.

`validated_options`는 광고된 타입/범위로 해석하고 `configure()`가 반환한 값이다. UCI에는 일반적인 읽기 확인 명령이 없으며, 설정 전송은 엔진 내부 상태 검증이 아니다. `Ponder/MultiPV/UCI_Chess960/UCI_Variant`는 python-chess가 관리하므로 명시 설정을 거절한다.

초기 실행 파일, manifest와 시작 위치 입력은 출력으로 덮어쓰지 않는다. 기존 출력도 `--overwrite` 없이는 거절한다. 미완료 판은 `*`로 남기고 오류/중단과 미실행 판을 따로 집계한다. 일부 완료 판의 득점률은 전체 예정 대국의 확정 성적이 아니다.

이 도구는 기존 수당 시간·노드·깊이 제한을 지원한다. 양측의 같은 노드 수나 같은 깊이는 같은 시간 또는 같은 계산량이 아니다. 전체 대국 시계 심판, 완전한 CPU 부하 격리와 인간 FIDE Elo 추정은 제공하지 않는다. 측정 전에 버전·하드웨어·자원·시작 위치·종료/시간 초과/오류 정책을 고정하고 외부 부하를 통제한다.

## 검증

`tests/external_baseline_tests.py`는 manifest 검증, 출처 분리, 내부 모드 호환, 출력 충돌 방지와 외부 UCI 테스트 프로세스 연결을 검사한다. 테스트 프로세스는 합법 수를 고르는 연결 확인용이며 실력 평가 상대가 아니다. 기존 `tests/compare_engines_tests.py`에서 함께 실행하므로 Windows/Ubuntu CI에도 포함된다.

```powershell
.\.venv-tests\Scripts\python.exe tests/external_baseline_tests.py .\build\regicide.exe
```

UCI 옵션 파싱·설정에는 기존 의존성 python-chess 1.11.2의 `Option.parse()`, `Option.is_managed()`, `SimpleEngine.configure()`를 사용한다. 참고: [원저자 엔진 소스](https://github.com/niklasf/python-chess/blob/v1.11.2/chess/engine.py). 패키지의 역할과 이용 조건은 [엔진 문서](ENGINE.md)에 따른다.
