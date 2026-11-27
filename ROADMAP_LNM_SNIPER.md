# Hoja de Ruta Técnica — LNM Sniper v7.4+
> **Acompañamiento a [`CONTEXT_LNM_SNIPER.md`](./CONTEXT_LNM_SNIPER.md)**  
> **Última Actualización:** `2026-11-26` | **HEAD Base:** `89036e0` (`origin/master`, desplegado en Termux)  
> **Regla Antigravity:** Cero scraping web. Lectura estrictamente local y acotada (`<= 150` líneas por bloque, solo archivos autorizados). En `MODO SOLO-EDICIÓN`, delegar `verify_repo.py` y `git` a la terminal WSL del usuario.

---

## 1. Línea Base Certificada (`HEAD = 89036e0`) e Invariantes P0

| Dimensión | Valor Comprobado | Fuente |
| :--- | :--- | :--- |
| **Cadena de commits** | `d0ea3fd` (Fase 1B RAM) -> `5196b85` (fix camelCase fees) -> `cef38c0`/`10a87c9`/`249ac63` (Fase 4 Sidecar `/market/stream`) -> `d38f977`/`89036e0` (Fase 4B/4B.1 JSONL + volúmenes crudos + desglose FER) | `origin/master` y Termux |
| **Backtest 18M (`5196b85` @ `0.16%` RT)** | `Net = +7,301 sats` \| `OOS 12M = +23,992 sats` \| `MaxDD = 4.62%` \| `DDG = 0` | WSL 18M replay |
| **Paridad OOS Fold 1 (`S0`)** | `N = 130` \| `Net = +26,933` \| `PF = 1.729 (1.73)` \| `MaxDD = 4.10%` \| `DD>=4.5% = 0/6` \| `M16 = +31,746 (52)` | `verify_repo.py --mode oos-fold1` |
| **Salud en vivo (Termux `89036e0`)** | `Bal = Eq = 518,764 sats` \| `Act = 0/5` \| `DD = 2.1%` \| Sidecar `connected=True`, `stale=False`, `P95 lectura = 0.0056 ms` | `telemetry_live_testnet.json` y `test_binance_micro_sidecar.py` (`9/9`) |

### Invariantes P0 Innegociables
- **Riesgo y ejecución:** `MAX_CONCURRENT_TRADES = 5`, Modo B `LIQ_BIAS_ONLY` (`liq_en=True`, `liq_shadow=False`, `opp_wall=False`), `100% MARKET` (`lim_en=False`), `HardFloorGuardian`, `C1_STREAK3_24H` (`86,400s` tras 3 pérdidas/lado), `risk = 0.0075`, `sl_floor = 0.0065`, `hedge = False`.
- **Sistema e IPC:** Cero I/O bloqueante o HTTP extra en el bucle de `5s`; `TokenBucket(20/s, burst 40)`; `fcntl` exclusivo bajo `$HOME` (jamás `/sdcard`); `strict_sha=True` en los `4` JSON; esquema `100/12 + 28 runtime`; `python3 -B` bajo `tmux` + `termux-wake-lock`.

### Cementerio de Ideas Descartadas (No Reabrir sin Nueva Evidencia OOS)
- Fase 2 (`LIQ_RAID_1M`), Fase 3 (`RUNNER_4H`), `V_EXIT` y `V_ENTRY`: empeoraron `5196b85` entre `-12,140` y `-33,107 sats` en 18M.
- Scale-out parcial (`50%`) y salidas tempranas: recortan ganadores (captura MFE actual `63.9%`).
- `StochRSI` sobre cierres: repinta intrabar y entra contra rupturas de `RANGE_SQUEEZE`.
- Reemplazar `q_table` por aproximadores lineales/bandits: rompe auditabilidad SHA-256 sin resolver cuello de botella real.

---

## 2. Matemática Exacta del Contrato Inverso (`BTCUSD`)

