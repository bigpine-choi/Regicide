# Archived 1-second SEE losses: descriptive replay

No engine was launched. Scores are the players' own reports; the word suspicious does not mean a verified blunder.

Initial manual prose incorrectly called game 10 ply 77 Nxe5 a quiet Ne5 and the predicted Qb8 a check. These descriptions are corrected; generated SAN/quiet/check fields were always derived from python-chess and remain authoritative.

Replayed 16 games, 1697 game plies, and 10744 PV plies legally.

| Game | Color | Main-search minimum | Mean depth | Mean selective depth | Hypothesis |
|---|---|---:|---:|---:|---|
| 1 | white | 5 | 8.01 | 23.26 | Long minor-piece/pawn ending; king activity and passed-pawn race, not an isolated exchange error. |
| 2 | black | 4 | 5.62 | 26.74 | Early ...c5 and a later exposed-king attack; quiet follow-up Qe6/+f7 is outside a same-square exchange model. |
| 4 | black | 5 | 6.45 | 22.95 | Rook sacrifice/capture greed followed by a mating net; own PV reaches the dangerous king walk but misses its final consequence. |
| 10 | black | 4 | 5.76 | 24.06 | Queen/rook/knight coordination; after ...Ra2 the actual reply Nxe5 captures a bishop, whereas its PV predicted Qb8. |
| 11 | white | 2 | 5.12 | 28.72 | Very shallow completed main search despite large extension depth; quiet ...f3 attack after Qg3+ causes a large score reversal. |
| 12 | black | 4 | 5.64 | 23.08 | ...Nb6 permits quiet Bg5, attacking the queen; the predicted reply was Rf4 instead. |
| 16 | black | 5 | 7.45 | 19.84 | Rook ending and remote passed c-pawn; candidate sees the promotion threat too late despite depth 8-9. |

## Selected development positions

All positions include full actual move history in development-positions.json. No best move is asserted.

### Game 1: SEE replication paired bishops and c-pawn support

Long minor-piece/pawn ending; king activity and passed-pawn race, not an isolated exchange error.

- Ply 123, 62.f4: cp -143, depth 9, seldepth 21, nodes 746067; next own cp -143; material before/after 8 actual plies 0/-100.
  - FEN: `8/4k3/p3p3/P1p1p2n/2P1P3/2PR1P1p/6r1/5N1K w - - 7 62`
  - Published PV: f4 Rf2 Kg1 Rg2+ Kh1 Rf2 Kg1 Nxf4 Kxf2 Nxd3+ Ke3
  - Actual reply: Rf2 (quiet=True, check=False); predicted reply matches=True.
- Ply 153, 77.Ke2: cp -117, depth 10, seldepth 18, nodes 776669; next own cp -226; material before/after 8 actual plies -100/-100.
  - FEN: `8/3k4/p1n1p3/P1p1p3/2P1P3/5N2/5K2/8 w - - 16 77`
  - Published PV: Ke2 Kd6 Ng5 Ke7 Nf3 Kf6 Nh2 Nd4+ Ke3 Ke7
  - Actual reply: Kd6 (quiet=True, check=False); predicted reply matches=True.

### Game 2: SEE replication paired bishops and c-pawn support

Early ...c5 and a later exposed-king attack; quiet follow-up Qe6/+f7 is outside a same-square exchange model.

- Ply 22, 11...c5: cp -10, depth 4, seldepth 35, nodes 404725; next own cp -103; material before/after 8 actual plies 0/0.
  - FEN: `r2q1rk1/1pp1nppp/1b1pbn2/pP2p3/2B1P3/B1PP1N2/P2N1PPP/1R1Q1RK1 b - - 2 11`
  - Published PV: c5 bxc6 bxc6 Nxe5 dxe5 Bxe7 Bxf2+ Kh1 Qxe7 Rxf2
  - Actual reply: bxc6 (quiet=False, check=False); predicted reply matches=True.
