# 동일 위치의 네 조건과 후속 후보 비교

점수 단위 cp는 폰 1개=100입니다. 모든 비교 점수는 해당 위치에서 원래 둘 차례인 쪽 관점입니다. 각 조건의 대표 수는 3회 중 최빈수이며, 동률이면 UCI 좌표 문자열순으로 정했습니다. 대표 PV는 그 최빈수를 고른 첫 반복입니다. 다른 반복도 원시 JSON에 모두 보존했습니다.

후속 참조는 후보 수를 먼저 둔 뒤 양 평가기로 각각 200,000노드를 탐색한 값입니다. 상대 차례의 cp 부호를 뒤집었습니다. 같은 평가기 안에서 후보를 비교할 수 있지만 정답·원래 루트 점수·기력의 증거는 아닙니다.

## P01

출처: G5 15ply / first_divergence

FEN: `rnbq1rk1/p1p1bpp1/1p2pn1p/3p4/2PP3B/2N1PN2/PP3PPP/R2QKB1R w KQ - 0 8`

정적 평가: 기존 -14cp / NN -15cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Bxf6 (3/3) | +0cp | 3 | 8. Bxf6 gxf6 9. Bd3 |
| 기존 1000ms | Bxf6 (3/3) | +1cp | 5 | 8. Bxf6 gxf6 9. cxd5 exd5 10. Bd3 |
| NN 100ms | cxd5 (2/3) | -18cp | 4 | 8. cxd5 exd5 9. Bb5 Be6 |
| NN 1000ms | Bxf6 (3/3) | +7cp | 5 | 8. Bxf6 gxf6 9. cxd5 exd5 10. Bd3 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| cxd5 | 기존 | -23cp | 8...exd5 9. Bxf6 gxf6 10. Bb5 Bf5 |
| cxd5 | NN | -21cp | 8...exd5 9. Bxf6 gxf6 10. Bb5 Be6 |
| Bxf6 | 기존 | -20cp | 8...gxf6 9. Be2 dxc4 10. Nd2 Nd7 11. Nxc4 |
| Bxf6 | NN | -20cp | 8...gxf6 9. Bd3 dxc4 10. Bxc4 Nd7 |

## P02

출처: G5 111ply / largest_nonmate_drop_pre_at_least_minus200, G5 111ply / before_first_three_own_scores_at_most_minus100

FEN: `8/8/8/3kP3/p4P2/P5P1/2n5/6K1 w - - 2 56`

정적 평가: 기존 -1cp / NN +48cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | g4 (3/3) | -115cp | 8 | 56. g4 Nd4 57. Kf2 Ke4 58. Kg3 Ne2+ 59. Kh4 Nxf4 |
| 기존 1000ms | Kf2 (3/3) | -92cp | 10 | 56. Kf2 Nd4 57. Ke3 Nc2+ 58. Kd3 Nxa3 59. g4 Nb5 60. g5 a3 |
| NN 100ms | Kf1 (3/3) | -98cp | 8 | 56. Kf1 Nxa3 57. Kf2 Nc4 58. Kf1 a3 59. Kg1 Kd4 |
| NN 1000ms | g4 (3/3) | -114cp | 10 | 56. g4 Ke6 57. Kf1 Nxa3 58. Ke2 Kf7 59. Kf1 Kg8 60. Kg1 Nc4 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Kf1 | 기존 | -237cp | 56...Nxa3 57. Ke2 Nc4 58. Kd3 a3 59. e6 Nb2+ 60. Ke3 Kxe6 |
| Kf1 | NN | -202cp | 56...Nxa3 57. Kf2 Nc4 58. Kg2 a3 59. e6 Kxe6 60. Kg1 |
| Kf2 | 기존 | -92cp | 56...Nd4 57. Ke3 Nc2+ 58. Kd3 Nxa3 59. g4 Nb5 60. g5 a3 |
| Kf2 | NN | -116cp | 56...Nxa3 57. g4 Ke4 58. e6 Nc4 59. e7 Nd6 60. f5 a3 |
| g4 | 기존 | -102cp | 56...Nd4 57. Kf2 Ke4 58. e6 Nxe6 59. f5 Nd4 60. f6 Kd5 |
| g4 | NN | -106cp | 56...Nd4 57. Kf1 Ke4 58. e6 Nxe6 59. f5 Nd4 60. Kg1 Ne2+ 61. Kh1 |