$$\text{GrossPnL}_{\text{LONG}}(P_0, P_1) = M \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right), \qquad \text{GrossPnL}_{\text{SHORT}}(P_0, P_1) = M \cdot L \cdot \left(\frac{P_0}{P_1} - 1\right)$$
$$C_{\text{open}} = M \cdot L \cdot f_{\text{open}}, \qquad C_{\text{close}} = M \cdot L \cdot \frac{P_0}{P_1} \cdot f_{\text{close}}, \qquad C_{\text{RT}} = C_{\text{open}} + C_{\text{close}} + C_{\text{funding}}$$
$$\text{NetPnL} = \text{GrossPnL} - C_{\text{RT}}, \qquad \text{FER} = \frac{\text{NetPnL}}{C_{\text{RT}}}, \qquad \left|\frac{P_1 - P_0}{P_0}\right|_{\text{BE}} \approx \frac{\phi}{1 \mp \phi}$$

---

## 3. Plan Secuencial (Estado Vigente: Fases 4B a 7)

### Fase 4B / 4B.1 — Persistencia JSONL `shadow_only` (`COMPLETADA` en `d38f977` / `89036e0`, Recolectando `30–50` Cierres)
- **Activo en Termux:** `logs/binance_micro_events_testnet.jsonl` en `OPEN` y `CLOSE` con `connected`, `trades_1m`, `buy_vol_1m_usd`, `sell_vol_1m_usd`, `cvd_1m_usd`, `cvd_5m_usd`, `liq_buy_1m_usd`, `liq_sell_1m_usd`, `basis_bps`, `net_pnl_sats`, `opening_fee`, `closing_fee`, `funding`.
- **Análisis offline tras $N \in [30, 50]$ cierres reales:**
  $$I_{\text{CVD},1m} = \frac{V_{\text{buy},1m} - V_{\text{sell},1m}}{\max(V_{\text{buy},1m} + V_{\text{sell},1m},\; 10^{-6})}, \qquad I_{\text{liq},1m} = \frac{L_{\text{sell},1m} - L_{\text{buy},1m}}{\max(L_{\text{sell},1m} + L_{\text{buy},1m},\; 10^{-6})}$$
  *(Validar empíricamente el signo de $I_{\text{liq},1m}$ —momentum vs reversión— sobre el dataset crudo antes de promover Fase 7A).*
- **Criterio de promoción a 7A:** $\Delta \mathbb{E} = \mathbb{E}[\text{NetPnL}\mid\text{alineado}] - \mathbb{E}[\text{NetPnL}\mid\text{contrario}] > 0$ con $\Delta\text{FER} \ge +0.15$.

### Fase 5 — Estrés de Microestructura en WSL (`SIGUIENTE ACCIÓN`, 100% `simulation/`)
- **Modelo conservador opt-in (default neutro `S0` en `simulation/`):**
  $$\delta_{\text{slip}}(t) = \delta_{\text{base}} + k_{\text{vol}} \cdot \max\left(0,\; \frac{\text{ATR}_{5m}(t)}{P_t} - \theta_{\text{vol}}\right) + 0.25 \cdot \sqrt{\frac{\Delta t_{\text{exec}}}{60}} \cdot \frac{H_t - L_t}{P_t}$$
  $$P_{\text{exec}}^{\text{entry}} = P_0 \cdot (1 + a_{\text{side}} \cdot \delta_{\text{slip}}(t)), \qquad P_{\text{exec}}^{\text{SL}} = P_{\text{SL}} \cdot (1 - a_{\text{side}} \cdot \delta_{\text{slip}}(t_{\text{exit}}))$$
- **Escenarios obligatorios (18M + OOS Fold 1):**
  1. `S0` (Nominal): $\delta_{\text{base}}=0\text{ bps}$, $\Delta t=0\text{ s}$ (paridad exacta bit a bit con `oos-fold1`).
  2. `S1` (Termux Realista): $\delta_{\text{base}}=2\text{ bps}$, $k_{\text{vol}}=0.15$, $\theta_{\text{vol}}=0.004$, $\Delta t=1.5\text{ s}$.
  3. `S2` (Pico Volatilidad): $\delta_{\text{base}}=5\text{ bps}$, $k_{\text{vol}}=0.25$, $\theta_{\text{vol}}=0.004$, $\Delta t=3.0\text{ s}$.
- **Criterio GO:** `S1` positivo en OOS 12M con `MaxDD < 4.8%` y `DDG = 0`; `S2` sin romper `HardFloorGuardian`.

