import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median
import sys

root = Path(sys.argv[1])
def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
def document(path):
    return json.loads(path.read_text()) if path.exists() else {}
def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds') if ms is not None else '—'
def link(path):
    rel = path.relative_to(root)
    return f'[{rel}]({rel})'
def role_input(role):
    for call in role.get('calls', []):
        message = call.get('user_message', '')
        if '\nInput: ' in message:
            try:
                return json.loads(message.split('\nInput: ', 1)[1])
            except json.JSONDecodeError:
                return None
    return None
def role_time(role):
    return min((c.get('simulation_started_at_ms', 2**63-1) for c in role.get('calls', [])),
        default=2**63-1)
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
    return sealed, {'expected_count': len(hashes), 'verified_count': len(verified),
        'missing': missing, 'mismatches': mismatches,
        'all_match': len(verified) == len(hashes) and not missing and not mismatches,
        'role_file_count': sum(path.startswith('role_runs/') for path in hashes),
        'wire_file_count': sum(path.startswith('wire_requests/') for path in hashes)}
manifest = json.loads((root/'run_manifest.json').read_text())
assert manifest['status'] in ('completed', 'paused', 'failed'), manifest['status']
cid = manifest['only_trade']
trades = [row for row in rows(root/'simulation/trades.jsonl') if row.get('correlation_id') == cid]
fills = [row for row in rows(root/'simulation/fills.jsonl') if row.get('correlation_id') == cid]
outcomes = [row for row in rows(root/'management_outcomes.jsonl') if row.get('correlation_id') == cid]
failures = rows(root/'failures.jsonl')
roles = sorted([(p, json.loads(p.read_text())) for p in (root/'role_runs').glob('*.json')],
    key=lambda pr: role_time(pr[1]))
pm = [(p,r) for p,r in roles if r['role']=='POSITION_MANAGER']
locks = rows(root/'long_lock_events.jsonl')
duration_records = rows(root/'long_duration_events.jsonl')
duration_requests = [r for r in duration_records
    if r.get('observed_at_ms') is not None and not r.get('action')]
duration_extensions = [r for r in duration_records if r.get('action') == 'HOLD_EXTENSION']
events = rows(root/'events.jsonl')
published_duration_events = [e for e in events
    if e.get('type') == 'PM_TIMER' and e.get('payload', {}).get('duration_limit_reached')]
periodic_timer_events = [e for e in events
    if e.get('type') == 'PM_TIMER' and not e.get('payload', {}).get('duration_limit_reached')]
entry_intents = [e for e in events if e.get('type') == 'TRADER_INTENT_CREATED'
    and e.get('correlation_id') == cid]
entry_approvals = [e for e in events if e.get('type') == 'GM_ENTRY_APPROVED'
    and e.get('correlation_id') == cid]
timings = [sum(c.get('elapsed_ms',0) for c in r.get('calls',[]))/1000 for _,r in pm]
actions = Counter(row['decision'].get('action') for row in outcomes)
errors = Counter(row.get('error',{}).get('type') for row in failures if row.get('error',{}).get('type'))
repairs=[(path,call) for path,role in roles for call in role.get('calls',[])
    if call.get('user_message','').startswith('Your previous reply failed the host output contract.')]
pm_rounds = {r.get('round_id') for _,r in pm}
pm_scope_failures = [f for f in failures
    if f.get('round_id') in pm_rounds or str(f.get('round_id','')).startswith(('timer-pm-','pm-pm-'))]
failed_pm_roles = [(path,role) for path,role in pm
    if role.get('status') != 'completed' or role.get('host',{}).get('status') != 'completed'
    or role.get('host',{}).get('accepted') is False]
model_failure_rounds = {f.get('round_id') or f.get('error',{}).get('type') or 'unknown'
    for f in pm_scope_failures}
model_failure_rounds.update(role.get('round_id') or str(path)
    for path,role in failed_pm_roles)
model_failure_types = Counter(f.get('error',{}).get('type','unknown') for f in pm_scope_failures)
for _,role in failed_pm_roles:
    if role.get('round_id') not in {f.get('round_id') for f in pm_scope_failures}:
        error_type = role.get('host',{}).get('error',{}).get('type') or role.get('status') or 'unknown'
        model_failure_types[error_type] += 1
