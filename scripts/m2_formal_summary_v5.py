"""Descriptive cluster bootstrap and per-condition WDL for frozen formal M2."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import random

import m2_formal_v5 as formal


def percentile(values, probability):
    ordered=sorted(values)
    position=(len(ordered)-1)*probability
    lower=int(position); upper=min(lower+1,len(ordered)-1)
    return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)


def distribution(values):
    return {'p50':percentile(values,.5) if values else None,
            'p95':percentile(values,.95) if values else None,
            'max':max(values) if values else None,'total':sum(values)}


def bootstrap_clusters(clusters, seed, resamples):
    keys=sorted(clusters)
    if len(keys)<2:
        return None
    generator=random.Random(seed)
    totals=[(sum(clusters[key]),len(clusters[key])) for key in keys]
    scores=[]
    for _ in range(resamples):
        chosen=[totals[generator.randrange(len(totals))] for _ in totals]
        scores.append(sum(x[0] for x in chosen)/sum(x[1] for x in chosen))
    return [percentile(scores,.025),percentile(scores,.975)]


def summarize(directory, phase='main'):
    directory=Path(directory).resolve()
    audit=formal.audit_phase(directory,phase)
    frozen,_=formal.verify_frozen(directory)
    report=formal.read_json(directory/phase/'report.json')
    groups=defaultdict(list)
    for cell in report['cells']:
        metadata=formal.read_json(directory/phase/'cells'/cell['id']/'games.json')
        groups[(cell['candidate'],cell['opponent'],cell['movetime_ms'])].append((cell,metadata))
    rows=[]
    rule=frozen['plan']['statistics']
    for (candidate,opponent,movetime),cells in sorted(groups.items()):
        games=[game for _,metadata in cells for game in metadata['games']]
        complete=[g for g in games if g['completed']]
        clusters=defaultdict(list)
        for cell,metadata in cells:
            clusters[cell['cluster_id']].extend(g['candidate_points'] for g in metadata['games'] if g['completed'])
        resources={}
        for role in ('candidate','baseline'):
            moves=[m for g in games for m in g['search_moves'] if m['role']==role]
            elapsed=[m['elapsed_seconds'] for m in moves]
            receipts=[m['engine_cpu'] for m in moves]
            ratios=[r['cpu_per_wall_ratio'] for r in receipts if r['cpu_per_wall_ratio'] is not None]
            ratio_distribution=distribution(ratios)
            ratio_distribution.pop('total')
            resources[role]={'search_moves':len(moves),'requested_seconds_per_move':movetime/1000,
                'elapsed_p50_seconds':percentile(elapsed,.5) if elapsed else None,
                'elapsed_p95_seconds':percentile(elapsed,.95) if elapsed else None,
                'max_elapsed_seconds':max(elapsed) if elapsed else None,
                'requests_exceeded':sum(x>movetime/1000 for x in elapsed),
                'comparison_outer_elapsed_is_diagnostic_only':True,
                'cpu_ticks':sum(r['cpu_delta_ticks'] for r in receipts),
                'cpu_seconds':sum(r['cpu_delta_ticks'] for r in receipts)/10000000,
                'kernel_ticks':sum(r['kernel_delta_ticks'] for r in receipts),
                'user_ticks':sum(r['user_delta_ticks'] for r in receipts),
                'response_seconds':distribution([r['response_elapsed_seconds'] for r in receipts]),
                'envelope_seconds':distribution([r['envelope_elapsed_seconds'] for r in receipts]),
                'outer_minus_envelope_seconds':distribution([m['elapsed_seconds']-m['engine_cpu']['envelope_elapsed_seconds'] for m in moves]),
                'response_requests_exceeded':sum(r['response_elapsed_seconds']>movetime/1000 for r in receipts),
                'zero_cpu_delta_count':sum(r['cpu_delta_ticks']==0 for r in receipts),
                'cpu_per_wall_ratio':dict(ratio_distribution,count_none=len(receipts)-len(ratios)),
                'cpu_per_wall_ratio_is_diagnostic_only':True}
        rows.append({'candidate':candidate,'opponent':opponent,'movetime_ms':movetime,
            'wins':sum(g['candidate_points']==1 for g in complete),'draws':sum(g['candidate_points']==.5 for g in complete),
            'losses':sum(g['candidate_points']==0 for g in complete),'completed_games':len(complete),
            'score':sum(g['candidate_points'] for g in complete)/len(complete) if complete else None,
            'opening_pairs':len(cells),'clusters':len(clusters),'resources':resources,
            'cluster_bootstrap95_descriptive':bootstrap_clusters(clusters,rule['seed'],rule['resamples']) if audit['valid_for_comparison'] else None})
    return {'schema_version':3,'status':report['status'],'valid_for_comparison':audit['valid_for_comparison'],
        'phase':phase,'scheduled_games':report['scheduled_games'],'completed_games':report['completed_games'],
        'source_report_sha256':formal.sha256(directory/phase/'report.json'),'rows':rows,
        'confidence_scope':'Preregistered descriptive percentile bootstrap of declared broad opening clusters, preserving paired colors and families within each cluster. Previously exposed fixed authored baseline, not a fresh holdout, and one controlled local parallel run; not a universal strength/Elo CI or a multiple-testing promotion rule.',
        'elo_estimate':None,'promotion_performed':False,'load_summary':audit['guard_summary'],
        'engine_cpu_scope':'Raw all-thread process CPU deltas of the verified actual UCI endpoint during each search; launcher and bootstrap CPU stay in the separate owned Job total.',
        'latency_scope':'Response callable and metered envelope retain requested*2+0.25 seconds deadline; comparison outer elapsed also includes receipt serialization and flush and is diagnostic only.',
        'residual_cpu_policy':'warning-only',
        'endpoint_ownership_scope':'calling-worker-current-job-with-no-breakaway-and-creation-order',
        'percentile_method':'sorted linear interpolation at (n-1)*p; Hyndman-Fan type 7'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--phase',choices=('preflight','main'),default='main')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): raise ValueError('preserve existing summary')
    formal.write_json(args.output,summarize(args.directory,args.phase))
