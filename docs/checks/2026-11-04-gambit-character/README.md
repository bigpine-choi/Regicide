# 갬빗 진입과 공격 지속 강화

최종 정책은 `regicide-boss-play-v5`, `regicide-boss-traps-v4`, `regicide-gambit-initiative-v2`다. 실제 착수는 Regicide 자체 엔진이 선택한다. 갬빗 이름·준비 수 선택·성립 수 제안·상대의 포획·남은 기물 투자·공격 대안 선택을 구분한다.

## 원인과 변경

기존에는 갬빗 성립 수에도 중반 미끼의 초기 10% 허용률을 적용했다. 이 제한을 개시에서 분리하고, 준비 수는 같은 완료 깊이와 최종 평가 모두 35cp 이내, 성립 수는 기존의 유한 평가·동일 완료 깊이 검증과 비용 공시를 유지했다. 메이트·불완전 근거·실패 때는 기준수를 보존한다.

첫 수정본의 고정 네 국면은 통과했지만 실제 네 판에서는 준비 수만 3회 선택되고 갬빗 성립·공격 지속은 모두 0회였다. 상대가 개시를 벗어나면 기능이 꺼졌다. 이 실패는 `audit-v1-final.json` 및 `docs/matches/2026-11-04-gambit-character-v1/`에 보존했다.

최종 보완은 다음과 같다.

- 당장 가능한 갬빗 성립 수를 긴 준비 수보다 먼저 검토한다. 성립 전에는 처음의 임시 라인 이름에 고정되지 않고 실제 분기에 맞는 경로를 다시 고른다.
- 로컬 CC0 카탈로그에서 확인한 Tennison·Caro-Kann von Hennig와 명시적 `1.d4 c6` 전치 진입을 추가했다. **21개 명명 패턴·22개 진입 수순**이며 전치본은 원문 그대로라고 표시하지 않는다. 원문·해시·서명 전후 FEN 검증은 `catalog-prefixes-v2.json`에 있다.
- 실제 갬빗 성립 뒤 최대 20반수 동안 전개·중앙 돌파·유지된 열린 길·왕 협공을 선호한다. 상대가 준비 수순을 벗어났을 때도 실제 준비 수를 기준으로 공격 기풍을 이어간다. `offered`와 `preparation`을 분리하고 준비 창을 갬빗 성립으로 세지 않는다.
- 공격 대안은 같은 완료 깊이·최종 점수 모두 기준수 −35cp 이상이어야 한다. 실제 상대 응수가 포함된 합법 수순 뒤의 특징을 비교하고 큰 새 기물 헌납을 걸러낸다. 기존 기준 탐색 뒤 **추가 최대 1초**를 상대 관찰과 공유한다. 미끼의 모든 합법 방어 검증은 유지한다.

## 실제 자체 엔진 재현

`native-fixtures-v2/report.json`의 King's·Evans·Englund·Stafford **4개 모두 통과**했다. 개발 국면과 시드는 엔진 결과를 보기 전에 개시 제안만으로 정했고, 기준 수나 최종 수를 강제로 주입하지 않았다. 실제 등록 성립 수 선택, 합법 수락 포획, 자체 후속 수, 공격 상태 진입, 완료 깊이, 추가 예산, 원시 탐색·PGN을 보존했다.

초기 실전에서 벗어났던 `1.e4 d5`도 `tennison-native-v2/report.json`에서 별도로 재현했다. 실제 자체 엔진이 `2.Nf3`, 상대의 `...dxe4` 뒤 `Ng5`를 선택했고 공격 전개에 진입했다. 이때 잡힌 것은 Nf3의 목적지 f3가 아니라 e4의 폰이다. 서명 칸의 포획과 실제 폰 투자를 혼동하지 않는다. Caro/von Hennig의 폰 투자는 별도 정적 합법 수순으로 확인했으며 이 보조 확인을 실제 엔진 실행으로 표시하지 않았다. `material-channel-checks.json`을 참조한다.

## 최종 표준 시작 네 판

