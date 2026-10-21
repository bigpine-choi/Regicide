"""Audit preserved observations from actual native GUI play, without running UI."""
from pathlib import Path
import hashlib
import json
import chess
import chess.pgn

OUT = Path(__file__).resolve().parent


def record(name):
    return json.loads((OUT / (name + '.json')).read_text(encoding='utf-8'))


def equal(before, after, fields=('fen', 'moves', 'revision'), files=True):
    left, right = record(before), record(after)
    for field in fields:
        assert left['state'][field] == right['state'][field], (before, after, field)
    if files:
        assert left['saved_file_sha256'] == right['saved_file_sha256'], (before, after, 'saved files')


def main():
    checks = []
    for before, after in [
        ('01-initial', '02-after-preview'),
        ('03-sicilian-before-preview', '04-sicilian-after-preview'),
        ('13-before-illegal-drag', '14-after-illegal-drag'),
        ('15-after-castling', '19-after-replay-return'),
        ('23-fixed-prefix', '24-fixed-prefix-after-preview'),
    ]:
        equal(before, after)
        checks.append(before + ' -> ' + after + ': board/history/revision/saved files unchanged')
    equal('08-after-undo', '09-handicap-preview', fields=())
    equal('08-after-undo', '10-handicap-cancel', fields=('fen', 'moves'), files=False)
    left, right = record('08-after-undo'), record('10-handicap-cancel')
    assert all(digest == right['saved_file_sha256'][name]
               for name, digest in left['saved_file_sha256'].items() if name.endswith('.pgn'))
    checks.append('Handicap preview preserved all files; cancel restored board/history/PGN. JSON revision updates normally.')
    equal('25-promotion-before', '28-promotion-undo', fields=('fen',), files=False)
    checks.append('Promotion undo restored the complete initial FEN')
    assert record('21-black-move-response')['state']['opening_guide'] == record('23-fixed-prefix')['state']['opening_guide']
    checks.append('GUI label corrected with identical position/history/opening data')
    board = chess.Board(record('15-after-castling')['state']['fen'])
    assert board.piece_at(chess.G1) == chess.Piece(chess.KING, chess.WHITE)
    assert board.piece_at(chess.F1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert not board.has_kingside_castling_rights(chess.WHITE)
    board = chess.Board(record('27-queen-promotion')['state']['fen'])
    assert board.piece_at(chess.B8) == chess.Piece(chess.QUEEN, chess.WHITE)
    knight = record('30-knight-promotion-draw')['state']
    assert knight['result'] == '1/2-1/2' and knight['termination'] == 'insufficient_material'
    assert len(knight['moves']) == 1 and not knight['engine_to_move']
    board = chess.Board(record('32-en-passant-after')['state']['fen'])
    assert board.piece_at(chess.D6) == chess.Piece(chess.PAWN, chess.WHITE)
    assert board.piece_at(chess.D5) is None
    mate = record('34-after-mate')['state']
    assert mate['result'] == '1-0' and mate['termination'] == 'checkmate'
    assert len(mate['moves']) == 1 and not mate['engine_to_move']
    resign = record('22-black-resigned')['state']
    assert resign['result'] == '1-0' and resign['termination'] == 'resignation'
    checks.append('Castling, capture-promotion, knight draw, en-passant, checkmate and resignation assertions PASS')
    games = []
    for file in sorted(OUT.glob('saved-*.pgn')):
        with file.open(encoding='utf-8') as stream:
            game = chess.pgn.read_game(stream)
        assert game is not None and not game.errors, file.name
        saved = json.loads(file.with_suffix('.json').read_text(encoding='utf-8'))
        assert game.end().board().fen(en_passant='fen') == saved['state']['fen'], file.name
        assert [move.uci() for move in game.mainline_moves()] == saved['uci_moves'], file.name
        assert saved['uci_moves'] == [move['uci'] for move in saved['state']['moves']], file.name
        games.append({'file': file.name, 'plies': len(list(game.mainline_moves())),
                      'result': game.headers['Result'], 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    assert len(games) == 6, len(games)
    checks.append('All six isolated auto-saved PGNs parse legally and match their JSON records')
    result = {'status': 'PASS_WITH_NATIVE_EXPORT_UI_LIMITATION', 'manual_native_ui': True,
              'normal_start_games': 2, 'programmatically_prepared_diagnostic_positions': 4,
              'personal_game_library_used': False, 'checks': checks, 'games': games,
              'native_export_dialog': 'Opened/cancelled; focus returned to main window, so final native save is not verified.'}
    (OUT / 'session-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Actual native UI record audit: PASS, {len(checks)} checks, {len(games)} PGNs; native export save unverified.')


if __name__ == '__main__':
    main()
