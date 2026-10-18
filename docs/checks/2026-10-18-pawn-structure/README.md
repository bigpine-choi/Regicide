# v0.5.6 통과폰·폰 구조 평가 검증 자료

이 폴더는 `132f45451d0af68168cfe15507a3fdc069c86d9c`의 엔진·검증 코드를 대상으로 수행한 검사와 측정의 공개 사본이다. 기준 버전은 v0.5.5의 `585710724e93329a8f8baddfbc82724db4a4ce2a`다. 시간 측정은 평가 비용과 고정 깊이 탐색 비용을 다루며, 대국 성적이나 Elo 향상을 입증하지 않는다.

## 공개 사본의 선택·치환 정책

`build/pawn-structure-validation`과 `build/pawn-structure-linux`의 최상위 파일을 명시 목록으로 선택했다. 실제 엔진을 사용한 최종 `*-real.log`를 선택하고, 먼저 실행한 엔진 없는 중복 로그는 제외했다. 프로파일 빌드·표준 출력의 중복 자료는 제외하고 결과 JSON과 표준 오류를 보존했다. 프로파일을 같은 명령으로 빌드할 수 있도록 `build-pawn-profile/compile-profile.cmd` 하나도 명시적으로 추가했다.

실행 파일·오브젝트, 개인 기보, 백업 위치, 접속 파일·토큰·세션 자료, Godot 검증 출력 하위 폴더는 포함하지 않는다. CPU 자료는 집계 수치만 담고 있으며 개별 프로세스 이름·ID 목록은 없다.

환경 경로와 호스트명만 다음 공개 변수로 바꿨다. JSON 숫자·점수·수순·시드·옵션·원본 해시는 바꾸지 않았다. 원본의 절대 경로를 재현할 필요는 없으며, 각자 환경에 맞는 경로를 사용하면 된다.

| 표기 | 의미 |
| --- | --- |
| `$REPO` | v0.5.6 검증 저장소의 Windows 경로 |
| `$BASELINE_REPO` | Python 가상환경이 있는 로컬 저장소의 Windows 경로 |
| `$WSL_REPO` | 동일 검증 저장소의 WSL 경로 |
| `$VS_VCVARS64` | Visual Studio의 `vcvars64.bat` 경로 |
| `$HOSTNAME` | 검증 호스트 이름 |

공개 텍스트는 UTF-8·LF로 저장했다. **`linux-final-source-sha256.txt`는 원본 바이트를 그대로 보존한 예외**이며, 기록된 작업 파일 해시는 CRLF 파일의 해시다. Git의 LF blob 해시와는 줄바꿈 정규화 후 비교해야 한다. 독립 감사는 원본 파일 바이트와 정규화한 Git 소스 일치를 각각 확인했다.

`independent-audit.json` 내부의 `evidence_sha256`·`linux_evidence_sha256`는 감사 당시 비공개 원자료의 해시다. 경로 치환·공개 사본의 줄바꿈 변환 후에는 일부 공개 파일의 해시와 달라진다. 공개 파일의 최종 바이트 검증에는 배포 공개 결과가 추가된 뒤 생성하는 별도 `sha256.json`을 사용한다.

## 로그 해석

- `windows-native.log`, `linux-native.log`: 처음 수행한 전체 6종 검사. 이후 추가된 앙파상·승격·무르기 검사는 아래 최종 평가 로그가 대체한다.
- `windows-evaluation-final.log`, `linux-evaluation-final.log`: 최종 평가 검사. 전체 집계에서는 기존 평가 결과를 이것으로 교체하며 두 평가 실행을 합산하지 않는다. 최종 전체 집계는 플랫폼별 216,710개 assertion이다.
- `linux-sanitizers.log`, `linux-evaluation-sanitizers-final.log`: ASan·UBSan의 탐색·평가 검사와 최종 평가 재검사. 동일하게 최종 평가 결과가 초기 평가 결과를 대체한다.
- `linux-summary.json`: 원본 `build/pawn-structure-linux/summary.json`의 이름만 구분해 보존한 최종 집계.
- `tools-summary.json`: Python 고유 검사 102개 중 101개 통과·1개 생략, 별도 도구·브라우저 40개 케이스 통과. Windows 심볼릭 링크 생성 권한 때문에 1개를 생략했다. 상위 검사에서 다시 호출되는 하위 검사는 합계에서 제외했다.
- `engine_integration.log`, `differential.log`: UCI·CLI 연동 23개 그룹과 독립 규칙 교차검증 2,997개 사례.
- `*_godot_tests.log`, `godot_integration_tests.log`: 별도 임시 게임을 이용한 Godot 회귀. 개인 게임 기록이나 접속 자료는 이 폴더에 포함하지 않는다.
- `legacy-depth3.json`, `legacy-depth4.json`: 구조 평가 OFF와 v0.5.5의 고정 깊이 비교. 점수·수·PV·탐색 작업량의 엄격한 동일성을 요구했다.
- `structure-depth3.json`: 같은 새 바이너리의 구조 OFF/ON 비교. 평가 변경 때문에 점수·탐색량 차이를 허용한 비용 관찰이며 기력 비교가 아니다.
- `profile-windows.json`: 고정된 자체 작성 위치 58개의 평가 함수 비용. `profile-load.json`과 `search-load.json`은 측정 중의 집계 CPU 표본이다. 짧게 생겼다가 끝난 프로세스와 온도·클럭 상태 등을 완전히 관찰하지 못한다.
- `independent-audit.json`: 위 소스 신원·원자료·집계의 독립 재분석. 새 빌드나 대국을 수행한 검사는 아니다.