설정은 Regicide 1000ms 및 기존 추가 최대 1000ms, Stockfish 18의 제한 설정 1700·300ms, Threads 1·Hash 16MB, classical 평가, 표준 시작이다. 두 시드 쌍에서 백·흑을 교대하고 쌍별 두 판을 묶은 작업 두 개를 병렬로 실행했다. 최대 80반수의 제한은 미완료 `*`로 남긴다. Stockfish는 대국 상대이며 실제 수의 공급원이 아니다.

| 확인 항목 | 결과 |
| --- | ---: |
| 새로 진행한 합법 반수 | 298 |
| 실제 갬빗 성립 | 2회: Blackmar·King |
| 성립 뒤 공격 전개 / 실제 대안 적용 | 19턴 / 2회 |
| 준비 단계 공격 전개 / 실제 대안 적용 | 10턴 / 1회 |
| 실제 서명 칸 포획 | 폰 1개, 100cp |
| 다음 자체 수 뒤에도 순기물 투자가 남은 갬빗 | 1회 |
| 대국 결과 | 정상 1승·1패, 미완료 2판 |

`audit-v2.json`에서 전체 합법 수·PGN·JSONL·진영과 시드 쌍·정책과 수순 문맥·동일 완료 깊이·35cp의 두 점수 하한·합법 재포획·예산·소스 및 실행 파일 불변을 독립 재검사했다. **필수 불일치 0건**이다. 실제 적용된 공격 대안의 로컬 재포획은 별도로 전부 계산했다.

미완료 두 판을 무승부로 계산하지 않는다. 같은 자체 시드를 사용했어도 제한 기력 Stockfish의 착수와 시간제 탐색은 고정된 경로가 아니므로 두 실험을 엄밀한 동일 포지션 기력 비교로 해석하지 않는다. 이 결과는 기풍의 작동을 확인한 소표본이며 Elo·승률 향상·희생의 객관적 건전성 증거가 아니다.

원본은 `docs/matches/2026-11-04-gambit-character-v2/pair-a/`와 `pair-b/`의 `match.json`, 각 `game-*.json`, `.moves.jsonl`, `.pgn`, `host/`에 있다. 각 실행의 조건·실행 파일·환경·소스 해시를 함께 보존했다.

## 자동 검사와 소스 보존

- 최종 통합 회귀 **83개 통과**: `integration-tests-v2.log`.
- 새 공격 모듈 및 인접 회귀 **71개 통과**: `../2026-11-04-gambit-identity/initiative-tests-v2.json`·로그. 위 검사와 일부 중복되므로 합산하지 않는다.
- 웹 모드 검사와 격리 Godot **80개 통과**: `ui/`.
- 앞선 정상 모드·시계 등 인접 회귀는 132개 중 선택적 실제 실행 파일 smoke 1개를 생략하고 통과했다. 실제 엔진은 위 별도 고정 재현과 네 판에서 실행했다. `regression-tests.log`.

최종 소스 227개는 `source-snapshot-v2.zip`과 `source-snapshot-v2-manifest.json`에 보존한다. 기존 v1의 원시 결과와 소스 스냅샷도 덮어쓰지 않았다. 사용자 대국·설정·인증 정보 및 동기화된 `sources/` 자료는 변경하지 않았다. 내부 `boss_mode`·기보 키와 옛 v3/v4 PGN 정책명은 보존한다. 과거 프레임의 단계 표시에는 현재 개시 카탈로그가 사용될 수 있으나 원본 기보·정책을 수정하지 않는다.

재실행은 저장소 루트에서 새로운 출력 경로를 지정한다.

```powershell
.venv-tests\Scripts\python.exe tests/gambit_character_validation.py --output-dir <새로운-고정재현-폴더>
.venv-tests\Scripts\python.exe docs/checks/2026-11-04-gambit-character/replay-tennison.py --output-dir <새로운-Tennison-폴더>
```

일반 대국은 `scripts/boss_strength_match.py`에 `--games 2 --boss-ms 1000 --stockfish-ms 300 --elo 1700 --threads 1 --hash-mb 16 --max-plies 80 --cap-policy incomplete --classical`을 지정한다. 시드는 각각 `2026-11-04-gambit-character-v1-a`, `...-b`이며 `--stockfish` 실행 파일과 서로 다른 새 `--output-dir`이 필요하다. 감사 명령은 `audit-character.py --matches <새-pair-a> <새-pair-b> --output <새-감사.json> --require-live-source-match`다.