crash_record = document(root/'interruption_24h/CRASH_RECORD.json')
resume_lineage = document(root/'RESUME_LINEAGE.json')
sealed, sealed_check = verify_sealed_prefix()
snapshot = document(root/'simulation/snapshot.json')
binding_path = root/'trade_state'/cid/'binding.json'
binding = document(binding_path)
main_position_id = binding.get('main_position_id')
main_open_fills = [f for f in fills if f.get('action') == 'MAIN_OPEN'
    and (not main_position_id or not f.get('position_id') or f.get('position_id') == main_position_id)]
entry_lifecycle = {
    'TRADER_INTENT_CREATED': unique_identity_summary(entry_intents, ('event_id',)),
    'GM_ENTRY_APPROVED': unique_identity_summary(entry_approvals, ('event_id',)),
    'MAIN_OPEN_fills': unique_identity_summary(main_open_fills, ('fill_id', 'order_id', 'client_order_id')),
}
final_legs = [leg for leg in snapshot.get('open_positions', []) if leg.get('correlation_id') == cid]
pm_inputs = []
for _,role in pm:
    packet = role_input(role)
    if packet is not None:
        pm_inputs.append((packet.get('decision_time_ms', -1), packet))
latest_pm_packet = max(pm_inputs, key=lambda x:x[0])[1] if pm_inputs else {}
latest_policy = latest_pm_packet.get('management_policy',{}).get('applicable_risk_behavior',{})
latest_policy_state = latest_policy.get('policy_state',{})
latest_policy_time = latest_pm_packet.get('decision_time_ms')
lines = [f'# Operação {cid} — replay do PM com exposição até 5R', '',
    '## Resultado e escopo', '',
    f"Estado do replay: **{manifest['status']}**; motivo: `{manifest.get('stop_reason','horizonte configurado')}`.",
    f"Relógio histórico final: {utc(manifest.get('simulation_time_ms'))}.",
    f"Código final: `{manifest['git_head']}`. Modelo: `{manifest['model']}`.", '',
    'Trader e analistas D1/H4 foram reproduzidos a partir dos registros originais nos horários gravados. '
    'As inferências PM foram chamadas reais via PydanticAI e API OpenCode Go, modelo DeepSeek 4.1 Flash. '
    'Venue e fills são simulados; nenhuma ordem Binance real foi enviada.', '',
    f'Só `{cid}` foi habilitado para abrir MAIN. Avaliações PM podem vir do timer periódico de 30 minutos simulados, '
    'de expirações de duração e de eventos de lifecycle; expirações adicionam wakes entre ticks do timer, então '
    'não há relação de uma única avaliação PM por período de 30 minutos. '
    f"Timeout configurado: {manifest.get('assumptions',{}).get('pm_timeout_sec')} s; isso é específico deste replay, "
    'não altera default ou produção.', '',
    'O `initial_R_usdt` foi congelado no valor original calculado pelo GM. O host projeta o resultado líquido '
    'conjunto MAIN+HEDGE, incluindo custos modelados, nos extremos de cada M1 fechado; em -5R pede hedge 1:1 '
    'por meio do GM. PM pode hedgear antes. O limite estrutural inicial permanece informacional. Uma MAIN '
    'LONG não é encerrada com projeção líquida negativa; com HEDGE ativa, a perna HEDGE precisa ser desfeita '
    'e o estado reconciliado reavaliado antes de fechar MAIN. Funding não é modelado.', '',
    '## Operação, resultado realizado e marcação', '']
if not trades:
    lines.append(f'Não há registro de operação para `{cid}` em `simulation/trades.jsonl`.')
for trade in trades:
    for key in ('correlation_id','status','opened_at_ms','closed_at_ms','entry_price','initial_quantity',
                'remaining_main_quantity','initial_risk_usd','stop_price','target_price','close_reason','exit_price',
                'realized_gross_pnl','fees','slippage_cost','net_realized_pnl',
                'unrealized_pnl','net_pnl_including_open','r_multiple','mfe_r','mae_r'):
        value = trade.get(key)
        if key.endswith('_at_ms'):
            value = utc(value)
        lines.append(f'- `{key}`: {value}')
    lines.append(f"- `5R` em USDT, sobre o R original: {Decimal(trade['initial_risk_usd'])*Decimal('5')}")
    if trade.get('status') == 'open':
        lines.append('')
        lines.append(f"No último registro de trade, `net_realized_pnl` é {trade.get('net_realized_pnl')} USDT "
            'e `net_pnl_including_open` é o líquido marcado com o floating atual. O segundo não é lucro '
            'finalizado nem uma cotação garantida de saída: não inclui a taxa e o slippage futuros para liquidar '
            'as pernas ainda abertas.')
    else:
        lines.append('')
        lines.append(f"A operação consta como encerrada; `net_realized_pnl` é {trade.get('net_realized_pnl')} USDT, "
            'após taxas registradas. Funding não está incluído.')
