# Perpcrab Skill

![Perpcrab Logo](assets/skillperppump.png)

A Codex skill and runnable Python loop for PumpPerps automation with strict safety defaults:

- immediate paper trading on activation
- continuous self-improvement of strategy parameters
- explicit opt-in required before any live order placement

## For Humans

### What this does

- Pulls pools from PumpPerps (`/api/pools`)
- Selects a candidate using simple scoring
- Opens paper positions and tracks them across cycles
- Applies configurable paper TP/SL/time-stop exits
- Optionally places live orders only with `--live`
- Updates strategy parameters from realized trade outcomes

### Safety model

- Default mode is paper mode.
- Live mode requires both:
  - valid public wallet address
  - authenticated PumpPerps session cookie
- Script will not place real orders unless `--live` is explicitly passed.

### Quick start

```bash
cd /tmp/perpcrab
python3 scripts/quick_validate.py
python3 scripts/trader_loop.py --dashboard --cycles 9999 --sleep-seconds 10
```

The dashboard flow prompts for missing LLM key, wallet, cookie, and Kelly aggressiveness unless `--no-prompts` is passed.

### Dashboard Mode

```bash
python3 scripts/trader_loop.py --dashboard --cycles 9999 --sleep-seconds 10
```

Dashboard shows:

- Open positions (opened time, unrealized PnL, side, leverage)
- Recent closed positions (TP/SL/time-stop/hard-stop reason and final PnL)
- Win/loss and win-rate
- Latest LLM reasoning output

### Paper mode examples

```bash
# one immediate paper cycle
python3 scripts/trader_loop.py --cycles 1

# continuous paper cycles
python3 scripts/trader_loop.py --cycles 9999 --sleep-seconds 15
```

### Program SL/TP Ahead Of Time

Yes. You can preconfigure paper risk controls before starting the loop:

```bash
python3 scripts/trader_loop.py --dry-run --cycles 9999 --sleep-seconds 10 \
  --paper-max-open-positions 3 \
  --paper-min-hold-seconds 90 \
  --paper-max-hold-seconds 1800 \
  --paper-take-profit-bps 600 \
  --paper-stop-loss-bps -400
```

Env var equivalents are also supported (`PERPCRAB_PAPER_*`, with `PUMPCRAB_PAPER_*` fallback).

### Long-run paper mode notes

During long paper runs, `/api/pools` may intermittently time out. This is expected and non-fatal.

- The loop continues on the next cycle.
- If cached pools are available, they are used automatically.
- Strategy self-improvement still persists each run.

Recommended long-run command:

```bash
python3 scripts/trader_loop.py --cycles 1000 --sleep-seconds 10 --request-timeout 6 --request-retries 1
```

Stop safely with `Ctrl+C` and restart with the same command.

### Live mode example

```bash
PERPCRAB_WALLET=<your_public_wallet> \
PERPCRAB_COOKIE='<session_cookie>' \
python3 scripts/trader_loop.py --live --cycles 1
```

### Self-improvement behavior

Each run updates `data/strategy_state.json` based on realized `closed` entries in `data/trade_history.jsonl` from both paper and live trades.
Paper mode now keeps open positions in `data/paper_positions.json` and only learns once they close:

- `max_leverage`
- `risk_per_trade_bps` (fractional Kelly sized, capped)
- `min_signal_score`

Run adaptation only:

```bash
python3 scripts/trader_loop.py --improve-only
```

Kelly sizing controls:

```bash
python3 scripts/trader_loop.py --improve-only \
  --kelly-fraction 0.25 \
  --min-risk-bps 25 \
  --max-risk-bps 1000
```

Formula used on rolling closed trades: `f* = W - (1-W)/R`, then fractional Kelly and safety caps.

### Files

- `SKILL.md`: agent behavior contract
- `scripts/trader_loop.py`: runner + adaptation logic
- `scripts/quick_validate.py`: integrity checks
- `data/strategy_state.json`: current strategy parameters
- `data/trade_history.jsonl`: realized trade outcome history
- `data/paper_positions.json`: open paper positions across cycles

## For Agents

### Activation protocol (must follow)

1. Run:
   - `python3 scripts/quick_validate.py`
2. Start paper trading immediately:
   - `python3 scripts/trader_loop.py --cycles 1`
3. Ask for missing details in chat:
   - `Send your Solana public wallet address (base58 public key only, not private key).`
   - `When ready for live mode, send your PumpPerps session cookie string for authenticated API calls.`
4. Before live trading, require explicit user confirmation:
   - `Confirm live trading: reply exactly 'GO LIVE' to allow real-money order placement.`
5. Only after confirmation + credentials:
   - `python3 scripts/trader_loop.py --live --cycles 1`

### Guardrails

- Never assume a private key string is a wallet address.
- Never run live mode without explicit user instruction.
- Keep running paper mode if wallet/cookie are not provided.
- Keep adaptation enabled after each cycle and ensure both paper and live closed trades are recorded to history.

### Operational notes

- Network failures are handled with retry/backoff.
- If `/api/pools` times out during long runs, the cycle logs the failure and continues.
- If pool fetch fails and cache exists, cached pools are used.
- Long-run paper trading should use explicit timeout/retry flags.
- Outputs are intentionally concise and machine-parsable where possible.

## Disclaimer

This repository is for educational and automation workflow purposes. You are responsible for risk, compliance, and security when enabling live trading.
