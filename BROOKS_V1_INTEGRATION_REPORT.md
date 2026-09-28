# BROOKS_V1_INTEGRATION_REPORT

Branch: `feat/brooks-agents-v1` · HEAD: `3483b6df` · Fork: https://github.com/waldefran/condor (push feito) · Base: `d89e74f2`

## 1. Commits realizados
49 commits desde `d89e74f2` (lista completa: `git log --oneline d89e74f2..HEAD`). Marcos:
- `db252d2d` isolated brooks_agents mode · `66d9a81d` contracts · `56cf1458` event bus+store · `c5fa5001` closed bar + safe market tools
- `e5f81e5e..1cbcfc37` role runner / Trader / HTF
- `18812845..ee6c078c` deterministic GM + MAIN + reduce/close
- `87a0fa02..bc17c45a` position watcher + Position PM
- `e861ef91` hedge compiler · `11e8e75d` hedge execution (FEAT-013)
- `24f3df90` fresh market analysis · `6e688dc8` replay/evidence
- `9c38da77..2a717312` independent lifecycle composition (supervisor, 7 tasks)
- `4ccc1912` production adapters wiring (engine)
- `951d1cee` opencode CLI bridge · `7c4c59ec` opencode session headers (Go)
- `ecc65073` REQUEST_MARKET_ANALYSIS runtime handshake
- `3346030e..26874d81` E2E suite (scenarios 1-10) + 3 gap fixes
- `bdb2dd10` independent adversarial review doc
- `cba02342..fa5bfcb4` demo smoke + live-venue fixes (adapters shapes, assessment by position transition, creator binding)
- `ebcc510c..608463d7` P1 review fixes (privacy normalization, shadow management gate, tool exceptions, unified store/GM root)
- `6050033f`/`3483b6df` real restart validation (runner + evidence)

## 2. Problemas encontrados (destaques)
- PM sem contexto de produção (`_default_pm_load` -> None); interface `run_role` incompatível com `pm.py` (tools=/context= vs prompt/market_tools); MAIN `position_id` circular (binding nulo pre-entry).
- Venue viva (demo): trading-rules reais (`min_notional_size`, sem `max_leverage`); posições sem `position_id`; profundidade de candles insuficiente (119/120); fills fantasma na TP order do executor; lag de índice de executors.
- Hedge: assessment circular (filled derivado da própria posição usada para validar) e wedge `reconciliation_required` sem recuperação; INCREASE criando 2º executor de hedge quebrava a resolução de ownership.
- Revisão adversarial independente (`bdb2dd10`): wedge deadlock P0; privacy guard bypass por chaves compostas; shadow-mode hole no roteamento de MANAGEMENT; exceção de tool derruba o role run; raiz de store/GM duplicada; + P2s.
- Smoke S1: JSON inválido do modelo com prompt de 120 barras (seed H4/H1/M15).
- Operacional: TIME_LIMIT do opencode travou o worker de restart por ~3.5h (recuperado por nudge do coordenador).

## 3. Correções (por área)
- **Runner/PM**: convenção canônica `run_role(role, prompt, output_model, market_tools, *, agent_key, ...)` para Trader/HTF/PM; `build_pm_load_context` fail-closed (binding + pernas frescas + ordens + intents + histórico + policy + hedge + margem; sem candles).
- **GM/Hedge**: decision completo no `execute_management` (hedge_plan preservado); `reduce_fraction` validado no contrato; shadow-zero-write (entrada E MANAGEMENT); fingerprint estrutura-only (mark não invalida); bounded re-reads (6×10s) + corroboração por delta de posição; recuperação read-only do wedge; creator-binding (INCREASE não rotaciona o executor criador).
- **Adapters**: shapes reais da venue (rules, candles, identidade derivada `executor:<id>`); open-orders fail-closed; margem por available value; profundidade de candles; watcher com POSITION_OPENED + fills reais (ordens FILLED, cursor).
- **Privacidade**: normalização de chaves compostas/camel no guard do runner; handshake de market analysis com sanitização e anti-loop.
- **Operação**: docstrings de invariantes; evidências reprodutíveis; runners `scripts/brooks_demo_smoke.py` e `scripts/brooks_restart_demo.py`.

