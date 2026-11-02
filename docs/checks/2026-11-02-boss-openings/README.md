# 갬빗 보스 개발 검증 — 2026-11-02

목표는 별도 보스 모드의 갬빗 도입, 조건부 미끼 검증, 대국 중 상대 관찰과 빈도 조절이다. 자체 Regicide를 사용하며 C++ 탐색기와 신경망 가중치는 이번 변경에서 수정하지 않았다. 사용 방법과 판단 원리는 [보스 모드 설명](../../BOSS_MODE.md)에 정리했다.

## 실제 엔진 실행

실행 환경은 Windows 11, Python 3.13.16, python-chess 1.11.2이며, 앱과 동일하게 `UsePieceActivity=true`를 사용했다. 엔진 SHA-256은 `5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba`이다. 각 JSON은 옵션, 시간, 수순, 판정 기준과 한계를 보존한다.

| 기록 | 확인한 내용 | 범위 |
| --- | --- | --- |
| [native-traps.json](native-traps.json) | 갬빗 관련 메이트·메이트 회피 8/8 | 포지션당 1초, 프로젝트가 작성한 소규모 개발 문제 |
| [native-poisoned-material.json](native-poisoned-material.json) | 나이트→폰 이동→비숍 길→룩 회수, 재포획 반례, 여전히 불리한 형세의 순기물 이익 3/3 | 자체 검색과 모든 합법 방어 검사, 즉시 회수하는 패턴 |
| [native-poisoned-delayed.json](native-poisoned-delayed.json) | 합성 비숍 미끼 이후 3~4수 메이트 증명; 알빈 Lasker 구도는 짧은 획득 증명 실패로 거절 | 2개 구도 중 1개만 증명, 성공·실패 함께 보존 |
| [native-selector-adaptive.json](native-selector-adaptive.json) | 최종 v3 실제 대국 처리 경로 4/4: 신중한 갬빗 도입, 미끼 보류, 위험한 미끼 선택, 인간 실수 관찰 | 실행 당시 최종 소스 해시와 재현 드라이버 포함 |
| [native-unsound-lure.json](native-unsound-lure.json) | 정확한 방어에는 손해인 미끼와 탐나는 포획 이후의 응징 | 검색·증명 진단, 실제 수 선택 정책과 구분 |
| [native-selector.json](native-selector.json) | 실제 HumanGame이 O-O 대신 Nxe5 퀸 미끼를 선택 | 실시간 빈도 조절을 추가하기 **전**의 v2 중간 정책 실행 기록 |

중간 정책의 실제 선택에서는 같은 깊이 6의 기준 +51cp와 미끼 −228cp를 비교했다. 손실 279cp를 감수했으며, `...Bxd1 Bxf7+ Ke7 Nd5#`를 독립 검사로 확인했다. 현재의 보수적 초기 빈도 정책에서도 매번 이 수를 선택한다는 뜻은 아니다. 이 사례의 응징은 두 번의 자기 착수이며, 3~4수짜리 미끼를 발견했다는 증거는 아니다.

최종 v3 진단에서도 미끼가 허용되는 시드에서는 같은 `Nxe5`와 손실 279cp를 확인했고, 미끼를 쉬는 시드에서는 `O-O`를 유지했다. 시드는 정책 추첨을 열거나 닫도록 고른 것이며, 후보 수와 전술 증명은 강제하지 않았다. 첫 수는 기준 `Nc3`보다 5cp 낮은 `d4`를 안전한 갬빗 도입수로 골랐다. 사람의 `...Qc4` 실수는 같은 직전 국면·깊이 4에서 912cp 손실로 관측했고, 시도 빈도가 10%에서 18.5%로 올라갔다. 이 수치는 보스의 정책 빈도이며 사람의 실수 확률이 아니다.

지연 응징 구도는 `Bc4 bxc4 Nf7+ Kg8 Nh6+ Kh8 Qg8+ Rxg8 Nf7#`를 포함한다. 독립 검사는 모든 방어를 12노드로 확인했고, 검증된 전략에서 최소 3번·최대 4번의 자기 착수로 메이트한다. 원래 메이트 공격망이 있던 합성 구도이므로, 미끼가 승리를 새로 만들었다거나 실전에서 이 함정을 자동 생성했다는 주장은 하지 않는다.

## 정책·상태·화면 검사

- 보스 관련 Python 검사 61개 통과: 레퍼토리 10, 전술 증명 10, 상대 관찰 7, 선택 정책·대국 상태 28, 보관함 6. [핵심 정책 출력](policy-tests.txt), [도우미 출력](helper-tests.json), [보관함·기존 회귀 출력](regression-tests.json).
- 일반 대국, 형세 표시, 생각 시간, 기존 기풍, 오프닝 안내, 핸디캡과 보관함 회귀 검사 통과. 실제 엔진 옵션 검사는 별도 네이티브 진단으로 수행했고, 로그에 표시된 선택적 네이티브·Windows 심볼릭 링크 검사는 건너뛴 상태로 구분한다. 잘못 지정한 파일명 두 건은 실행 경로를 고친 이력으로 별도 기록했으며 테스트 실패로 숨기거나 통과 수에 포함하지 않았다.
- 웹 보스 UI 검사 통과, [Godot UI 검사 20개 통과](godot-tests.txt). [웹 화면](web-boss.png)과 [Godot 화면](godot-boss.png)은 격리된 화면용 국면을 렌더링해 확인했다. 실제 사용자의 대국이나 사람이 직접 끝까지 플레이한 결과가 아니다.
- 기존 안내 자료의 불변 배열이 HTTP 전후에 서로 다른 형식으로 보이던 문제를 응답 경계에서 수정했다. 원본 카탈로그는 불변으로 유지하며 JSON 왕복·호출자 변경 격리 검사로 확인했다.

## 재현

저장소의 테스트용 Python 환경과 활성 런타임 엔진을 사용한다. 기존 결과 파일을 덮지 않도록 새 출력 경로를 지정한다.

```powershell
.venv-tests\Scripts\python.exe tests/boss_trap_integration.py --output build/boss-traps-repeat.json
.venv-tests\Scripts\python.exe tests/poisoned_piece_integration.py --output build/poisoned-material-repeat.json
.venv-tests\Scripts\python.exe tests/poisoned_piece_delayed_integration.py --output build/poisoned-delayed-repeat.json
.venv-tests\Scripts\python.exe tests/boss_selector_integration.py --output build/boss-selector-repeat.json
```

무작위 대국 시드와 시간 제한 검색은 실행마다 후보·깊이·시간이 달라질 수 있다. 수순·옵션·바이너리 해시가 같은지 확인하고, 테스트 통과와 경기력을 구분한다.

## 아직 주장하지 않는 것

사람이 미끼를 먹을 실제 확률, Chess.com·Lichess Elo, 승률 개선, 일반 모드보다 높은 경기력은 측정하지 않았다. 상대 관찰의 수치와 빈도 변화 검사는 구현 동작의 검증이며 인간 실력 추정의 보정 정확도를 증명하지 않는다. 최선 방어를 허용하면 의도적으로 불리해질 수 있다는 것이 보스 정책의 일부다.
