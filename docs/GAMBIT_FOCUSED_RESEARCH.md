# 취약한 갬빗 위치를 새 연구 시작점으로 준비하기

`prepare_gambit_research.py`는 기존 연구 기록이나 기보에서 고른 위치를 **새 커리큘럼으로 준비**한다. 엔진 검색·대국·기억 설치를 실행하지 않는다. 기존 연구 디렉터리와 캐시는 보존하며, 더 높은 노드 수의 연구는 별도 출력 디렉터리에서 새 revision으로 시작한다.

## 기존 연구에서 고르기

완료한 연구의 `run.json`, `records.json`, `practice.json`, `queries.json`이 필요하다. 전체 이력과 실제 연습 착수의 일치, 완료 상태와 엔진 신원을 검사한다. 각 저장 후보의 점수 부호·완료 깊이·노드·PV가 원래 제한 없는 자식 검색과 일치하는지도 확인한다. 별도 상대 응수의 제한 검색은 후보 점수의 근거로 사용할 수 없다. 일시정지 중인 연구는 먼저 기존 명령의 `--resume`으로 끝낸다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/prepare_gambit_research.py `
  --research build/my-research --record "kings-case:3" `
  --output build/kings-focus-preparation
```

`--case`와 `--record`는 반복할 수 있다. 둘을 함께 주면 어느 쪽이든 일치하는 기록이 범위에 포함된다. 별도 점수 규칙이 없으면 그 범위 전체를 고른다. 다음 규칙을 함께 지정하면 그 범위에서 조건을 충족한 위치만 고른다. ID 없이 규칙만 지정할 수도 있다.

- `--gap-cp 25`: 저장 후보 중 점수가 가장 높은 두 수가 **같은 완료 깊이**이고 차이가 25cp 이하인 위치. 후보가 하나뿐이거나 깊이가 다르면 이 규칙으로 고르지 않는다.
- `--comparison build/another-research --swing-cp 100`: 같은 엔진 SHA·평가기·실제 옵션, 정확히 같은 FEN과 전체 이력에서 공통 후보의 저장 점수가 100cp 이상 달라진 위치. 서로 다른 깊이의 변화는 각 깊이와 함께 기록한다.
- 두 규칙을 함께 쓰면 어느 하나라도 충족한 위치를 고른다.

작은 점수 차이는 선택이 어렵다는 가설이고, 점수 변화는 재검토할 신호다. 둘 다 오류·정답·승리의 확정 판정이 아니다. 더 깊은 자체 점수가 반드시 더 정확하다는 가정도 하지 않는다.

## 사용자가 기보의 위치를 지정하기

```powershell
.\.venv-tests\Scripts\python.exe scripts/prepare_gambit_research.py `
  --pgn path/to/game.pgn --ply 12 --ply 16 `
  --output build/pgn-focus-preparation
```

`--ply`는 처음부터 둔 반수의 수다. `--ply 12`는 12번째 반수 **직후**, 다음 진영이 둘 위치를 뜻한다. 기본적으로 첫 기보를 읽으며, 여러 기보 파일에서는 `--game 2`처럼 순서를 고른다. 사용자가 지정한 위치를 입력할 뿐 자동으로 실수를 판정하지 않는다. 표준 초기판부터의 합법 이력, 실제 희생 수락 근거와 갬빗 진영 차례가 필요하다. 개인 기보의 전체 이력이 출력되므로 결과를 공개할 때에는 기존 개인 기보 보존 경계를 따른다.

현재 실전 운영 창인 희생 수락 후 48반수 밖의 위치도 오프라인 연구용으로 준비할 수 있다. 보고서의 `runtime_context`에 비활성 이유가 남는다. 준비됐다는 사실이 실전 기억 활용 범위 확대를 뜻하지 않는다. 반복·자동 종국 등 기존 기억 상태 검증을 통과하지 못하는 위치는 제외한다.

## 출력과 다음 단계

새 디렉터리에 `curriculum.json`과 `focus-report.json`을 만든다. 보고서에는 입력 SHA256, 선택 규칙·제외 이유와 `focus-...` ID가 있다. 기본 커리큘럼의 모든 보류 계열 case는 내용 그대로 보존한다. 입력·비교 연구에서 추가로 보류했던 계열과 위치도 합집합으로 보존한다. 추가 보류 계열은 기본 커리큘럼의 해당 계열 case를 보류로 복사하고, 지원하지 않는 계열이면 거절한다. 보류 계열 및 선언된 보류 시작판·즉시 자식과 겹치는 위치는 심화 대상에서 제외한다. 이는 이전에 본 적 없는 전역 holdout을 새로 만든다는 뜻이 아니다.

보고서의 정확한 ID를 확인한 뒤 기존 연구 도구로 별도 revision을 시작한다.

```powershell
.\.venv-tests\Scripts\python.exe scripts/train_gambit_operations.py `
  --binary path/to/regicide.exe `
  --curriculum build/kings-focus-preparation/curriculum.json `
  --case "focus-kings-보고서의ID" --nodes 1000000 `
  --owner-turns 2 --candidates 2 --wall-seconds 120 `
  --output build/kings-deeper-revision
```

예시는 실행되지 않았다. `--nodes`가 달라지면 기존 연구에 `--resume`하지 않는다. 새 revision에서 선택·점수·비용을 비교하며, 활성 기억으로의 병합·설치는 별도 검증 단계다. 선택된 위치가 없으면 보고서의 제외 이유를 확인하고, 보류 case만 남은 커리큘럼을 심화 성공으로 해석하지 않는다.

검증은 `python -m unittest discover -s tests -p gambit_research_focus_tests.py`로 수행한다. 이 검사는 엔진을 실행하지 않는다.
