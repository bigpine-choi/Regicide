"""Compare independently generated legal paths to python-chess; never used by the engine."""
import argparse
import random
import subprocess
from pathlib import Path

import chess


def expected_outcome(board):
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return "ongoing"
    return {
        chess.Termination.CHECKMATE: "checkmate",
        chess.Termination.STALEMATE: "stalemate",
        chess.Termination.INSUFFICIENT_MATERIAL: "known_dead_material",
        chess.Termination.SEVENTYFIVE_MOVES: "seventy_five_moves",
        chess.Termination.FIVEFOLD_REPETITION: "fivefold_repetition",
    }[outcome.termination]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--games", type=int, default=32)
    parser.add_argument("--plies", type=int, default=100)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    fixtures = [
        chess.STARTING_FEN,
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
        "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
        "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
        "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10",
        "7k/8/8/r4pPK/8/8/8/8 w - f6 0 1",
        "k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
        "7k/8/8/3pP3/4K3/8/8/8 w - d6 0 1",
        "1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1",
        "4k3/8/8/8/8/8/p7/1R2K3 b - - 0 1",
        "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    ]
    cases = []

    def add_case(root, moves):
        board = chess.Board(root)
        for move in moves:
            board.push_uci(move)
        text = root + "|" + " ".join(moves)
        repetitions = 1
        while board.is_repetition(repetitions + 1):
            repetitions += 1
        ongoing = expected_outcome(board) == "ongoing"
        cases.append((text, board.fen(en_passant="fen"),
                      sorted(m.uci() for m in board.legal_moves), expected_outcome(board), repetitions,
                      ongoing and board.is_repetition(3), ongoing and board.is_fifty_moves()))

    # Every immediate child of targeted fixtures: captures, castles, all promotions, ep.
    for root in fixtures:
        board = chess.Board(root)
        assert board.is_valid(), root
        add_case(root, [])
        for move in board.legal_moves:
            add_case(root, [move.uci()])

    # Full histories verify counters, repetition identity and make/undo after each path.
    for game in range(args.games):
        root = fixtures[game % len(fixtures)]
        board = chess.Board(root)
        path = []
        for _ in range(args.plies):
            add_case(root, path)
            if board.is_game_over():
                break
            move = rng.choice(sorted(board.legal_moves, key=lambda m: m.uci()))
            board.push(move)
            path.append(move.uci())

    cycle = ["g1f3", "g8f6", "f3g1", "f6g8"]
    for length in range(17):
        add_case(chess.STARTING_FEN, (cycle * 4)[:length])
    for halfmove in [99, 100, 149, 150]:
        add_case(f"7k/8/8/8/8/8/R7/4K3 w - - {halfmove} 1", [])

    result = subprocess.run([str(args.binary.resolve()), "batch"],
                            input="\n".join(case[0] for case in cases) + "\n",
                            capture_output=True, text=True, check=True, timeout=180)
    lines = result.stdout.splitlines()
    assert len(lines) == len(cases), (len(lines), len(cases), result.stderr)
    for number, (line, case) in enumerate(zip(lines, cases)):
        fields = line.split("\t")
        assert len(fields) == 6, (number, case[0], line)
        fen, moves, outcome, reps, threefold, fifty = fields
        actual = (fen, moves.split(), outcome, int(reps), bool(int(threefold)), bool(int(fifty)))
        assert actual == case[1:], f"case {number}\ninput={case[0]}\nactual={actual}\nexpected={case[1:]}"
    print(f"PASS differential: {len(cases)} cases; seed={args.seed}; games={args.games}; "
          f"max_plies={args.plies}; chess={chess.__version__}")
    print("Compared FEN, exact legal moves, outcome, repetition count, current draw claims; "
          "C++ verified complete make/undo state for every legal child.")


if __name__ == "__main__":
    main()
