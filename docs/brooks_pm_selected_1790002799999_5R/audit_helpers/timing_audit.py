import json,statistics,sys
from collections import defaultdict
from pathlib import Path
root=Path(sys.argv[1]); manifest=json.loads((root/'run_manifest.json').read_text()); assert manifest['status'] in ('completed','paused')
outcomes=[json.loads(x) for x in (root/'management_outcomes.jsonl').read_text().splitlines() if x.strip()]
by_time=defaultdict(list)
for x in outcomes: by_time[x['decision']['decision_time_ms']].append(x)
records=[]
for p in (root/'role_runs').glob('*.json'):
 r=json.loads(p.read_text())
 if r['role']!='POSITION_MANAGER': continue
 calls=r['calls']; packet=json.loads(calls[0]['user_message'].split('\nInput: ',1)[1]); dtime=packet['decision_time_ms']; rows=by_time[dtime]
 record={'role_file':str(p.relative_to(root)),'decision_time_ms':dtime,'call_count':len(calls),'api_call_seconds':sum(c.get('elapsed_ms',0) for c in calls)/1000,'role_wall_seconds':(max(c['finished_at_ms'] for c in calls)-min(c['started_at_ms'] for c in calls))/1000,'role_simulation_start_ms':calls[0]['simulation_started_at_ms'],'role_simulation_finish_ms':calls[-1]['simulation_finished_at_ms'],'role_start_lag_seconds':(calls[0]['simulation_started_at_ms']-dtime)/1000,'matched_outcomes':len(rows),'host':r.get('host')}
 if len(rows)==1:
  gm_time=rows[0]['simulation_time_ms']; record.update({'gm_outcome_time_ms':gm_time,'decision_to_gm_seconds':(gm_time-dtime)/1000,'role_finish_to_gm_seconds':(gm_time-calls[-1]['simulation_finished_at_ms'])/1000})
 records.append(record)
records.sort(key=lambda x:x['decision_time_ms'])
def stats(field):
 values=[r[field] for r in records if field in r]; return {'count':len(values),'min':min(values,default=0),'median':statistics.median(values) if values else 0,'max':max(values,default=0),'sum':sum(values)}
summary={'schema':'brooks.replay-investigation.timing-audit.v1','operation_correlation_id':manifest['only_trade'],'method':'Call latency uses capture.elapsed_ms; real role wall time uses actual started/finished timestamps. Decision/GM lags use separate historical timestamps and include host/event scheduling, not just model inference. Source Trader/D1/H4 are recorded and excluded.','roles':len(records),'stats':{f:stats(f) for f in ['api_call_seconds','role_wall_seconds','role_start_lag_seconds','decision_to_gm_seconds','role_finish_to_gm_seconds']},'ambiguous_outcome_matches':[r['role_file'] for r in records if r['matched_outcomes']!=1],'records':records}
(root/'TIMING_AUDIT.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k!='records'},ensure_ascii=False,indent=2))
