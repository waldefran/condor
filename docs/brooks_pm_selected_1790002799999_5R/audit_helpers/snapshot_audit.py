import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
import sys

root = Path(sys.argv[1])
manifest = json.loads((root/'run_manifest.json').read_text())
assert manifest['status'] in ('completed','paused'), manifest['status']
cid = manifest['only_trade']

def document(path):
    return json.loads(path.read_text()) if path.exists() else {}

def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

def closed_bars(value):
    if isinstance(value, list):
        for item in value:
            yield from closed_bars(item)
    elif isinstance(value, dict):
        if all(k in value for k in ('open_time_ms', 'close_time_ms', 'open', 'high', 'low', 'close')):
            yield value
        for item in value.values():
            yield from closed_bars(item)

def input_packet(role):
    for call in role.get('calls', []):
        message = call.get('user_message', '')
        if '\nInput: ' in message:
            try:
                return json.loads(message.split('\nInput: ', 1)[1])
            except json.JSONDecodeError:
                return None
    return None

def unique_identity_summary(items, identity_fields):
    identities = []
    for item in items:
        identity = next((f"{field}:{item[field]}" for field in identity_fields if item.get(field) not in (None, '')), None)
        if identity is None:
            identity = 'sha256:' + sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        identities.append(identity)
    counts = Counter(identities)
    return {'rows': len(items), 'unique_identities': len(counts),
        'duplicate_rows': len(items) - len(counts),
        'duplicate_identities': {key: count for key, count in counts.items() if count > 1}}
def verify_sealed_prefix():
    sealed = document(root/'interruption_24h/SEALED_MODEL_PREFIX.json')
    hashes = sealed.get('hashes', {})
    verified, missing, mismatches = [], [], []
    for rel, expected in hashes.items():
        path = root/rel
        if not path.is_file():
            missing.append(rel)
            continue
        actual = sha256(path.read_bytes()).hexdigest()
        if actual == expected:
            verified.append(rel)
        else:
            mismatches.append({'path': rel, 'expected': expected, 'actual': actual})
    return sealed, {
        'expected_count': len(hashes),
        'verified_count': len(verified),
        'missing': missing,
        'mismatches': mismatches,
        'all_match': len(verified) == len(hashes) and not missing and not mismatches,
        'role_file_count': sum(rel.startswith('role_runs/') for rel in hashes),
        'wire_file_count': sum(rel.startswith('wire_requests/') for rel in hashes),
    }

def market_contexts(value):
    if isinstance(value, list):
        for item in value:
            yield from market_contexts(item)
    elif isinstance(value, dict):
        if str(value.get('schema', '')).startswith('brooks.market-context.'):
            yield value
        for item in value.values():
            yield from market_contexts(item)