## P03

출처: G7 18ply / first_divergence

FEN: `r1bq1rk1/ppp1npbp/3p1np1/3Pp3/2P1P3/2N5/PP2BPPP/R1BQNRK1 b - - 2 9`

정적 평가: 기존 -5cp / NN +4cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Bd7 (3/3) | +3cp | 3 | 9...Bd7 10. Nd3 c5 |
| 기존 1000ms | c6 (3/3) | -15cp | 6 | 9...c6 10. Be3 Bd7 11. Nd3 b5 12. b3 cxd5 13. cxd5 |
| NN 100ms | c6 (3/3) | -13cp | 4 | 9...c6 10. Nd3 b5 11. b3 bxc4 12. bxc4 cxd5 13. cxd5 |
| NN 1000ms | b6 (3/3) | -24cp | 6 | 9...b6 10. Be3 Ba6 11. Nf3 c6 12. Re1 cxd5 13. cxd5 Bxe2 14. Qxe2 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| b6 | 기존 | -24cp | 10. Nd3 c6 11. Be3 Ba6 12. Qa4 |
| b6 | NN | -24cp | 10. Be3 Ba6 11. Nf3 c6 12. Re1 cxd5 13. cxd5 Bxe2 14. Qxe2 |
| c6 | 기존 | -15cp | 10. Be3 Bd7 11. Nd3 b5 12. b3 cxd5 13. cxd5 |
| c6 | NN | -25cp | 10. Be3 Bd7 11. Nd3 b5 12. b3 |
| Bd7 | 기존 | -26cp | 10. g4 h6 11. Be3 c6 12. Nd3 cxd5 13. cxd5 |
| Bd7 | NN | -30cp | 10. g4 h6 11. Nd3 Qc8 12. f3 |

## P04

출처: G7 49ply / largest_nonmate_drop_pre_at_least_minus200, G7 49ply / before_first_three_own_scores_at_most_minus100

FEN: `r4rk1/5b2/1q1p1P1p/pN1n2b1/Pp2p1B1/1Q1N4/1P3P1P/1R2R1K1 w - - 4 25`

정적 평가: 기존 -40cp / NN -7cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Rxe4 (3/3) | -34cp | 2 | 25. Rxe4 Bxf6 |
| 기존 1000ms | Bh5 (3/3) | -191cp | 5 | 25. Bh5 Nxf6 26. Bxf7+ Rxf7 27. Nc1 |
| NN 100ms | Rxe4 (3/3) | -23cp | 2 | 25. Rxe4 Bxf6 |
| NN 1000ms | Bh5 (3/3) | -142cp | 5 | 25. Bh5 Nxf6 26. Bxf7+ Rxf7 27. Nc1 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Rxe4 | 기존 | -376cp | 25...Nxf6 26. Re6 Nxg4 27. Qd5 Bf4 |
| Rxe4 | NN | -348cp | 25...Nxf6 26. Re6 Nxg4 27. Rg6+ Kh8 |
| Bh5 | 기존 | -202cp | 25...Nxf6 26. Bxf7+ Rxf7 27. Nc1 Nd5 |
| Bh5 | NN | -160cp | 25...Nxf6 26. Bxf7+ Rxf7 27. Nc1 Re8 |

## P05

출처: G9 15ply / first_divergence

FEN: `r2qkbnr/pp1nppp1/2p3bp/8/3P3P/5NN1/PPP2PP1/R1BQKB1R w KQkq - 2 8`

