"""Freeze sibling pairs around real mistakes, reusing exact teacher labels.

Only the current model's unlabelled child positions need new Stockfish work.
Neither the new candidate nor the later gate is run here.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import shutil
import time

import chess
import chess.engine

import neural_training as base
from prepare_neural_huber import read, require, sha, write
from stockfish_teacher import label_rows, source_board, _json_info
from train_neural_stm import ROOT

PREVIOUS = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
MODEL_SHA = '02ccdf9b7fd1ac6206774bbc356081977db26cc2c8610176f732ff6ceadc9a09'


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def jsonl(path, rows):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')


def prepare(output):
    require(not output.exists(), 'Refuse to overwrite an ordering dataset')
    started = time.monotonic()
    binary = ROOT / 'build-m3-stm/regicide.exe'
    model = PREVIOUS / 'attempt-001/model.nnue'
    require(sha(model) == MODEL_SHA, 'Current model differs')
    profile = read(PROFILE)
    sf = Path(profile['binary']['path'])
    require(sha(sf) == profile['binary']['sha256'], 'Stockfish differs')
    background = {p: lines(PREVIOUS / 'data-001' / (p + '.jsonl')) for p in ('train', 'tuning', 'test')}
    old_labels = [r for batch in ('roots', 'children') for r in lines(PREVIOUS / 'data-001' / (batch + '-teacher/labels.jsonl'))]
    by_fen = {r['fen']: r for r in old_labels}
    excluded_fens = {q['fen'] for batch in ('roots', 'children')
                     for q in lines(PREVIOUS / 'data-001' / (batch + '-teacher/teacher-queries.jsonl'))
                     if q['status'] != 'accepted'} - set(by_fen)
    roots = lines(PREVIOUS / 'data-001/roots-teacher/labels.jsonl')
    queries = {q['source_query_id']: q for q in lines(PREVIOUS / 'data-001/roots-teacher/teacher-queries.jsonl')}
    cached = read(PREVIOUS / 'diagnosis/search-recheck.json')
    cached_moves = {c['source']['query_id']: c['searches']['after'] for c in cached['cases']}
    options = {**read(ROOT / 'config/engine-profiles/classical.json')['options'],
               'UseNNUEEvaluation': True, 'NeuralModelFile': str(model)}
    inputs = [Path(__file__), ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_training.py',
              PROFILE, sf, binary, model, PREVIOUS / 'diagnosis/search-recheck.json',
              PREVIOUS / 'data-001/manifest.json', *[PREVIOUS / 'data-001' / (p + '.jsonl') for p in background],
              *[PREVIOUS / 'data-001' / (batch + '-teacher/' + name) for batch in ('roots', 'children')
                for name in ('labels.jsonl', 'teacher-queries.jsonl')]]
    output.mkdir(parents=True)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'warm_sha256': MODEL_SHA,
                'teacher_profile': profile, 'maximum_new_teacher_queries': 60, 'teacher_nodes': 50000,
                'maximum_new_warm_searches': 30, 'warm_search_nodes': 10000,
                'pairing': 'Exact accepted root teacher recommendation vs original move and current warm move; no assumed good/bad sign',
                'point_background': 'Preserve current1361train/307tuning/69test including replay weights',
                'splits': 'Loss opening pairs1..6 train;7..8 tuning, retain background splits',
                'loss': 'point MSE + pair white-score-difference MSE, pair coefficient1',
                'new_point_weight': 4, 'no_refill': True,
                'gate': {'minimum_teacher_matches': 5, 'preserve_two_current_correct': True,
                         'minimum_tuning_matches': 2, 'games_if_pass': 16, 'games_if_fail': 0},
                'input_sha256': {str(p): sha(p) for p in inputs}}
    write(output / 'protocol.json', protocol)
    for p in inputs:
        if p.suffix == '.py':
            dest = output / 'source-snapshot' / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    require(sha(binary) == '97140c4ad1c40a9f2c67e2fb9a8c0718fe70722e83dcbe50a7011b71c97558d1', 'Native differs')
    warm_receipts = []
    with chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30) as engine:
        engine.configure(options)
        for root in roots:
            board = source_board(root)
            if root['query_id'] in cached_moves:
                info = cached_moves[root['query_id']]
                require(info['model_sha256'] == MODEL_SHA and info['options'] == options, 'Cached warm search differs')
                move = chess.Move.from_uci(info['move_uci'])
                receipt = {'root_id': root['query_id'], 'reused': True, 'move': move.uci(), 'cached_search': info}
            else:
                engine.configure({'Clear Hash': None})
                info = engine.analyse(board, chess.engine.Limit(nodes=10000), game=object(), info=chess.engine.INFO_ALL)
                require(info.get('pv'), 'Warm search lacks PV')
                move = info['pv'][0]
                receipt = {'root_id': root['query_id'], 'reused': False, 'move': move.uci(), 'raw_info': _json_info(info)}
            require(move in board.legal_moves, 'Illegal warm move')
            warm_receipts.append(receipt)
    write(output / 'warm-searches.json', warm_receipts)
    warm_moves = {r['root_id']: r['move'] for r in warm_receipts}
    requested, requests_seen, branches = [], set(), []
    for root in roots:
        root_board = source_board(root)
        best = queries[root['query_id']]['bestmove_uci']
        candidates = list(dict.fromkeys([best, root['played_move'], warm_moves[root['query_id']]]))
        children = {}
        for move_text in candidates:
            board = root_board.copy(stack=True)
            board.push_uci(move_text)
            fen = board.fen(en_passant='fen')
            child = {**root, 'query_id': root['query_id'] + '-child-' + move_text,
                     'fen': fen, 'history': root['history'] + [move_text],
                     'feature_hash': base.feature_identity(board), 'source_role': 'ordering_child',
                     'target_white_cp': 0, 'source_root_id': root['query_id'], 'root_move': move_text,
                     'replay_weight': 4}
            children[move_text] = child
            if fen not in by_fen and fen not in excluded_fens and fen not in requests_seen:
                requested.append(child)
                requests_seen.add(fen)
        branches.append({'root': root, 'reference_move': best, 'children': children})
    require(0 < len(requested) <= 60, 'New-label budget differs; no automatic retry')
    write(output / 'new-selected.json', requested)
    print(f'Reuse111exactlabels; label only {len(requested)}new current-choice children.', flush=True)
    new_labels, teacher = label_rows(sf, requested, output / 'new-teacher', nodes=50000, max_abs_cp=1500)
    by_fen.update({r['fen']: r for r in new_labels})
    partitions = {p: [dict(row) for row in rows] for p, rows in background.items()}
    old_feature_partition = {r['feature_hash']: p for p, rows in partitions.items() for r in rows}
    all_feature_partitions = {}
    for branch in branches:
        for child in branch['children'].values():
            all_feature_partitions.setdefault(child['feature_hash'], set()).add(child['partition'])
    cross = {h for h, ps in all_feature_partitions.items() if len(ps) > 1}
    pairs = {'train': [], 'tuning': []}
    exclusions = Counter()
    added = set(old_feature_partition)
    for branch in branches:
        root, best = branch['root'], branch['reference_move']
        a = branch['children'][best]
        for move, b in branch['children'].items():
            if move == best:
                continue
            if a['fen'] not in by_fen or b['fen'] not in by_fen:
                exclusions['missing_exact_child'] += 1
                continue
            partition = root['partition']
            if any(row['feature_hash'] in cross or old_feature_partition.get(row['feature_hash'], partition) != partition for row in (a, b)):
                exclusions['cross_partition_feature'] += 1
                continue
            aa, bb = by_fen[a['fen']], by_fen[b['fen']]
            pair = {'root_id': root['query_id'], 'source_game': root['source_game'],
                    'source_ply': root['source_ply'], 'family_id': root['family_id'],
                    'partition': partition, 'root_sign': 1 if source_board(root).turn else -1,
                    'root_fen': root['fen'], 'initial_fen': root['initial_fen'], 'root_history': root['history'],
                    'move_a': best, 'move_b': move,
                    'a': {**a, 'target_white_cp': aa['target_white_cp']},
                    'b': {**b, 'target_white_cp': bb['target_white_cp']},
                    'target_gap_white_cp': aa['target_white_cp'] - bb['target_white_cp']}
            pairs[partition].append(pair)
            for child, label in ((a, aa), (b, bb)):
                if child['feature_hash'] not in added:
                    partitions[partition].append({**child, 'target_white_cp': label['target_white_cp'],
                                                  'teacher_white_cp': label['target_white_cp'],
                                                  'teacher_stm_cp': label['teacher_stm_cp'],
                                                  'teacher_query_id': label['teacher_query_id'],
                                                  'teacher_batch': 'ordering-current-child'})
                    added.add(child['feature_hash'])
    require(len(pairs['train']) >= 8 and len(pairs['tuning']) >= 5, 'Too few pairs; no automatic retry')
    for p, rows in partitions.items():
        jsonl(output / (p + '.jsonl'), rows)
    for p, rows in pairs.items():
        jsonl(output / ('pairs-' + p + '.jsonl'), rows)
    manifest = {'schema_version': 1, 'rows': {p: len(r) for p, r in partitions.items()},
                'files': {p + '.jsonl': sha(output / (p + '.jsonl')) for p in partitions},
                'pair_rows': {p: len(r) for p, r in pairs.items()},
                'pair_files': {'pairs-' + p + '.jsonl': sha(output / ('pairs-' + p + '.jsonl')) for p in pairs},
                'clear_gap_ge50cp': {p: sum(abs(row['target_gap_white_cp']) >= 50 for row in rows) for p, rows in pairs.items()},
                'background': str(PREVIOUS / 'data-001'), 'anchor': str(ROOT / 'docs/checks/2026-10-22-nnue-diversity/data-001/tuning.jsonl'),
                'protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                'new_teacher_queries': len(requested), 'new_teacher_accepted': len(new_labels),
                'new_teacher_seconds': teacher['elapsed_seconds'],
                'new_warm_searches': sum(not r['reused'] for r in warm_receipts),
                'pair_exclusions': dict(exclusions), 'wall_seconds': time.monotonic() - started,
                'test_limit': 'Previously exposed69 development examples; never gradient or checkpoint selection'}
    require(all(sha(Path(p)) == h for p, h in protocol['input_sha256'].items()), 'Input changed during generation')
    write(output / 'manifest.json', manifest)
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    prepare(parser.parse_args().output.resolve())