## 재현 방법

공개 스크립트의 `$REPO`, `$BASELINE_REPO`, `$WSL_REPO`, `$VS_VCVARS64`는 경로를 숨기기 위한 **문자열 템플릿**이다. 실행 전에 사본에서 실제 경로로 바꿔야 한다. PowerShell 변수의 자동 확장을 가정하지 않는다. **이 보관 폴더에서 스크립트를 직접 실행하면 안 된다.** 아래의 `build` 위치로 복사한 사본에서 실행하며, 새 결과도 `build` 아래에 생성한다. 원자료를 덮어쓰지 않도록 새 검증 checkout과 비어 있는 출력 디렉터리를 사용한다.

1. 위 코드 커밋을 별도 checkout에 준비한다. Windows에서는 Visual Studio C++ 도구로 `scripts/build.ps1 -Test`를 실행한다. Python에는 저장소의 테스트 의존성을 설치하고 Node를 준비한다.
2. `engine_integration.py`와 `differential.py`에 `build/regicide.exe`를 위치 인자로 전달한다. 후자는 `--seed 20261017 --games 32 --plies 100`을 사용했다.
3. `tools-summary.json`에 나열된 Python 테스트를 실행한다. `evaluation_state_tests.py`에는 바이너리를 전달하지 않고, 나머지 Python 테스트에는 `build/regicide.exe`를 위치 인자로 전달한다. Node는 `node tests/web_hint_tests.js`를 실행한다. 중복 하위 검사와 Windows 권한에 따른 생략을 따로 집계한다.
4. Godot 검사는 각 `tests/godot_integration_tests.py`, `hint_godot_tests.py`, `handicap_godot_tests.py`, `archive_godot_tests.py`에 `--godot`, `--binary`, 독립적인 `--output` 경로를 전달한다. Godot 로그는 자동화 검증 결과이며 사용자의 실제 대국 기록이 아니다.
5. Linux 스크립트 사본을 `build/pawn-structure-linux`에 복사하고 경로 템플릿을 치환한다. `cd`의 실제 경로는 공백에도 안전하도록 큰따옴표로 감싼다. 저장소 루트에서 `validate-linux.sh`를 실행한다. `recheck-evaluation.sh`는 첫 스크립트가 생성한 오브젝트를 사용하는 증분 재검사다. 현재 최종 소스에서 첫 스크립트를 실행하면 추가된 특수 수 검사도 처음부터 포함된다.
6. 고정 깊이 측정을 위해 v0.5.5를 별도 checkout에서 같은 빌드 설정으로 만든 뒤 검증 checkout의 `build/pawn-structure-validation/regicide-v055.exe`에 준비한다. 보존된 바이너리 해시는 JSON에 있으나 실행 파일은 이 공개 폴더에 배포하지 않는다.
7. 재현용 `create-corpus.py`, `run-fixed-depth.ps1`, `run-monitored.ps1`, `retest-evaluation.cmd`를 `build/pawn-structure-validation`에 복사한다. 이 상대 위치를 기준으로 저장소 루트를 계산한다. 폰 프로파일 corpus는 저장소에 이미 보존되어 있어 다시 만들 필요가 없다.
8. `build-pawn-profile`을 만든 뒤 경로를 치환한 `compile-profile.cmd`로 프로파일 실행 파일을 빌드한다. `run-monitored.ps1 -Mode profile`로 함수 비용을, `run-monitored.ps1 -Mode search`로 고정 깊이를 측정한다. 두 측정을 동시에 실행하지 않는다. 이미 결과 파일이 있으면 새 검증 디렉터리를 사용하고, 각 JSON의 반복·워밍업·시드·해시·옵션을 확인한다.

공개 사본은 기록된 실행의 검토와 재현을 돕는다. 새로운 환경의 실행 시간, 검증 당시 바이너리의 빌드 경로 전체, 통과폰 평가의 실제 대국 강도까지 독립적으로 보증하지는 않는다.
