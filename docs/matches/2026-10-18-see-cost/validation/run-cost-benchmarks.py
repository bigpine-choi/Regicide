"""Frozen development cost blocks; uses the existing native CPU guard."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import benchmark_search as benchmark
from see_clean_guard import EngineCpuGuard, GuardedEngine, WindowsProcesses

FOLDER = Path(__file__).resolve().parent
freeze = json.loads((ROOT / 'tests/data/see_cost_freeze_2026-10-18.json').read_text(encoding='utf-8'))
subset_path = ROOT / 'build/see-cost-improvement/analysis/development-cost-subset.json'
subset = json.loads(subset_path.read_text(encoding='utf-8'))
cost_positions = {'schema_version': 1, 'source_subset_sha256': benchmark.digest(subset_path),
                  'selection_rule': subset['selection_rule'],
                  'purpose': 'The same five development positions with full move history for the UCI benchmark',
                  'positions': [{'name': row['id'], 'moves': row['opening_uci_moves'],
                                 'expected_fen': row['fen'], 'source_game': row['game'], 'source_ply': row['ply']}
                                for row in subset['positions']]}
for row in cost_positions['positions']:
    assert benchmark.board_for(row).fen(en_passant='fen') == row['expected_fen'], 'History does not recreate selected scene'
cost_path = FOLDER / 'development-cost-benchmark.json'
with cost_path.open('x', encoding='utf-8', newline='\n') as stream:
    stream.write(json.dumps(cost_positions, indent=2) + '\n')
blocks = [('depth3', 'tests/data/profile_positions.json', 3, 5, True),
          ('depth4', 'tests/data/profile_positions.json', 4, 5, True),
          ('see-off-depth4', 'tests/data/profile_positions.json', 4, 1, False),
          ('loss-scenes-depth4', str(cost_path), 4, 3, True)]
for name, positions, depth, repeats, enabled in blocks:
    print('START ' + name, flush=True)
    output = FOLDER / (name + '.json')
    monitor_output = FOLDER / (name + '.monitor.json')
    assert not output.exists() and not monitor_output.exists(), 'Preserve earlier complete or failed blocks'
    options = json.dumps({'Hash': 16, 'Move Overhead': 20, 'PVS': True, 'UseSEE': enabled})
    argv = [freeze['baseline']['binary'], freeze['candidate']['binary'], '--output', str(output),
            '--positions', positions, '--depth', str(depth), '--repeats', str(repeats),
            '--warmup', '1', '--seed', '20261020', '--require-same-search',
            '--baseline-commit', freeze['baseline']['commit'], '--candidate-commit', freeze['candidate']['commit'],
            '--baseline-options', options, '--candidate-options', options]
    args = benchmark.arguments(argv)
    guard = EngineCpuGuard(WindowsProcesses(), FOLDER / (name + '.monitor.jsonl'))
    result = None
    failure = None
    def launch(command):
        guard.assert_clean()
        roles = [role for role in ('baseline', 'candidate') if Path(command[0]).resolve() == getattr(args, role)]
        assert len(roles) == 1, 'Unrecognized measured engine'
        engine = benchmark.chess.engine.SimpleEngine.popen_uci(command)
        try:
            guard.register_transport(engine.transport.get_pid(), roles[0], command)
        except BaseException:
            engine.quit()
            engine.close()
            raise
        return GuardedEngine(engine, guard)
    try:
        guard.start()
        guard.require_idle(10)
        with output.open('x', encoding='utf-8', newline='\n') as stream:
            result = benchmark.run(args, stream, engine_factory=launch)
        guard.assert_clean()
        guard.assert_transports()
        assert result['exit_code'] == 0, 'Search graph or benchmark failure'
    except BaseException as error:
        failure = repr(error)
    finally:
        guard.close()
        guard.summary.update(error=failure, workload_kind='same-search fixed-depth cost, not a strength test',
                             valid_for_comparison=failure is None and not guard.summary['contaminated'],
                             benchmark_arguments=argv)
        with monitor_output.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(guard.summary, indent=2) + '\n')
    print(json.dumps({'block': name, 'failure': failure, 'valid': guard.summary['valid_for_comparison'],
                      'summary': result['summary'] if result else None}), flush=True)
    if failure or not guard.summary['valid_for_comparison']:
        raise SystemExit(1)
