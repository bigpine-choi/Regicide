# 대국 중 오프닝 안내

작성일: 2026-10-21, 한국 시간.

현재 오프닝과 다음 수에 따라 이어질 수 있는 등록 수순을 Godot·브라우저 대국 화면에서 확인할 수 있다. 게임에 자료를 포함하므로 실행 중 인터넷 연결이 필요하지 않다.

## 사용하는 방법

1. 대국 화면의 **오프닝 안내**에서 **백의 오프닝**, **흑의 대응**, 현재 세부 변형과 ECO 코드를 확인한다. ECO는 대국의 오프닝을 분류하는 코드이며 백·흑 각각의 별도 점수가 아니다.
2. **다음 분기**를 펼쳐 다음 수와 그 뒤에 연결되는 이름을 살펴본다. 상대 차례에도 볼 수 있다.
3. 분기나 이름을 선택해 실제로 이어지는 SAN 수순(수를 적는 표준 표기)을 읽는다. 예를 들어 `...a6` 한 수로 확인되는 이름과, 그 뒤 `Bxc6`까지 둬야 확인되는 변형을 구분한다.
4. 무르기·핸디캡·기보 재생에서는 현재 표시하는 위치에 맞춰 안내가 바뀐다.

분기를 펼치고 수순을 미리 보는 행동은 대국에 착수하지 않는다. 추천 수 기능, 생각 시간과 기보 저장은 기존처럼 사용할 수 있다. 화면 안내는 한국어이며 오프닝·변형은 영어 이름으로 표시한다. 아래에 명시한 일부 이름은 Chess.com에서 확인한 표기를 우선한다.

예를 들어 `1. e4 e5 2. Nf3 Nc6 3. Bb5 a6`에서는 **Ruy Lopez Opening: Morphy Defense · C70**으로 표시된다. 다음 분기에서 **Bird's Defense Deferred**를 선택하면 `4. Ba4 Nd4`를 볼 수 있다. 백 비숍을 a4로 옮기고 흑 나이트가 d4로 옮겨야 그 이름의 위치에 도달한다는 뜻이다.

## 어떤 의미의 이름인가

- **현재 이름:** 지금 배치가 자료의 이름 있는 위치와 일치한다. 같은 배치에 여러 이름이 등록됐으면 함께 표시한다.
- **다른 순서로 도달:** 수를 둔 순서는 달라도 기물·둘 차례·캐슬링·유효 앙파상 조건이 같은 위치로 이어졌다.
- **마지막 확인:** 현재 위치에 별도 이름이 없으면, 이전 수순에서 확인된 이름을 이력으로 남긴다. 자료에 이어지는 중간 위치인지, 등록 수순 밖인지 설명을 함께 표시한다.
- **사용자 지정 배치:** FEN 등에서 시작했다면 그 이전 착수는 알 수 없다. 현재 위치의 분류와 이전 수순의 확인을 구분한다.

다음 수로 바로 확인되는 이름과 여러 번 더 착수해야 도달하는 이름을 구분하며, 남은 수순은 현재 위치의 수 번호로 표시한다. 이름까지의 착수 수는 백·흑 각각의 한 번 이동을 한 번으로 센다.

## 백의 오프닝과 흑의 대응 (2026-11-02)

백·흑 두 행은 하나의 오프닝 안에서 각자 선택한 구상을 설명한다. 항상 서로 독립된 공식 오프닝 이름 두 개가 존재한다는 의미는 아니다. 아래의 전체 변형명과 ECO는 계속 함께 표시한다.

| 실제로 둔 수순 | 백의 오프닝 | 흑의 대응 |
| --- | --- | --- |
| `1.e4 c5` | King's Pawn Opening | Sicilian Defense |
| `1.e4 c5 2.c3` | Alapin Variation (Sicilian) | Sicilian Defense |
| `1.e4 e5 2.Nf3 Nc6 3.Bb5 a6` | Ruy Lopez Opening | Morphy Defense |
| `1.d4 d5 2.c4 e6` | Queen's Gambit | Queen's Gambit Declined |
| `1.d4 Nf6 2.c4 g6` | Queen's Pawn Opening | King's Indian Defense |

확인된 계열·변형만 역할에 연결한다. 예를 들어 Sicilian의 French Variation을 독립적인 French Defense로 바꾸거나, Réti의 `…g6`만 보고 King's Indian으로 부르지 않는다. 아직 별도의 방어 이름을 확인하지 못했다면 실제로 둔 흑의 대응과 미확정 상태를 표시한다. `1.Nf3` 뒤 `…c5 2.e4`로 전환되면 현재의 Sicilian 분류를 따른다.

사용자 지정 FEN의 이름에는 **현재 배치**, 자료 밖으로 나간 뒤 유지하는 이름에는 **마지막 확인**을 표시한다. 과거 착수 이력이 없는 FEN에서 실제 첫 수를 추측하지 않는다. 무르기와 기보 재생도 지금 표시하는 위치로 두 행을 다시 계산한다. 긴 이름은 줄임표와 툴팁으로 표시하며 두 행의 높이를 고정해 보드 크기가 이름 길이에 따라 바뀌지 않게 한다.

