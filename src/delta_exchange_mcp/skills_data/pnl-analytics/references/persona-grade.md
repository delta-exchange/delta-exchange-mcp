# Persona and grade

The interpretive layer. Numbers come from `metrics.md`; this file turns them
into a verdict.

## Trader persona

Classified from the round trips. Compute first:

```
avg_hold           = mean(hold_duration_hours)
num_assets         = distinct underlyings
asset_concentration= top asset's trade count / total trades
long_share         = count(direction == "long") / total trades
```

Flags:

| Flag | Condition |
|---|---|
| speed trader | `avg_hold < 1` hour |
| diamond hands | `avg_hold >= 24` hours |
| diversified | `num_assets >= 5` **and** `asset_concentration < 0.5` |
| high frequency | `total_trades > 500` |
| perps heavy | most-traded instrument type is `perpetual` |
| options heavy | most-traded instrument type is `call` or `put` |

Direction: long-biased above 0.6, short-biased below 0.4, otherwise "trades both
sides".

Evaluate in this order and stop at the first match:

1. diversified → **Multi-Asset Diversifier**
2. speed + perps + high frequency → **Degen Speed Trader**
3. speed + perps → **Perps Scalper**
4. diamond hands → **Diamond Hands Holder**
5. options heavy → **`<top asset>` Options Strategist**
6. perps heavy, top asset ETH → **ETH Perps Warrior**
7. perps heavy → **`<top asset>` Perps Warrior**
8. otherwise → **`<top asset>` Options Strategist**

No trades at all → **The Observer**.

Order matters. A diversified scalper is a diversifier, because breadth says more
about the approach than speed does.

## Grade: four dimensions, 25 points each

### Win Rate (25)

| Win rate | Points |
|---|---|
| ≥ 65 | 25 |
| 55–65 | `20 + (wr - 55) * 0.5` |
| 45–55 | `12 + (wr - 45) * 0.8` |
| 35–45 | `5 + (wr - 35) * 0.7` |
| < 35 | `max(0, wr * 0.14)` |

### Risk-Reward (25)

Clamp `profit_factor` and `payoff_ratio` to 5; clamp `sharpe` to [-2, 5].

```
pf_pts = min(10, profit_factor * 3.33)
pr_pts = min(8,  payoff_ratio  * 2.67)
sh_pts = min(7,  max(0, (sharpe + 0.5) * 2.33))
```

### Charges Discipline (25)

Maker share, up to 12:

| Maker fill rate | Points |
|---|---|
| ≥ 80 | 12 |
| 50–80 | `6 + (maker - 50) * 0.2` |
| 20–50 | `2 + (maker - 20) * 0.133` |
| < 20 | `maker * 0.1` |

Fee burden, up to 13. When `net_pnl > 0`, score on `fees_pct_pnl`:

| fees % of gross P&L | Points |
|---|---|
| ≤ 5 | 13 |
| 5–15 | `10 - (f - 5) * 0.3` |
| 15–30 | `7 - (f - 15) * 0.233` |
| 30–60 | `3.5 - (f - 30) * 0.117` |
| > 60 | `max(0, 1.5 - (f - 60) * 0.025)` |

When `net_pnl <= 0`, fees as a share of P&L is meaningless, so score on
`fees_pct_volume` instead: ≤ 0.02 → 10, ≤ 0.05 → 6, ≤ 0.1 → 3, else 1.

### Asymmetry & Edge (25)

```
asymmetry  = best_trade / abs(worst_trade)
kelly_full = win_rate/100 - (1 - win_rate/100) / payoff_ratio     # as a fraction
expectancy = from metrics.md

asym_pts   = min(8, min(asymmetry, 10) * 1.6)
kelly_pts  = min(8, max(0, max(kelly_full * 100, -50) * 0.4))
expect_pts = min(5, max(0, expectancy * 2)) if expectancy > 0 else 0
streak_pts = min(4, consecutive_wins * 0.5)
```

`consecutive_wins` is the best run of winning **days**, matching `metrics.md`.

### Letter

Sum the four, clamp to 0–100.

| Score | Grade | | Score | Grade |
|---|---|---|---|---|
| ≥ 93 | A+ | | ≥ 53 | C+ |
| ≥ 87 | A | | ≥ 47 | C |
| ≥ 80 | A- | | ≥ 40 | C- |
| ≥ 73 | B+ | | ≥ 33 | D+ |
| ≥ 67 | B | | ≥ 27 | D |
| ≥ 60 | B- | | < 27 | D- |

Report the letter with all four dimension scores. A B+ built on 24/25 win rate
and 6/25 charges is a different problem from one built on the reverse, and the
fix differs. For the weakest dimension, phrase it as
**"currently X → could be Y"** with the point gain from a specific, reachable
change.

The grade is arithmetic on a fill history. It is not a judgement of skill, and a
sample under about 30 round trips cannot support one — say so instead of
grading.
