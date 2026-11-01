"""Compare ordinary and artistic play using frozen public legal histories.

Only fresh disposable games and owned hidden engines are used. Screen helpers
are disabled in both runs; this is a behavior/guard diagnostic, not an Elo test.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time

import chess
import chess.engine

from check_opening_variety import QuietScreenHints, load_module, start_engine
from check_evaluation_quality import query, same_reference_choice_delta
from engine_profile import load_profile
from opening_guide import _catalog
from runtime_engine import resolve_runtime_engine

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'b98a322461a0c77a6361b9b44ecb95c21b7b211f'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def roots(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    result = []
    for row in data['positions']:
        board = chess.Board(row['initial_fen'])
        for uci in row['history_uci']:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError('Invalid fixture history: ' + row['id'])
            board.push(move)
        if board.fen(en_passant='fen') != row['fen'] or board.is_game_over(claim_draw=True):
            raise ValueError('Invalid fixture: ' + row['id'])
        result.append((row, board))
    return data, result


def snapshot(out, label):
    folder = out / (label + '-source')
    folder.mkdir()
    names = ['opening_variety.py', 'play_human.py']
    if label == 'dynamic':
        names += ['dynamic_play.py', 'game_archive.py']
    hashes = {}
    for name in names:
        relative = 'scripts/' + name
        content = (subprocess.check_output(['git', 'show', BASELINE + ':' + relative], cwd=ROOT)
                   if label == 'legacy' else (ROOT / relative).read_bytes())
        (folder / name).write_bytes(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    host = load_module('dynamic_audit_' + label + '_host', folder / 'play_human.py')
    host.opening_variety = load_module('dynamic_audit_' + label + '_opening', folder / 'opening_variety.py')
    if label == 'dynamic':
        host.dynamic_play = load_module('dynamic_audit_style', folder / 'dynamic_play.py')
    host.MoveHints, host.ROOT = QuietScreenHints, ROOT
    return host, hashes


def validate_pv(board, value):
    if not value:
        return
    replay = board.copy(stack=True)
    for uci in value['pv_uci']:
        move = chess.Move.from_uci(uci)
        if move not in replay.legal_moves:
            raise AssertionError('Illegal recorded PV')
        replay.push(move)


def validate_decision(board, actual, decision):
    if decision is None:
        return
    assert decision['selected_uci'] == actual.uci()
    validate_pv(board, decision.get('baseline'))
    limit = 25 if len(board.move_stack) < 16 else 15
    for probe in decision['probes']:
        validate_pv(board, probe.get('candidate_compared'))
        validate_pv(board, probe.get('candidate_final'))
        if probe['admitted']:
            assert probe['candidate_compared']['depth'] == decision['baseline']['depth']
            assert decision['baseline']['depth'] >= 4
            assert 0 <= probe['loss_cp'] <= limit
    if actual.uci() != decision['baseline_uci']:
        assert any(p['uci'] == actual.uci() and p['admitted'] for p in decision['probes'])


def one_move(host, binary, directory, board, seed, budget, dynamic):
    options = {'dynamic_play_enabled': True} if dynamic else {}
    game = host.HumanGame(binary, directory, engine_factory=start_engine, **options)
    try:
        game.new_game('black' if board.turn else 'white', budget, game.revision)
        with game._lock:
            game.board = board.copy(stack=True)
            game._opening_seed = seed
            game.revision += 1
            game._sync_evaluation()
        started = time.monotonic()
        state = game.engine_move(game.revision)
        elapsed = (time.monotonic() - started) * 1000
        assert not state.get('error'), state.get('error')
        assert state['engine'].get('evaluation') == 'classical_activity_v1', 'This comparison requires the activity-enabled Regicide runtime'
        actual = game.board.peek()
        assert actual in board.legal_moves and len(game.board.move_stack) == len(board.move_stack) + 1
        decisions = game._dynamic_decisions if dynamic else game._opening_decisions
        decision = decisions.get(len(game.board.move_stack))
        validate_decision(board, actual, decision)
        return {'selected_uci': actual.uci(), 'selected_san': board.san(actual),
                'wall_ms': round(elapsed, 3), 'decision': decision,
                'engine': state['engine']}
    finally:
        game.close()


def summarize(records, confirmations):
    result = {'positions': len({r['id'] for r in records}), 'pairs': len(records),
              'different_choices': sum(r['legacy']['selected_uci'] != r['dynamic']['selected_uci'] for r in records)}
    for label in ('legacy', 'dynamic'):
        result[label] = {'mean_wall_ms': statistics.mean(r[label]['wall_ms'] for r in records),
                         'max_wall_ms': max(r[label]['wall_ms'] for r in records),
                         'distinct_per_position': {key: len({r[label]['selected_uci'] for r in records if r['id'] == key})
                                                  for key in sorted({r['id'] for r in records})}}
    decisions = [r['dynamic']['decision'] for r in records if r['dynamic']['decision']]
    result['dynamic']['reasons'] = dict(Counter(d['reason'] for d in decisions))
    result['dynamic']['nonbaseline'] = sum(d['selected_uci'] != d['baseline_uci'] for d in decisions)
    result['dynamic']['by_mode'] = {mode: {'requests': sum(d['mode'] == mode for d in decisions),
        'nonbaseline': sum(d['mode'] == mode and d['selected_uci'] != d['baseline_uci'] for d in decisions),
        'reasons': dict(Counter(d['reason'] for d in decisions if d['mode'] == mode))}
        for mode in ('opening', 'middlegame')}
    result['dynamic']['maximum_selected_loss_cp'] = max((d['loss_cp'] or 0 for d in decisions), default=0)
    gains = [d['style_evidence'][d['selected_uci']]['points'] - d['style_evidence'][d['baseline_uci']]['points']
             for d in decisions if d['selected_uci'] != d['baseline_uci'] and
             d['selected_uci'] in d['style_evidence'] and d['baseline_uci'] in d['style_evidence']]
    assert all(gain >= 0 for gain in gains), 'Selected move regressed the stated style rule'
    result['dynamic']['selected_style_gains'] = gains
    result['dynamic']['strictly_more_active_after_reply'] = sum(gain > 0 for gain in gains)
    finite = [r['delta']['new_minus_old_cp_mover'] for r in confirmations if r['delta']['status'] == 'finite']
    result['reference_changed_choice_pairs'] = {'pairs': len(confirmations), 'finite': len(finite),
        'new_preferred_over_20cp': sum(v > 20 for v in finite), 'old_preferred_over_20cp': sum(v < -20 for v in finite),
        'within_20cp': sum(abs(v) <= 20 for v in finite),
        'mean_new_minus_old_cp_mover': statistics.mean(finite) if finite else None,
        'worst_new_minus_old_cp_mover': min(finite) if finite else None}
    return result


def run(args):
    out = args.output.resolve()
    if out.exists():
        raise ValueError('Use a fresh output directory')
    data, positions = roots(args.positions)
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    binary_hash, fixture_hash = digest(binary), digest(args.positions)
    _catalog()
    out.mkdir(parents=True)
    hosts, sources = {}, {}
    for label in ('legacy', 'dynamic'):
        hosts[label], sources[label] = snapshot(out, label)
    metadata = {'baseline_commit': BASELINE, 'head_at_start': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        'binary': str(binary), 'binary_sha256': binary_hash, 'evaluation': 'classical_activity_v1 in both runs',
        'fixture_sha256': fixture_hash, 'sources': sources, 'driver_sha256': digest(__file__),
        'python': platform.python_version(), 'python_chess': chess.__version__, 'platform': platform.platform(),
        'conditions': data['conditions'], 'order': 'Alternate legacy/dynamic order for each paired request; fresh engine per request',
        'limitations': ['Public authored development roots; not an independent strength holdout.',
                       'Shared CPU, no external-load isolation. Different total time caps in middlegame.',
                       'No screen helpers, GUI, personal archive or user processes accessed.',
                       'Reference constrained estimates are finite searches, not exact regret or Elo.']}
    write_json(out / 'manifest.json', metadata)
    write_json(out / 'positions.json', data)
    records = []
    with tempfile.TemporaryDirectory(prefix='dynamic-play-owned-') as temporary:
        with (out / 'records.jsonl').open('x', encoding='utf-8') as stream:
            for source, board in positions:
                for seed in data['conditions']['seeds']:
                    row = {'id': source['id'], 'seed': seed, 'fen': source['fen'], 'history_uci': source['history_uci']}
                    labels = ('legacy','dynamic') if len(records) % 2 == 0 else ('dynamic','legacy')
                    for label in labels:
                        row[label] = one_move(hosts[label], binary, temporary, board, seed,
                                              data['conditions']['budget_ms'], label == 'dynamic')
                    records.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n'); stream.flush()
                    print(json.dumps({'completed_pairs':len(records), 'id':source['id']}), flush=True)
    confirmations = []
    if args.reference_profile:
        profile = load_profile(args.reference_profile)
        metadata['reference_profile'] = profile
        write_json(out / 'manifest.json', metadata)
        extra = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        cache = {}
        boards = {source['id']: board for source,board in positions}
        with chess.engine.SimpleEngine.popen_uci([str(profile['binary']['path'])], timeout=10, **extra) as reference:
            reference.configure(profile['options'])
            with (out/'confirmations.jsonl').open('x', encoding='utf-8') as stream:
                pairs = sorted({(r['id'],r['legacy']['selected_uci'],r['dynamic']['selected_uci']) for r in records
                                if r['legacy']['selected_uci'] != r['dynamic']['selected_uci']})
                for key, old, new in pairs:
                    board = boards[key]
                    for move in (old,new):
                        if (key,move) not in cache:
                            cache[key,move] = query(reference,board,2000,move)
                    row = {'id':key,'old_uci':old,'new_uci':new,'old':cache[key,old],'new':cache[key,new],
                           'delta':same_reference_choice_delta(board,cache[key,old],cache[key,new])}
                    confirmations.append(row)
                    stream.write(json.dumps(row) + '\n'); stream.flush()
        write_json(out/'reference-query-count.json', {'unique_queries':len(cache),'milliseconds_each':2000})
    assert digest(binary) == binary_hash and digest(args.positions) == fixture_hash
    for name, expected in sources['dynamic'].items():
        assert digest(ROOT/name) == expected, 'Source changed during measurement: '+name
    result = summarize(records, confirmations)
    write_json(out/'summary.json',result)
    print(json.dumps(result),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--positions',type=Path,default=ROOT/'tests/data/dynamic_play_positions_v1.json')
    parser.add_argument('--binary',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference-profile',type=Path)
    run(parser.parse_args())
