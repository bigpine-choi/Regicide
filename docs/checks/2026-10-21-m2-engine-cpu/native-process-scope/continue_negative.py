"""Continue the original probe's unexecuted negative case, without new product code.

The original positive load and its driver-level FAIL are immutable. The only
audit adapter reads the already serialized summary, exactly as the formal
runner does, avoiding the original diagnostic's tuple-versus-JSON policy input.
"""
import ctypes as C
import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('original_scope_probe', HERE / 'probe.py')
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
ROOT = original.ROOT
original_validator = original.validate_job_receipts
exit_proofs = []
original_popen = original.subprocess.Popen


def diagnostic_worker(folder, lane):
    """An idle own descendant remains observable; no search is requested."""
    gate = folder / f'gate-{lane}.json'
    deadline = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() > deadline:
            raise TimeoutError('own diagnostic worker gate not published')
        time.sleep(.025)
    params = json.loads(gate.read_text())
    if params['worker_pid'] != os.getpid() or params['coordinator_pid'] != os.getppid():
        raise RuntimeError('diagnostic worker gate identity differs')
    engine = None
    report = dict(worker_pid=os.getpid(), coordinator_pid=os.getppid(), lane=lane,
                  status='RUNNING', requested_searches=0)
    try:
        engine = original.chess.engine.SimpleEngine.popen_uci(params['engine_command'], timeout=5.)
        engine.configure(params['engine_options'])
        report['owned_idle_engine_pid'] = engine.transport.get_pid()
        original.write(folder / 'owned-idle-ready.json', report)
        deadline = time.monotonic() + 30
        while not (folder / 'stop-idle.json').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('idle diagnostic stop not published')
            time.sleep(.025)
        report['status'] = 'PASS'
    finally:
        if engine is not None:
            try:
                engine.quit()
            finally:
                engine.close()
        original.write(folder / f'worker-{lane}.json', report)


def gated_diagnostic_popen(command, *args, **kwargs):
    command = list(command)
    if '--worker' in command and str(HERE / 'probe.py') in command:
        command[command.index(str(HERE / 'probe.py'))] = str(Path(__file__).resolve())
    return original_popen(command, *args, **kwargs)


def validate_saved_json(log_path, unused_in_memory_summary, final_job, policy):
    summary = json.loads((Path(log_path).parent / 'load-summary.json').read_text())
    return original_validator(log_path, summary, final_job, policy)


class DiagnosticExitMeter(original.EngineCpuMeter):
    """An extra read/synchronize handle proves our own outside engine exited."""
    def __init__(self, *args, **kwargs):
        self.exit_handle = None
        super().__init__(*args, **kwargs)
        self.api.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]
        self.api.WaitForSingleObject.restype = C.c_uint32
        self.exit_handle = self.api.OpenProcess(0x101000, False, self.pid)
        if not self.exit_handle:
            super().close()
            raise RuntimeError('diagnostic query/synchronize handle unavailable')
        actual_pid = self.api.GetProcessId(self.exit_handle)
        if actual_pid != self.pid:
            self.api.CloseHandle(self.exit_handle)
            self.exit_handle = None
            super().close()
            raise RuntimeError('diagnostic exit handle PID differs')

    def close(self):
        if self.exit_handle is not None:
            from m2_job_accounting import _process_cpu
            handle, self.exit_handle = self.exit_handle, None
            value = dict(pid=self.pid, creation_ticks=self.expected_creation_ticks)
            try:
                value['wait_status'] = int(self.api.WaitForSingleObject(handle, 5000))
                value['final_cpu'] = _process_cpu(self.api, handle, self.pid, self.clock)
                if value['final_cpu']['creation_ticks'] != self.expected_creation_ticks:
                    raise RuntimeError('diagnostic held exit creation differs')
            finally:
                value['handle_closed'] = bool(self.api.CloseHandle(handle))
                exit_proofs.append(value)
                super().close()
            if value['wait_status'] != 0 or not value['handle_closed']:
                raise RuntimeError('own outside diagnostic engine did not exit/close')
        else:
            super().close()

    def measure_play(self, function, *args, **kwargs):
        # This class is installed only in the coordinator, for its own outside
        # engine. Allow the Job child to become observable for >=2 timer samples.
        ready = HERE / 'attempt-002/external-negative/owned-idle-ready.json'
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('owned idle engine did not become ready')
            time.sleep(.025)
        time.sleep(1.2)
        return super().measure_play(function, *args, **kwargs)


