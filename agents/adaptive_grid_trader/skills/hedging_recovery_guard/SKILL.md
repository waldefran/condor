---
name: hedging_recovery_guard
description: 'MetaTrader Zone Recovery & Hedging Lock protocol for adaptive perpetual grid trading: asymmetric buy/sell locking, zero negative closes on buys, max 2 locks on shorts, up to 10 positions per side (max 20 total).'
when_to_use: 'Before and during every grid cycle evaluation on perpetual futures (15-minute cadence). Governs position locking, recovery zones, and asymmetric stop policies.'
created: '2026-09-24T01:50:00Z'
source: agent:adaptive_grid_trader
---

# Hedging Recovery Guard & Zone Recovery Protocol

This skill codifies MetaTrader 4/5 (MQL) **Zone Recovery** and **Hedging Lock (Trava de Hedging)** mechanics adapted for 100x perpetual futures on Binance Perpetual Demo.

---

## 1. Core Principles & Edge

1. **Structural Upward Bias:** Bitcoin and major crypto assets possess long-term upward drift and high volatility. Time works in favor of spot/perp Longs when properly capitalized.
2. **Asymmetric Risk Management:**
   - **BUY (LONG) Positions:** **NEVER close in negative.** Never accept a stop loss on a Long. When price moves adverse, deploy a Hedging Lock (opposing Short) to freeze drawdown, and use time to exit the recovery zone in profit.
   - **SELL (SHORT) Positions:** **Can close in negative.** Shorts face unlimited upside risk in strong trend expansions. A Short position may lock (hedge with Long) at most **2 times**. If adverse movement persists after 2 locks, cut the loss at the invalidation boundary.
3. **Floating Drawdown Tolerance:** "Não se importe com o flutuante... use o tempo a favor." Unsettled floating PnL is temporary volatility within the recovery zone. Do not panic-close underwater Longs.
4. **Aggressive Scale:** Up to **10 simultaneous operations/orders per side**, totaling a **maximum of 20 open positions**.

---

## 2. Multi-Position Sizing & Envelope

With account balance ~$4,670+ USDT on Binance Perpetual Demo:
- **Min Order Size:** 55 USDT notional (~0.0007 BTC).
- **Max Positions Per Side:** 10 LONG, 10 SHORT (Total max: 20 positions).
- **Notional Allocation:**
  - 10 Long orders × $55 = $550 quote.
  - 10 Short orders × $55 = $550 quote.
  - Total active notional: up to $1,100 quote across 20 positions.
- **Margin Requirement at 100x:**
  - $1,100 / 100 = **$11.00 USDT initial margin** (<0.25% of account equity!).
  - Account equity is heavily insulated against liquidation, giving vast breathing room to withstand swings and let recovery zones work.

---

## 3. Asymmetric Hedging Lock (Trava) Rules

### A. BUY (LONG) Management:
1. **No Stop Loss:** Long grid executors MUST NOT have a stop-loss order that realizes a loss (`stop_loss: None`, `keep_position: True`).
2. **Lock Trigger (Zone Entry):**
   - If price drops by $D_{lock} \approx 1.0 \times ATR(15m)$ below the lowest Long entry, or breaches the bottom of the Long grid without taking profit:
   - **Action:** Open a **Hedging SHORT position (Trava)** of equal or matching size ($0.0007$ BTC per order).
   - **Result:** The net equity loss is locked in at the zone delta ($Price_{long} - Price_{short}$). No further loss can accumulate while both positions are open.
3. **Exit & Recovery (Unilateral Short Profit Harvesting & Long DCA):**
   - **Gatilho 1 — Realização Unilateral do SHORT no Fundo / Suporte:**
     - Quando o SHORT da trava estiver em lucro flutuante individual ($\ge +0.25\%$) E/OU o preço do BTC testar o suporte do range de 6h (faixa inferior < 25% do range) ou mostrar exaustão de venda no 15m:
     - **AÇÃO IMEDIATA:** **FECHAR O SHORT UNILATERALMENTE E EMBOLSAR O LUCRO NO CAIXA.**
     - **NÃO ESPERE A CESTA INTEIRA ZERAR.**
     - **NÃO HÁ PROBLEMA EM DEIXAR O LONG DESPROTEGIDO NO REPIQUE.** O Long não tem stop-loss e o tempo corre a favor.
   - **Gatilho 2 — Compra Agressiva no Suporte (DCA para Baixar Preço Médio):**
     - Imediatamente após fechar o Short com lucro (ou ao confirmar suporte no fundo), abrir nova ordem de **COMPRA (LONG)** de 55 USDT notional a 100x (até o limite de 10 longs).
     - Isso derruba o preço médio do Long drasticamente, fazendo com que um pequeno repique já coloque a posição compradora no lucro total.
   - **Gatilho 3 — Re-trava Dinâmica (Se o preço voltar a cair):**
     - Se o mercado não sustentar o repique, romper o suporte e voltar a cair $> 1.0 \times ATR(15m)$ abaixo da nova compra: **rearmar uma nova trava de SHORT sem hesitar**.
     - Pode re-travar quantas vezes for necessário ao longo de uma tendência de baixa, sempre embolsando o lucro do short nos repiques.
   - **Gatilho 4 — Saída Conjunta no Lucro (Net Basket Profit):**
     - Se ambas as posições estiverem abertas e a soma líquida atingir `Net Basket PnL >= +0.3%`, encerrar ambas simultaneamente no lucro.
   - **Regra Imutável:** NUNCA fechar o LONG sozinho no negativo.

