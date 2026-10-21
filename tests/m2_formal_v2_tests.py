"""Offline control/evidence checks; no real process is started or stopped."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
import m2_formal_v2 as formal
import m2_formal_summary_v2 as statistics
import external_baseline_matrix_tests as fixtures
from m2_job_receipts_tests import make_clean_fixture


class FormalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.MatrixTests.setUpClass()

    def setUp(self):
        self.fixture = fixtures.MatrixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory, _, self.profiles = self.fixture.freeze()
        _, rows = formal.comparison.load_openings(self.fixture.opening_path)
        rows[0].update(family_id='fixture-family', cluster_id='fixture-cluster')
        self.cell = dict(id='main-0001', lane=0, candidate='classical-a', opponent='opponent',
                         movetime_ms=50, opening_index=0, family_id='fixture-family', cluster_id='fixture-cluster')
        self.bank = self.directory/'main-openings.json'
        formal.write_json(self.bank, {'schema_version':1, 'openings':rows})
        self.frozen = {'plan':{'max_plies':100,'lane_affinity_masks':[15,240,3840,61440]},
                       'banks':{'main':{'path':str(self.bank)}},
                       'git_receipts':[{'arguments':['rev-parse','HEAD'],'stdout':self.fixture.commit,'exit_code':0},
                                       {'arguments':['status','--porcelain'],'stdout':'','exit_code':0}]}
        self.target = self.directory/'main/cells/main-0001'
        self.target.mkdir(parents=True)
        formal.write_json(self.target/'openings.json', {'schema_version':1,'openings':[rows[0]]})
        metadata = self.fixture.metadata(profiles=self.profiles, cell=self.cell)
        metadata['games'] = metadata['games'][:2]
        metadata['selected_openings'] = [rows[0]]
        metadata['games_requested'] = 2
        metadata['summary'] = formal.comparison.summarize(metadata['games'],metadata['selected_openings'])
        metadata['formal_cell'] = self.cell
        metadata['formal_git_receipts']=self.frozen['git_receipts']
        metadata['harness_checkout']={'commit':self.fixture.commit,'status_porcelain':''}
        metadata['opening_file_sha256'] = formal.sha256(self.target/'openings.json')
        metadata['affinity'] = {'worker_mask':15,'expected_mask':15,
                                'transports':[{'affinity_mask':15,'pid':1000+i,'creation_ticks':100,
                                               'command':metadata['engines'][role]['command']}
                                              for i,role in enumerate(('baseline','candidate'))]}
        self.metadata = metadata
        self.save()

    def save(self):
        formal.write_json(self.target/'games.json',self.metadata)
        (self.target/'games.pgn').write_text(formal.comparison.pgn_text(self.metadata),encoding='utf-8')

    def audit(self):
        return formal.audit_cell(self.directory,self.frozen,self.profiles,self.cell)

    def test_complete_pair_replays_mainline_pv_and_frozen_conditions(self):
        result=self.audit()
        self.assertEqual(result['summary']['completed_games'],2)
        self.assertGreater(result['pv_plies'],0)

    def test_root_fen_alias_cannot_pass_as_standard_start(self):
        for game in self.metadata['games']:
            game['initial_fen']=game['initial_fen'].replace(' 0 1',' 0 0')
        self.save()
        with self.assertRaisesRegex(ValueError,'initial board'):
            self.audit()

    def test_illegal_pv_rejected(self):
        self.metadata['games'][0]['search_moves'][0]['pv']=['e2e5']
        self.save()
        with self.assertRaises(ValueError): self.audit()

    def test_search_receipt_role_and_missing_receipt_rejected(self):
        original=deepcopy(self.metadata)
        self.metadata['games'][0]['search_moves'][0]['role']='wrong'
        self.save()
        with self.assertRaises(ValueError): self.audit()
        self.metadata=original
        self.metadata['games'][0]['search_moves'].pop()
        self.save()
        with self.assertRaises(ValueError): self.audit()

    def test_affinity_option_and_movetime_tampering_rejected(self):
        original=deepcopy(self.metadata)
        for mutate in [lambda x:x['affinity']['transports'][0].update(affinity_mask=240),
                       lambda x:x['engines']['candidate']['requested_options'].update(Hash=32),
                       lambda x:x['limits'].update(movetime_ms=1000)]:
            self.metadata=deepcopy(original); mutate(self.metadata); self.save()
            with self.assertRaises(ValueError): self.audit()

    def test_schedule_covers_all_conditions_and_static_lanes(self):
        openings=[{'family_id':str(i),'cluster_id':str(i//4)} for i in range(32)]
        cells=formal.cell_schedule(['a','b','c'],['x','y'],[100,1000],openings,'main')
        self.assertEqual(len(cells),384)
        unique={(c['candidate'],c['opponent'],c['movetime_ms'],c['family_id']) for c in cells}
        self.assertEqual(len(unique),384)
        self.assertEqual([sum(c['lane']==lane for c in cells) for lane in range(4)],[96]*4)
        for candidate in ['a','b','c']:
            for lane in range(4):
                self.assertEqual(sum(c['candidate']==candidate and c['lane']==lane for c in cells),32)
                for opponent in ['x','y']:
                    for movetime in [100,1000]:
                        self.assertEqual(sum(c['candidate']==candidate and c['lane']==lane
                            and c['opponent']==opponent and c['movetime_ms']==movetime for c in cells),8)

    def test_physical_cores_cannot_be_shared_or_partially_assigned(self):
        plan={'lane_affinity_masks':[15,240,3840,61440],
              'topology':{'physical_core_masks':[3,12,48,192,768,3072,12288,49152]}}
        formal.validate_resources(plan)
        for masks in [[15,240,3840,61441],[5,250,3840,61440],[3,252,3840,61440]]:
            with self.assertRaises(ValueError): formal.validate_resources(dict(plan,lane_affinity_masks=masks))

    def test_plan_cannot_quietly_reduce_time_or_change_retry_policy(self):
        plan={'schema_version':2,'phase':'formal-m2-job-accounting','accounting_policy':{'mode':'windows-job-plus-coordinator-cpu'},'load_policy':{'max_nonworker_cpu_fraction':.2},'movetimes_ms':[50,200],'draw_policy':'claim'}
        path=self.directory/'invalid-plan.json'
        formal.write_json(path,plan)
        with self.assertRaisesRegex(ValueError,'100/1000'): formal.load_plan(path)
        plan.update(movetimes_ms=[100,1000],retry_policy='retry-until-good')
        formal.write_json(path,plan)
        with self.assertRaisesRegex(ValueError,'nonadaptive'): formal.load_plan(path)

    def test_main_does_not_launch_without_valid_preflight(self):
        with patch.object(formal,'verify_frozen',return_value=(self.frozen,self.profiles)), \
             patch.object(formal,'topology',return_value={}), \
             patch.object(formal,'audit_phase',return_value={'status':'PASS','valid_for_comparison':False}), \
             patch.object(formal.subprocess,'Popen') as popen:
            self.frozen['plan']['topology']={}
            with self.assertRaisesRegex(ValueError,'preflight'): formal.run_phase(self.directory,'main')
            popen.assert_not_called()

    def test_complete_phase_cannot_hide_dirty_or_incomplete_load_guard(self):
        target=self.directory/'main'
        self.frozen['cells']={'main':[self.cell]}
        self.frozen['plan']['idle_probe_seconds']=5
        checked=self.audit()
        rows,clean,final_job,policy=make_clean_fixture()
        self.frozen['plan']['load_policy']={k:v for k,v in policy.items() if k!='idle_probe_seconds'}
        formal.write_json(target/'job-final-accounting.json',final_job)
        (target/'load.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        for guard in [clean,dict(clean,valid_for_load_check=False),dict(clean,external_cpu_increase_ticks=1),
                      dict(clean,completed_idle_seconds=1),dict(clean,failure={'reason':'contaminated'})]:
            formal.write_json(target/'load-summary.json',guard)
            report={'status':'COMPLETE','valid_for_comparison':True,'cells':[dict(self.cell,status='COMPLETE',**checked)],
                    'scheduled_cells':1,'scheduled_games':2,'completed_games':2,
                    'frozen_plan_sha256':formal.sha256(self.directory/'frozen-plan.json'),
                    'guard_summary_sha256':formal.sha256(target/'load-summary.json'),
                    'guard_log_sha256':formal.sha256(target/'load.jsonl'),
                    'job_final_accounting_sha256':formal.sha256(target/'job-final-accounting.json')}
            formal.write_json(target/'report.json',report)
            with patch.object(formal,'verify_frozen',return_value=(self.frozen,self.profiles)):
                if guard==clean:
                    self.assertTrue(formal.audit_phase(self.directory,'main')['valid_for_comparison'])
                else:
                    with self.assertRaises(ValueError):
                        formal.audit_phase(self.directory,'main')

    def test_guard_initialization_failure_is_durable_and_never_launches(self):
        self.frozen.update(cells={'preflight':[dict(self.cell,id='preflight-0001')]})
        self.frozen['plan'].update(topology={},load_policy={'max_nonworker_cpu_fraction':.2},preflight_timeout_seconds=10)
        with patch.object(formal,'verify_frozen',return_value=(self.frozen,self.profiles)), \
             patch.object(formal,'topology',return_value={}), \
             patch('m2_job_load_guard.JobGuardMonitor',side_effect=OSError('access denied')), \
             patch.object(formal,'OwnedJob') as owned_job, \
             patch.object(formal.subprocess,'Popen') as popen:
            owned_job.return_value.terminate_and_snapshot.return_value={'active_processes':0,'coordinator_in_job':False}
            report=formal.run_phase(self.directory,'preflight')
        self.assertEqual(report['status'],'STOPPED')
        self.assertFalse(report['valid_for_comparison'])
        self.assertIn('access denied',report['stop_reason'])
        popen.assert_not_called()
        self.assertEqual(formal.read_json(self.directory/'preflight/report.json')['status'],'STOPPED')


class StatisticsTests(unittest.TestCase):
    def test_only_exact_successful_sealed_git_queries_can_be_reused(self):
        commit='a'*40
        cache=formal.git_receipt_cache([{'arguments':['rev-parse','--verify',commit+'^{commit}'],
                                        'stdout':commit,'exit_code':0}])
        self.assertEqual(cache('rev-parse','--verify',commit+'^{commit}'),commit)
        with self.assertRaisesRegex(ValueError,'not verified'): cache('rev-parse','HEAD')
        with self.assertRaises(ValueError): formal.git_receipt_cache([{'arguments':['rev-parse','HEAD'],'stdout':commit,'exit_code':1}])

    def test_original_commit_validator_remains_active_under_cached_git(self):
        commit='a'*40
        cache=formal.git_receipt_cache([{'arguments':['rev-parse','--verify',commit+'^{commit}'],
                                        'stdout':commit,'exit_code':0}])
        with patch.object(formal.comparison,'git_command',side_effect=cache):
            self.assertEqual(formal.comparison.verified_commit(commit),commit)
            with self.assertRaises(ValueError): formal.comparison.verified_commit('main')
            with self.assertRaises(ValueError): formal.comparison.verified_commit('b'*40)

    def test_cluster_resampling_keeps_all_members_and_is_deterministic(self):
        clusters={'a':[0,0,.5,1],'b':[1,1]}
        first=statistics.bootstrap_clusters(clusters,2026102101,1000)
        self.assertEqual(first,statistics.bootstrap_clusters(clusters,2026102101,1000))
        self.assertEqual(first,[.375,1.])
        self.assertIsNone(statistics.bootstrap_clusters({'single':[1,1]},1,100))

    def test_percentile_interpolation(self):
        self.assertEqual(statistics.percentile([1,0,2,3],.5),1.5)


if __name__=='__main__':
    unittest.main()
