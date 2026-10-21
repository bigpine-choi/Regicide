# PGN 실제 파일 저장·다시 열기 검증 — 2026-10-21

실제 Godot 게임에서 **파일로 저장 → 새 대국 → 저장한 파일 열기 → 처음·다음·끝 재생 → 다른 이름으로 재저장 → 진행 대국 복귀**를 완료했다. 한글 파일명으로 생성된 두 파일은 모두 505바이트이며 SHA256까지 일치한다. 파일은 테스트 코드가 대신 작성한 것이 아니라 게임의 저장 버튼과 파일 선택창을 직접 조작해 만들었다.

- [실제로 내보낸 PGN](manual/실제저장-체크메이트-f.pgn)
- [다시 열고 재저장한 PGN](manual/다시열고-재저장.pgn)
- [독립 재생·원본 보존·코드 해시 검증](verification.json)
- [최종 PGN 파일 선택창·HTTP 자동 검사](godot/final-selection-bom-custom-root/report.json)

## 실제 화면에서 확인한 동작

진단용 대국의 첫 6반수(`e4 e5 Qh5 Nc6 Bc4 Nf6`)만 별도 관리기에서 준비했다. 마지막 `Qxf7#`는 실제 화면에서 퀸과 f7을 클릭해 두었다. 수순 준비를 사람이 직접 둔 것으로 주장하지 않는다. 개인 보관함 대신 격리된 임시 보관함과 기존 자체 C++ 엔진을 사용했다.

1. 체크메이트·`1-0` 표시를 확인했다. [종료 화면](manual/01-mate.png)
2. **기보 파일로 저장**을 누르고 검증 폴더·한글 파일명 `실제저장-체크메이트-f.pgn`을 입력한 뒤 **저장**했다. 파일명에 `f`를 입력해도 판이 뒤집히지 않았다. [저장 창](manual/02-save-dialog.png)
3. 새 대국을 시작하고 실제 `e4`를 두었다. 자체 엔진의 `c5` 응수를 확인했다. [별도의 진행 대국](manual/03-live-game-before-open.png)
4. 저장한 PGN을 열어 시작 배치, 첫 수 `e4`, 마지막 `Qxf7#`와 메이트 배치를 확인했다. 오프닝 안내도 해당 복기 위치를 따라 바뀌었다. [첫 수](manual/06-reopened-first-move.png), [마지막 배치](manual/07-reopened-mate.png)
5. 복기한 기보를 `다시열고-재저장.pgn`으로 저장했다. [재저장 완료](manual/08-reexport-success.png)
6. 현재 대국으로 돌아가 `e4 c5` 배치를 확인했다. HTTP로 읽은 전체 대국 상태와 보관함 파일 해시는 복기 전후 정확히 같았다. [복귀 화면](manual/09-live-return-preserved.png)
7. 열기 버튼이 비활성으로 남는 문제를 수정한 후 새 일반 게임 창을 실행했다. 파일 한 번 선택 → 활성 **열기** 버튼으로 재생 진입, 최근 폴더에서 다시 열기, 취소 후 복기 유지, 현재 대국 복귀를 다시 확인했다. [수정 후 선택](manual/10-fixed-single-selection.png), [열기 성공](manual/11-fixed-open-button-success.png), [취소 유지](manual/13-open-cancel-preserved.png)

내보낸 PGN은 python-chess로 별도 검증했다. 모든 수가 합법이며 마지막 FEN은 저장 직전 대국과 일치하고 실제 체크메이트다. 내보내기 전후 상태도 바뀌지 않았다. 실제 조작은 Windows Sky API를 이용했으며 매 입력 전에 게임 창의 새 화면을 확인했다.

## 발견한 문제와 수정

| 문제 | 최종 동작 |
| --- | --- |
| OS 저장 창의 열림 여부를 `visible`만으로 판단하여 중복 저장 요청·보류 기보 교체 가능 | 게임 안 모달 FileDialog와 명시적 열림 상태를 사용한다. 응답 대기·선택창·파일 작업 중 관련 명령을 막고 내보낼 내용을 고정한다. |
| 파일을 열기만 하면 저장 성공으로 처리 | 쓰기·flush·길이·오류 상태와 디스크 재읽기 결과를 확인한 뒤 완료를 표시한다. |
| 외부 PGN을 게임에서 다시 여는 경로 없음 | 로컬 파일을 읽고 검증한 뒤 기존 읽기 전용 복기에 연결한다. 진행 대국·보관함·엔진 검색을 변경하지 않는다. |
| 최근 파일을 `current_path`로 미리 지정하면 선택돼 보여도 열기 버튼 비활성 | 최근 폴더를 사용하고 파일 선택 경로를 거친다. 활성 버튼인지 확인하는 자동 검사와 실제 단일 선택 검증을 추가했다. |
| 새 가져오기에서 기존 핸디캡 화면 표시 누락 | 자체 내보내기의 핸디캡 주석을 복원하며 원문도 보존한다. |
| Godot UTF-8 디코더가 BOM을 제거해 정상 파일 거부 | BOM을 복원해 디스크 원문과 바이트 비교한다. BOM·CRLF 파일도 재저장 바이트가 같다. |
| FEN으로 흑 차례·임의 수 번호에서 시작한 PGN의 화면 이력 오표시 | 초기 FEN의 차례·수 번호를 반영한다. `2... exd3`은 2수 흑 칸에 표시한다. |