def main():
    folder = HERE / 'attempt-002'
    folder.mkdir(exist_ok=False)
    plan = json.loads((ROOT / 'docs/experiments/2026-10-21-m2-engine-cpu-plan.json').read_text())
    profile = original.load_profile(ROOT / 'config/engine-profiles/classical.json')
    params = dict(engine_command=[profile['binary']['path'], *profile['args']],
                  engine_options=original.compare_arguments(profile)['options'])
    sources = [Path(__file__), HERE / 'probe.py', ROOT / 'scripts/m2_job_process_scope.py',
               ROOT / 'scripts/m2_warning_load_guard_v2.py', ROOT / 'scripts/m2_warning_receipts_v2.py',
               ROOT / 'scripts/m2_load_guard.py', ROOT / 'scripts/see_clean_guard.py']
    report = dict(schema_version=1, status='RUNNING', formal_games=0,
                  playing_strength_claim=False,
                  started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  original_driver_sha256=original.sha(HERE / 'probe.py'),
                  preserved_positive='attempt-001/owned-burst/receipt.json',
                  preserved_original_result='attempt-001/receipt.json',
                  positive_read_only_replay='attempt-001/owned-burst/replay-correction.json',
                  positive_load_rerun=False,
                  diagnostic_adapter='Read persisted JSON summary before product raw validation.',
                  source_sha256={str(p.relative_to(ROOT)): original.sha(p) for p in sources})
    original.validate_job_receipts = validate_saved_json
    original.EngineCpuMeter = DiagnosticExitMeter
    original.subprocess.Popen = gated_diagnostic_popen
    try:
        case = original.run_case(folder / 'external-negative', 'external-negative', params,
                                 plan['worker_runtime']['executable'])
        report['case_status'] = case['status']
        report['expected_negative_kind'] = case.get('expected_failure', {}).get('kind')
        report['cleanup'] = case.get('cleanup')
        report['raw_audit'] = case.get('raw_audit')
        ready = json.loads((folder / 'external-negative/owned-idle-ready.json').read_text())
        report['owned_idle_engine_pid'] = ready['owned_idle_engine_pid']
        events = [json.loads(line) for line in (folder / 'external-negative/load.jsonl').read_text().splitlines()]
        owned_pid = ready['owned_idle_engine_pid']
        observations = []
        for event in events:
            scope = event.get('process_ownership_scope', {})
            for query in scope.get('queries', []):
                if query['pid'] == owned_pid:
                    observations.append(dict(in_owned_job_at_read=query['in_owned_job_at_read'],
                                             result=query['result']))
        report['owned_idle_engine_observations'] = observations
        if case['status'] != 'PASS_EXPECTED_EXTERNAL_CPU_FAILURE' or 'raw_audit_error' in case:
            raise RuntimeError('negative guard result or serialized raw validation failed')
        if report['expected_negative_kind'] != 'external_cpu':
            raise RuntimeError('wrong expected negative kind')
        if case['final_job_accounting']['active_processes'] != 0 or not case['job_closed']:
            raise RuntimeError('owned Job cleanup is incomplete')
        if len(exit_proofs) != 1 or exit_proofs[0]['wait_status'] != 0:
            raise RuntimeError('own outside engine held exit proof unavailable')
        if sum(row['in_owned_job_at_read'] for row in observations) < 2:
            raise RuntimeError('idle own engine did not have two positive membership reads')
        report['status'] = 'PASS_EXPECTED_NEGATIVE'
    except BaseException as error:
        report.update(status='FAIL', error=dict(type=type(error).__name__, message=str(error),
                                              traceback=traceback.format_exc()))
        raise
    finally:
        report['outside_exit_proofs'] = exit_proofs
        report['source_sha256_after'] = {str(p.relative_to(ROOT)): original.sha(p) for p in sources}
        report['source_unchanged'] = report['source_sha256'] == report['source_sha256_after']
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        original.write(folder / 'receipt.json', report)


if __name__ == '__main__':
    if '--worker' in sys.argv:
        diagnostic_worker(Path(sys.argv[sys.argv.index('--worker') + 1]),
                          int(sys.argv[sys.argv.index('--lane') + 1]))
    else:
        main()
