# v0.5.4 왕 앞 폰 방패 평가 개발·배포 기록

기준일: 2026-10-18 (한국 시간). 기준 버전은 `b3a782262a19db514c8dcd295bf5c8b50e8fc54e`의 v0.5.3이며, 엔진·C++ 검사 구현은 `bd50bba1529e917e6c0cc64eaaaaad47c7a37a94`에 저장했다. UCI 검사 위치의 잘못된 흑 폰 9개를 8개로 고친 검증 소스는 `5a01085911319413bd5b237e0cce223599ab6c35`이다. 이 수정은 엔진 소스를 바꾸지 않았다.

## 구현과 의미

[계획](2026-10-18-pawn-shield-plan.md)에 따라 왕 파일과 양옆 파일의 가장 가까운 앞 자기 폰까지 거리로 0/4/8/12cp의 작은 감점을 적용했다. 각 색의 감점은 상대 잔여 기물로 축소하고 중반 항목에만 넣는다. 기존 국면 혼합·왕 중앙화·폰 전진·기물 가치·탐색·규칙 처리는 유지한다. [사용 설명](../PAWN_SHIELD.md)에 원리와 한계를 정리했다.

기본 켬인 `UsePawnShield`와 콘솔 `--no-pawn-shield`, C++ `SearchOptions::king_pawn_shield`로 항목을 분리한다. 일반 탐색·안정화 탐색·중단 대체 평가에 같은 설정을 전달하며, `UsePositionalEvaluation=false`에서는 사용하지 않는다. 옵션 전환은 기존 탐색 중단·합류와 전이표 세대 격리 정책을 따른다.

소스·계수는 자체 구현한 초기 가정이다. 상대 공격 지도·적 폰 폭풍·피앙케토 비숍의 방어·고급 폰 구조는 추가하지 않았다. 특정 왕 파일이나 랭크에서 항목을 갑자기 끄지 않으며, 상대 기물이 없거나 폰만 남은 종반에는 새 감점이 0이다.

## 실제 수행한 검사

Windows 11, MSVC Release `/O2 /DNDEBUG /W4 /WX /permissive- /utf-8`, Python 3.13.16, chess 1.11.2, Godot 4.7.2에서 검사했다. Linux는 WSL2 Ubuntu 24.04 / GCC 13.3.0이며 경고를 오류로 처리했다. 기존 검사 그룹은 유지하고 신규 C++ 10그룹 및 UCI 2그룹을 추가했다.

| 검사 | 실제 결과 |
| --- | --- |
| Windows C++ 규칙 / 탐색 / 평가 / 상태복원 / 전이표 / SEE | 106그룹, 54,209 assertions 통과 |
| Linux GCC C++ 동일 6개 실행 파일 | 106그룹, 54,209 assertions 통과 |
| Linux ASan+UBSan 평가 / 탐색 | 59그룹, 17,694 assertions 통과, 메모리·UB·누수 보고 없음 |
| Python 독립 규칙 교차검증 | 2,997사례 통과 |
| UCI 전체 통합 | 기존 19 + 신규 2 = 21그룹 통과 |
| 사람 대국 / 형세 상태 / 추천 수 | 25 / 16 / 15개 통과 |
| 핸디캡 / 기보 보관 / 대국 실행 준비 | 19 / 19 / 3개 통과, 기보 심볼릭 링크 권한 검사 1개 건너뜀 |
| 기보 대국 실행 / 비교 실행 / 고정 깊이 도구 / 외부 기준 상대 도구 | 11 / 10 / 9 / 4개 통과 |
| Godot 시점·입력 / 형세 막대 / 실제 엔진 연결 | 637 / 40 / 82검사 통과 |
| Godot 정상·탐색 중·접속 중 종료 | 3사례 통과 |
| Godot 핸디캡 / 기보 보관함 | 53 / 78검사 통과 |
| Godot 추천 수 합성 / 실제 엔진 연동 | headless 38 / 35검사 통과 |
| 브라우저 추천 수 | Node VM 10사례 통과 |

신규 검사는 같은 재료·왕 좌표의 방패 거리 차이, 양색·양날개·가장자리, 가까운 앞 폰·뒤 폰·겹친 폰, 피앙케토의 작은 비용, 상대 기물 없는 비대칭 위치, 승격 과다의 상한, 색 반전·부호·보드 이력 불변을 확인한다. 독립 전수 탐색과 일반/안정화 탐색을 대조하고, 제한 노드 및 사전 취소의 대체 점수와 재사용 메모리의 ON/OFF 전환도 검사한다.

초기 UCI 신규 위치의 `is_valid()` 검사에서 흑 폰 9개를 검출해 중복 d6 폰을 제거했다. 수정 후 신규 2그룹과 전체 21그룹을 다시 실행해 통과했다. Linux 환경 수집은 Windows worktree 링크를 WSL Git이 읽지 못해, Windows Git에서 확인한 커밋과 소스 해시를 기록하는 방식으로 대체했다. 원격 CI는 별도 실행 상태이며 이 표의 수치는 실제 로컬 실행 결과다.

