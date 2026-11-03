# 초반 접전 실제 대국 재검증

이전 세 대국을 보존하고 새 대국을 별도 표본으로 표시한다. 상대의 확률적 선택 때문에 동일 수순이나 승률의 직접 전후 비교로 해석하지 않는다.

| 표본 | 대국 | 색 | ply | 종료 | 20수 도달 | 스파링 직후 접전 | 양측 전체 접전 | 20수 연속 접전 |
|---|---|---|---:|---|---|---|---|---|
| previous | 1700-black | black | 86 | checkmate | 예 | 17/20 | 34/41 | 미충족 |
| previous | 1700-white | white | 33 | checkmate | 아니오 | 15/17 | 28/34 | 미충족 |
| previous | 1700-white-extra | white | 119 | checkmate | 예 | 12/20 | 23/41 | 미충족 |
| new | pair1-1700-black | black | 66 | checkmate | 예 | 20/20 | 40/41 | 미충족 |
| new | pair1-1700-white | white | 63 | checkmate | 예 | 20/20 | 37/41 | 미충족 |
| new | pair2-1700-black | black | 62 | checkmate | 예 | 16/20 | 32/41 | 미충족 |
| new | pair2-1700-white | white | 89 | checkmate | 예 | 16/20 | 33/41 | 미충족 |
| new | pair3-1700-black | black | 100 | checkmate | 예 | 20/20 | 39/41 | 미충족 |
| new | pair3-1700-white | white | 79 | checkmate | 예 | 16/20 | 26/41 | 미충족 |
| new | pair4-1700-black | black | 92 | checkmate | 예 | 20/20 | 37/41 | 미충족 |
| new | pair4-1700-white | white | 89 | checkmate | 예 | 20/20 | 38/41 | 미충족 |

## 실행 소스 구분

- `153780887d2d`: 1700-black, 1700-white, 1700-white-extra. 기록 정책 regicide-sparring-quality-v1. 전체 파일별 해시는 각 metadata.json에 보존했다.
- `6e5f749fac38`: pair1-1700-black, pair1-1700-white, pair2-1700-black, pair2-1700-white. 기록 정책 regicide-sparring-quality-v2. 전체 파일별 해시는 각 metadata.json에 보존했다.
- `61b9d76316bf`: pair3-1700-black, pair3-1700-white. 기록 정책 regicide-sparring-quality-v3. 전체 파일별 해시는 각 metadata.json에 보존했다.
- `bb66a68eb6e0`: pair4-1700-black, pair4-1700-white. 기록 정책 regicide-sparring-quality-v4. 전체 파일별 해시는 각 metadata.json에 보존했다.

## 가장 최근 구현의 대국

소스 `bb66a68eb6e0`의 2판·181ply를 별도로 본다. 이전 중간 구현의 실패 대국도 위 표에 남겼다. 독립 감사는 2/2판 완료했다. 완료한 감사에서 스파링 직후 접전은 40/40이며, 양측 모든 위치의 20수 연속 접전은 0/2판이다. 독립 기물 손실 원시 의심 신호 1건, 심층 미완료 0건이다. 원본 큰 손실 신호에는 안전한 우세 반환과 메이트 거리 소실도 포함되므로 아래 해석과 함께 읽는다.

실제 대체 수 10건의 native 반환검색을 대조했고, 영수증 위치 불일치 0건·출처 누락 0건이었다. 모든 실행은 해당 시점의 소스·실행 파일 해시가 불변인 조건이다.

## 새 표본의 접전 이탈