공식 Chess.com에서 확인한 `1.d4 d6 2.c4 g6`의 Modern Defense는 원자료에 이름 있는 끝 위치가 없으므로 **표시 전용 수순 1개**로 보완한다. 확인되지 않은 ECO 코드는 붙이지 않는다. 현재 이름·다음 수·이후 도달할 이름은 같은 표시 색인을 사용한다. 자체 엔진의 오프닝 후보를 만드는 원래 색인은 유지한다.

역할 분리·명칭 출처·무르기·화면 검증은 [양쪽 오프닝 표시 검증](checks/2026-11-02-opening-sides/README.md)에 기록한다.

## 자료의 범위

자료는 Lichess의 공개 오프닝 이름·대표 수순 목록이다. 고정한 버전에는 **3,865개 등록 행과 7,983개 위치**가 있다. 같은 이름의 여러 수순도 있을 수 있으므로 이를 전 세계 오프닝의 총수로 해석하지 않는다.

유한한 이름 목록으로 모든 체스 이론·가능한 변형을 담지는 않는다. 목록 밖으로 나간 것은 악수 판정이 아니다. 표시 순서는 SAN 표기순이며 기보 빈도·승률·엔진 추천 순위가 아니다. 한 분기의 이름과 대표 수순도 화면에서 일부만 표시할 수 있으며 생략 여부를 알린다.

대국 중 Regicide의 수 선택은 계속 자체 엔진이 담당한다. 이 자료로 엔진이 대신 수를 고르거나 학습 모델을 바꾸는 기능은 추가하지 않았다.

## Chess.com 표기를 우선하는 명칭 정책 (2026-11-02)

확인된 명칭은 Chess.com 표기를 우선한다. 9개 계열의 기본 이름을 공식 페이지에 맞췄다. King's Pawn Game → King's Pawn Opening, Queen's Pawn Game → Queen's Pawn Opening, Ruy Lopez → Ruy Lopez Opening, Alekhine Defense → Alekhine's Defense, Owen Defense → Owen's Defense, Bird Opening → Bird's Opening, Nimzo-Larsen Attack → Nimzowitsch-Larsen Attack, Hungarian Opening → King's Fianchetto Opening, Indian Defense → Indian Game이다.

계열명 보정은 이름 전체 또는 콜론 앞의 정확한 계열명에만 적용한다. 같은 단어가 다른 변형명 안에 나왔다고 바꾸지 않는다. 이때 세부 변형 접미사는 확인한 경우에만 별도로 바꾸며, 계열명이 맞다는 사실을 세부 변형 전체의 검증으로 표시하지 않는다. `name_verification`의 `family_only`와 `exact_line`, 각 출처 URL을 구분한다. 대국 화면·분기·현재 배치·복기 이름과 코칭의 공식 이름 표시에 같은 명칭 함수를 사용한다. [이번 용어 대조 기록](checks/2026-11-02-opening-sides/terminology-verification.json)에 공식 페이지와 제외 범위를 남겼다.

