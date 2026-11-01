# Alternatives with useful chess lessons

2026-11-02 · base `fbc65ce` · dialogue version 4.

For a move classified as an inaccuracy, mistake, blunder or missed/allowed mate,
Rook now gives the alternative from **`row.best`**, explains one verified effect
of that move, and adds one reusable chess principle. `row.alternative` belongs to
the separate excellent-move comparison and is never used as the correction.

The selected principle follows the position: immediate mate or mating threat,
answering check, an attacked piece, a defended square, captures/recaptures,
development, promotion, castling, checks or threats. For example, a bishop move
that blocks check teaches check defense before ordinary development advice.
An optional observation about the mistake is retained when the full explanation
fits **40 words**. There are at most three short sentences, usually two.

When no supported motif explains the preference, the coach still names the move
and teaches how to compare the forcing replies, while explicitly saying the reason
needs a closer look. An attack is not a forced win, protection is not guaranteed
safety, and a mating PV is not proof against every defense. The chess evaluator,
classification thresholds and actual move-selection engine are unchanged.

## Verification

- New teaching tests use legal synthetic positions in both colors. Their supplied
  `best`/classification labels are test inputs, not new strength measurements.
- Teaching, existing dialogue, character/retry and coaching suites: 81 tests pass. A legacy
  cached mistake gains the new teaching on read, without an engine call or rewriting
  its original file. Positive/uncertain/pending moves receive no invented correction.
- Godot: 434 checks. Browser: 65 checks. The visible dialogue is narrated including
  its principle. Nested teaching data does not leak a solution during retry; only
  the existing hint or submitted-attempt feedback can be spoken until reveal.
- Own-engine authenticated HTTP: 11 check groups with a disposable game. Original
  game state, analysis state, archive JSON/PGN and cancellation behavior are preserved.
- Read-only review of one existing 38-ply report: all six critical moves receive a
  legal alternative and a principle. Four have a verified concrete move effect;
  two explicitly retain the explanation-uncertainty fallback. Their dialogue is
  17–33 words. No private game rows, FENs or PGNs are committed here.
- The independent synthetic audit uses seed 6213 and 2,000 positions, with three
  perspectives each. Conditions, code hash and limits are in `fuzz-summary.json`.
- The installed American voice generated the longest sampled new explanation
  (33 words, 12.565 seconds of audio) in 5.729 seconds, within the unchanged
  20-second helper deadline. Audio was generated without playing it.

Production UI and speech-helper code need no changes: both clients already render
and narrate the perspective's `dialogue`. This turn's UI changes are regression tests.
No user game was stopped or restarted, and no private game was sent to a service.

## Reproduce

```powershell
.venv-tests/Scripts/python.exe tests/coach_teaching_tests.py
.venv-tests/Scripts/python.exe tests/coach_explanations_tests.py
.venv-tests/Scripts/python.exe tests/character_coach_tests.py
.venv-tests/Scripts/python.exe tests/coaching_tests.py
node tests/web_coaching_tests.js
.venv-tests/Scripts/python.exe tests/coaching_http_tests.py --binary build/regicide.exe --output build/coach-teaching-v4/http
```

Use a copied Godot project and the existing coaching fixture runner for the native
checks. Machine-readable results and source hashes accompany this note.

## Teaching references

Principles were checked against primary teaching material, accessed 2026-11-02:
[Chess.com: opening principles](https://www.chess.com/article/view/the-principles-of-the-opening),
[Chess.com: hanging pieces](https://www.chess.com/terms/hanging-piece-chess), and
[Lichess: practice topics](https://lichess.org/practice).
These are conceptual references. Dialogue is original local rule-based text;
no lesson text, private course data or third-party engine output is imported.
