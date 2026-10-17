import pathlib,json,statistics,hashlib,sys
sys.path.insert(0,str(pathlib.Path('scripts').resolve()))
from see_clean_guard import WindowsProcesses
root=pathlib.Path('.');out=root/'build-see-cost'
provider=WindowsProcesses(engine_names=frozenset({'profile_see.exe','profile_see_search.exe','see_tests.exe'}))
try:
 active=[{'pid':p.pid,'name':p.name} for p in provider.snapshot().values() if p.name.casefold() in {'profile_see.exe','profile_see_search.exe','see_tests.exe'}]
 assert not active, active
finally:provider.close()
profile=json.loads((out/'loss-scenes-search-profile-detailed-guarded.json').read_text())
rows=profile['rows']; totals={key:sum(r[key] for r in rows) for key in ['nodes','qnodes','pvs_probes','pvs_researches','see_calls','see_instrumented_ns','search_instrumented_ns']}
totals['qnode_fraction']=totals['qnodes']/totals['nodes'];totals['see_instrumented_fraction']=totals['see_instrumented_ns']/totals['search_instrumented_ns']
print(json.dumps(totals,indent=2))
summary={}
for name in ['loss-scenes-profile-guarded','mixed-regression-profile-guarded']:
 data=json.loads((out/(name+'.json')).read_text());ratios=[s['candidate']['nanoseconds']/s['reference']['nanoseconds'] for s in data['samples']]
 summary[name]={'queries':data['queries'],'samples':len(ratios),'paired_median_candidate_over_reference':statistics.median(ratios),'candidate_slower_pairs':sum(r>1 for r in ratios),'reference_median_ns_per_call':statistics.median(s['reference']['nanoseconds']/s['reference']['calls'] for s in data['samples']),'candidate_median_ns_per_call':statistics.median(s['candidate']['nanoseconds']/s['candidate']['calls'] for s in data['samples'])}
files=[root/'src/see.cpp',root/'tests/see_tests.cpp',root/'scripts/profile_see.cpp']+sorted(p for p in out.iterdir() if p.is_file() and p.suffix in {'.cpp','.cmd','.json','.jsonl','.tsv','.fens','.py','.log','.exe','.obj'})
manifest={'schema_version':1,'baseline_commit':'5148721e7f357c72400c359cd8ac6ab4f27a7341','reference_see_sha256':hashlib.sha256((out/'reference_see.cpp').read_bytes()).hexdigest(),'candidate_value_semantics':'Unchanged local legal LVA, same source-square tie break, all kings/pins/en passant/promotion rules retained. Only traversal and temporary storage optimized.','test_result':{'groups':9,'assertions':9119,'failures':0,'oracle_differential_positions':125,'oracle_differential_moves':3967,'reference_microprofile_queries':402},'profiling':summary,'detailed_instrumented_search':totals,'notes':['No pruning, exchange thresholds or tactical evaluation changed.','Microprofile timings include evaluator calls only; parsing, legal generation and reference equality checks are outside timing.','9 paired samples alternate reference/candidate order. Loss scenes: 43 queries x 100000 repetitions; mixed: 359 x 10000.','Instrumented search uses archived original SEE and original search object from build-see-native, 16 development positions x 3 repeats x 50000 nodes. The SEE clock wrapper adds overhead: do not equate its fraction with exact production share.','First preliminary profiles are retained without CPU guard and are not the final controlled comparison.','Final guarded microprofile and first instrumented search: final-profile.monitor.*. Supplementary detailed search adds qnodes/PVS output only: detailed-search-profile.monitor.*.','The custom compile-see-microprofile.cmd uses MSVC C++20 /O2 /DNDEBUG /W4 /WX with original SEE symbol renamed for same-binary exact-value comparison.','compile-profile-search.cmd is the detailed search instrument build. The detailed driver retains the same search/SEE implementation.','The shell verification returned exit 1 only because a final Get-Process lookup found no remaining test/profile processes; all 9 test groups passed with exit 0 before that lookup.'], 'remaining_profile_processes':active,'files':[]}
for p in files:
 manifest['files'].append({'path':str(p).replace('\\','/'),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(out/'core-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print('Manifest:',out/'core-manifest.json','files',len(manifest['files']),'no active profiling processes')
