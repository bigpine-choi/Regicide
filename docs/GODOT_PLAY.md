# Godot에서 최신 Regicide와 대국하기

체스판 옆 **실시간 형세 막대**는 엔진 사고 중 백·흑 우세와 백 기준 점수를 갱신한다. `+`는 백, `−`는 흑 우세이며 승률이 아니다. 계산 전에는 대기, 착수 뒤에는 직전 탐색 평가, 종국에는 실제 결과를 표시한다. [평가값·갱신·검증 안내](LIVE_EVALUATION.md)를 참고한다.

Windows에서는 저장소의 **`Play-Regicide-Godot.cmd`를 더블클릭**한다. 브라우저와 별도의 Godot 창에서 자체 Regicide 엔진과 대국한다.

**추천 수: 끔/켬** 버튼으로 내 다음 수에 대한 도움을 켜고 끌 수 있다. 켜면 내 차례에 Regicide가 추천한 수를 체스판과 문구로 표시하며, 착수는 직접 선택한다. 추천 시간은 대국에 적용된 한 수 생각 시간을 따른다. [추천 수 사용법과 한계](MOVE_HINTS.md)를 참고한다.

바탕화면의 **Regicide** 바로가기도 같은 Godot 대국을 실행한다. 바로가기는 일반 개발 폴더의 실행기를 가리키므로 Codex 프로젝트 폴더를 열 필요가 없다. 새 컴퓨터나 다른 저장 위치에서는 준비를 마친 저장소에서 다음을 실행해 바로가기를 만든다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_desktop_shortcut.ps1
```

이 설치기는 Windows가 지정한 실제 바탕화면 위치(OneDrive로 옮긴 경우 포함)를 사용하고, 자체 왕 아이콘을 연결한다. 다른 위치를 가리키는 기존 동명 바로가기는 덮어쓰지 않는다. 실행 파일·Python 환경·Godot은 아래 실행 준비와 같이 필요하다.

## 시점과 기물 이동

- **시점 뒤집기 ⇅** 버튼 또는 **F** 키를 누를 때마다 백 시점 ↔ 흑 시점으로 바뀐다. 백 시점은 백이 아래, 흑 시점은 흑이 아래다.
- 내 기물 색과 시점은 독립적이다. 내가 백이어도 흑 시점에서 둘 수 있고, 내가 흑이어도 백 시점에서 둘 수 있다. 엔진이 생각하는 중, 승격 기물을 고르는 중, 대국이 끝난 후에도 전환할 수 있다.
- 뒤집으면 기물·좌표·클릭 방향이 함께 바뀐다. 기물 배치, 선택한 기물, 승격 후보, 차례, 수순은 유지된다. 새 대국과 되돌리기도 현재 시점을 유지한다. 프로그램을 다시 열면 백 시점으로 시작한다.
- 기물을 누른 뒤 도착 칸을 누르거나 드래그한다. 캐슬링은 왕을 두 칸 이동하고, 승격은 퀸·룩·비숍·나이트 중 고른다.

오른쪽에서 **내 기물**을 고른 뒤 **새 대국 시작**을 누른다. 흑을 고르면 Regicide가 먼저 둔다. **한 수 생각 시간**은 0.1·1·3·5초이며 기본 1초다. 대국 중에도 선택하면 즉시 저장되고 **다음 Regicide 수부터 적용**된다. 이미 생각 중인 수의 시간은 유지하며, 화면에 이번 수와 다음 수의 시간을 따로 표시한다. 시간 변경으로 새 대국이 시작되거나 기물 색·수순이 바뀌지 않는다. 무르기를 눌러도 현재 시간 설정을 유지한다. 사람의 생각 시간에는 제한이 없다.

**무르기**는 내 수와 엔진 응수를 함께 되돌린다. 여러 번 눌러 더 앞선 내 차례로 돌아갈 수 있다. 응수 전 내 수만 있으면 그 수만 되돌리며, 내 마지막 수로 메이트·무승부가 난 경우에도 무를 수 있다. 내가 흑이면 엔진의 첫 수는 남는다. 창을 닫으면 진행한 수를 보관하고 이 창의 대국 관리기와 엔진을 종료한다. 별도로 열어 둔 대국에는 영향을 주지 않는다.

## 내 대국 기보 보관함

매 수가 끝날 때 **PGN과 재현용 JSON을 자동 저장**한다. Windows 기본 위치는 실제 **문서 폴더의 `Regicide/games/`**다. OneDrive로 문서 폴더를 옮겼다면 그 위치를 사용한다. 저장소·빌드 폴더를 옮기거나 프로그램을 다시 실행해도 같은 보관함을 사용한다. 개인 기보는 소스 코드와 별도로 PC에 보관한다.

1. 오른쪽 **내 대국 기보**를 누른다. 최근 대국부터 날짜·내 색·승패/미완료·착수 수·핸디캡 횟수를 확인한다.
2. 대국을 선택하고 **선택한 기보 보기**를 누른다.
3. **처음·이전·다음·끝**으로 체스판을 한 수씩 재생한다. 백·흑 시점도 자유롭게 바꾼다. 다시 보기에서는 현재 대국의 수를 바꾸거나 엔진을 실행하지 않는다.
4. **이 기보 파일로 저장 (PGN)**은 선택한 대국 전체 기보를 저장한다. **현재 대국으로 돌아가기**를 누르면 보던 대국으로 복귀한다.

새 대국마다 고유 번호와 별도 파일을 사용하므로 앞선 대국이 남는다. 새 대국을 시작하거나 창을 닫았을 때 끝나지 않은 대국은 결과 `*`인 **미완료**로 보관한다. 완료·오류·핸디캡 기록도 구분하며, 한 수도 두지 않은 빈 시작 화면은 목록에서 제외한다. 미완료 기보도 다시 볼 수 있으며, 보관함의 재생은 읽기 전용이다.

이전 `build/human-games/`의 기보는 실행 시 가져온다. 원본은 수정하지 않고 중복을 피하며, 같은 번호의 다른 기록은 별도로 보존한다. 사용하던 두 개발 폴더의 기존 실제 대국 12개도 초기 가져오기로 보관했다. 오래된 기록에 없는 종료 시각은 만들어 내지 않는다. 손상된 파일은 원본을 보존하고 정상 기보 목록과 분리한다.

명령줄의 `--save-dir`로 별도 보관함을 지정할 수 있고, `--import-games 경로`를 여러 번 지정해 이전 폴더를 추가로 가져올 수 있다. Linux의 기본 위치는 `$XDG_DATA_HOME/regicide/games` 또는 `~/.local/share/regicide/games`이며, macOS는 `~/Library/Application Support/Regicide/games`다.

## Regicide에 핸디캡 주기

1. 오른쪽 목록에서 **바꾸고 싶은 Regicide의 수**를 고른다. 가장 최근 응수가 기본이며, 과거 수나 흑 플레이 시 엔진의 첫 수도 고를 수 있다.
2. **Regicide 수 바꾸기**를 누른다. 선택한 수를 두기 직전으로 돌아가며 **그 수 이후의 기존 수순은 대체 대상**이 된다. 엔진은 자동으로 두지 않고 사용자의 지정을 기다린다.
3. Regicide의 기물과 도착 칸을 클릭하거나 드래그해 **합법 수 중 원하는 수**를 둔다. 예를 들어 원래 `...e5`를 `...f6`로 바꿔 약점을 만들 수 있다. 승격할 때는 기물도 직접 고른다.
4. 지정한 수가 확정되면 내 차례다. 내가 응수하면 Regicide는 **강제한 수를 포함한 새 수순**에서 다시 계산한다. 이전에 버린 수순은 사용하지 않는다.

지정하기 전 **수 바꾸기 취소**를 누르면 원래 기물 배치·전체 수순·종료 상태로 복원된다. 편집 중에는 무르기·기권·새 대국·PGN 저장 버튼을 잠시 잠근다. 지정 전 미리보기로 원래 자동 저장 파일을 덮어쓰지 않으며, 그 상태로 창을 닫으면 원래 기보가 남는다.

수 바꾸기 미리보기와 과거 기보 다시 보기 중에는 생각 시간도 변경하지 않는다. 시간 변경 이력과 각 실제 검색의 요청 시간은 JSON·PGN 주석에 보존한다. [대국 중 시간 변경 검증](experiments/2026-10-19-live-think-time.md)을 참고한다.

핸디캡 중에도 백·흑 시점 전환과 승격 선택을 사용할 수 있다. 엔진이 생각하는 중에는 응수가 끝난 뒤 수 바꾸기·무르기를 누른다. 체크 방치·불법 이동·왕 제거 같은 자유 편집은 제공하지 않는다.

사용자가 대신 둔 수는 화면 기보에 **핸디캡**으로 표시하며 PGN 주석과 `HandicapMoves` 머리글에도 기록한다. JSON의 `handicap_events`에는 바뀐 수와 버린 이전 수순을 남긴다. 무르기·수 교체 후에는 엔진의 검색 상태를 새로 시작하되 반복·캐슬링·앙파상·50수 판정을 위한 전체 유효 수순은 보존한다. 이 대국은 사용자가 개입하는 연습이며 엔진의 독립 경기력 기록과 구분한다.

반복·50수 규칙으로 청구 가능한 무승부는 양쪽 모두 자동 청구한다. 예정 수로 청구 가능한 경우도 포함한다. 되돌리기를 허용하는 연습 대국이므로 공식 경기력 측정과 구분한다.

## 실행 준비

현재 개발 PC에서는 Godot **4.7.2 stable**, Python 3.13.16, 기존 C++ 실행 파일로 확인했다. 런처는 Godot 4.4 이상을 요구하며, 모든 하위 버전을 검증한 것은 아니다. 독립 실행 파일로 내보낸 배포판은 아직 만들지 않았다.

새 Windows 환경에서는 Godot, Python 3.11 이상, Visual Studio의 C++ 데스크톱 개발 도구를 설치한 뒤 다음을 실행한다.

```powershell
python -m venv .venv-tests
.\.venv-tests\Scripts\python.exe -m pip install -r requirements-test.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Test
.\Play-Regicide-Godot.cmd
```

Godot 실행 파일은 `-GodotPath`, `REGICIDE_GODOT_PATH`, PATH의 `godot`/`godot4`, 현재 PC의 `~/Tools/Godot/Godot_v4.7.2-stable_win64.exe` 순서로 찾는다. 다른 위치라면 다음처럼 지정한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/play_godot.ps1 -GodotPath "C:\Tools\Godot\Godot.exe"
```