### B. SELL (SHORT) Management:
1. **Stop Loss Allowed:** Shorts are vulnerable to parabolic squeezes. A Short position may take a stop-loss, but ONLY after utilizing its locking allowance.
2. **Locking Allowance: MAXIMUM 2 LOCKS:**
   - **Lock #1:** When Short suffers adverse movement $\ge 1.0 \times ATR(15m)$ above entry, open a **Hedging LONG (Trava 1)**. If price turns down, close Trava 1 at profit and let Short recover.
   - **Lock #2:** If price rallies again and threatens the Short, open **Hedging LONG (Trava 2)**.
   - **Terminal Stop:** If after Lock #2 the market continues to power higher and reaches the macro resistance invalidation level, **close the SHORT position at a loss** (cut loss). Do NOT open a 3rd lock on the Short.
3. **Journaling:** Always track `short_lock_count: 0 | 1 | 2` in the tick journal.

---

### 4. Active Multi-Grid Scalping & DCA While Locked (Never Freeze Trading)
- **Do NOT Freeze Grid Operations:** When a position is locked by an opposing hedge, do NOT sit idle in HOLD waiting for the basket to exit.
- **Aggressive DCA on Support:** Deploy fresh BUY orders when near range lows to improve average entry price.
- **Continuous Cashflow Generation:** Continue deploying active grid limit orders (up to 10 LONGs and 10 SHORTs) across the 15m ATR range to scalp oscillations.
- **Bypass Drift/Ghost Paralysis:** If positions are visible and confirmed on `binance_perpetual_demo`, do NOT pause trading due to internal `GHOST` warnings or session attribution mismatches. Act on the real exchange state!

---

## 5. Grid Spacing & Take Profit Settings (15m Evaluation Cadence)

- **Evaluation Frequency:** **15 minutes (900 seconds)**.
- **Dynamic ATR Spacing:** $D = ATR(15m) \times \sqrt{2.0}$.
- **Grid Order Spacing:** $0.15\%$ to $0.35\%$ between levels.
- **Take-Profit per Level:** $+0.20\%$ to $+0.50\%$ (fee-clearing: Binance perp taker is 0.05%, maker 0.02%, so $+0.20\%$ easily clears round-trip fees and locks in cash).
- **Execution Mode:** Multi-executor or multi-order grid with `max_open_orders: 10` per executor, or multiple single-level executors up to 20 total.

---

## 6. Pre-Tick Checklist for the Agent

Every 15-minute tick:
1. **Live State Audit:**
   - Verify live positions directly via venue portfolio/positions.
   - Count open Longs and open Shorts (max 10 Longs, max 10 Shorts).
2. **Short Profit Harvest Check (CRITICAL):**
   - Is an active Short in profit $\ge +0.25\%$ while price is near range lows / support?
   - **YES $\to$ Close the Short immediately!** Embolsar o lucro no caixa.
3. **Long DCA Check (CRITICAL):**
   - Did we close a Short or is price testing support?
   - **YES $\to$ Open a new BUY (LONG) at 100x** to improve average entry price (if open Longs $< 10$).
4. **Adverse Position Check:**
   - Long adverse $> 1.0 \times ATR$ and unprotected? $\to$ Deploy Hedging Short.
   - Short adverse $> 1.0 \times ATR$? $\to$ Check `short_lock_count < 2`. If $< 2$, deploy Long lock. If $\ge 2$ and invalidation reached, cut Short.
5. **Profit Capture:**
   - Long reached Take Profit or Basket reached $+0.3\%$? $\to$ Realize full profit.