unparsed_tool_responses = []
context_time_violations = []
records = []
scope_mismatches = []
policy_mismatches = []
unavailable_inputs = []
pm_packets = []
for path in sorted((root/'role_runs').glob('*.json')):
    role = document(path)
    if role.get('role') != 'POSITION_MANAGER':
        continue
    packet = input_packet(role)
    if packet is None:
        unavailable_inputs.append(str(path.relative_to(root)))
        records.append({
            'role_file': str(path.relative_to(root)),
            'input_available': False,
            'scope_verified': False,
            'policy_verified': False,
            'observed_closed_bars_checked': 0,
            'latest_observed_close_ms': None,
            'tools': dict(Counter(t.get('name', 'unknown') for t in role.get('tools', []))),
            'tool_error_count': sum(t.get('status') == 'error' for t in role.get('tools', [])),
            'host': role.get('host'),
            'future_bar_violations': 0,
        })
        continue

    decision = packet.get('decision_time_ms')
    pm_packets.append((decision if decision is not None else -1, packet))
    behavior = packet.get('management_policy', {}).get('applicable_risk_behavior', {})
    scope = behavior.get('operation_correlation_id')
    scope_ok = scope == packet.get('correlation_id') == cid
    risk_ok = behavior.get('max_unhedged_loss_r') == '5'
    if not scope_ok:
        scope_mismatches.append({
            'role_file': str(path.relative_to(root)),
            'correlation_id': packet.get('correlation_id'),
            'operation_correlation_id': scope,
        })
    if not risk_ok:
        policy_mismatches.append({
            'role_file': str(path.relative_to(root)),
            'max_unhedged_loss_r': behavior.get('max_unhedged_loss_r'),
        })

    observed = []
    for tool in role.get('tools', []):
        reply = tool.get('response_message')
        if reply and ' result: ' in reply:
            try:
                result = json.loads(reply.split(' result: ', 1)[1].rsplit('\nContinue.', 1)[0])
            except ValueError:
                unparsed_tool_responses.append({'role_file':str(path.relative_to(root)), 'tool':tool.get('name')})
                continue
            observed.extend(closed_bars(result))
    for context in market_contexts(packet):
        if context.get('decision_time_ms') is None or context['decision_time_ms'] > decision:
            context_time_violations.append({'role_file':str(path.relative_to(root)), 'schema':context.get('schema'), 'context_time_ms':context.get('decision_time_ms'), 'role_time_ms':decision})
    observed.extend(closed_bars(packet))
    violations = [b for b in observed if b['close_time_ms'] > decision or b.get('closed', True) is not True]
    records.append({
        'role_file': str(path.relative_to(root)),
        'decision_time_ms': decision,
        'operation_correlation_id': scope,
        'scope_verified': scope_ok,
        'policy_verified': risk_ok,
        'canonical_packet_sha256': sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        'observed_closed_bars_checked': len(observed),
        'latest_observed_close_ms': max((b['close_time_ms'] for b in observed), default=None),
        'tools': dict(Counter(t.get('name', 'unknown') for t in role.get('tools', []))),
        'tool_error_count': sum(t.get('status') == 'error' for t in role.get('tools', [])),
        'host': role.get('host'),
        'future_bar_violations': len(violations),
    })

sealed, sealed_check = verify_sealed_prefix()
resume = document(root/'RESUME_LINEAGE.json')
crash = document(root/'interruption_24h/CRASH_RECORD.json')
duration_rows = rows(root/'long_duration_events.jsonl')
duration_requests = [r for r in duration_rows if r.get('observed_at_ms') is not None and not r.get('action')]
duration_extensions = [r for r in duration_rows if r.get('action') == 'HOLD_EXTENSION']
events = rows(root/'events.jsonl')
duration_pm_timers = [e for e in events
    if e.get('type') == 'PM_TIMER' and e.get('payload', {}).get('duration_limit_reached')]
periodic_pm_timers = [e for e in events
    if e.get('type') == 'PM_TIMER' and not e.get('payload', {}).get('duration_limit_reached')]
entry_intents = [e for e in events if e.get('type') == 'TRADER_INTENT_CREATED'
    and e.get('correlation_id') == cid]
entry_approvals = [e for e in events if e.get('type') == 'GM_ENTRY_APPROVED'
    and e.get('correlation_id') == cid]
all_cid_fills = [f for f in rows(root/'simulation/fills.jsonl') if f.get('correlation_id') == cid]
snapshot = document(root/'simulation/snapshot.json')
binding = document(root/'trade_state'/cid/'binding.json')
open_legs = [leg for leg in snapshot.get('open_positions', []) if leg.get('correlation_id') == cid]
main_position_id = binding.get('main_position_id')
main_open_fills = [f for f in all_cid_fills if f.get('action') == 'MAIN_OPEN'
    and (not main_position_id or not f.get('position_id') or f.get('position_id') == main_position_id)]
