# Verificação do experimento 5R

Commit de implementação: `fdc5bd60eb3dd98f834834bcaa315cd5d2d237d4`.

Comando executado antes do replay:

```sh
.venv/bin/pytest -q tests/test_brooks_first_long_replay.py tests/test_brooks_long_lock_venue.py tests/test_brooks_long_lock_policy.py tests/test_brooks_recorded_replay.py tests/test_brooks_pm.py tests/test_brooks_prompt_runtime.py
```

Resultado: **43 passed**, em 3,26s. Há 12 avisos existentes do Pydantic sobre
o nome `schema`; nenhum erro na execução final.

Cobertura material:

- projeção Decimal do líquido conjunto com taxas e slippage;
- MAIN LONG negativa não fecha; hedge pode realizar perda mantendo MAIN aberta;
- MAIN não fecha deixando HEDGE órfã; SHORT mantém STOP_LOSS normal;
- stop original informativo em 5R, gatilho conjunto no teto, R original fixo;
- hedge completo impede repetição de trava; checkpoint preserva política;
- trajetória histórica na regra 1R confirma hedge pelo GM existente;
- mesma trajetória na regra 5R não exige trava e permite TP líquido positivo;
- entrada gravada e ativação pending; regressões PM/runtime relevantes.

O primeiro teste de integração 5R presumiu incorretamente que a trajetória
atingiria o teto antes de 12:00 UTC. Os M1 históricos mostraram que não
atinge: o teste foi corrigido para refletir a trajetória, sem mudar o
validator ou criar uma trava fictícia. O gatilho de 5R é provado por uma
barra artificial no teste de venue, não por candles futuros no packet PM.

O timeout explícito de 180s é exclusivamente um argumento deste replay;
o default continua 60s. GM, PM consumer, hedge de produção, ExecutionPort
e os contratos não foram alterados nesta etapa 5R. A Skill só descreve
a política condicional presente no input; a estratégia Brooks permanece.