정적 평가: 기존 +21cp / NN +47cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Be2 (3/3) | +38cp | 4 | 8. Be2 f5 9. O-O Ngf6 |
| 기존 1000ms | Bd3 (3/3) | +44cp | 6 | 8. Bd3 Ngf6 9. O-O Qc7 10. Be3 O-O-O |
| NN 100ms | Be2 (3/3) | +54cp | 4 | 8. Be2 Ngf6 9. O-O Be4 10. Nxe4 Nxe4 |
| NN 1000ms | h5 (3/3) | +64cp | 6 | 8. h5 Bh7 9. Be2 Ngf6 10. O-O Be4 11. Nxe4 Nxe4 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Bd3 | 기존 | +44cp | 8...Ngf6 9. O-O Qc7 10. Be3 O-O-O |
| Bd3 | NN | +56cp | 8...Bxd3 9. cxd3 Ngf6 10. O-O e6 |
| Be2 | 기존 | +32cp | 8...Ngf6 9. O-O Qb6 10. c4 O-O-O |
| Be2 | NN | +60cp | 8...Ngf6 9. O-O Qb6 10. Re1 O-O-O |
| h5 | 기존 | +44cp | 8...Bh7 9. Be2 Ngf6 10. O-O Be4 |
| h5 | NN | +64cp | 8...Bh7 9. Be2 Ngf6 10. O-O Be4 11. Nxe4 Nxe4 |

## P06

출처: G9 45ply / largest_nonmate_drop_pre_at_least_minus200

FEN: `4rrk1/p1qn2p1/2p1p2p/7P/1b1Pb1P1/4BN2/PP2B3/R2QR1K1 w - - 3 23`

정적 평가: 기존 +4cp / NN +12cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Bf2 (3/3) | -161cp | 2 | 23. Bf2 Bxe1 24. Bxe1 |
| 기존 1000ms | Kg2 (3/3) | -172cp | 5 | 23. Kg2 Bxe1 24. Qxe1 a5 25. a4 |
| NN 100ms | Bf2 (3/3) | -169cp | 2 | 23. Bf2 Bxe1 24. Bxe1 |
| NN 1000ms | Kg2 (3/3) | -197cp | 5 | 23. Kg2 Bxe1 24. Qxe1 Nf6 25. Qh4 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Bf2 | 기존 | -474cp | 23...Qf4 24. Rf1 Qxg4+ 25. Kh1 Rxf3 |
| Bf2 | NN | -482cp | 23...Qf4 24. Rc1 Qxg4+ 25. Kf1 Bxf3 26. Bxf3 Rxf3 |
| Kg2 | 기존 | -172cp | 23...Bxe1 24. Qxe1 a5 25. a4 |
| Kg2 | NN | -197cp | 23...Bxe1 24. Qxe1 Nf6 25. Qh4 |

## P07

출처: G9 43ply / before_first_three_own_scores_at_most_minus100

FEN: `4rrk1/p1qn2p1/2pbp2p/7P/2BPb1P1/4BN2/PP6/R2QR1K1 w - - 1 22`

정적 평가: 기존 +4cp / NN +18cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Rf1 (3/3) | -88cp | 3 | 22. Rf1 Ba3 23. Kh1 Bxb2 |
| 기존 1000ms | Rf1 (3/3) | -88cp | 4 | 22. Rf1 Ba3 23. Kh1 Bxb2 |
| NN 100ms | Be2 (3/3) | +0cp | 2 | 22. Be2 Bf4 |
| NN 1000ms | Be2 (3/3) | -185cp | 5 | 22. Be2 Bb4 23. Kg2 Bxe1 24. Qxe1 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Be2 | 기존 | -172cp | 22...Bb4 23. Kg2 Bxe1 24. Qxe1 |
| Be2 | NN | -185cp | 22...Bb4 23. Kg2 Bxe1 24. Qxe1 |
| Rf1 | 기존 | -232cp | 22...Bf4 23. Bxf4 Qxf4 24. Bxe6+ Rxe6 |
| Rf1 | NN | -233cp | 22...Bf4 23. Bxf4 Qxf4 24. Bxe6+ Rxe6 |

## P08

출처: G13 16ply / first_divergence, G14 16ply / first_divergence

FEN: `r1bqk2r/ppp1bppp/1nn5/4p3/8/2N1PNP1/PP1P1PBP/R1BQ1RK1 b kq - 0 8`