Godot의 native chooser는 별도 OS 창이며 `visible`과 실제 열림 상태를 일치시켜 해석할 수 없다. 이번 완료 검증 대상은 **게임 안의 파일 선택창**이다. native 창을 성공적으로 검증했다고 주장하지 않는다. 최근 파일 선택 문제는 [Godot 4.7.2 공식 구현](https://github.com/godotengine/godot/blob/4.7.2-stable/scene/gui/file_dialog.cpp#L1003-L1012), BOM 처리는 [공식 UTF-8 디코더](https://github.com/godotengine/godot/blob/4.7.2-stable/core/string/ustring.cpp#L1661-L1669)와 대조했다.

## 자동 검사 결과

| 검사 | 이번 실행 결과 | 증빙 |
| --- | --- | --- |
| PGN 파서·인증·요청 한도·읽기 전용 HTTP | 16 PASS | [파서 최종 기록](backend/final-parser-validation.json) |
| 게임 내 저장·취소·중복 방지·오류 후 재시도 | 41 PASS | [최종 검사](godot/final-selection-bom-custom-root/report.json) |
| 실제 HTTP·디스크 왕복·BOM·흑 시작 FEN | 42 PASS | 위와 같음 |
| 실제 엔진 양색 대국 | 25 PASS | [실제 엔진 회귀](backend/real-engine-validation.json) |
| 기존 보관함·재시작·독립 재생 | 19 PASS, 1 SKIP | 위와 같음. Windows 계정의 심볼릭 링크 생성 권한 때문에 해당 1건만 생략 |
| 기존 Godot 방향·입력·형세·양색 대국 | 637 + 43 + 82 PASS, 신규 체크아웃 실행·종료 3종 PASS | [대국 결과](regression/core/integration-result.json), 같은 폴더의 로그·close JSON |
| 기존 Godot 보관함·재생 | 78 PASS | [최종 보관함 회귀](regression/archive-final/archive-integration-result.json) |
| 기존 오프닝 안내·분기·복기 | 42 + 17 PASS | [오프닝 회귀](regression/opening/report.json) |

자동 FileDialog 검사는 항목 선택 신호와 활성 확인 버튼을 사용한다. 비활성 버튼의 `pressed` 신호를 강제로 호출해 성공을 만들지 않는다. 실제 HTTP 검사는 원본 파일·진행 대국 revision·기존 보관 파일 불변, 추가 엔진 검색 0회를 확인한다. 엔진 프로세스는 기존 대국 관리기를 위해 초기화되지만 가져오기·복기가 수를 검색하지 않는다.

초기 핸디캡 표시 실패는 [첫 실행 로그](godot/final/godot-live.txt)에 남겼다. 버튼 상태 검사를 보완하는 도중의 결과도 각 시도 폴더에 보존했다. **최종 판정은 `godot/final-selection-bom-custom-root/report.json`**이며 이전 시도의 PASS/FAIL과 혼합하지 않는다.

재현 명령(저장소 루트, 설치된 Godot 경로에 맞춤):

```powershell
.venv-tests/Scripts/python.exe tests/pgn_preview_tests.py
.venv-tests/Scripts/python.exe tests/pgn_file_godot_tests.py --godot C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe --binary build/regicide.exe --output build/pgn-check
```

두 검사를 기존 CI 절차에 연결했다. 원격 CI는 이번 작업에서 실행하지 않았다.

## 환경·지원 범위·보존

Windows 11, Godot 4.7.2 stable, Python 3.13.16, python-chess 1.11.2, 기존 `Regicide 0.7.0-dev` 실행 파일로 검증했다. 엔진 SHA256은 `edd950fb673cfca4134968b64cc5ef0f3d77eda698e9f40d2965170f143d6e6a`로 이전 기준과 같다. 실행 중 수정한 개발 작업 트리를 사용했으며 기준 커밋은 `c58ebac`이다. 최종 소스 해시는 `verification.json`에 기록했다.

UTF-8 표준 체스 한 대국, 최대 1MiB·512반수의 주 수순을 지원한다. 주석·대안 수순·머리글은 원문에 보존하고 화면은 주 수순만 재생한다. 여러 대국·Chess960 등 변형·빈 파일·불법 수·널 수·과도한 자료는 거부한다. 512반수의 합법 샘플은 513프레임·약922KB 응답이었고 준비된 환경의 한 차례 측정에서 8.81초가 걸렸다. 이 수치는 다른 PC의 응답 시간이나 경기력 향상을 보장하지 않는다.

원래 연구 체크아웃과 설치 체크아웃의 HEAD·기존 변경 목록·수정 파일 해시가 이전 기준과 모두 같음을 확인했다. 개인 기보와 동기화 `sources/`를 수정하지 않았다. 인증 정보는 생성물 폴더에만 두었고 증빙에 포함하지 않았다. 검증용 실제 게임·대국 관리기는 정상 종료했다.