- Ply 70, 35...Kh8: cp -274, depth 5, seldepth 24, nodes 493256; next own cp -495; material before/after 8 actual plies -220/-490.
  - FEN: `6k1/2q4p/1b3Pp1/r1p1p3/p1PpN1QP/B2P4/P7/3R3K b - - 0 35`
  - Published PV: Kh8 Qe6 Ra8 f7 Kg7
  - Actual reply: Qe6 (quiet=True, check=False); predicted reply matches=True.

### Game 4: SEE replication queen pawn chain and king shelter

Rook sacrifice/capture greed followed by a mating net; own PV reaches the dangerous king walk but misses its final consequence.

- Ply 88, 44...Rxa4+: cp 56, depth 7, seldepth 19, nodes 532724; next own cp -35; material before/after 8 actual plies 0/100.
  - FEN: `8/p4p2/1p5k/3P3q/P2K1P2/r7/R5Q1/8 b - - 12 44`
  - Published PV: Rxa4+ Rxa4 Qd1+ Ke5 Qxa4 Qg5+ Kh7
  - Actual reply: Rxa4 (quiet=False, check=False); predicted reply matches=True.
- Ply 90, 45...Qd1+: cp -35, depth 8, seldepth 19, nodes 530226; next own cp -35; material before/after 8 actual plies -400/0.
  - FEN: `8/p4p2/1p5k/3P3q/R2K1P2/8/6Q1/8 b - - 0 45`
  - Published PV: Qd1+ Ke5 Qxa4 Qg5+ Kh7 Kf6 Qd4+ Kxf7
  - Actual reply: Ke5 (quiet=True, check=False); predicted reply matches=True.
- Ply 92, 46...Qxa4: cp -35, depth 6, seldepth 20, nodes 549372; next own cp None; material before/after 8 actual plies -400/-800.
  - FEN: `8/p4p2/1p5k/3PK3/R4P2/8/6Q1/3q4 b - - 2 46`
  - Published PV: Qxa4 Qg5+ Kh7 Kf6 Qd4+ Kxf7
  - Actual reply: Qg5+ (quiet=True, check=True); predicted reply matches=True.

### Game 10: SEE replication open d-file and queenside pawn step

Queen/rook/knight coordination; after ...Ra2 the actual reply Nxe5 captures a bishop, whereas its PV predicted Qb8.

- Ply 68, 34...Qg8: cp -27, depth 6, seldepth 24, nodes 484606; next own cp -96; material before/after 8 actual plies 0/-100.
  - FEN: `1r5k/5Q2/1p1p3p/1P1Nb1qP/2P2pB1/5P1K/4n3/3R4 b - - 3 34`
  - Published PV: Qg8 Qxg8+ Kxg8 Rd2 Ng3 Be6+ Kh8
  - Actual reply: Qc7 (quiet=True, check=False); predicted reply matches=False.
- Ply 76, 38...Ra2: cp -136, depth 4, seldepth 25, nodes 448606; next own cp -584; material before/after 8 actual plies -100/-430.
  - FEN: `r7/5qk1/1Q1p2Np/1P2b2P/2P2pB1/5P1K/4n3/3R4 b - - 0 38`
  - Published PV: Ra2 Qb8 Qg8 Qb7+ Qf7 Qxf7+ Kxf7 Nxe5+ dxe5 Rd7+ Kf6
  - Actual reply: Nxe5 (quiet=False, check=False); predicted reply matches=False.

### Game 11: SEE replication broad center facing castled fianchetto

Very shallow completed main search despite large extension depth; quiet ...f3 attack after Qg3+ causes a large score reversal.

- Ply 39, 20.b3: cp 18, depth 2, seldepth 29, nodes 390771; next own cp -11; material before/after 8 actual plies 10/10.
  - FEN: `2rq1rk1/Q2n2b1/1pp3p1/p2Pnp1p/P1P1P3/2N1B2P/1P2B3/2R2RK1 w - f6 0 20`
  - Published PV: b3 cxd5 cxd5
  - Actual reply: cxd5 (quiet=False, check=False); predicted reply matches=True.