정적 평가: 기존 -47cp / NN -55cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | O-O (3/3) | -21cp | 4 | 8...O-O 9. d4 Nc4 10. Ne4 |
| 기존 1000ms | Bg4 (3/3) | -7cp | 5 | 8...Bg4 9. d4 exd4 10. exd4 O-O |
| NN 100ms | O-O (3/3) | -22cp | 4 | 8...O-O 9. d4 Bf6 10. Nb5 |
| NN 1000ms | Bg4 (3/3) | -3cp | 5 | 8...Bg4 9. d4 exd4 10. exd4 O-O |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Bg4 | 기존 | -12cp | 9. h3 Bxf3 10. Bxf3 O-O 11. d3 |
| Bg4 | NN | -11cp | 9. h3 Bxf3 10. Bxf3 O-O 11. Ne4 |
| O-O | 기존 | -15cp | 9. d4 exd4 10. Nxd4 Bd7 |
| O-O | NN | -9cp | 9. b3 Bg4 10. Bb2 f5 11. Re1 |

## P09

출처: G13 53ply / largest_nonmate_drop_pre_at_least_minus200, G13 53ply / before_first_three_own_scores_at_most_minus100

FEN: `3r1r1k/1pp5/2n3Q1/p5pp/1b1P2q1/1P2B1P1/P3R1NP/2R3K1 w - - 6 27`

정적 평가: 기존 -10cp / NN +38cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Qh6+ (3/3) | +0cp | 3 | 27. Qh6+ Kg8 28. Qg6+ Kh8 |
| 기존 1000ms | Qh6+ (3/3) | +0cp | 5 | 27. Qh6+ Kg8 28. Qg6+ Kh8 29. Qh6+ |
| NN 100ms | Bxg5 (3/3) | +34cp | 2 | 27. Bxg5 Qxd4+ 28. Ne3 |
| NN 1000ms | Rf2 (3/3) | +25cp | 5 | 27. Rf2 Rxf2 28. Bxf2 Rc8 29. Ne3 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Rf2 | 기존 | -14cp | 27...Rxf2 28. Bxf2 Be7 29. d5 |
| Rf2 | NN | +25cp | 27...Rxf2 28. Bxf2 Rc8 29. Ne3 |
| Bxg5 | 기존 | -244cp | 27...Rd6 28. Qe4 Qxg5 29. Rc4 Rfd8 |
| Bxg5 | NN | -206cp | 27...Rd6 28. Qe8 Rxe8 29. Rxe8+ Kh7 |
| Qh6+ | 기존 | +0cp | 27...Kg8 28. Qg6+ Kh8 29. Qh6+ |
| Qh6+ | NN | +14cp | 27...Kg8 28. Rf2 Rxf2 29. Qg6+ Kh8 30. Bxf2 |

## P10

출처: G14 122ply / largest_nonmate_drop_pre_at_least_minus200

FEN: `5k2/P4p1p/5P1P/8/8/1BK5/8/7b b - - 0 61`

정적 평가: 기존 -294cp / NN -155cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Bf3 (3/3) | -312cp | 6 | 61...Bf3 62. Kd4 Bc6 63. Bd5 Bxd5 64. Kxd5 |
| 기존 1000ms | Ke8 (3/3) | -1004cp | 9 | 61...Ke8 62. Kd4 Bc6 63. Bd5 Kd7 64. Bxc6+ Kxc6 65. a8=Q+ Kb6 66. Qd8+ Kb5 |
| NN 100ms | Kg8 (3/3) | -142cp | 6 | 61...Kg8 62. Kc2 Bg2 63. Kc1 Be4 64. Bc4 |
| NN 1000ms | Kg8 (3/3) | -463cp | 8 | 61...Kg8 62. Kd4 Ba8 63. Bd5 Kh8 64. Bxa8 Kg8 65. Kd5 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Ke8 | 기존 | -636cp | 62. Kd4 Ba8 63. Bd5 Kd7 64. Bxa8 Kd6 65. Be4 |
| Ke8 | NN | -463cp | 62. Kd4 Ba8 63. Bd5 Kf8 64. Bxa8 Kg8 65. Kd5 |
| Kg8 | 기존 | -658cp | 62. Kd4 Bc6 63. Bd5 Ba8 64. Bxa8 Kf8 65. Be4 |
| Kg8 | NN | -463cp | 62. Kd4 Ba8 63. Bd5 Kh8 64. Bxa8 Kg8 65. Kd5 |
| Bf3 | 기존 | -650cp | 62. Kd4 Ba8 63. Bd5 Ke8 64. Bxa8 Kd8 65. Be4 |
| Bf3 | NN | -463cp | 62. Kd4 Bc6 63. Bd5 Ba8 64. Bxa8 Kg8 65. Kd5 |

