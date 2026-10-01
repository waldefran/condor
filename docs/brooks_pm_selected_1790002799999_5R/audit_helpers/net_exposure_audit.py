import json,sys
from pathlib import Path
from decimal import Decimal
from scripts.brooks_long_lock_policy import projected_operation_exit_net
root=Path(sys.argv[1]); manifest=json.loads((root/'run_manifest.json').read_text())
assert manifest['status'] in ('completed','paused','failed'), manifest['status']
trades=[json.loads(x) for x in (root/'simulation/trades.jsonl').read_text().splitlines() if x.strip()]
fills=[json.loads(x) for x in (root/'simulation/fills.jsonl').read_text().splitlines() if x.strip()]
assert len(trades)==1
trade=trades[0]; cid=trade['correlation_id']; relevant=[f for f in fills if f['correlation_id']==cid]
opening=[f for f in relevant if f['action']=='MAIN_OPEN']; assert len(opening)==1
other=[f for f in relevant if f['action'] not in ('MAIN_OPEN','MAIN_CLOSE')]
result={'correlation_id':cid,'initial_R_usdt':trade['initial_risk_usd'],'allowed_loss_usdt':str(Decimal(trade['initial_risk_usd'])*5),'modeled_funding':'not_modeled','method':'Ex-post audit over observed closed M1 only; never supplied to PM ahead of time. Entry M1 is excluded because entry is at its close. Slippage embedded in fills; no double deduction.'}
if other:
 result.update({'status':'requires_leg_timeline_reconstruction','additional_management_fills':other,'limit':'Simple MAIN-only projection intentionally not used after hedge/reduction. Consult long_lock_events, management_outcomes, fills and venue ledger.'})
else:
 end=trade['closed_at_ms'] or manifest['simulation_time_ms']; worst=None; best=None; count=0
 for line in Path('/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl').read_text().splitlines():
  bar=json.loads(line)
  if not (trade['opened_at_ms'] < bar['close_time_ms'] <= end): continue
  assert bar.get('closed',True)
  count+=1
  for field in ('low','high'):
   net=projected_operation_exit_net('0',opening[0]['fee'],[('LONG',opening[0]['filled_amount'],opening[0]['price'])],bar[field],'0.0004','1')
   item={'projected_exit_net':str(net),'bar':bar,'field':field}
   if worst is None or net<Decimal(worst['projected_exit_net']): worst=item
   if best is None or net>Decimal(best['projected_exit_net']): best=item
 result.update({'status':'audited_main_only','observed_m1_bars':count,'worst':worst,'best':best,'worst_net_loss_R':str(-Decimal(worst['projected_exit_net'])/Decimal(trade['initial_risk_usd'])) if worst else None,'threshold_crossed':bool(worst and Decimal(worst['projected_exit_net'])<=-Decimal(trade['initial_risk_usd'])*5)})
 if trade['status']=='open':
  snap=json.loads((root/'simulation/snapshot.json').read_text()); result['final_mark_price']=json.loads((root/'venue_checkpoint.json').read_text())['mark_price']; result['final_projected_exit_net']=str(projected_operation_exit_net(trade['realized_gross_pnl'],trade['fees'],[('LONG',trade['remaining_main_quantity'],trade['entry_price'])],json.loads((root/'venue_checkpoint.json').read_text())['mark_price'],snap['execution_assumptions']['taker_fee_rate'],snap['execution_assumptions']['slippage_bps']))
(root/'NET_EXPOSURE_AUDIT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
