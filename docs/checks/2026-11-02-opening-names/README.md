# Chess.com 기준으로 확인한 오프닝 표시

Chess.com은 `1.Nf3`를 Réti Opening, `1.Nf3 d5 2.c4`를 Réti Gambit으로 분류한다고 [공식 안내](https://www.chess.com/openings/Reti-Opening)에 명시한다. 기존 Regicide의 Lichess CC0 목록은 각각 Zukertort Opening과 Réti Opening을 사용했다. 이는 분류·명칭 기준의 차이다.

공식 페이지의 제목과 시작 수순을 확인한 Réti 계열 32개, `1.g3`, `1.b3`, `1.f4`의 대표 이름 3개를 포함해 총 35개 표시를 보정했다. `source-verification.json`에 정확한 이름·수순·URL·확인 범위를 기록했다. 원본 TSV·LICENSE·manifest는 변경하지 않았다. `evidence.json`에 검증 당시 해시를 기록했다.

보정은 ECO·원래 이름·전체 수순이 일치하는 항목에만 적용한다. 전체 오프닝 목록을 Chess.com 데이터베이스로 교체한 것은 아니다. Queen's Pawn의 Zukertort Variation, Tennison Gambit, 공식 명칭을 확인하지 못한 변형을 일괄 치환하지 않는다.

## 통합 검증

- `python tests/opening_guide_tests.py`: 26개 통과. 모든 보정 항목·원본 보존·현재/과거 이름·분기·FEN·다른 수순으로 같은 위치에 도달·무르기·다른 오프닝으로의 전환을 검증한다.
- `python tests/opening_guide_integration_tests.py`: 11개 통과. 대국 상태·무르기·기보 보관함·HTTP 연결에서 기존 동작을 확인한다.
- `node tests/opening_guide_web_tests.js`: 8개 통과. 표시·미리보기·상태 갱신·출처·불완전 자료 처리를 확인한다.

모든 검증은 사용자 대국 창을 조작하지 않고 수행했다. 실제 플레이 중인 프로세스에는 새 코드를 강제로 다시 불러오지 않았으므로 다음 게임 프로그램 실행부터 적용된다.