원자료는 `1.Nf3`를 **Zukertort Opening**, `1.Nf3 d5 2.c4`를 **Réti Opening**으로 분류한다. 이는 수의 합법성 문제가 아니라 이름을 붙이는 범위의 차이다. Chess.com의 [공식 Réti 안내](https://www.chess.com/openings/Reti-Opening)는 `1.Nf3`를 **Réti Opening**, `1.Nf3 d5 2.c4`를 [**Réti Opening: Réti Gambit**](https://www.chess.com/openings/Reti-Opening-Reti-Gambit)으로 구분한다고 명시한다.

사용자가 보는 이름을 이에 맞추기 위해 `scripts/opening_names.py`에 **정확한 ECO·원래 이름·전체 수순을 키로 하는 38개 표시 보정**을 두었다. Réti 계열 34개, 추가로 확인한 첫 수 이름 3개, King's Indian 시작 수순 1개이며, 각 항목은 공식 오프닝 페이지의 제목과 시작 수순 또는 같은 위치로의 전환을 확인하고 해당 URL을 기록했다. `Zukertort` 등의 단어를 전역 치환하지 않는다.

- 기본 Réti 이름 4개 수순: `1.Nf3`, `1.Nf3 Nf6`, `1.Nf3 d5`, `1.Nf3 Nf6 2.Nc3 Nc6`.
- 확인된 직계 변형 22개: Arctic/Drunken Knight, Basman, Black Mustang, Dutch/Lisitsyn/Deferred Lisitsyn, Herrstrom, Kingside Fianchetto, Pirc, Polish, Queen's Gambit Invitation, Queenside Fianchetto, Sicilian, Slav, St. George, Ware, Nimzo-Larsen, Quiet System, Old Indian, Reversed Mexican, The Potato. `Kingside Fianchetto Variation`과 `The Potato Variation` 등 세부 표기도 확인된 제목에 맞췄다.
- Réti Gambit 계열 6개: 기본 Gambit, Accepted, Declined Advance, Michel, Penguin, Reversed Blumenfeld.
- 추가로 확인한 Réti 변형 2개: [Tennison Gambit](https://www.chess.com/openings/Reti-Opening-Tennison-Gambit)과 [Santasiere's Folly](https://www.chess.com/openings/Reti-Opening-Santasieres-Folly). 공식 페이지의 수순과 원자료의 다른 착수 순서가 같은 위치에 도달하는지도 확인했다. 독립적으로 확인하지 않은 후속 변형까지 연쇄 치환하지 않는다.
- 첫 수 이름 3개: `1.g3`는 [King's Fianchetto Opening](https://www.chess.com/openings/Kings-Fianchetto-Opening), `1.b3`는 [Nimzowitsch-Larsen Attack](https://www.chess.com/openings/Nimzowitsch-Larsen-Attack), `1.f4`는 [Bird's Opening](https://www.chess.com/openings/Birds-Opening). 이 이름의 세부 변형을 연쇄 치환하지 않으며, 확인 결과 같았던 Grob Opening도 그대로 둔다.
- King's Indian 시작 수순 1개: `1.d4 Nf6 2.c4 g6`의 원자료 이름 `Indian Defense: West Indian Defense · E60`을 공식 [King's Indian Defense](https://www.chess.com/openings/Kings-Indian-Defense) 표기로 보정한다. 이후 `…d5`로 Grünfeld의 이름 있는 위치에 도달하면 새 분류로 바뀐다.

현재 이름·마지막 확인 이름·다음 한 수의 이름·몇 수 뒤 도달할 이름은 같은 보정된 색인을 사용한다. FEN에서 직접 시작하거나 다른 수순으로 같은 위치에 도달해도 같은 이름이 나오며, 무르기는 이전 위치의 이름으로 돌아간다. 이후 Ruy Lopez나 Queen's Pawn 등 다른 이름의 위치로 전환되면 그 위치의 분류를 따른다.

원본 TSV·LICENSE·manifest와 그 SHA256은 변경하지 않는다. CC0 자료의 출처는 계속 Lichess이며, 화면 출처에는 **일부 명칭 Chess.com 기준**임을 덧붙인다. 3,865개 전체 세부 변형이 Chess.com과 일치한다는 의미는 아니다. 확인하지 못한 Lemberger Gambit, Tennison의 후속 Briggs Trap 등은 원자료 이름을 유지한다. Queen's Pawn의 독립적인 Zukertort Variation도 Réti로 바꾸지 않는다. 기존 35개 보정의 근거는 [이전 명칭 검증 기록](checks/2026-11-02-opening-names/source-verification.json), 이번 추가·확대 범위는 [양쪽 표시 검증 기록](checks/2026-11-02-opening-sides/README.md)에 남긴다.

## 출처와 재현

- 원자료: [Lichess chess-openings](https://github.com/lichess-org/chess-openings).
- 고정 버전: [`a6189a30dc273ccb21fc2536a9a2fefd5592a67a`](https://github.com/lichess-org/chess-openings/tree/a6189a30dc273ccb21fc2536a9a2fefd5592a67a).
- 이용 조건: [CC0 1.0 원문](https://github.com/lichess-org/chess-openings/blob/a6189a30dc273ccb21fc2536a9a2fefd5592a67a/COPYING.txt).
- 배포 파일: `data/openings/`의 원본 TSV5개·LICENSE·manifest. manifest는 출처·버전·원본 바이트 수·SHA256을 기록한다.
- 명칭·수순은 외부 자료이며 위치 색인·변형 연결·게임/재생 연동·화면 표시는 Regicide에서 구현했다.

대국 프로그램을 다른 폴더에 옮길 때에는 `data/openings/`도 함께 포함한다. 자료가 없거나 손상되면 안내를 사용할 수 없다고 표시하고 대국은 계속할 수 있다.

다시 검증하는 기본 명령은 다음과 같다.

```powershell
.\.venv-tests\Scripts\python.exe tests/opening_guide_tests.py
.\.venv-tests\Scripts\python.exe tests/opening_guide_integration_tests.py
node tests/opening_guide_web_tests.js
.\.venv-tests\Scripts\python.exe tests/opening_guide_godot_tests.py --godot C:\path\to\godot.exe --binary build/regicide.exe
```

Godot 장면과 실제 엔진 연결 검사, 성능 표본·표시 예·원본 유지 확인은 [검증 기록](checks/2026-10-21-opening-guide/)에 남긴다. 자동 장면·합성 입력 검사와 사람이 직접 조작한 검증은 구분한다.

실제 게임 창에서의 클릭·드래그·분기 미리보기·기보 재생·특수 규칙 확인과 발견한 표시 오류 수정은 [실제 화면 검증 기록](checks/2026-10-21-opening-guide-manual/README.md)에 남긴다. 이후 파일 선택창을 게임 안의 모달 창으로 바꾸고, PGN 실제 저장 → 다시 열기 → 복기 → 재저장까지 직접 확인했다. [PGN 후속 검증](checks/2026-10-21-pgn-roundtrip/README.md)을 참고한다.
