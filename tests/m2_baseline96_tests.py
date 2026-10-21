"""Small synthetic adapter checks; no engine, native API or full-game replay."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import m2_baseline96 as baseline


class SelectionTests(unittest.TestCase):
    def plan(self):
        return {'baseline96_selection': dict(baseline.SELECTION), 'preflight_transfer': {
            'directory': str(ROOT/'synthetic-parent'),
            'peer_audit': {'path': 'peer.json', 'sha256': 'a'*64},
            'product_audit': {'path': 'product.json', 'sha256': 'b'*64}}}

    def test_original_full_validation_precedes_fixed_subset_without_mutation(self):
        rows = [{'family_id': str(i), 'cluster_id': str(i//2)} for i in range(32)]
        banks = {'main': (Path('full.json'), rows), 'preflight': (Path('pre.json'), [{'family_id': 'pre'}])}
        with patch.object(baseline, 'ORIGINAL_LOAD_PLAN', return_value=(self.plan(), {}, banks)) as original:
            _, _, selected = baseline.load_plan('plan.json')
        original.assert_called_once_with('plan.json')
        self.assertEqual(selected['main'][1], rows[:4])
        self.assertEqual(len(banks['main'][1]), 32)
        cells = baseline.formal.cell_schedule(['a','b','c'], ['x','y'], [100,1000], selected['main'][1], 'main')
        self.assertEqual(len(cells), 48)
        self.assertEqual(len({c['candidate'] for c in cells}), 3)

    def test_original_validation_failure_is_not_bypassed(self):
        with patch.object(baseline, 'ORIGINAL_LOAD_PLAN', side_effect=ValueError('bad family 32')):
            with self.assertRaisesRegex(ValueError, 'family 32'):
                baseline.load_plan('plan.json')

    def test_nonfixed_selection_or_unfinished_preflight_refs_rejected(self):
        for key, value in (('baseline96_selection', dict(baseline.SELECTION, main_games=98)),
                ('preflight_transfer', {'directory': str(ROOT), 'peer_audit': None, 'product_audit': None})):
            plan = self.plan(); plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                baseline.selection(plan)

    def test_freeze_temporarily_adds_only_adapter_and_restores_original_module(self):
        original_harness, original_loader = baseline.formal.HARNESS, baseline.formal.load_plan
        def fake_freeze(path, directory):
            self.assertEqual(baseline.formal.HARNESS, original_harness + ('m2_baseline96.py',))
            self.assertIsNot(baseline.formal.load_plan, original_loader)
            return {'cells': {'main': [None]*48}}
        with patch.object(baseline.formal, 'freeze_plan', side_effect=fake_freeze), patch.object(baseline, 'verify'):
            baseline.freeze_plan('plan.json', 'new-directory')
        self.assertIs(baseline.formal.HARNESS, original_harness)
        self.assertIs(baseline.formal.load_plan, original_loader)

    def test_same_count_substitution_of_a_different_family_is_rejected(self):
        full = [{'family_id':str(i)} for i in range(32)]
        frozen = {'plan':dict(self.plan(), openings={'path':'full.json','sha256':'a'*64}),
            'source_plan_path':str(ROOT/'synthetic-plan.json'),
            'harness_source_sha256':{str(ROOT/'scripts'/name):'a'*64 for name in baseline.HARNESS},
            'banks':{'main':{'path':'selected.json'}}, 'cells':{'main':[None]*48,'preflight':[None]*12}}
        with patch.object(baseline.formal,'verify_frozen',return_value=(frozen,{})), \
                patch.object(baseline.formal.pilot,'referenced_file',return_value=Path('full.json')), \
                patch.object(baseline.formal.comparison,'load_openings',side_effect=[(None,full),(None,full[1:5])]):
            with self.assertRaisesRegex(ValueError,'original first four'):
                baseline.verify(ROOT/'synthetic-new')

    def test_actual_json_string_bank_path_is_converted_to_path_for_reader(self):
        source = ROOT/'tests/data/m2_formal_openings_2026-10-21.json'
        bank = json.loads(source.read_text(encoding='utf-8-sig'))
        bank['openings'] = bank['openings'][:4]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            copied = directory/'main-openings.json'
            copied.write_text(json.dumps(bank),encoding='utf-8')
            frozen = {'plan':dict(self.plan(), openings={'path':str(source),'sha256':baseline.formal.sha256(source)}),
                'source_plan_path':str(directory/'plan.json'),
                'harness_source_sha256':{str(ROOT/'scripts'/name):'a'*64 for name in baseline.HARNESS},
                'banks':{'main':{'path':str(copied)}}, 'cells':{'main':[None]*48,'preflight':[None]*12}}
            with patch.object(baseline.formal,'verify_frozen',return_value=(frozen,{})):
                checked,_ = baseline.verify(directory)
            self.assertIs(checked,frozen)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.parent, self.target = self.base/'parent', self.base/'new'
        self.parent.mkdir(); self.target.mkdir()
        cells = [{'id': f'preflight-{i:04d}'} for i in range(1,13)]
        plan = {key: key+'-same' for key in baseline.PRECONDITIONS}
        plan.update(id='source-v6', openings={'path':'full32.json','sha256':'f'*64})
        sources = {str(ROOT/'scripts'/name): 'a'*64 for name in baseline.V6_HARNESS}
        self.old = {'plan':plan, 'source_plan_sha256':'d'*64, 'harness_source_sha256':sources,
            'banks': {'preflight': {'sha256':'p'*64}}, 'profiles': {'candidate':{'sha256':'c'*64}},
            'cells': {'preflight':cells}}
        raw_names = ['frozen-plan.json','seal.json','source-plan.json',
            'preflight/load.jsonl','preflight/job-final-accounting.json']
        raw_names += ['preflight/cells/'+cell['id']+'/'+name for cell in cells
            for name in ('games.json','games.pgn','engine-cpu.jsonl','invocation.json')]
        for name in raw_names:self.write(self.parent/name, {})
        self.guard = {'valid_for_load_check':True}
        self.write(self.parent/'preflight/load-summary.json', self.guard)
        self.write(self.parent/'preflight/report.json', {'status':'COMPLETE','valid_for_comparison':True,
            'completed_games':24,'scheduled_games':24,'frozen_plan_sha256':baseline.formal.sha256(self.parent/'frozen-plan.json')})
        self.product = {'status':'PASS','phase':'preflight','phase_status':'COMPLETE',
            'valid_for_comparison':True,'audited_pairs':12,'scheduled_pairs':12,'guard_summary':self.guard,
            'raw_cpu_audit':{'status':'PASS','valid_for_load_check':True}}
        self.product_path = self.base/'product.json'; self.write(self.product_path,self.product)
        self.peer = {'status':'PASS','phase':'preflight','directory':str(self.parent),'plan_id':'source-v6',
            'valid_for_declared_local_comparison':True,'scheduled_pairs':12,'audited_pairs':12,
            'scheduled_games':24,'completed_games':24,'incomplete_games':0,'errors':0,
            'input_sha256':{str(p.resolve()):baseline.formal.sha256(p) for p in self.parent.rglob('*') if p.is_file()}}
        self.peer_path = self.base/'peer.json'; self.write(self.peer_path,self.peer)
        self.new = deepcopy(self.old)
        self.new.update(source_plan_path=str(self.base/'new-plan.json'), source_plan_sha256='e'*64)
        self.new['harness_source_sha256'][str(ROOT/'scripts/m2_baseline96.py')]='b'*64
        self.new['plan']['preflight_transfer']={'directory':str(self.parent),
            'peer_audit':self.ref(self.peer_path),'product_audit':self.ref(self.product_path)}
        self.write(self.target/'frozen-plan.json',{})
        self.verify_patch = patch.object(baseline, 'verify', return_value=(self.new,{}));self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)
        self.old_patch = patch.object(baseline.formal,'verify_frozen',return_value=(self.old,{}));self.old_patch.start()
        self.addCleanup(self.old_patch.stop)

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')

    def ref(self,path):
        return {'path':str(path),'sha256':baseline.formal.sha256(path)}

    def audit(self):
        return baseline.verify_transfer(self.target)

    def test_transfer_passes_without_replaying_parent_product_audit(self):
        with patch.object(baseline,'ORIGINAL_AUDIT',side_effect=AssertionError('must not replay')):
            result=self.audit()
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['new_main_games'],96)
        self.assertEqual(result['verified_runtime_sources'],33)

    def test_changed_original_runtime_or_precondition_rejected(self):
        key=next(iter(self.new['harness_source_sha256']))
        self.new['harness_source_sha256'][key]='0'*64
        with self.assertRaisesRegex(ValueError,'33 runtime'):self.audit()
        self.new['harness_source_sha256'][key]='a'*64
        self.new['plan']['move_deadline']='changed'
        with self.assertRaisesRegex(ValueError,'preflight conditions'):self.audit()

    def test_changed_peer_raw_input_or_incomplete_audit_rejected(self):
        self.write(self.parent/'preflight/cells/preflight-0001/games.pgn',{'changed':True})
        with self.assertRaisesRegex(ValueError,'inputs changed'):self.audit()
        self.write(self.parent/'preflight/cells/preflight-0001/games.pgn',{})
        self.product['valid_for_comparison']=False;self.write(self.product_path,self.product)
        self.new['plan']['preflight_transfer']['product_audit']=self.ref(self.product_path)
        with self.assertRaisesRegex(ValueError,'product preflight'):self.audit()

    def test_gate_applies_only_to_exact_target_preflight_and_never_reruns_it(self):
        transfer=self.audit()
        def fake_run(directory,phase):
            self.assertEqual((Path(directory),phase),(self.target,'main'))
            self.assertEqual(baseline.formal.audit_phase(directory,'preflight'),transfer)
            self.assertEqual(baseline.formal.audit_phase(self.parent,'preflight'),{'original':True})
            self.assertEqual(baseline.formal.audit_phase(directory,'main'),{'original':True})
            return {'status':'COMPLETE'}
        with patch.object(baseline.formal,'run_phase',side_effect=fake_run), \
                patch.object(baseline,'ORIGINAL_AUDIT',return_value={'original':True}) as original:
            self.assertEqual(baseline.run_phase(self.target),{'status':'COMPLETE'})
            self.assertEqual(original.call_count,2)
        self.assertTrue((self.target/'preflight-transfer.json').is_file())
        self.assertFalse((self.target/'preflight').exists())


if __name__ == '__main__':
    unittest.main()
