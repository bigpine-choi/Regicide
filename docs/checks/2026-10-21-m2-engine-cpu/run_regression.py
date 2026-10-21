"""Persist offline regression results; native diagnostics are separate receipts."""
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]


def main():
    modules = [('m2_engine_cpu_tests', 'EngineCpuTests'),
               ('m2_engine_identity_tests', None),
               ('m2_engine_receipts_tests', None),
               ('m2_engine_tracking_tests', None),
               ('m2_warning_load_guard_tests', None),
               ('m2_formal_v4_tests', None)]
    suite = unittest.TestSuite()
    sources = {}
    for name, selected in modules:
        module = importlib.import_module(name)
        target = getattr(module, selected) if selected else module
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(target) if selected
                       else unittest.defaultTestLoader.loadTestsFromModule(target))
        path = Path(module.__file__)
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    import m2_formal_v4
    for name in m2_formal_v4.HARNESS:
        path = ROOT/'scripts'/name
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    started = time.monotonic()
    with (HERE/'regression.log').open('x', encoding='utf-8', newline='\n') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {'status':'PASS' if result.wasSuccessful() else 'FAIL',
              'tested_utc':datetime.now(timezone.utc).isoformat(),
              'tests_run':result.testsRun, 'failures':len(result.failures),
              'errors':len(result.errors), 'skipped':len(result.skipped),
              'elapsed_seconds':time.monotonic()-started,
              'native_diagnostics_rerun':False,
              'scope':'Offline fixtures and mocked process control only; native accounting and endpoint checks are separate preserved receipts, not formal games.',
              'source_sha256':sources,
              'failure_details':[(str(test), detail) for test, detail in result.failures+result.errors]}
    with (HERE/'regression.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps({key:report[key] for key in ('status','tests_run','failures','errors','skipped')}))
    return 0 if result.wasSuccessful() else 2


if __name__ == '__main__':
    raise SystemExit(main())