## 4. Testes executados
- `pytest -q tests/test_brooks_*.py` -> **367 passed**
- `pytest -q tests/test_agent_start_model_check.py tests/test_loop_authorization.py tests/test_executor_session_ownership.py tests/test_agent_tool_allowlists.py` -> **69 passed**
- E2E cenários 1-10: `tests/test_brooks_e2e.py` + `tests/test_brooks_e2e_hedge.py` (12)
- Boundary real do `run_role` (apenas o cliente LLM mockado): `tests/test_brooks_pm_runner.py`

## 5. Total de testes Brooks
**367** coletados (contratos, GM/hedge, PM, runner, watcher, market analysis, replay, lifecycle, E2E, decision/shadow, runtime).

## 6. Suíte Condor relevante
**69 verdes** (isolamento do modo `loop`, allowlists de tools, start/model check, executor session ownership). Correção incluída: allowlist do agente stock `brooks_price_action` (`ac6ddbf8`).

## 7. Smoke demo (binance_perpetual_demo, ETH-USDT) — PASS
`docs/brooks_demo_smoke_evidence.md` (run final):
- S1 Trader real (LLM via bridge opencode-go) -> ENTER_SHORT registrado (sem sinais fabricados)
- S2 MAIN 0.059 LONG aberto + binding reconciliado + POSITION_OPENED
- S3 HOLD sem write
- S4 HEDGE 0.30 (0->0.017) -> INCREASE 0.50 (0.017->0.029) -> REDUCE 0.20 (0.029->0.012) -> REMOVE (0.012->0): todos confirmed com deltas exatos
- S5 CLOSE; venue byte-identical ao baseline
- Reprodutível: `PYTHONPATH=. /home/valdemaster/brooks-condor/condor/.venv/bin/python scripts/brooks_demo_smoke.py`

## 8. Restart test (SOL-USDT, fronteira de processo real) — PASS
`docs/brooks_restart_evidence.md`:
- Fase A (processo 1): abre MAIN+HEDGE, persiste binding/hedge state, sai sem cleanup
- Fase B (processo novo): reconcilia MAIN/HEDGE do disco+venue, watcher reconhece, PM acorda, REDUCE_HEDGE 0.30->0.15 confirmado por delta exato (short 0.14->0.08; filled 0.06 == requested == delta; 1 read ambíguo resolvido pela corroboração bounded)
- Auditoria de trade acidental: correlações 1->1, executor MAIN inalterado, 0 executors novos; venue final flat
- Reprodutível: `python scripts/brooks_restart_demo.py --phase a` e `--phase b`

## 9. Evidências MAIN/HEDGE
- `docs/brooks_demo_smoke_evidence.md` (smoke: deltas, ratios, bindings, baselines)
- `docs/brooks_restart_evidence.md` (restart: fases, deltas, auditoria)
- `docs/brooks_review_findings.md` (revisão adversarial independente + plano de restart)
- Commits `6d4c2545`/`984308a2` e `ebcc510c..608463d7` (fixes de venue/assessment/P1)

## 10. Limitações restantes
- Bounded re-read em 6×10s (aprovado 3×5s): transientes da venue excedem 15s; mesmos guardrails; wedge permanece quando não corroborado (fail-closed).
- S1 seed: 30 barras no prompt + janelas profundas via tools (o modelo não emite contrato válido com prompt de 120 barras).
- P2 adiados (documentados): margin split em payloads não divididos (review 1.3) e verificação de executor in-flight no ExecutionPort (review 2.3).
- Restart: MAIN de 0.49 SOL por mínimo de notional (5 USDT) da venue; uma correlação perdida por auto-close de TIME_LIMIT durante a pausa do agente (recuperada fechando).
- O stack demo local sobe com `make deploy` no checkout `hummingbot-api`; os smokes exigem a API em `localhost:8000`.