## P11

출처: G14 112ply / before_first_three_own_scores_at_most_minus100

FEN: `6k1/5p1p/B3bP1P/P7/8/1pK5/8/8 b - - 1 56`

정적 평가: 기존 -75cp / NN +43cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Bd5 (3/3) | -216cp | 7 | 56...Bd5 57. Bf1 Be6 58. Bb5 Kf8 59. Ba4 b2 60. Kxb2 |
| 기존 1000ms | Bd5 (3/3) | -271cp | 10 | 56...Bd5 57. Bb5 Kf8 58. a6 Kg8 59. a7 Kf8 60. Bc4 Be4 61. Kxb3 |
| NN 100ms | Bd5 (3/3) | -99cp | 8 | 56...Bd5 57. Be2 Kf8 58. Kb2 Kg8 59. Bd1 Be4 60. Bxb3 |
| NN 1000ms | Bd5 (3/3) | -123cp | 10 | 56...Bd5 57. Bd3 Kh8 58. a6 Kg8 59. a7 Bg2 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Bd5 | 기존 | -261cp | 57. Bb5 Kf8 58. a6 b2 59. Kxb2 Kg8 60. a7 Kf8 |
| Bd5 | NN | -120cp | 57. Bd3 Bf3 58. a6 Bh1 59. a7 Bd5 60. Bc4 Be4 61. Kxb3 |

## P12

출처: G15 19ply / first_divergence

FEN: `r2q1rk1/pp1bbppp/2n1p3/2ppP3/2P3n1/3P1NP1/PP1N1PBP/R1BQ1RK1 w - - 1 10`

정적 평가: 기존 -15cp / NN -13cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | cxd5 (3/3) | -21cp | 4 | 10. cxd5 exd5 11. Qb3 Bf5 |
| 기존 1000ms | cxd5 (3/3) | -21cp | 6 | 10. cxd5 exd5 11. Re1 Nb4 12. d4 f5 |
| NN 100ms | Re1 (3/3) | -9cp | 4 | 10. Re1 f6 11. cxd5 exd5 |
| NN 1000ms | cxd5 (3/3) | -3cp | 6 | 10. cxd5 exd5 11. Re1 Nh6 12. b3 Nf5 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| cxd5 | 기존 | -21cp | 10...exd5 11. Re1 Nb4 12. d4 f5 |
| cxd5 | NN | -3cp | 10...exd5 11. Re1 Nh6 12. b3 Nf5 |
| Re1 | 기존 | -29cp | 10...Nb4 11. d4 Nd3 12. Rf1 f5 13. cxd5 exd5 |
| Re1 | NN | -6cp | 10...Nb4 11. Nb3 f6 12. Bf4 Re8 |

## P13

출처: G15 43ply / before_first_three_own_scores_at_most_minus100

FEN: `r4rk1/pp2b2p/1q2Ppb1/3p2p1/P1pP1nP1/7P/1P1N1PB1/R1BQR1K1 w - - 4 22`

