"""Offline compatibility/mutation checks for the independent v5 Job-scope audit.

Product fixture builders provide explicit synthetic data only. The production
independent auditor imports none of those product modules.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'scripts'))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
old=load('old_offline_peer_fixtures',HERE/'peer_cpu_fixture_tests.py')
audit=load('independent_v5_peer',HERE/'peer_game_audit_engine_cpu_v5.py')
from m2_engine_identity_v2_tests import build_scope_fixture
from m2_engine_identity_v2 import validate_resolution_scope


def integrated(directory,child=False):
    args=list(old.integrated_fixture(directory,child));metadata=args[0];worker=(900,900)
    args[5]=worker;args[6]={key:worker for key in args[6]}
    for entry in metadata['engine_cpu_processes']:
        resolution=build_scope_fixture(entry['endpoint_resolution'],worker_pid=worker[0],coordinator_pid=800)
        scope=resolution['endpoint_job_scope']
        for key in ('worker_identity','worker_identity_after'):scope[key]['creation_ticks']=worker[1]
        for key in ('membership_candidates','membership_candidates_after'):
            for row in scope[key]:
                if row['pid']==entry['transport_pid']:row['parent_creation_ticks']=worker[1]
        entry['endpoint_resolution']=resolution
    args[0]=json.loads(json.dumps(metadata))
    old.write_fixture_stream(args[0],args[1],directory)
    return args


def refresh_snapshot(snapshot,ids):
    ids=sorted(ids);snapshot['pids']=ids
    snapshot['assigned_processes']=snapshot['listed_processes']=len(ids)
    snapshot['returned_bytes']=8+snapshot['pointer_bytes']*len(ids)
    final=snapshot['query_attempts'][-1]
    final.update(pids=ids,assigned_processes=len(ids),listed_processes=len(ids),returned_bytes=snapshot['returned_bytes'])


def outside(resolution):
    scope=resolution['endpoint_job_scope'];parent=resolution['transport']['pid']
    for inventory in scope['global_inventories']:inventory.append({'pid':1396,'parent_pid':parent})
    scope['outside_job_edges'].append({'pid':1396,'parent_pid':parent,'in_job_before':False,
        'in_job_after':False,'observed_in_first':True,'observed_in_second':True,
        'reason':'outside-current-immediate-job'})


def stale(resolution):
    scope=resolution['endpoint_job_scope'];parent=resolution['transport']['pid'];created=resolution['transport']['creation_ticks']
    row={'pid':99,'creation_ticks':created-1,'parent_pid':parent,'parent_creation_ticks':created,
         'wait_status':258,'in_any_job':True,'read_started_monotonic':0.,'read_finished_monotonic':0.}
    for inventory in scope['global_inventories']:inventory.append({'pid':99,'parent_pid':parent})
    for snapshot in scope['job_snapshots']:refresh_snapshot(snapshot,snapshot['pids']+[99])
    scope['membership_candidates'].append(copy.deepcopy(row));scope['membership_candidates_after'].append(copy.deepcopy(row))
    scope['stale_job_edges'].append({'pid':99,'parent_pid':parent,'parent_creation_ticks':created,
        'child_identity':copy.deepcopy(row),'child_identity_after':copy.deepcopy(row),
        'reason':'creation-precedes-held-parent'})


class IndependentJobScopeTests(unittest.TestCase):
    def test_native_and_sunfish_full_cpu_fixture_match_raw_scope_and_scalar_summary(self):
        for child in (False,True):
            with self.subTest(child=child),tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);args=integrated(d,child);result=audit.engine_cpu_records(*args,d)
                self.assertEqual(result['searches'],4)
                self.assertEqual(result['raw_recomputed_summary'],args[0]['engine_cpu_summary'])
                expected=[validate_resolution_scope(row['endpoint_resolution'],expected_worker_pid=args[5][0])
                          for row in args[0]['engine_cpu_processes']]
                self.assertEqual(result['job_scope_audits'],expected)
    def test_outside_and_stale_positive_proofs_match_product_summary(self):
        for mutation in (outside,stale):
            with self.subTest(mutation=mutation.__name__),tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);args=integrated(d,True);resolution=args[0]['engine_cpu_processes'][0]['endpoint_resolution']
                mutation(resolution);old.write_fixture_stream(args[0],args[1],d)
                actual=audit.engine_cpu_records(*args,d)['job_scope_audits'][0]
                self.assertEqual(actual,validate_resolution_scope(resolution,expected_worker_pid=args[5][0]))
                key='excluded_outside_job_edges' if mutation==outside else 'excluded_stale_job_edges'
                self.assertEqual(actual[key],1)
    def test_other_lane_job_membership_churn_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);args=integrated(d);resolution=args[0]['engine_cpu_processes'][0]['endpoint_resolution'];scope=resolution['endpoint_job_scope']
            refresh_snapshot(scope['job_snapshots'][1],scope['job_snapshots'][1]['pids']+[777])
            scope['global_inventories'][1].append({'pid':777,'parent_pid':999})
            old.write_fixture_stream(args[0],args[1],d)
            self.assertEqual(audit.engine_cpu_records(*args,d)['job_scope_audits'][0]['status'],'PASS')
    def test_missing_scope_breakaway_incomplete_list_wrong_worker_and_query_attempt_rejected(self):
        for key in ('breakaway','missing_pid','wrong_worker','no_attempts','pointer','projection'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);args=integrated(d);resolution=args[0]['engine_cpu_processes'][0]['endpoint_resolution'];scope=resolution['endpoint_job_scope']
                if key=='breakaway':scope['job_snapshots'][0]['limit_flags']=0x800
                elif key=='missing_pid':refresh_snapshot(scope['job_snapshots'][1],[scope['worker_pid']])
                elif key=='wrong_worker':scope['worker_identity']['creation_ticks']+=1
                elif key=='no_attempts':scope['job_snapshots'][0]['query_attempts']=[]
                elif key=='pointer':scope['job_snapshots'][0]['pointer_bytes']=16
                else:resolution['matching_inventories'][0]['999']={'pid':999,'parent_pid':101}
                old.write_fixture_stream(args[0],args[1],d)
                with self.assertRaises(Exception):audit.engine_cpu_records(*args,d)
    def test_forged_outside_and_stale_positive_evidence_rejected(self):
        for kind in ('outside_membership','outside_omitted','stale_creation'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);args=integrated(d);resolution=args[0]['engine_cpu_processes'][0]['endpoint_resolution'];scope=resolution['endpoint_job_scope']
                if kind.startswith('outside'):
                    outside(resolution)
                    if kind=='outside_membership':scope['outside_job_edges'][0]['in_job_before']=True
                    else:scope['outside_job_edges']=[]
                else:
                    stale(resolution);scope['stale_job_edges'][0]['parent_creation_ticks']=1
                old.write_fixture_stream(args[0],args[1],d)
                with self.assertRaises(Exception):audit.engine_cpu_records(*args,d)


if __name__=='__main__':unittest.main()