실행 기록은 `build/godot-play/`에 남는다. 새 실행마다 빈 로컬 포트와 별도 대국을 사용한다. 실행기는 누락된 엔진을 기존 빌드 스크립트로 만들지만 Python 패키지나 Godot을 자동 설치하지 않는다. 프로젝트 편집은 Godot에서 `godot/project.godot`을 연다.

## 구조와 검증

Godot은 화면·입력·시점만 담당한다. 로컬 Python 대국 관리기가 합법 수·종료 판정·기보를 관리하고, **일반 응수는 기존 C++ Regicide가 선택**한다. 핸디캡으로 지정한 수만 사용자가 대신 선택한다. 통신은 비동기이므로 엔진 응수를 기다려도 뒤집기 버튼이 동작한다. 서버 상태를 바꾸는 요청에는 최신 대국 번호를 보내 오래된 요청을 거부한다. 시점 전환은 서버에 요청하지 않는다.

각 창은 실행 시 생성한 전용 파일로 로컬 인증 정보를 전달받는다. 인증 값은 콘솔 출력에 넣지 않는다. 이 파일은 생성물 폴더에 보관하므로 공유하지 않는다. 외부 서비스·폰트·기물 이미지는 내려받지 않으며, 화면의 벡터 기물은 자체 작성했다. Godot의 [MIT 이용 조건](https://godotengine.org/license/)과 기존 Python 패키지의 [이용 조건](ENGINE.md)은 각각 적용된다.

```powershell
.\.venv-tests\Scripts\python.exe tests/human_play_tests.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/handicap_play_tests.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/game_archive_tests.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/godot_bootstrap_tests.py build/regicide.exe
.\.venv-tests\Scripts\python.exe tests/godot_integration_tests.py --godot "C:\Users\hanso\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe" --binary build/regicide.exe
# 실제 창을 렌더링하고 자동 입력 및 화면 캡처까지 검사
.\.venv-tests\Scripts\python.exe tests/godot_integration_tests.py --godot "C:\Users\hanso\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe" --binary build/regicide.exe --rendered
.\.venv-tests\Scripts\python.exe tests/handicap_godot_tests.py --godot "C:\Users\hanso\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe" --binary build/regicide.exe --rendered
.\.venv-tests\Scripts\python.exe tests/archive_godot_tests.py --godot "C:\Users\hanso\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe" --binary build/regicide.exe --rendered
```

양쪽 시점의 전체 좌표, 클릭·드래그, 특수 수와 승격 선택, 실제 엔진의 응답 중 뒤집기, 양색 대국·되돌리기·종료, 저장 기보의 독립 재생을 검사한다. 자동 입력으로 실제 Godot 장면을 검증한 결과이며 사람의 수동 대국 검증과 구분한다. 기존 C++ 규칙 검증과 외부 기준의 2,997개 교차 검증도 유지한다. [검증 기록](experiments/2026-10-17-godot-play.md)에 실행 조건과 결과를 남긴다.