정적 평가: 기존 -19cp / NN +17cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Nf1 (3/3) | -81cp | 3 | 22. Nf1 Nd3 23. Bxd5 Nxe1 24. Qxe1 Qxd4 25. Bxb7 |
| 기존 1000ms | Nf1 (3/3) | -103cp | 5 | 22. Nf1 Nd3 23. Bxd5 Qa5 24. Bxb7 Qxe1 25. Qxe1 Nxe1 26. Bxa8 Rxa8 |
| NN 100ms | Nf1 (3/3) | -64cp | 3 | 22. Nf1 Nd3 23. Bxd5 Nxe1 24. Qxe1 Qxd4 25. Bxb7 |
| NN 1000ms | Nf3 (3/3) | -85cp | 5 | 22. Nf3 Nxe6 23. a5 Qa6 24. Be3 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Nf1 | 기존 | -103cp | 22...Nd3 23. Bxd5 Qa5 24. Bxb7 Qxe1 25. Qxe1 Nxe1 26. Bxa8 Rxa8 |
| Nf1 | NN | -114cp | 22...Nd3 23. a5 Qxd4 24. Ne3 |
| Nf3 | 기존 | -129cp | 22...Nxe6 23. a5 Qd6 24. Be3 |
| Nf3 | NN | -85cp | 22...Nxe6 23. a5 Qa6 24. Be3 |

## P14

출처: G16 14ply / first_divergence

FEN: `rnbq1rk1/pp2bppp/4pn2/2pp4/4P3/3P1NP1/PPPN1PBP/R1BQ1RK1 b - e3 0 7`

정적 평가: 기존 -15cp / NN -13cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Nc6 (3/3) | +9cp | 4 | 7...Nc6 8. e5 Ng4 9. Re1 |
| 기존 1000ms | Nc6 (3/3) | +9cp | 6 | 7...Nc6 8. c4 Bd7 9. cxd5 exd5 10. a4 |
| NN 100ms | Nbd7 (3/3) | +11cp | 5 | 7...Nbd7 8. e5 Ng4 9. Re1 Re8 |
| NN 1000ms | Nc6 (3/3) | +1cp | 6 | 7...Nc6 8. c4 Bd7 9. e5 Ng4 10. Re1 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Nc6 | 기존 | +13cp | 8. c4 Bd7 9. cxd5 exd5 10. e5 Ng4 |
| Nc6 | NN | +11cp | 8. c4 Bd7 9. e5 Ng4 10. cxd5 exd5 |
| Nbd7 | 기존 | +3cp | 8. c4 a5 9. cxd5 exd5 10. e5 Ng4 |
| Nbd7 | NN | -2cp | 8. Re1 Re8 9. e5 Ng4 10. c4 |

## P15

출처: G16 84ply / largest_nonmate_drop_pre_at_least_minus200, G16 84ply / before_first_three_own_scores_at_most_minus100

FEN: `6k1/1R6/2P2Kp1/p7/7P/6r1/8/8 b - - 0 42`

정적 평가: 기존 -64cp / NN +21cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Rf3+ (3/3) | -197cp | 7 | 42...Rf3+ 43. Kxg6 Rg3+ 44. Kh5 Re3 45. c7 Re8 |
| 기존 1000ms | Rf3+ (3/3) | -209cp | 10 | 42...Rf3+ 43. Kxg6 Rg3+ 44. Kh6 Re3 45. c7 Re6+ 46. Kg5 Re5+ 47. Kf4 |
| NN 100ms | a4 (3/3) | -98cp | 6 | 42...a4 43. c7 Rf3+ 44. Kxg6 Rg3+ 45. Kh5 |
| NN 1000ms | Rf3+ (3/3) | -126cp | 10 | 42...Rf3+ 43. Kxg6 Rg3+ 44. Kh6 Rc3 45. Rg7+ Kf8 46. Rc7 Kg8 47. Rc8+ Kf7 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| a4 | 기존 | -394cp | 43. c7 Rc3 44. Rb8+ Kh7 45. c8=Q Rxc8 46. Rxc8 |
| a4 | NN | -379cp | 43. c7 Rc3 44. Rb8+ Kh7 45. c8=Q Rxc8 46. Rxc8 |
| Rf3+ | 기존 | -203cp | 43. Kxg6 Rg3+ 44. Kh5 Re3 45. c7 Re5+ 46. Kg4 Re8 |
| Rf3+ | NN | -122cp | 43. Kxg6 Rg3+ 44. Kh6 Rc3 45. Rb8+ Kf7 46. Rb7+ Kf8 47. Rb8+ Ke7 |

