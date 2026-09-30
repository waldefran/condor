# Evidência Brooks V2

Investigação do HEAD `7760041a`, sem alterações de implementação.

- Baseline: arquivos em `docs/brooks_walkforward_2026-09-20_2026-09-29/` no commit 7760041a, 145ciclos.
- Posterior: cópia fixa feita em 2026-09-30T12:12:09.556539+00:00, 189ciclos; não é acompanhamento ao vivo.
- `posterior/`: ciclos,eventos,contextos e ledger desse corte; `binding.json` é a exportação persistida.
- `captures/`: SYSTEM, cada user message, raw response entregue pelo client, tools e host. Não contém raciocínio oculto.
- `wire_requests/`: body de requisições selecionadas, incluindo todos os turns dos Trader failures e decisões selecionadas; nenhum header de autenticação. Não são respostas HTTP completas.
- `frozen_packets/`: packets separados dos failures/decisões; também aparecem completos na primeira user message.
- `capture_index.json`: proveniência e hash das cópias exatas. `bundle_hashes.json`: hash de todos os artefatos entregues, excluindo o próprio arquivo.
- `failures.json`, `blocked_entries.json`, `decision_reviews.json`: análises com paths portáteis. `baseline@7760041a/<path>` é referência histórica via git show.
- `*_reproduction.json`, `lifecycle_*probe.json` e `REPRODUCTIONS.md`: diagnósticos offline, sem provider ou venue real.

Contagens e causas estão em `../BROOKS_V2_INVESTIGATION_REPORT.md`. Metadados derivados corrigem slips de análise (100000ms=100s; low 2624.89 no idx118); captures/wires permanecem byte a byte originais.
