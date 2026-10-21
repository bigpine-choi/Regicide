"""Offline mutation checks for the independent per-process CPU auditor."""
import copy
import importlib.util
import json
import hashlib
import sys
import tempfile
from pathlib import Path
import unittest

path=Path(__file__).with_name('peer_game_audit_engine_cpu.py')
spec=importlib.util.spec_from_file_location('peer_cpu_audit',path)
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


def receipt(delta=10000000):
    def raw(user,begin,end):
        return {'pid':42,'creation_ticks':100,'user_ticks':user,'kernel_ticks':0,
                'cpu_ticks':user,'read_started_monotonic':begin,'read_finished_monotonic':end}
    return {'schema_version':1,'sequence':1,'status':'OK','outcome':'complete',
            'scope':'actual UCI transport process, all threads; descendants excluded',
            'ratio_is_diagnostic_only':True,'before':raw(50,0.,1.),'after':raw(50+delta,5.,6.),
            'call_started_monotonic':2.,'call_finished_monotonic':4.,
            'response_elapsed_seconds':2.,'kernel_delta_ticks':0,'user_delta_ticks':delta,
            'cpu_delta_ticks':delta,'kernel_seconds':0.,'user_seconds':delta/1e7,
            'cpu_seconds':delta/1e7,'cpu_per_wall_ratio':delta/1e7/2}


def integrated_fixture(directory,child=False):
    # The production test helper supplies explicitly synthetic scalar records;
    # the auditor itself never imports a product validator or this helper.
    root=Path(__file__).resolve().parents[3]
    sys.path.insert(0,str(root/'tests'))
    from m2_engine_receipts_tests import fixture
    from m2_engine_receipts import validate_cell_cpu
    metadata,cell=fixture(child);cell['id']='synthetic-offline-0001'
    binary=Path(sys.executable).resolve();digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    profiles=[{'id':'synthetic-'+role,'command':[str(binary)],'binary':{'sha256':digest}}
              for role in ('baseline','candidate')]
    plan=json.loads((root/'docs/experiments/2026-10-21-m2-engine-cpu-plan.json').read_text(encoding='utf-8'))
    runtime=plan['worker_runtime']['executable']
    if child:
        wrapper=root/'build-m2-opponents/sunfish/runtime/python.exe'
        script=root/'build-m2-opponents/sunfish/source/sunfish.py'
        profiles[0]={'id':'sunfish-e396948-cpython313','command':[str(wrapper),'-u',str(script)],
                     'binary':{'sha256':hashlib.sha256(wrapper.read_bytes()).hexdigest()}}
    owner=(900,9000);observed={}
    for entry,profile in zip(metadata['engine_cpu_processes'],profiles,strict=True):
        pid,creation=entry['transport_pid'],entry['transport_creation_ticks']
        binary=Path(profile['command'][0]);digest=profile['binary']['sha256']
        metadata['engines'][entry['role']].update(sha256=digest,command=profile['command'])
        actual=(entry['baseline']['pid'],entry['baseline']['creation_ticks']);observed[actual]=owner
        image={'pid':pid,'creation_ticks':creation,'parent_pid':owner[0],
               'image_path':str(binary),'image_sha256':digest,'wait_status':258,
               'liveness_method':'nonsignaled-held-process-object',
               'read_started_monotonic':0.,'read_finished_monotonic':0.}
        endpoint=copy.deepcopy(image)
        use_child=child and entry['role']=='baseline'
        if use_child:
            endpoint.update(pid=actual[0],creation_ticks=actual[1],parent_pid=pid,
                            image_path=runtime['path'],image_sha256=runtime['sha256'])
        inventory={str(pid):{'pid':pid,'parent_pid':owner[0]}}
        if use_child:inventory[str(actual[0])]={'pid':actual[0],'parent_pid':pid}

        entry['endpoint_resolution']={'schema_version':1,
            'method':'frozen-image-and-held-creation-with-toolhelp-parent',
            'profile_id':profile['id'],'mode':'sunfish-venv-child' if use_child else 'native-direct','transport':copy.deepcopy(image),
            'endpoint':copy.deepcopy(endpoint),'transport_after':copy.deepcopy(image),
            'endpoint_after':copy.deepcopy(endpoint),'frozen_binary':{'path':str(binary),'sha256':digest},
            'frozen_script':({'path':str(script),'sha256':hashlib.sha256(script.read_bytes()).hexdigest()} if use_child else None),
            'expected_base_runtime':runtime if use_child else None,'observed_direct_children':[actual[0]] if use_child else [],
            'matching_inventories':[copy.deepcopy(inventory),copy.deepcopy(inventory)],
            'expected_console_helper':None,'console_helpers':[],'console_helpers_after':[],
            'query_access_mask':0x101000,'read_started_monotonic':0.,'read_finished_monotonic':0.}
    metadata['engine_cpu_summary']=validate_cell_cpu(metadata,cell)
    write_fixture_stream(metadata,cell,directory)
    return metadata,cell,profiles[1],profiles[0],{'move_deadline':{'multiplier':2.,'slack_seconds':.25},'worker_runtime':{'executable':runtime}},owner,observed


