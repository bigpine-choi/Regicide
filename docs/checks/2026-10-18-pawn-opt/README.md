# v0.5.7 폰 평가 동값 최적화 검증 자료

통과폰·겹친 폰·고립된 폰·왕 앞 방패의 평가값을 유지하면서 계산 비용을 줄인 최종 후보의 공개 검증 자료다. 계수, 네 옵션 조합, 중반·종반의 혼합과 마지막 정수 나눗셈은 기존 v0.5.6과 같다. 이 자료는 정확성과 계산 비용을 다루며 대국 강도나 Elo 향상을 입증하지 않는다.

## 후보와 기준 버전의 구분

| 구분 | 소스 커밋 | Windows 엔진 SHA-256 |
| --- | --- | --- |
| 기준 v0.5.6 | `09090d0c35aed9ae1643fa1785203ac5b87e678e` | `7663c84b9aa21c6e56374985e8419426e44afb42cfac75503ef1ee2635cd0747` |
| 첫 후보·실패·미배포 | `4db4d83d11a2070118ad5a5d9d1ffe63ca2a04b9` | `421d2e3a2432762b69ff4b45ba3b30365a6b1f63dd1e89bb1f2531a69e011aee` |
| 두 번째 후보·함수 비용만 개선·미배포 | `87943a6fb733323791f60cc83531fb49cd7d9030` | `8a202b88c6a2b760b801530869078c1faa9b9cf9b4438357877edf54461528af` |
| 최종 후보 v0.5.7 | `4ee6dde974c6e250c93d52d2ae812586d92f7dce` | `b9a7c733388714f4b11d4678f270cc56d8c68e4bc015992e6e08a7b4bf2ab6dc` |

첫 후보는 32바이트 표 항목과 백·흑 공유 순회를 사용했다. 점수 동값 검사는 통과했지만 함수 단독 비용이 늘어 채택하지 않았고 로컬에 설치하지 않았다. `pilot-4db4d83-profile-windows.json`, `pilot-4db4d83-profile-load.json`, `pilot-4db4d83-decision.json` **세 파일만 실패 부록**으로 남겼다. 이 결과를 최종 후보의 측정이나 검증으로 합산하지 않는다.

두 번째 후보는 8바이트 폰 표와 원래 색별 순회로 함수 비용을 줄였지만, 전체 탐색의 개선을 확인하지 못해 설치하지 않았다. `pilot-87943a6-*.json` 11개와 `independent-audit-87943a6.json`은 이 후보의 **중간 단계 부록**이다. 감사 내부의 원래 파일명은 당시 원자료를 가리키며, 이 공개 폴더에서는 대응하는 `pilot-87943a6-` 접두사 파일을 읽는다. 첫 후보 자료나 최종 결과와 합산하지 않는다.

최종 후보는 8바이트 폰 표와 색별 순회를 유지하면서, 기물의 색과 실제 칸에 맞춘 부호·상대 좌표 계산도 미리 계산한 표로 바꿨다. 평가 계수는 유지하며 정적 표 합계는 13,376바이트다. 접두사가 없는 최종 로그·JSON과 Linux 검사는 이 후보에 대해 다시 실행했다. 세 후보 모두 버전 문자열은 v0.5.7이므로 **소스 커밋과 실행 파일 해시로 구분**해야 한다.

## 공개 사본 정책

`build/pawn-opt-validation`의 최상위 최종 로그·JSON·PowerShell 스크립트와 위 실패·중간 단계 부록, `build/pawn-opt-linux`의 최종 여섯 파일을 명시 목록으로 선택했다. Windows 엔진 빌드의 `build/compile.cmd`와 함수 측정 빌드의 `build-pawn-opt-profile/compile-profile.cmd`도 포함했다. Linux의 원본 `summary.json`은 구분을 위해 `linux-summary.json`으로 보존했다.

실행 파일·오브젝트, 개인 기보·백업 위치, 토큰·접속·세션 파일, Godot 검증 출력 하위 폴더는 포함하지 않는다. CPU 자료는 집계 수치이며 개별 프로세스 이름·ID 목록을 담지 않는다.

환경 경로와 호스트명을 다음 공개 변수로 치환했다. **JSON 숫자·수순·점수·시드·옵션·원본 해시는 변경하지 않았다.**

