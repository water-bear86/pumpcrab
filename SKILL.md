---
name: pumpperps-trader
description: Use this skill to monitor PumpPerps markets, place and close positions through PumpPerps web API routes, and run a self-improving trading loop that tunes leverage and risk from recent trade outcomes.
---

# PumpPerps Trader

Use this skill when the user asks to automate or semi-automate trading on PumpPerps.

## Safety Defaults

- Default to `--dry-run` unless the user explicitly asks for live orders.
- Never execute live trades without a valid authenticated session cookie.
- Confirm max risk settings before increasing position size or leverage.

## Required Runtime Inputs

- `PUMPPERPS_BASE_URL` (optional, default `https://pumpperps.com`)
- `PUMPPERPS_COOKIE` (required for authenticated trade operations)
- `PUMPPERPS_WALLET` (required for placing/closing positions)

## Core Workflow

1. Run preflight validation:
   - `python3 scripts/quick_validate.py`
2. Pull market universe from `/api/pools`.
3. Score candidates using 24h volume, open interest imbalance, and recent price momentum.
4. Size position using configured risk budget.
5. Place order (`POST /api/positions`) unless `--dry-run`.
6. Evaluate open positions and close with rules (`DELETE /api/positions/:id?wallet=...`).
7. Persist outcomes in `data/trade_history.jsonl`.
8. Run parameter adaptation (`scripts/trader_loop.py --improve-only`) to update:
   - `max_leverage`
   - `risk_per_trade_bps`
   - `min_signal_score`

## Self-Improvement Rules

- Use only the last `N` closed trades (default `30`) for adaptation.
- If rolling win rate drops below target, reduce `max_leverage` by 1 and lower risk by 10%.
- If rolling win rate exceeds target and drawdown is controlled, increase `max_leverage` by 1 (up to cap) and raise risk by 5%.
- Persist current strategy in `data/strategy_state.json`.
- Never mutate parameters by more than one step per cycle.

## Commands

- Dry-run loop:
  - `python3 scripts/trader_loop.py --dry-run --cycles 1`
- Live loop (requires authenticated cookie):
  - `python3 scripts/trader_loop.py --cycles 1`
- Improve parameters only:
  - `python3 scripts/trader_loop.py --improve-only`

## Notes

- PumpPerps appears to use cookie-based auth on same-origin `/api/*` routes.
- This skill does not bypass authentication; it expects a valid user session cookie.
- If endpoints or payload shape change, update `scripts/trader_loop.py` route handlers.