lines += ['',
    '`net_realized_pnl` = PnL bruto já realizado menos taxas acumuladas. `net_pnl_including_open` soma esse '
    'líquido ao floating das pernas abertas, sem taxa/slippage futuros de fechamento. A projeção de saída '
    'conjunta enviada no snapshot mais recente do PM é um terceiro valor e inclui os custos futuros modelados '
    'para MAIN+HEDGE; o horário dela aparece abaixo. Slippage já afeta preço e PnL bruto, portanto '
    '`slippage_cost` é diagnóstico e não deve ser descontado novamente. Uma perna HEDGE pode realizar perda '
    'individual enquanto MAIN permanece aberta; isso não é o resultado final da operação.', '',
    'O TP permaneceu ativo e fixo conforme a submissão original; veja `target_price` acima. Uma resposta do modelo '
    'que o descreva como informational não altera o estado efetivo do host. Este replay não valida edge nem usa '
    'a trajetória para avaliar a entrada. A leitura ex ante está em '
    '[SOURCE_DECISION_REVIEW.md](SOURCE_DECISION_REVIEW.md).', '',
    '### Binding e pernas MAIN/HEDGE no snapshot final', '']
if binding:
    lines.append(f"Binding `{binding.get('status')}`: MAIN `{binding.get('main_position_id')}` "
        f"({binding.get('main_side')}); HEDGE `{binding.get('hedge_position_id')}` "
        f"({binding.get('hedge_side')}); conector `{binding.get('connector_name')}`. "
        f"[binding](trade_state/{cid}/binding.json). Snapshot em {utc(snapshot.get('as_of_ms'))}.")
else:
    lines.append('Binding final não encontrado.')