| 공개 표기 | 의미 |
| --- | --- |
| `$REPO` | 최종 후보 검증 checkout의 Windows 경로 |
| `$BASELINE_REPO` | 테스트용 Python 환경이 있는 로컬 저장소 경로 |
| `$WSL_REPO` | 같은 검증 checkout의 WSL 경로 |
| `$VS_VCVARS64` | Visual Studio의 `vcvars64.bat` 경로 |
| `$HOSTNAME` | 검증 호스트 이름 |
| `$PYTHON_HOME` | Python 표준 라이브러리 설치 경로 |
| `$USERPROFILE` | 그 밖의 사용자 홈 경로 |

텍스트 공개 사본은 UTF-8·LF로 저장했다. **`linux-final-source-sha256.txt`만 원본 바이트를 그대로 보존했다.** 이 파일의 해시는 Windows 작업 파일의 CRLF 바이트에 해당한다. Git의 LF blob과 소스 내용을 비교할 때에는 원자료 바이트 확인과 줄바꿈 정규화 후의 내용 비교를 구분해야 한다.

`independent-audit.json`과 중간 후보 감사 안의 원자료 해시들은 각각 감사 당시 치환 전 파일의 해시다. 경로 치환과 공개 사본의 줄바꿈 변환 때문에 일부 공개 파일의 해시와 달라질 수 있다. 감사 원본 해시를 공개 사본에 맞춰 다시 쓰지 않았다. 공개 결과 전체의 최종 바이트 검증에는 설치 공개 보고서까지 추가한 뒤 생성하는 **별도 `sha256.json`**을 사용한다.

## 기록별 의미

- `windows-native.log`, `linux-native.log`: 최종 후보 전체 핵심 6종 검사. 각 플랫폼 466,139개 assertion이며 첫 후보의 결과를 더하지 않는다.
- `linux-sanitizers.log`: 최종 후보 평가·탐색의 ASan·UBSan·누수 검사 429,624개 assertion.
- `linux-environment.txt`, `linux-final-source-sha256.txt`, `linux-summary.json`: 컴파일 환경·소스 신원·최종 집계.
- `provenance.json`: 최종 코드와 엔진·프로파일 실행 파일의 해시, 원본 작업 파일과 Git blob의 줄바꿈을 구분한 소스 신원 확인. 재현 가능한 빌드 전체를 보증하는 자료는 아니다.
- `engine_integration.log`, `differential.log`: UCI·CLI·옵션 수명주기·동시성의 23개 그룹과 python-chess 독립 규칙 비교 2,997개 사례.
- `tools-summary.json`과 해당 최종 `*-real.log`·`*-tools.log`: Python 고유 검사 101개 통과·1개 생략, 별도 도구·브라우저 40개 케이스 통과. 생략 사유는 Windows 심볼릭 링크 생성 권한이다. `human_play_tests`가 다시 호출한 평가 검사와 `compare_engines_tests`가 다시 호출한 외부 기준 검사는 중복 합산하지 않는다.
- `*_godot_tests.log`, `godot_integration_tests.log`: 최종 후보의 임시 게임을 이용한 GUI 자동 검증. 사용자 개인 대국 자료는 포함하지 않는다.
- `profile-verify.json`: 고정된 자체 작성 58개 위치의 사전 동값 확인. v0.5.6 네 옵션 조합 232회와 구조 OFF의 v0.5.4 참조 116회를 구분한다.
- `profile-windows.json`: 기존 동결 함수와 최종 함수를 같은 프로파일 실행 파일에서 비교한 함수 단독 비용. 체크섬, 동일 호출 수, 워밍업, 균형 순서를 보존한다. 엔진 전체 탐색의 시간과 다르다.
- `default-depth3/4/5.json`, `structure-off-depth3.json`, `shield-off-depth3.json`, `both-off-depth3.json`: 같은 옵션의 기준/최종 실행 파일을 비교한 고정 깊이 비용. 수·점수·PV·탐색 작업량의 엄격한 동일성을 요구한다. 로그와 JSON에 각 반복·워밍업·시드가 있다.
- `profile-load.json`, `search-load.json`, `desktop-load-boundary.json`: 성능 측정 동안의 집계 CPU 관찰과 알려진 데스크톱 엔진의 경계 관찰. 짧게 생겼다 사라진 프로세스·접근 불가 프로세스·온도·클럭 등을 완전히 관찰하지 못한다.
- `independent-audit.json`: 동결 소스 신원, 기존 원자료, 동값·집계·측정 수치의 독립 재분석. 새 빌드나 강도 대국을 실행한 기록은 아니다.
- `installation-public.json`: 별도로 추가하는 로컬 반영 결과 공개 요약. 세션 비밀이나 개인 기보를 포함하지 않는다.