- Ply 41, 21.exd5: cp -11, depth 4, seldepth 28, nodes 414437; next own cp -78; material before/after 8 actual plies -90/10.
  - FEN: `2rq1rk1/Q2n2b1/1p4p1/p2pnp1p/P1P1P3/1PN1B2P/4B3/2R2RK1 w - - 0 21`
  - Published PV: exd5 Qh4 Bxb6 Qg3+ Kh1 Qxh3+ Kg1 Nxb6 Qxb6 Qg3+ Kh1 Nxc4 bxc4 Bxc3 Rxc3 Qxc3 Qxg6+ Qg7 Qxh5
  - Actual reply: f4 (quiet=True, check=False); predicted reply matches=False.
- Ply 45, 23.Kg2: cp -102, depth 4, seldepth 28, nodes 421821; next own cp -865; material before/after 8 actual plies 10/-490.
  - FEN: `2r2rk1/Q2n2b1/1p4p1/p2Pn2p/P1P2p1q/1PN4P/3BB3/2R2RK1 w - - 2 23`
  - Published PV: Kg2 Qg3+ Kh1 Qxh3+ Kg1
  - Actual reply: Qg3+ (quiet=True, check=True); predicted reply matches=True.

### Game 12: SEE replication broad center facing castled fianchetto

...Nb6 permits quiet Bg5, attacking the queen; the predicted reply was Rf4 instead.

- Ply 36, 18...Qh4: cp 12, depth 5, seldepth 31, nodes 262444; next own cp 12; material before/after 8 actual plies -10/-230.
  - FEN: `2rq1rk1/1p1n1pb1/2p3p1/p2Pn2p/P1PQP3/2N1B2P/1P2B3/R4R1K b - - 3 18`
  - Published PV: Qh4 Kg2 c5 Qd2 f5
  - Actual reply: Kg2 (quiet=True, check=False); predicted reply matches=True.
- Ply 40, 20...Nb6: cp -4, depth 4, seldepth 31, nodes 262419; next own cp -218; material before/after 8 actual plies -10/-470.
  - FEN: `2r2rk1/1p1n1pb1/6p1/p1pPn2p/P1P1P2q/2N1B2P/1P1QB1K1/R4R2 b - - 1 20`
  - Published PV: Nb6 Rf4 Qg5+ Rg4
  - Actual reply: Bg5 (quiet=True, check=False); predicted reply matches=False.

### Game 16: SEE replication exchanged center and advanced h-pawn

Rook ending and remote passed c-pawn; candidate sees the promotion threat too late despite depth 8-9.

- Ply 88, 44...f3: cp -93, depth 8, seldepth 21, nodes 405859; next own cp -190; material before/after 8 actual plies 0/-100.
  - FEN: `8/6p1/2P3pk/2R5/5p1P/2K5/6P1/3r4 b - - 4 44`
  - Published PV: f3 gxf3 Rf1 c7 Rxf3+ Kd4 Rf4+ Kd5
  - Actual reply: gxf3 (quiet=False, check=False); predicted reply matches=True.
- Ply 96, 48...Re8+: cp -221, depth 8, seldepth 18, nodes 463123; next own cp -372; material before/after 8 actual plies -100/-100.
  - FEN: `3r4/2P3p1/6pk/2R5/7P/4KP2/8/8 b - - 0 48`
  - Published PV: Re8+ Kd4 Rc8 Kc3 Kh7 Kd4 Kg8 f4
  - Actual reply: Kd4 (quiet=True, check=False); predicted reply matches=True.

## Cost attribution limits

- Own score changes are suspicions, not independent centipawn loss or proof of a blunder.
- Material changes within eight actual plies are descriptive and may be temporary tactical investments.
- Mean nodes/depth/NPS compare different played positions, not a controlled SEE cost benchmark.
- Archived game JSON has no see_calls/qnodes/pvs_researches data; cannot attribute wall time to SEE from it.
- A high seldepth is maximum selective path length and does not quantify where total work went.
- Selected positions are development data; no future match positions may be selected based on these results.
