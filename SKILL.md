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
2. Start immediate paper trading cycle:
   - `python3 scripts/trader_loop.py --cycles 1`
3. If user wants continuous paper mode, run:
   - `python3 scripts/trader_loop.py --cycles 9999 --sleep-seconds 15`

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

- Uses recent closed trades from `data/trade_history.jsonl`.
- Updates and persists strategy values in `data/strategy_state.json`:
  - `max_leverage`
  - `risk_per_trade_bps`
  - `min_signal_score`
- Runs every cycle, including paper cycles.

## Safety

- Default mode is paper mode.
- `--live` is required for real order placement.
- Invalid wallet input in paper mode is ignored and replaced with a placeholder wallet.
- Live mode rejects invalid wallet or missing cookie.