entry_lifecycle = {
    'TRADER_INTENT_CREATED': unique_identity_summary(entry_intents, ('event_id',)),
    'GM_ENTRY_APPROVED': unique_identity_summary(entry_approvals, ('event_id',)),
    'MAIN_OPEN_fills': unique_identity_summary(main_open_fills, ('fill_id', 'order_id', 'client_order_id')),
}
trade_rows = [trade for trade in rows(root/'simulation/trades.jsonl')
    if trade.get('correlation_id') == cid]
latest_pm_packet = max(pm_packets, key=lambda row: row[0])[1] if pm_packets else {}
latest_policy = latest_pm_packet.get('management_policy', {}).get('applicable_risk_behavior', {})
latest_policy_state = latest_policy.get('policy_state', {})

summary = {
    'schema': 'brooks.replay-investigation.snapshot-audit.v3',
    'operation_correlation_id': cid,
    'pm_runs_checked': len(records),
    'closed_bars_checked': sum(r['observed_closed_bars_checked'] for r in records),
    'future_bar_violations': sum(r['future_bar_violations'] for r in records),
    'scope_mismatches': len(scope_mismatches),
    'scope_mismatch_details': scope_mismatches,
    'policy_mismatches': len(policy_mismatches),
    'policy_mismatch_details': policy_mismatches,
    'unavailable_inputs': unavailable_inputs,
    'unparsed_tool_responses': unparsed_tool_responses,
    'context_time_violations': context_time_violations,
    'limitations': 'Checks OHLC objects delivered in captured first inputs and read-tool replies. Does not grade market reasoning or reconstruct unobserved tool requests.',
    'sealed_model_prefix': {
        'file': 'interruption_24h/SEALED_MODEL_PREFIX.json',
        'role_count': sealed.get('role_count'),
        **sealed_check,
    },
    'resume_lineage': {
        'file': 'RESUME_LINEAGE.json' if resume else None,
        'from_git_head': resume.get('from_git_head'),
        'to_git_head': resume.get('to_git_head'),
        'resume_at_ms': resume.get('resume_at_ms'),
        'preserved_pm_runs': resume.get('preserved_pm_runs'),
        'crash_status': crash.get('status'),
        'crash_error_type': crash.get('error_type'),
        'failed_wake_model_calls': crash.get('LLM_calls_on_failed_duration_wake'),
        'venue_writes_after_checkpoint': crash.get('venue_writes_after_checkpoint'),
    },
    'duration_lifecycle': {
        'duration_requests': len(duration_requests),
        'published_duration_pm_timer_events': len(duration_pm_timers),
        'published_periodic_pm_timer_events': len(periodic_pm_timers),
        'hold_extensions': len(duration_extensions),
        'events': duration_rows,
    },
    'entry_lifecycle': entry_lifecycle,
    'final_snapshot': {
        'as_of_ms': snapshot.get('as_of_ms'),
        'binding': binding or None,
        'open_legs': open_legs,
        'trade': trade_rows[-1] if trade_rows else None,
        'trade_realized_net_pnl': trade_rows[-1].get('net_realized_pnl') if trade_rows else None,
        'trade_marked_net_including_open': trade_rows[-1].get('net_pnl_including_open') if trade_rows else None,
        'latest_pm_projection': {
            'decision_time_ms': latest_pm_packet.get('decision_time_ms'),
            'projection_scope': latest_policy.get('projection_scope'),
            'projected_exit_net': latest_policy_state.get('projected_exit_net'),
            'funding_mode': latest_policy.get('funding_mode'),
        } if latest_policy_state else None,
        'account_realized_gross_pnl': snapshot.get('realized_gross_pnl'),
        'account_unrealized_pnl': snapshot.get('unrealized_pnl'),
        'funding': snapshot.get('costs', {}).get('funding'),
        'funding_mode': snapshot.get('execution_assumptions', {}).get('funding'),
    },
    'runs': records,
}
(root/'SNAPSHOT_AUDIT.json').write_text(json.dumps(summary, indent=2)+'\n')
print(json.dumps({k: v for k,v in summary.items() if k != 'runs'}, ensure_ascii=False, indent=2))