- pair1-1700-black: 초반 이탈·메이트·종국·미확인 0회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 심층 손실 재검토 ply 20 Kxe7: 351→3cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
- pair1-1700-white: 초반 이탈·메이트·종국·미확인 0회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
- pair2-1700-black: 초반 이탈·메이트·종국·미확인 4회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 14수 Bxe3 (ply 28): 560→367cp, advantage_remains, 실행 중 verified.
  - 15수 dxc4 (ply 30): 545→427cp, advantage_remains, 실행 중 verified.
  - 16수 Bd4 (ply 32): 522→500cp, advantage_remains, 실행 중 verified.
  - 17수 Bf5 (ply 34): 467→165cp, advantage_remains, 실행 중 balance_improved.
  - 심층 손실 재검토 ply 28 Bxe3: 547→354cp, requires_review (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 34 Bf5: 487→172cp, requires_review (원래 신호 material_loss_with_deep_tactical_loss).
  - 심층 손실 재검토 ply 36 h5: 184→-9cp, requires_review (원래 신호 material_loss_with_deep_tactical_loss).
- pair2-1700-white: 초반 이탈·메이트·종국·미확인 4회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 17수 c6 (ply 33): -100→-116cp, disadvantage_remains, 실행 중 verified.
  - 18수 Qa6 (ply 35): -133→-127cp, disadvantage_remains, 실행 중 verified.
  - 19수 c3 (ply 37): -128→-181cp, disadvantage_remains, 실행 중 verified.
  - 20수 a5 (ply 39): -153→-183cp, disadvantage_remains, 실행 중 replaced.
  - 심층 손실 재검토 ply 77 Nxe4: 411→193cp, requires_review (원래 신호 deep_large_positional_or_tactical_loss).
- pair3-1700-black: 초반 이탈·메이트·종국·미확인 0회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 심층 손실 재검토 ply 28 a6: 143→-30cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
- pair3-1700-white: 초반 이탈·메이트·종국·미확인 4회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 15수 Bxb5 (ply 29): 135→139cp, advantage_remains, 실행 중 unverified.
  - 16수 Nxb7 (ply 31): 156→150cp, advantage_remains, 실행 중 verified.
  - 17수 Ba4 (ply 33): 479→317cp, advantage_remains, 실행 중 balance_improved.
  - 18수 Kf1 (ply 35): 292→283cp, advantage_remains, 실행 중 verified.
  - 심층 손실 재검토 ply 21 Rfb1: 183→-5cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 23 Qd2: 211→27cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 25 Nb3: 224→-3cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 37 f4: 375→23cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 39 Bd4: 294→0cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
  - 심층 손실 재검토 ply 65 Rxh4: None→1025cp, winning_mate_lost_with_large_advantage (원래 신호 deep_large_positional_or_tactical_loss).
- pair4-1700-black: 초반 이탈·메이트·종국·미확인 0회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.
  - 심층 손실 재검토 ply 24 Ra7: 250→-31cp, requires_review (원래 신호 material_loss_with_deep_tactical_loss).
  - 심층 손실 재검토 ply 30 Nh7: 268→-43cp, verified_non_material_balance_concession (원래 신호 deep_large_positional_or_tactical_loss).
- pair4-1700-white: 초반 이탈·메이트·종국·미확인 0회; 영수증 위치 불일치 0회; 적용 대체 수 native 반환 근거 누락 0회.

## 안전·해석

±100cp 접전, 승리/패배 메이트, 종국과 미확인은 별도다. 20수 이전 종료는 연속 접전 통과가 아니다. 상대의 실수로 이미 생긴 격차를 실제 스파링 착수 오류로 단정하지 않는다. 기물 노출·다음 4ply 기물 감소와 평가 손실의 결합은 추가 수순 검토가 필요한 신호이며 보상 없는 헌납의 확정 판정이 아니다. 각 대국의 원본 감사와 실행 기록을 함께 보관한다.

각 대국은 실제 HumanGame의 수 선택·시계를 실행한 자동 대국이다. 화면을 수동 조작한 검증, Elo, 보편적 접전 보장 또는 기물 헌납 완전 차단의 증거가 아니다.

## 별도 수순 재검토

pair3-1700-white의 15.Bxb5(ply 29)는 실행 중 엄격 검사에서 기존 Na5의 노출 때문에 거부됐고 원래 unverified 상태를 보존한다. 별도 합법 수순 열거에서 `Bxb5 Qxa5 Bxd7+` 뒤 모든 합법 왕 응수에 `Bxc8`로 룩을 회수하며 상대 재포획 뒤에도 시작 대비 기물 점수 +100cp임을 확인했다. 따라서 해당 국소 신호를 보상 없는 헌납으로 확정하지 않는다.

pair4-1700-black의 12...Ra7(ply 24)에서 원시 기물 감소 신호 1건을 보존했다. 실제 `13.e5 Nxe5 14.Nxe5 Bxe6`의 나이트·두 폰 교환 사이 최저값이 −200cp였고, 흑의 다음 착수까지 순감소는 −100cp였다. 중간에 합법 ...dxe5 회수 기회가 있었으며 독립 형세는 교환 중 흑 +66→+45→+31cp로 유지됐다. 이 수순을 보상 없는 기물 헌납으로 확정하지 않는다. 실제 재료 변화와 평가를 함께 기록한 아래 별도 검토를 참조한다.

- [bxb5-material-review.md](../../checks/2026-11-04-early-balance/diagnostics/bxb5-material-review.md), SHA256 `2ad776c83d4b33cdd72c5077e831e99574a71f65f0c64348b17a0261ef0b670a`
- [bxb5-material-review.json](../../checks/2026-11-04-early-balance/diagnostics/bxb5-material-review.json), SHA256 `6c2c7273e6ebbdc55a3fff0647a9ed144e1d3650ab38d036159f6d4d77326faa`
- [final-native-review.md](../../checks/2026-11-04-early-balance/diagnostics/final-native-review.md), SHA256 `f0449ee01baa90fbb12a57437827f4a56060d12b0091bc78ff0df3eecba8c108`
- [final-native-cross-check.json](../../checks/2026-11-04-early-balance/diagnostics/final-native-cross-check.json), SHA256 `d8d3dd11bdd100f05ecd2d4bb54e4381a9dafd5de1ea4203ad28ee79b4e24e22`
- [ra7-material-review.md](ra7-material-review.md), SHA256 `3292171d372fc5453c3d36e80ed8f916cadb018f016043428e5d1f0728ed54bd`
- [ra7-material-review.json](ra7-material-review.json), SHA256 `3caf35b769a32811b5e062d3619dea68c129aaac8919b609898915410185646a`
