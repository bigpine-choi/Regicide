"""Read-only interpretation and reference-packet audit; never runs engines."""
import hashlib
import json
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[2]
folder = ROOT / 'build/dynamic-play/comparison-002'
rows = [json.loads(line) for line in (folder / 'records.jsonl').read_text().splitlines()]
references = [json.loads(line) for line in (folder / 'confirmations.jsonl').read_text().splitlines()]
fixtures = json.loads((ROOT / 'tests/data/dynamic_play_positions_v1.json').read_text())
boards = {}
for fixture in fixtures['positions']:
    board = chess.Board(fixture['initial_fen'])
    for uci in fixture['history_uci']:
        board.push_uci(uci)
    boards[fixture['id']] = board

pv_count = 0
def check_pv(board, pv, expected):
    global pv_count
    assert pv and pv[0] == expected
    replay = board.copy(stack=True)
    for uci in pv:
        move = chess.Move.from_uci(uci)
        assert move in replay.legal_moves
        replay.push(move)
    pv_count += 1

changed = [row for row in rows if row['legacy']['selected_uci'] != row['dynamic']['selected_uci']]
expected = {(row['id'], row['legacy']['selected_uci'], row['dynamic']['selected_uci']) for row in changed}
assert len(expected) == len(references) == 5
assert {(row['id'], row['old_uci'], row['new_uci']) for row in references} == expected
findings = []
for row in references:
    board = boards[row['id']]
    for label in ('old', 'new'):
        query, uci = row[label], row[label + '_uci']
        result = query['result']
        assert query['status'] == 'ok' and query['requested_ms'] == 2000
        assert query['root_uci'] == query['returned_best_uci'] == result['best_uci'] == uci
        assert result['bound'] == 'exact' and result['mate_white'] is None and result['mate_mover'] is None
        assert result['cp_mover'] == result['cp_white'] * (1 if board.turn else -1)
        check_pv(board, result['pv_uci'], uci)
        exact_packets = []
        for packet in query['score_packets']:
            assert packet['score_turn'] == ('white' if board.turn else 'black')
            check_pv(board, packet['pv_uci'], uci)
            if not packet['lowerbound'] and not packet['upperbound']:
                exact_packets.append(packet)
        assert len(exact_packets) == query['completed_exact_packets']
        last = exact_packets[-1]
        assert (last['depth'], last['cp_white'], last['pv_uci']) == (result['depth'], result['cp_white'], result['pv_uci'])
    delta = row['new']['result']['cp_mover'] - row['old']['result']['cp_mover']
    assert row['delta']['status'] == 'finite' and row['delta']['new_minus_old_cp_mover'] == delta
    matches = [item for item in changed if (item['id'], item['legacy']['selected_uci'], item['dynamic']['selected_uci'])
               == (row['id'], row['old_uci'], row['new_uci'])]
    kinds = {('style_selected' if item['dynamic']['decision']['selected_uci'] != item['dynamic']['decision']['baseline_uci']
              else 'timed_baseline_difference') for item in matches}
    assert len(kinds) == 1
    findings.append({'position': row['id'], 'old_uci': row['old_uci'], 'new_uci': row['new_uci'],
                     'requests': len(matches), 'cause': kinds.pop(), 'reference_delta_cp_mover': delta,
                     'reference_old_depth': row['old']['result']['depth'], 'reference_new_depth': row['new']['result']['depth']})

opening = [row for row in rows if row['dynamic']['decision']['mode'] == 'opening']
middle = [row for row in rows if row['dynamic']['decision']['mode'] == 'middlegame']
selected_style = [row for row in middle if row['dynamic']['decision']['reason'] == 'accepted_dynamic_alternative']
queen_pawn = [row for row in rows if row['id'] == 'queen-pawn-center']
assert len(opening) == 8 and all(row['legacy']['selected_uci'] == row['dynamic']['selected_uci'] for row in opening)
assert len(middle) == 24 and len(selected_style) == 6
assert all(row['dynamic']['selected_uci'] == row['dynamic']['decision']['baseline_uci'] == 'f7f5' for row in queen_pawn)
assert sum(row['legacy']['selected_uci'] == 'f7f5' for row in queen_pawn) == 3

report = {'status': 'passed', 'reference_score_packet_pvs_checked': pv_count,
          'reference_unique_changed_choices': findings,
          'opening_requests': len(opening), 'opening_actual_choices_identical_to_legacy': len(opening),
          'middle_requests': len(middle), 'style_selected_requests': len(selected_style),
          'max_selected_middle_own_engine_loss_cp': max(row['dynamic']['decision']['loss_cp'] for row in selected_style),
          'style_selected_reference_deltas_cp_unique': [row['reference_delta_cp_mover'] for row in findings if row['cause'] == 'style_selected'],
          'worst_reference_delta_explanation': 'The -111cp case is the unchanged full-budget baseline f7f5 at queen-pawn-center, not a style-selected alternative. The legacy run also chose f7f5 for three other seeds.',
          'initial_audit_failure_explanation': 'The initial auditor incorrectly summed maximum per-probe allowances. Target-depth searches release unused time back to the shared phase deadline. Corrected checks validate each allowance against remaining measured time, actual phase timing, and the documented 100ms watchdog grace. Initial failed files remain preserved.',
          'limitations': ['Only 32 paired single-move choices, not full games or independent holdout.',
                         'Own-engine CP gates and geometry do not prove retained playing strength.',
                         'Reference estimates use finite 2-second constrained queries, with different completed depths; no causal Elo or strength claim.',
                         'Timed baseline variation and shared CPU load remain observable; middle dynamic receives extra search time.'],
          'records_sha256': hashlib.sha256((folder / 'records.jsonl').read_bytes()).hexdigest(),
          'confirmations_sha256': hashlib.sha256((folder / 'confirmations.jsonl').read_bytes()).hexdigest(),
          'interpretation_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
output = folder / 'independent-findings.json'
assert not output.exists(), 'Use a fresh output file'
output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