if final_legs:
    lines += ['', '| Role | ID | Lado | Qtd aberta | Entrada | Taxa entrada | Taxa saída | Realizado bruto da perna |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for leg in final_legs:
        lines.append(f"| {leg.get('role')} | `{leg.get('position_id')}` | {leg.get('side')} | "
            f"{leg.get('quantity')} | {leg.get('entry_price')} | {leg.get('entry_fees')} | "
            f"{leg.get('exit_fees')} | {leg.get('realized_gross')} |")
else:
    lines.append('Nenhuma perna permanece aberta no snapshot final.')
if latest_policy_state:
    lines += ['', f"Última projeção de saída fornecida ao PM, decisão {utc(latest_policy_time)}: "
        f"`projected_exit_net={latest_policy_state.get('projected_exit_net')}` USDT, "
        f"`initial_R_usdt={latest_policy_state.get('initial_R_usdt')}`, "
        f"`allowed_loss_usdt={latest_policy_state.get('allowed_loss_usdt')}`, "
        f"MAIN qty `{latest_policy_state.get('main_quantity')}`, HEDGE qty `{latest_policy_state.get('hedge_quantity')}`. "
        'É uma projeção de saída no horário do snapshot do PM, não PnL realizado nem garantia de preço de execução.']
lines += ['', '## Unicidade do lifecycle de entrada', '',
    'Contagens de eventos/fills do CID exato; identidade deduplicada por event_id e, para MAIN_OPEN, pelo ID de fill/ordem. '
    'Linhas repetidas com o mesmo identificador ficam explícitas após a retomada.', '',
    '| Tipo | Registros | Identidades únicas | Linhas duplicadas |', '|---|---:|---:|---:|']
for label, summary in entry_lifecycle.items():
    lines.append(f"| {label} | {summary['rows']} | {summary['unique_identities']} | {summary['duplicate_rows']} |")
lines += ['', '## Wakes de duração', '',
    f'Pedidos de duração registrados: **{len(duration_requests)}**; eventos `PM_TIMER` publicados com prova: '
    f'**{len(published_duration_events)}**; eventos `PM_TIMER` periódicos registrados: **{len(periodic_timer_events)}**; '
    f'extensões registradas após HOLD: **{len(duration_extensions)}**.',
    'Cada wake pede decisão PM na expiração simulada; o host só estende o prazo depois de `HOLD` aprovado pelo GM.', '',
    '| Tipo | Horário UTC | Expira em UTC |', '|---|---|---|']
for request in duration_requests:
    lines.append(f"| Pedido observado em M1 | {utc(request.get('observed_at_ms'))} | {utc(request.get('expires_at_ms'))} |")
for extension in duration_extensions:
    lines.append(f"| HOLD_EXTENSION | {utc(extension.get('simulation_time_ms'))} | {utc(extension.get('expires_at_ms'))} |")
lines += ['', '## Interrupção e retomada', '']
if crash_record:
    lines.append(f"O registro congelado da primeira interrupção classifica o erro como "
        f"`{crash_record.get('status')}` (`{crash_record.get('error_type')}: {crash_record.get('error_message')}`), "
        f"ao tentar despachar o wake de duração observado em {utc(crash_record.get('duration_observed_at_ms'))}. "
        f"O arquivo registra {crash_record.get('completed_PM_decisions')} decisões PM anteriores preservadas, "
        f"{crash_record.get('LLM_calls_on_failed_duration_wake')} chamadas de modelo no wake que falhou e "
        f"{crash_record.get('venue_writes_after_checkpoint')} writes de venue após o checkpoint. "
        'Foi uma falha do harness no formato do evento, não uma falha do modelo nem uma decisão PM.')
    lines.append(f"A retomada está registrada como `{resume_lineage.get('from_git_head')}` → "
        f"`{resume_lineage.get('to_git_head')}` em {utc(resume_lineage.get('resume_at_ms'))}; "
        f"{resume_lineage.get('preserved_pm_runs')} role runs PM foram preservados. Motivo registrado: "
        f"{resume_lineage.get('reason')}. [CRASH_RECORD](interruption_24h/CRASH_RECORD.json), "
        '[RESUME_LINEAGE](RESUME_LINEAGE.json), [sufixo de journal arquivado]'
        '(interruption_24h/events_after_uncheckpointed_wake.jsonl).')
    lines.append(f"Integridade do prefixo selado: **{sealed_check['verified_count']}/{sealed_check['expected_count']}** "
        f"arquivos conferidos por SHA-256; role runs selados {sealed_check['role_file_count']}, "
        f"wire requests selados {sealed_check['wire_file_count']}; correspondência completa: "
        f"**{sealed_check['all_match']}** ([SEALED_MODEL_PREFIX](interruption_24h/SEALED_MODEL_PREFIX.json)).")
    if sealed_check['missing']:
        lines.append('Arquivos ausentes: ' + ', '.join(f'`{p}`' for p in sealed_check['missing']))
    if sealed_check['mismatches']:
        lines.append('Hashes divergentes: ' + ', '.join(f"`{row['path']}`" for row in sealed_check['mismatches']))
else:
    lines.append('Não há `CRASH_RECORD.json` arquivado para este replay.')
lines += ['', '## PM: decisões, reparos e falhas', '',
    f'Role runs PM: **{len(pm)}**; decisões persistidas: **{len(outcomes)}**; ações: `{dict(actions)}`.',
    f'Reparos do contrato de saída dentro de chamadas: **{len(repairs)}**; falhas de execução PM/role: '
    f'**{len(model_failure_rounds)}** (`{dict(model_failure_types)}`); falhas do harness registradas: '
    f"**{int(bool(crash_record))}** (CRASH_RECORD). Tipos em `failures.jsonl`: `{dict(errors)}`.",
    f'Tempo por avaliação PM: mediana {median(timings) if timings else 0:.3f}s; '
    f'mínimo {min(timings,default=0):.3f}s; máximo {max(timings,default=0):.3f}s; '
    f'soma {sum(timings):.3f}s. Valores registrados por role run incluem as chamadas de tools.', '',
    'Reparos são tentativas internas para obter saída conforme contrato; não contam como decisões adicionais. '
    'Timeouts/falhas do role/modelo e a falha do harness acima são categorias separadas.', '',
    *[f'- {link(path)}: {call.get("user_message","")[:600]}' for path,call in repairs], '',
    '## Timeline dos fills', '', '| Horário UTC | Fill |', '|---|---|']
for fill in fills:
    ms = fill.get('timestamp_ms', fill.get('time_ms', fill.get('created_at_ms')))
    lines.append(f"| {utc(ms)} | `{json.dumps(fill,ensure_ascii=False)}` |")
lines += ['', '## Trava observada e decisões de gestão', '']
if locks:
    for lock in locks:
        lines.append(f"```json\n{json.dumps(lock,ensure_ascii=False,indent=2)}\n```\n")
else:
    lines.append('Nenhum pedido obrigatório de trava de 5R foi registrado; PM ainda pode ter pedido hedge antecipado.')
lines += ['', '| Horário UTC | Ação PM | Resultado GM | Motivo PM |', '|---|---|---|---|']
for row in outcomes:
    gm = row.get('gm_result') or {}
    reason = row.get('decision',{}).get('reason','').replace('|','/').replace('\n',' ')
    lines.append(f"| {utc(row.get('simulation_time_ms'))} | {row.get('decision',{}).get('action')} | "
        f"{gm.get('type')} | {reason} |")
lines += ['', '## Cada avaliação PM: prompt, tools e resposta', '',
    'Os role runs preservam o system prompt, entrada inicial, respostas brutas, mensagens/results de tools e '
    'aceitação do host. `wire_requests/` contém o corpo HTTP enviado à API, sem cabeçalhos de autenticação.', '',
    '| UTC da decisão | Host | Chamadas / segundos | Tools | Role run |',
    '|---|---|---|---|---|']
for path,role in pm:
    calls = role.get('calls',[])
    tools = ', '.join(t.get('name','?') for t in role.get('tools',[])) or 'nenhuma'
    host = role.get('host',{})
    status = host.get('status', role.get('status','desconhecido'))
    if host.get('error'):
        status += f" / {host['error'].get('type')}"
    lines.append(f"| {utc(role_time(role) if calls else None)} | {status} / accepted={host.get('accepted')} | "
        f"{len(calls)} / {sum(c.get('elapsed_ms',0) for c in calls)/1000:.3f} | {tools} | {link(path)} |")
lines += ['', '## Limites', '',
    '- Cada leitura do PM fica limitada ao decision_time do snapshot enviado; o replay não faz uma conclusão '
    'sobre barras posteriores em nome do modelo.',
    '- A perda flutuante pode ultrapassar 5R entre candles, por gap, slippage ou latência; a trava observa M1 fechado.',
    '- Bloquear fechamento conjunto negativo não elimina floating negativo, custos pagos, risco de liquidação ou '
    'garante retorno ao zero, lucro ou recuperação.',
    '- Funding não foi modelado; alvo de TP é fixo; os resultados são desta trajetória simulada, sem inferência de edge.', '',
    '## Artefatos', '',
    '- [Manifest](run_manifest.json), [métricas](metrics.json), [eventos](events.jsonl), [auditoria dos snapshots](SNAPSHOT_AUDIT.json).',
    '- [Operações](simulation/trades.jsonl), [snapshot](simulation/snapshot.json), [fills](simulation/fills.jsonl).',
    '- [Resultados GM](management_outcomes.jsonl), binding final e pernas em `trade_state/`.',
    '- [Histórico de wakes de duração](long_duration_events.jsonl), [travamentos](long_lock_events.jsonl).',
    '- `source_artifacts/` preserva decisões originais; `role_runs/` e `wire_requests/` preservam chamadas desta execução.',
    '- [Método](README.md), [revisão ex ante](SOURCE_DECISION_REVIEW.md).', '']
(root/'FINAL_REPORT.md').write_text('\n'.join(lines))
print(json.dumps({'status':manifest['status'],'pm_runs':len(pm),'actions':dict(actions),
    'entry_lifecycle':entry_lifecycle,
    'model_failures':len(model_failure_rounds),'harness_failures':int(bool(crash_record)),
    'duration_requests':len(duration_requests),'duration_extensions':len(duration_extensions),
    'sealed_prefix':sealed_check,'trades':trades},ensure_ascii=False,indent=2))