def write_fixture_stream(metadata,cell,directory):
    rows=[{'event':'policy','record':{'schema_version':1,'cell_id':cell['id'],
          'scope':'Actual verified UCI endpoint, all threads; launcher/bootstrap CPU remains in the full Job.',
          'cpu_ratio_is_diagnostic_only':True}}]
    for entry in metadata['engine_cpu_processes']:
        initial=copy.deepcopy(entry);initial['final']=None;initial['meter_closed']=False
        rows.append({'event':'endpoint','record':initial})
    rows += [{'event':'search','record':row} for row in metadata['engine_cpu_trace']]
    rows += [{'event':'process_final','record':row} for row in metadata['engine_cpu_processes']]
    p=directory/'engine-cpu.jsonl';p.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    metadata['engine_cpu_trace_file']={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}


class IndependentCpuChecks(unittest.TestCase):
    def check_valid(self,r):
        return audit.process_cpu_receipt(r,(42,100))
    def test_full_product_fixture_scalar_summary_and_raw_stream_match_independent(self):
        for child in (False,True):
            with self.subTest(child=child),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);args=integrated_fixture(directory,child)
                result=audit.engine_cpu_records(*args,directory)
                self.assertEqual(result['searches'],4)
                self.assertEqual(result['raw_recomputed_summary']['total_cpu_ticks'],468750)
                self.assertEqual(result['raw_recomputed_summary'],args[0]['engine_cpu_summary'])
    def test_integrated_wrong_parent_or_summary_rejected(self):
        for mutation in ('parent','summary'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);args=integrated_fixture(directory);metadata=args[0]
                if mutation=='parent':metadata['engine_cpu_processes'][0]['endpoint_resolution']['transport']['parent_pid']=901
                else:metadata['engine_cpu_summary']['total_cpu_ticks']+=1
                write_fixture_stream(metadata,args[1],directory)
                with self.assertRaises(Exception):audit.engine_cpu_records(*args,directory)
    def test_integrated_raw_stream_order_or_search_loss_rejected(self):
        for mutation in ('order','missing'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);args=integrated_fixture(directory);metadata=args[0]
                p=directory/'engine-cpu.jsonl';rows=[json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()]
                if mutation=='order':rows[1],rows[-1]=rows[-1],rows[1]
                else:rows.pop(3)
                p.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
                metadata['engine_cpu_trace_file']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
                with self.assertRaises(Exception):audit.engine_cpu_records(*args,directory)
    def test_decoded_warning_scope_text(self):
        audit.equal_number(json.loads('"warning-only"'),'warning-only','warning text')
        with self.assertRaises(Exception):audit.equal_number('warning-only','stop','warning text')
    def test_recomputes_cpu_and_wall(self):
        result=self.check_valid(receipt())
        self.assertEqual(result['cpu_seconds'],1.)
        self.assertEqual(result['response_elapsed_seconds'],2.)
    def test_zero_cpu_is_valid(self):
        self.assertTrue(self.check_valid(receipt(0))['zero_cpu'])
    def test_ratio_above_one_is_valid(self):
        self.assertEqual(self.check_valid(receipt(30000000))['cpu_seconds'],3.)
    def test_wrong_delta_rejected(self):
        r=receipt();r['cpu_delta_ticks']+=1
        with self.assertRaises(Exception):self.check_valid(r)
    def test_wrong_identity_rejected(self):
        r=receipt();r['after']['creation_ticks']+=1
        with self.assertRaises(Exception):self.check_valid(r)
    def test_clock_order_rejected(self):
        r=receipt();r['call_finished_monotonic']=5.5
        with self.assertRaises(Exception):self.check_valid(r)
    def test_cpu_regression_rejected(self):
        r=receipt();r['before'].update(user_ticks=10000051,cpu_ticks=10000051)
        with self.assertRaises(Exception):self.check_valid(r)
    def test_unknown_start_sequence_rejected(self):
        r=receipt();r['sequence']=2
        with self.assertRaises(Exception):self.check_valid(r)

if __name__=='__main__':unittest.main()
