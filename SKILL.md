---
name: perpcrab
description: Use this skill to activate immediate paper trading on PumpPerps, continuously self-improve strategy settings from trade history, and only switch to live trading after explicit user confirmation plus wallet/session credentials.
---

# Perpcrab

Use this skill when the user asks to automate or run trading on PumpPerps.

## Required Behavior

- Always start in paper mode on activation.
- Always run self-improvement after each cycle.
- Never place live orders unless the user explicitly says to trade live.
- If wallet/session details are missing, ask for them in chat using the prompt templates below.

## Activation Flow (Run Immediately)

1. Validate setup:
   - `python3 scripts/quick_validate.py`
2. Start dashboard-driven paper trading immediately:
   - `python3 scripts/trader_loop.py --dashboard --cycles 9999 --sleep-seconds 10`
3. Let the script prompt for missing settings (LLM key, wallet, cookie, Kelly aggressiveness).
4. For unattended mode, run:
   - `python3 scripts/trader_loop.py --dashboard --no-prompts --cycles 9999 --sleep-seconds 10`

## Chat Prompts For Missing Info

Use these exact prompts when details are missing:

- Missing wallet:
  - `Send your Solana public wallet address (base58 public key only, not private key).`
- Missing session cookie for future live mode:
  - `When ready for live mode, send your PumpPerps session cookie string for authenticated API calls.`
- Before any live trade:
  - `Confirm live trading: reply exactly 'GO LIVE' to allow real-money order placement.`

## Live Trading Gate

Only after explicit confirmation and credentials:

1. User confirms with `GO LIVE`.
2. Wallet public key is provided and valid.
3. Session cookie is provided.
4. Run live mode:
   - `python3 scripts/trader_loop.py --live --cycles 1`

## Self-Improvement

- Uses recent realized closed trades from `data/trade_history.jsonl`.
- Paper positions persist in `data/paper_positions.json` and close via TP/SL/time-stop rules.
- Updates and persists strategy values in `data/strategy_state.json`:
  - `risk_per_trade_bps` is computed using fractional Kelly sizing on realized outcomes.
  - `max_leverage`
  - `min_signal_score`
- Runs every cycle, including paper cycles.

## Safety

- Default mode is paper mode.
- `--live` is required for real order placement.
- Invalid wallet input in paper mode is ignored and replaced with a placeholder wallet.
- Live mode rejects invalid wallet or missing cookie.

## Paper Risk Controls

Preconfigure these flags/env vars before running paper mode:

- `--paper-max-open-positions`
- `--paper-min-hold-seconds`
- `--paper-max-hold-seconds`
- `--paper-take-profit-bps`
- `--paper-stop-loss-bps`
- `--paper-noise-bps`

## Kelly Controls

Tune Kelly sizing with:

- `--kelly-fraction`
- `--min-risk-bps`
- `--max-risk-bps`

## Dashboard

Use `--dashboard` to render a live colored CLI monitor with:

- open positions
- recent closes with TP/SL/time-stop/hard-stop reason
- win/loss and win-rate
- latest LLM rationale
