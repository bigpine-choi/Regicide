# Opening coaching: source and quality review

Checked 2026-11-02 against 19 official Chess.com opening pages. The complete URLs, short original paraphrases, 20 family profiles and eight reproducible transposition pairs are in [source-verification.json](source-verification.json). This is a research artifact, not a record of engine strength or a completed UI test.

## Presentation recommendation

Explain what White aimed for, how Black responded, and what the resulting position offers each side. These are two roles in one shared opening position. They are not separate opening teams with a fixed winner. Use concise English plans that say what to watch next, and keep a typical plan distinct from a verified event on the board.

Use the broadest label justified by the actual moves. If a later named position is reached through a different move order, that position should determine its relevant plan. An early label can remain useful historical context, but should not lock the rest of the game into the original plan. The [official Reti page](https://www.chess.com/openings/Reti-Opening) explicitly discusses this flexibility, including transposition to the Sicilian.

For family matching, prefer specific supported subclasses before broad parent labels: Open/Closed treatment within Sicilian; Italian/Ruy/Scotch/Petrov before general e4-e5; Accepted/Declined/Slav before general Queen's Gambit. Match complete family labels or explicit aliases, not any appearance of a keyword inside a variation name. For example, Sicilian's French Variation is not automatically the French Defense.

## Important factual boundaries

- The [King's Indian](https://www.chess.com/openings/Kings-Indian-Defense) starting label shares an early position with other Indian systems. Verify later central moves before describing a closed-center attack. The [Grunfeld page](https://www.chess.com/openings/Grunfeld-Defense) identifies the distinguishing central d5 strike.
- Plans involving an exchanged bishop, doubled pawns, an open file or a pawn wedge require that feature in the actual position. Names alone establish none of these facts.
- A family's typical pawn break is a candidate idea. It is not a promise that the move is legal or best now. The existing own-engine result remains the concrete move evidence.
- A historical opening label is not invalid just because the current position has changed, but current-tense claims must be checked against the selected board.
- The [Queen's Gambit parent page](https://www.chess.com/openings/Queens-Gambit) has a prose typo in its Accepted paragraph. The [dedicated Accepted page](https://www.chess.com/openings/Queens-Gambit-Accepted) confirms dxc4. The research record uses the legal sequence.
- The [e4-e5 page](https://www.chess.com/openings/Kings-Pawn-Opening-1...e5) names the position King's Pawn Opening and contains no strategic prose. Open Game may be used as a descriptive grouping; it should not be presented as the exact title of that page.

## Assessing the opening phase

Do not infer which side had a better start from the family names, nor use article win percentages as an individual-game prediction. The articles do not provide a consistently controlled population, time control, rating mix or observation period for such a comparison.

For the feature under development, report an explicitly identified actual-game position, its own-engine evaluation and the limitations of that search. The initial implementation used depth at least 8, from an actual position at least 12 plies into the standard-start game, with opening evidence limited to the first 24 plies. The depth policy was subsequently revised as described below. None of these thresholds comes from Chess.com or proves objective chess truth. A small positive score need not mean White outplayed Black; first-move initiative is part of the position.

Keep these three questions separate: what plans the opening suggests; which side the current search prefers at the selected opening checkpoint; who eventually won the game. A later blunder can reverse the result. Shallow, bounded, missing, malformed or mismatched evaluations should yield an honest unavailable/uncertain explanation, not a guessed winner.

## Checks performed

All 20 canonical example lines replayed legally with python-chess 1.11.2. All eight move-order pairs reached identical positions when comparing piece placement, side to move, castling rights and legal en-passant availability. Move clocks were intentionally excluded. This validates the examples, not the implementation's classifier.

No engine search, external engine, user archive, live game, browser UI or server was used by this research audit. The separate targeted regression plan in the JSON is a proposed module review checklist; it is not claimed to have run yet.

Seventeen further legal move sequences are prepared with FEN, complete UCI history and actual catalog output for the forthcoming implementation audit. A useful boundary appears in the London-to-c4 example: the latest named catalog match remains the early Accelerated London at ply 3, although the selected ply-12 position has a pawn on c4 rather than c3. Historical naming must not be mistaken for the current structure. Another example reaches a catalogued Bishop's Opening through an initial Petrov move order; a hand-written classifier should not overwrite that result just because it saw an earlier prefix.

## Initial implementation audit

The newly written module was then exercised on the 17 prepared cases with 11 synthetic score values per case. Together with input immutability, malformed/bounded/shallow score guards, mismatched position checks and terminal checkmate handling, 222 checks passed. These are deterministic data-contract checks, not real engine searches or evidence of evaluation accuracy. The tested source hash and detailed scope are recorded under `implementation_audit_initial` in the JSON.

Four presentation findings were sent to the implementation owner: the catalog's Rapport-Jobava alias; QGD source coverage for the c5 plan; current-tense summaries for historical pieces that have already been exchanged; and unknown-family fallbacks putting a Black defense name in the White label. At this initial checkpoint their fixes are not yet independently rechecked. This is not a final approval of the evolving module.

## Revised display policy

The parent integration run reported completed depth-5/6 searches at a 500 ms budget. This research agent did not run that engine experiment. On that usability evidence, the team selected depth 4 as a minimum display policy for a provisional opening-position assessment. The same actual-history, matching-FEN, legal-PV, exact-score, known-classification and minimum-12-ply checks remain. This does not establish depth 4 as accurate enough in general, nor depth 8 as a reliability guarantee. All accepted numeric verdicts remain provisional at every depth; stronger wording is not unlocked at depth 8. The fifty- and hundred-centipawn categories are presentation ranges, not measured confidence intervals or playing-strength claims.

The original audit result above remains tied to its recorded earlier source hash and depth-8 policy. The separate final audit entry identifies the tested revision and revised depth-4 boundary.

## Final targeted audit

The frozen `coach_openings.py` SHA-256 is `53eb611bfc1816e690f027f679eb3b7bd7a585b79675f3c2c17d3ffdab3459b3`. The same 17 cases and 222 data-boundary checks passed with exact accepted scores at depth 4 and depth 3 withheld. Accepted nonterminal verdicts remain explicitly provisional. Nine focused factual checks also passed: the original four presentation fixes, the Alapin pawn-recapture distinction, a rebuilt French pawn chain, both d2 and d3 Closed Sicilian setups, and the actual French Advance chain.

The newly added structure classifier initially treated an Alapin c-pawn recapture as Open Sicilian. This was reported and corrected by the owner before the final audit. The final code leaves that Alapin case outside the Open label. Both reproduced legal examples and the two executable audit snippets are stored under `implementation_audit_final` in the JSON; the earlier snapshot remains preserved separately.

No clear factual or data-contract blocker remains in this targeted sample. This conclusion does not certify all possible openings, evaluate engine strength, or replace the parent's UI and actual-engine tests.
