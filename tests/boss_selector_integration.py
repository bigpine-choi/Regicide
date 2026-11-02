"""Native boss selector and online observation diagnostic, in isolated games.

Deterministic seeds select open/closed policy draws, never a candidate move.
Synthetic full legal histories exercise production HumanGame.engine_move.
This is implementation evidence, not human calibration or playing strength.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import boss_opponent
import boss_play
from runtime_engine import resolve_runtime_engine
import human_play_tests as harness


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixtures():
    legal_lure = 'e2e4 e7e5 g1f3 b8c6 f1c4 d7d6 b1c3 c8g4'.split()
    return [
        ('initial-conservative', [], False, 'initial'),
        ('queen-lure-held', legal_lure, False, 'held'),
        ('queen-lure-open-draw', legal_lure, True, 'lure'),
        ('human-queen-error', 'e2e4 d7d5 e4d5 d8d5 b1c3 d5c4'.split(), False, 'error'),
    ]


def seed_for(board, admit):
    # Stay beyond the entire adaptive frequency range for a held draw, and
    # below its floor for an admitted draw. This is explicit fixture selection.
    for number in range(10000):
        seed = f'{number:032x}'
        draw = boss_opponent.gate(board, [], seed)['draw']
        if (admit and draw < .01) or (not admit and draw > .95):
            return seed
    raise AssertionError('No fixture seed found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', dest='cases')
    args = parser.parse_args()
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    sources = ['scripts/boss_play.py', 'scripts/boss_opponent.py', 'scripts/poisoned_piece.py',
               'scripts/boss_openings.py', 'scripts/play_human.py', 'tests/boss_selector_integration.py']
    report = {
        'schema': 'regicide.boss.native-adaptive-selector.v1',
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'source_base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_sha256': {path: sha(ROOT / path) for path in sources},
        'binary': str(binary), 'binary_sha256': sha(binary),
        'python': platform.python_version(), 'python_chess': chess.__version__, 'platform': platform.platform(),
        'policy': boss_play.POLICY, 'opponent_policy': boss_opponent.POLICY,
        'execution': 'Real own engine through HumanGame.engine_move; isolated temporary saves; fake UI helper engines only.',
        'configured_options': {'UsePieceActivity': True}, 'movetime_ms': 1000,
        'seed_selection': 'Chosen by deterministic gate draw only, before engine search. No candidate or proof is forced.',
        'limitations': ['Authored development fixtures, not played games, held-out human data, Elo or calibrated probabilities.',
                        'A rare admitted initial draw does not imply frequent risky play against a new opponent.',
                        'Search scores and observations are own-engine estimates under short budgets.'],
        'cases': []}
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    for case_id, history, admit, criterion in fixtures():
        if args.cases and case_id not in args.cases:
            continue
        row = {'id': case_id, 'history_uci': history, 'criterion': criterion, 'expected_gate_open': admit}
        game = None
        try:
            with tempfile.TemporaryDirectory(prefix='regicide-native-boss-') as temporary:
                helpers = harness.FakeFactory()
                game = harness.play_human.HumanGame(
                    binary, Path(temporary),
                    engine_factory=lambda path: chess.engine.SimpleEngine.popen_uci([str(path), 'uci'], timeout=10, **flags),
                    hint_engine_factory=helpers, evaluation_engine_factory=helpers)
                game.new_game('black', 1000, game.revision, True)
                board = chess.Board()
                for uci in history:
                    board.push_uci(uci)
                game.board = board.copy(stack=True)
                game._opening_seed = seed_for(board, admit)
                game.revision += 1
                game._sync_evaluation()
                row.update(before_fen=board.fen(en_passant='fen'), seed=game._opening_seed)
                started = time.monotonic()
                state = game.engine_move(game.revision)
                row['wall_ms'] = round((time.monotonic() - started) * 1000, 3)
                decision = game._boss_decisions.get(len(history) + 1, {})
                row.update(error=state['error'], selected_uci=state['last_move'], receipt=decision)
                selected = chess.Move.from_uci(state['last_move']) if state.get('last_move') else None
                checks = {'no_error': state['error'] is None, 'legal_move': selected in board.legal_moves,
                          'policy_recorded': decision.get('policy') == boss_play.POLICY,
                          'gate_expected': decision.get('frequency', {}).get('attempt_trap') is admit}
                if criterion in ('held', 'error'):
                    checks['baseline_preserved'] = decision.get('selected_uci') == decision.get('baseline_uci')
                if criterion == 'initial':
                    profile = decision.get('frequency', {}).get('profile', {})
                    checks['strong_prior'] = profile.get('quality_observations') == 0 and profile.get('frequency') == .1
                    checks['gambit_setup_selected'] = (decision.get('selected_uci') == decision.get('candidate_uci')
                                                       and bool(decision.get('line_id')))
                    checks['conservative_setup'] = (decision.get('conservative_setup') is True
                                                     and decision.get('loss_cp', 1000) <= boss_play.SETUP_MAX_LOSS_CP)
                if criterion == 'lure':
                    evidence = decision.get('trap_evidence') or {}
                    checks['verified_lure'] = evidence.get('verified') is True
                    checks['speculative_selection'] = decision.get('selected_uci') != decision.get('baseline_uci')
                if criterion == 'error':
                    observation = decision.get('opponent_observation') or {}
                    profile = decision.get('frequency', {}).get('profile', {})
                    checks['error_observed'] = observation.get('valid') is True and (observation.get('loss_cp') or 0) >= 150
                    checks['frequency_raised'] = profile.get('quality_observations') == 1 and profile.get('frequency', 0) > .1
                row.update(checks=checks, passed=all(checks.values()))
                game.close()
                game = None
        except Exception as error:
            row.update(passed=False, exception=f'{type(error).__name__}: {error}')
        finally:
            if game is not None:
                game.close()
        report['cases'].append(row)
        print(json.dumps({'id': case_id, 'passed': row['passed'], 'move': row.get('selected_uci'),
                          'checks': row.get('checks'), 'exception': row.get('exception')}, ensure_ascii=False), flush=True)
    report['passed'] = sum(row['passed'] for row in report['cases'])
    report['failed'] = len(report['cases']) - report['passed']
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    report['sources_unchanged'] = all(sha(ROOT / path) == digest for path, digest in report['source_sha256'].items())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    return 1 if report['failed'] or not report['sources_unchanged'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