### Fase 6 — Sizing Dinámico por Kelly Fraccional Acotado ($0.25\times$) bajo P0
- **Prerrequisito:** `30–50` cierres reales en Testnet sobre `89036e0` + aprobación `S1` de Fase 5.
- **Estimador conservador ($N \ge 30$, $z_{\alpha}=1.0$, $\kappa=0.25$):**
  $$p_{\text{eff}} = \max\left(0,\; p - \sqrt{\frac{p(1-p)}{N}}\right), \qquad f^*_{\text{cons}} = \max\left(0,\; p_{\text{eff}} - \frac{1 - p_{\text{eff}}}{b}\right)$$
  $$c_i = \text{clip}\left(1.0 + 0.35 \cdot \frac{\text{Edge}_{\text{pre}} - 0.06}{0.10} + 0.20 \cdot \max(0, \text{liq\_align}),\; 0.65,\; 1.35\right)$$
  $$r_i = \text{clip}(\kappa \cdot f^*_{\text{cons}} \cdot c_i,\; 0.0050,\; 0.0125) \quad (\text{forzar } 0.0050 \text{ si } \text{side\_loss\_streak} == 2)$$
- **Margen inverso exacto y compuerta HardFloor:**
  $$\ell_{\text{SL}} = \frac{|P_0 - P_{\text{SL}}|}{P_{\text{SL}}} + f_{\text{open}} + \frac{P_0}{P_{\text{SL}}} f_{\text{close}}, \qquad M_i = \left\lfloor \frac{r_i \cdot E_{\text{avail}}}{L \cdot \ell_{\text{SL}}} \right\rfloor, \qquad \sum_{j=1}^{k} \text{LossAtSL}_j + M_i L \ell_{\text{SL}} \le 0.80 \cdot B_{\text{floor}}$$

### Fase 7 — Decisiones Condicionales (Solo con Evidencia Empírica)
- **7A (`micro_bias` en `compute_live_edge`):** Solo si Fase 4B confirma $\Delta\text{FER} \ge +0.15$. Fail-open (`0.0` si `not available`, `stale` o `age_sec > 5.0s`), acotado en $[-0.08, +0.06]$ (nunca anula veto OVR `-0.35` ni macro `-0.12`).
- **7B (`LIMIT + TTL`):** Solo si tarifa maker API v3 `< 0.08%`/lado y simulador conservador supera la cota de selección adversa:
  $$\Delta_{\text{imp}} - \Delta_{\text{adv}} > E_{\text{mkt}} \cdot \frac{1 - p_{\text{fill}}}{p_{\text{fill}}}$$

---

## 4. Matriz Maestra de Compuertas Go / No-Go

| Fase | Entorno | Criterio GO | Condición NO-GO |
| :--- | :--- | :--- | :--- |
| **4B / 4B.1 (JSONL + logs)** | WSL + Termux | **COMPLETADO (`89036e0`)**: paridad exacta `oos-fold1`, `P95=0.0056 ms`, `9/9` PASS | — |
| **Ventana `30–50` cierres** | Termux Testnet | Desfase `0 sats`, tarifa `~0.16% RT`, `MaxDD < 4.5%`, `DDG = 0`, `stale < 2%` | Fugas en fees, órdenes huérfanas o caídas `tmux` |
| **5 (Estrés WSL)** | WSL 18M + Fold 1 | `S1` positivo OOS 12M, `MaxDD < 4.8%`, `DDG = 0`; `S0` bit a bit igual | `PF < 1.10` bajo latencia realista Termux |
| **6 (Kelly `0.25x`)** | WSL 18M + Testnet 30 | Mayor PnL compuesto y Sharpe/Calmar con `MaxDD < 4.5%`, `DDG = 0` | `MaxDD > +0.40%` o violación HardFloor |
| **7A / 7B (Micro / Limit)** | WSL 18M + Fold 1 | $\Delta\text{FER} \ge +0.15$ (7A) o mejora con $p_{\text{fill}} \ge 0.50$ sobre cota (7B) | Pérdida de PnL neto vs `5196b85` o degradación M16 |