## S01

출처: G15 85ply / largest_nonmate_drop_pre_at_least_minus200

FEN: `8/1R5p/5pk1/N7/P1p2P2/1p1b2P1/r6P/6K1 w - - 3 43`

정적 평가: 기존 -55cp / NN -26cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Nc6 (3/3) | -149cp | 5 | 43. Nc6 Ra1+ 44. Kf2 Ra2+ 45. Ke3 Rxh2 |
| 기존 1000ms | Nc6 (3/3) | -217cp | 8 | 43. Nc6 b2 44. Ne7+ Kh5 45. Rb5+ f5 46. g4+ Kxg4 |
| NN 100ms | Rc7 (2/3) | -122cp | 4 | 43. Rc7 Rxa4 44. Nc6 Ra1+ 45. Kf2 |
| NN 1000ms | Nc6 (3/3) | -132cp | 8 | 43. Nc6 Ra1+ 44. Kf2 Ra2+ 45. Ke1 Ra1+ 46. Kf2 Rxa4 47. Ne7+ Kh5 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Nc6 | 기존 | -217cp | 43...b2 44. Ne7+ Kh5 45. Rb5+ f5 46. g4+ Kxg4 |
| Nc6 | NN | -132cp | 43...Ra1+ 44. Kf2 Ra2+ 45. Ke1 Ra1+ 46. Kf2 Rxa4 47. Ne7+ Kh5 |
| Rc7 | 기존 | -418cp | 43...b2 44. Rb7 c3 45. h4 b1=Q+ 46. Rxb1 Bxb1 |
| Rc7 | NN | -410cp | 43...b2 44. Rb7 Ra1+ 45. Kf2 b1=Q 46. Rxb1 Rxb1 |

## S02

출처: G15 79ply / before_first_three_own_scores_at_most_minus100

FEN: `7k/7p/1RN2p2/p7/P1p2P2/1p1b2P1/1r5P/6K1 w - - 0 40`

정적 평가: 기존 -189cp / NN -136cp. 이는 평가기 사이의 차이입니다.

| 평가기·시간 | 최빈수·일치 횟수 | 대표 점수 | 완료 깊이 | 대표 예상 수순 |
|---|---|---:|---:|---|
| 기존 100ms | Nxa5 (3/3) | -65cp | 6 | 40. Nxa5 Rb1+ 41. Kf2 Rb2+ 42. Ke3 Re2+ 43. Kd4 |
| 기존 1000ms | Rb8+ (3/3) | -73cp | 8 | 40. Rb8+ Kg7 41. Rb7+ Kg6 42. Nxa5 Ra2 43. Rd7 b2 |
| NN 100ms | Rb8+ (3/3) | -43cp | 6 | 40. Rb8+ Kg7 41. Rb7+ Kg6 42. Nxa5 Rb1+ 43. Kf2 Rf1+ 44. Kg2 |
| NN 1000ms | Rb8+ (3/3) | -48cp | 8 | 40. Rb8+ Kg7 41. Rb7+ Kg6 42. Nxa5 Rb1+ 43. Kf2 Be4 |

| 후보 수 | 참조 평가기 | 원래 차례 기준 점수 | 후속 예상 수순 |
|---|---|---:|---|
| Rb8+ | 기존 | -73cp | 40...Kg7 41. Rb7+ Kg6 42. Nxa5 Ra2 43. Rd7 b2 |
| Rb8+ | NN | -48cp | 40...Kg7 41. Rb7+ Kg6 42. Nxa5 Rb1+ 43. Kf2 Be4 |
| Nxa5 | 기존 | -73cp | 40...Ra2 41. Rb8+ Kg7 42. Rd8 b2 43. Rd7+ Kg6 |
| Nxa5 | NN | -57cp | 40...Rb1+ 41. Kg2 Rb2+ 42. Kh3 Bf1+ 43. Kg4 h5+ 44. Kf5 |

