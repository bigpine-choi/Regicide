from pathlib import Path
import chess

root = Path(__file__).resolve().parents[2]
source = root / 'tests/data/evaluation_profile_2026_10_18.fens'
lines = source.read_text(encoding='utf-8').splitlines()
fixtures = [
    ('Unblocked advanced passer', '1r5k/8/P7/8/8/8/7P/7K w - - 0 1'),
    ('Rook blocks advanced passer', '7k/r7/P7/8/8/8/7P/7K w - - 0 1'),
    ('Triple isolated pawns', '7k/7p/8/8/4P3/4P3/4P2P/6K1 w - - 0 1'),
    ('Connected friendly files', '7k/7p/8/8/4PP2/8/8/6K1 w - - 0 1'),
    ('Adjacent enemy ahead', '7k/8/8/3p4/4P3/8/8/6K1 w - - 0 1'),
    ('Adjacent enemy behind', '7k/8/8/8/4P3/3p4/8/6K1 w - - 0 1'),
    ('Adjacent enemy same rank', '7k/8/8/8/3pP3/8/8/6K1 w - - 0 1'),
    ('King blocks promotion square', 'k7/P7/8/8/8/8/8/7K w - - 0 1'),
]
for name, fen in fixtures:
    board = chess.Board(fen)
    assert board.is_valid(), (name, board.status())
    for label, value in [(name, board), (name + ' color reflection', board.mirror())]:
        lines.extend(['# ' + label, value.fen(en_passant='fen')])
fens = [line for line in lines if line and not line.startswith('#')]
assert len(fens) == len(set(fens)), 'duplicate FEN'
assert all(chess.Board(fen).is_valid() for fen in fens)
path = root / 'tests/data/pawn_structure_profile_2026_10_18.fens'
path.write_bytes(('# Fixed before pawn-structure timing: prior 42 plus 16 own edge fixtures.\n' + '\n'.join(lines) + '\n').encode('utf-8'))
print(f'{len(fens)} valid unique self-composed positions')
