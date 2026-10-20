"""Freeze a uniform early probe after late loss pivots proved already losing."""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import chess


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--match', type=Path, required=True)
    parser.add_argument('--late-selection', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve prior selection')
    matches = json.loads(args.match.read_text(encoding='utf-8'))['games']
    previous = [json.loads(line) for line in args.late_selection.read_text(encoding='utf-8').splitlines()]
    by_game = {r['game']: r for r in previous}
    result = []
    for game in matches:
        if game['game'] not in by_game:
            continue
        old = by_game[game['game']]
        role = 'baseline' if old['losing_arm'] == 'A' else 'candidate'
        decisions = [m for m in game['search_moves'] if m['role'] == role]
        assert len(decisions) >= 6
        selected = decisions[5]
        move_index = selected['ply']-1
        board = chess.Board(game['initial_fen'])
        for move in game['uci_moves'][:move_index]:
            board.push_uci(move)
        assert board.fen(en_passant='fen') == selected['fen_before']
        move = chess.Move.from_uci(selected['move'])
        row = {**old, 'query_id': f"game-{game['game']:04d}-sixth-decision",
            'selection_reason': 'sixth_losing_side_searched_move_uniform_for_all_decisive_games',
            'ply': selected['ply'], 'moves_before': game['uci_moves'][:move_index],
            'fen_before': board.fen(en_passant='fen'), 'played_uci': move.uci(),
            'played_san': board.san(move), 'original_search': selected}
        board.push(move)
        row['fen_after'] = board.fen(en_passant='fen')
        # Fields from material-based pivots are inapplicable to this fixed probe.
        for key in ('material_before','material_after','continuation_san_next8',
                    'continuation_uci_next8','phase','uci_range'):
            row.pop(key, None)
        result.append(row)
    assert len(result) == 76 and len({r['game'] for r in result}) == 76
    args.output.mkdir(parents=True)
    path = args.output/'selected-positions.jsonl'
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        for row in result:
            stream.write(json.dumps(row, sort_keys=True)+'\n')
    document = {'created_utc': datetime.now(timezone.utc).isoformat(), 'cases': 76,
        'policy': 'Sixth losing-side decision, every decisive game, without using its score or reanalysis to choose the point',
        'reason': 'All late pivots had already losing own-engine root references; add an earlier uniform exploratory probe',
        'scope': 'Post-hoc exploratory followup, not a fresh independent confirmatory sample; prior selection is retained',
        'selection_sha256': digest(path), 'inputs': {str(p.resolve()): digest(p)
            for p in (args.match,args.late_selection,Path(__file__))}}
    with (args.output/'selection-manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(document,stream,indent=2)
    print(json.dumps(document,indent=2))


if __name__ == '__main__':
    main()