`hint_godot_tests.log`는 취소 시험 중 끊긴 로컬 HTTP 응답의 `ConnectionAbortedError` 기록도 그대로 보존한다. 이후 추천 수 synthetic 38개·실제 연동 35개 검사는 실패 0으로 종료했다. 예외 원문을 지워 성공 로그처럼 만들지 않았다.

## 재현 방법

공개 변수는 경로를 숨기기 위한 **문자열 템플릿**이다. 실행 전에 작업 사본의 템플릿을 자신의 실제 경로로 바꾼다. PowerShell에서 자동으로 확장되는 환경 변수라고 가정하지 않는다. WSL 스크립트의 `cd` 경로는 공백에도 안전하도록 큰따옴표로 감싼다.

**이 보관 폴더에서 재현 스크립트를 직접 실행하면 안 된다.** 아래 원래 `build` 위치로 복사한 사본에서 실행하며 새 출력도 `build` 아래에 생성한다. 기존 증거를 덮어쓰지 않도록 새 검증 checkout과 비어 있는 출력 디렉터리를 사용한다.

1. 최종 소스 커밋을 별도 checkout에 준비한다. Windows에는 Visual Studio C++ 도구, 테스트 의존성을 설치한 Python, Node를 준비한다.
2. 경로를 치환한 `compile.cmd`를 **`build/compile.cmd`**에 복사해 저장소 루트에서 실행한다. 이 스크립트는 엔진과 테스트를 빌드한다. 이후 생성된 규칙·탐색·평가·보드 상태·TT·SEE 여섯 테스트 실행 파일을 실행한다. 저장소의 `scripts/build.ps1 -Test`도 이 빌드와 검사 순서를 재현한다.
3. `tests/engine_integration.py build/regicide.exe`와 `tests/differential.py build/regicide.exe --seed 20261017 --games 32 --plies 100`을 실행한다. `tools-summary.json`의 Python 테스트는 `evaluation_state_tests.py`만 바이너리 인자 없이 실행하며, 나머지에는 `build/regicide.exe`를 전달한다. Node는 `node tests/web_hint_tests.js`를 실행한다.
4. Godot 검사 네 종류는 해당 `tests/*godot_tests.py` 또는 `tests/godot_integration_tests.py`에 `--godot`, `--binary`, 서로 다른 `--output` 경로를 전달한다. 출력은 보관 폴더 밖 `build` 아래에서 생성한다.
5. 경로를 치환한 `validate-linux.sh`를 **`build/pawn-opt-linux`**에 복사하고 WSL에서 실행한다. 핵심 여섯 검사와 평가·탐색 sanitizer 검사를 모두 새 오브젝트로 만든다. `linux-summary.json`의 설정·컴파일러·소스 해시를 확인한다.
6. 기준 v0.5.6 소스를 별도 checkout에서 같은 설정으로 빌드해 검증 checkout의 `build/pawn-opt-validation/regicide-v056.exe`에 준비한다. 실행 파일 자체는 공개 폴더에 배포하지 않는다. 새 빌드의 바이트는 도구 환경에 따라 달라질 수 있으므로 기록된 소스와 옵션도 함께 확인한다.
7. `run-fixed-depth.ps1`과 `run-monitored.ps1` 사본을 **`build/pawn-opt-validation`**에 복사한다. 이 위치에서 `$PSScriptRoot`의 두 단계 상위가 저장소 루트가 된다. 다른 위치에서 실행하면 루트 계산이 맞지 않는다.
8. 경로를 치환한 `compile-profile.cmd`는 **`build-pawn-opt-profile/compile-profile.cmd`**에 복사하고 해당 디렉터리를 만든 뒤 실행한다. 코드를 별도 오브젝트와 `/GL-`로 빌드하는 실제 명령을 보존했다.
9. 프로파일 실행 파일에 `--compare-pawn-optimization --verify-only --fen-file tests/data/pawn_structure_profile_2026_10_18.fens`를 전달해 동값을 먼저 확인한다. `run-monitored.ps1 -Mode profile`로 함수 비용을, `run-monitored.ps1 -Mode search`로 고정 깊이 비교를 수행한다. 두 측정을 동시에 실행하지 않는다. 모니터는 이미 출력이 있으면 실행을 거부하므로 새 검증 경로를 사용한다.

이 공개 사본은 기록된 실행의 검토와 재현을 돕는다. 새 환경의 동일한 실행 시간, 컴파일 과정 전체에 대한 독립 증명, 최종 후보의 실전 기력 향상까지 보증하지는 않는다.