## OFF 대조와 고정 깊이 관측

기존 `tests/data/profile_positions.json` 전체 10위치, 깊이 3, warmup 1, repeats 2, seed 20261018, Hash 16, Move Overhead 20으로 실행했다. 위치·반복·계수는 결과에 따라 변경하지 않았다. 실행 파일의 시작 비용은 제외하고 UCI 요청의 벽시계와 엔진 보고 시간을 별도로 보존했다.

- 구버전 대 후보 OFF 20쌍은 수·점수·깊이·노드·seldepth·PV·TT 카운터가 모두 정확히 일치했다. 새 항목을 끄면 기존 동작을 재현한다.
- 후보 OFF/ON 20쌍 중 점수 차이 14쌍, 탐색 경로 차이 18쌍이었다. 폰 종반의 수·점수·탐색 경로는 일치했다. 평가를 바꾼 실험이므로 ON/OFF의 점수·경로 차이는 허용한 결과이며 최선 수의 개선 증명이 아니다.
- OFF/ON의 쌍별 중앙 `OFF 시간 / ON 시간`은 벽시계 0.83085, 엔진 보고 0.75636이었다. 이 작은 표본에서는 켠 쪽이 더 오래 걸리는 경향을 보였다. 서로 다른 탐색 트리와 추가 평가 비용이 섞여 있으며, 짧은 요청의 OS/통신 시간과 통제하지 않은 외부 앱 부하도 포함한다. 평가 함수 단독의 오버헤드, 일반적인 감속률 또는 같은 시간의 기력으로 환산하지 않는다.

원자료·로그·재현 스크립트·환경 및 해시는 [검증 폴더](../checks/2026-10-18-pawn-shield/)에 보존했다. 공개 증거에서는 로컬 사용자 폴더 경로만 `$REPO`/`$BASELINE_REPO`로 치환했으며 비공개 개인 기보·접속 파일·실행 파일은 포함하지 않았다. 원래 경로가 포함된 원본은 로컬 ignored 빌드 폴더에 남는다.

주요 재현 명령은 다음과 같다. Python은 `requirements-test.txt` 환경을 사용하며 `$oldBinary`에는 별도 보존한 기준 커밋의 실행 파일을 지정한다. 기존 출력이 있으면 새 출력 폴더를 사용한다. Linux 재현 스크립트의 `REGICIDE_ROOT`에는 체크아웃 절대 경로를 지정한다.

```powershell
./scripts/build.ps1 -Test
python tests/differential.py build/regicide.exe
python tests/engine_integration.py build/regicide.exe
python scripts/benchmark_search.py $oldBinary build/regicide.exe --output build/check-pawn-shield/legacy-off.json --depth 3 --repeats 2 --warmup 1 --seed 20261018 --baseline-commit b3a782262a19db514c8dcd295bf5c8b50e8fc54e --candidate-commit 5a01085911319413bd5b237e0cce223599ab6c35 --baseline-options '{"Hash":16,"Move Overhead":20}' --candidate-options '{"Hash":16,"Move Overhead":20,"UsePawnShield":false}' --require-same-search
python scripts/benchmark_search.py build/regicide.exe build/regicide.exe --output build/check-pawn-shield/off-on.json --depth 3 --repeats 2 --warmup 1 --seed 20261018 --baseline-commit 5a01085911319413bd5b237e0cce223599ab6c35 --candidate-commit 5a01085911319413bd5b237e0cce223599ab6c35 --baseline-options '{"Hash":16,"Move Overhead":20,"UsePawnShield":false}' --candidate-options '{"Hash":16,"Move Overhead":20,"UsePawnShield":true}' --allow-score-differences
```

## 로컬 배포

검사한 Windows 실행 파일 SHA256은 `f5ba425b04eeae63459c30e71b20052dfbdfd2c3c6d1ac89642c16bb3ccbefe3`이다. 이전 v0.5.3 실행 파일·바로가기·개인 기보를 별도 백업하고 정상 종료한 뒤 설치본에 교체했다. 바탕화면 바로가기로 v0.5.4를 열어 UCI `UsePawnShield` 기본 true, 실제 창의 버전, 추천 ON·화살표·OFF를 확인했다. 추천 검증 중 실제 착수는 없었으며, 창은 추천 OFF 상태로 두었다.

기존 기보 파일 124개의 정상 종료 후 원본과 백업 해시를 모두 대조해 보존을 확인했다. 개인 기보와 설치 경로가 들어간 상세 배포 기록은 로컬 `build/pawn-shield-validation/installation-report.json`에 둔다. 문서·증거 후속 커밋은 실행 파일의 엔진 코드를 바꾸지 않는다.

기존에 중단한 버전 대국 비교는 재개하지 않았다. **이번 완료는 왕 앞 폰 방패 항목의 구현·기존 규칙 보존·연동·로컬 배포이며, 전체 기력 향상과 Elo는 미검증이다.**
