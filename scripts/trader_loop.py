#!/usr/bin/env python3
import argparse
import contextlib
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "strategy_state.json"
HISTORY_PATH = DATA_DIR / "trade_history.jsonl"
POOLS_CACHE_PATH = DATA_DIR / "pools_cache.json"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PAPER_WALLET = "PAPER_TRADING_WALLET"
PAPER_POSITIONS_PATH = DATA_DIR / "paper_positions.json"

BRAND_ART = [
    "░░░░▄█▀▀▀░░░░░░░░▀▀▀█▄░░░░",
    "░░▄███▄▄░░▀▄██▄▀░░▄▄███▄░░",
    "░░▀██▄▄▄▄██PERP██▄▄▄▄██▀░░",
    "░░░░░▄▄▄▄█░CRAB░█▄▄▄▄░░░░░",
    "░░░░▞▐▀▐▀░▀██████▀░▀▌▚░░░░",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def load_history(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_history(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def load_paper_positions(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("positions"), list):
        return [x for x in payload["positions"] if isinstance(x, dict)]
    return []


def save_paper_positions(path: Path, positions: List[Dict[str, Any]]) -> None:
    save_json(path, {"updated_at": now_iso(), "positions": positions})


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_iso_timestamp(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("TERM", "") not in {"", "dumb"}


def color_text(text: str, color: str) -> str:
    if not supports_color():
        return text
    palette = {
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "bold": "\033[1m",
        "dim": "\033[2m",
    }
    prefix = palette.get(color, "")
    suffix = "\033[0m" if prefix else ""
    return f"{prefix}{text}{suffix}"


def bicolor_text(text: str, left_color: str = "green", right_color: str = "red") -> str:
    if not text:
        return text
    mid = max(1, len(text) // 2)
    left = color_text(text[:mid], left_color)
    right = color_text(text[mid:], right_color)
    return left + right


def render_brand_block() -> None:
    for line in BRAND_ART:
        print(bicolor_text(line, "green", "red"))
    print()


def truncate_text(value: Any, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def build_table_lines(
    headers: List[str],
    rows: List[List[Any]],
    aligns: Optional[List[str]] = None,
    max_col_width: int = 44,
) -> Tuple[List[str], List[int]]:
    col_count = len(headers)
    widths = [min(max(4, len(str(h))), max_col_width) for h in headers]

    for row in rows:
        for i in range(col_count):
            cell = row[i] if i < len(row) else ""
            widths[i] = min(max(widths[i], len(str(cell))), max_col_width)

    def _format_row(row: List[Any]) -> str:
        cells: List[str] = []
        for i, width in enumerate(widths):
            align = aligns[i] if aligns and i < len(aligns) else "left"
            value = row[i] if i < len(row) else ""
            text = truncate_text(value, width)
            if align == "right":
                padded = text.rjust(width)
            elif align == "center":
                padded = text.center(width)
            else:
                padded = text.ljust(width)
            cells.append(f" {padded} ")
        return "│" + "│".join(cells) + "│"

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    lines = [top, _format_row(headers), mid]
    lines.extend(_format_row(r) for r in rows)
    lines.append(bot)
    return lines, widths


def render_table(
    title: str,
    headers: List[str],
    rows: List[List[Any]],
    aligns: Optional[List[str]] = None,
    row_colors: Optional[List[str]] = None,
    max_col_width: int = 44,
) -> None:
    print(color_text(title, "bold"))
    lines, _ = build_table_lines(headers, rows, aligns=aligns, max_col_width=max_col_width)
    for idx, line in enumerate(lines):
        if idx in {0, 2, len(lines) - 1}:
            print(color_text(line, "dim"))
            continue
        data_row_idx = idx - 3
        if row_colors and 0 <= data_row_idx < len(row_colors) and row_colors[data_row_idx]:
            print(color_text(line, row_colors[data_row_idx]))
        else:
            print(line)
    print()


def wrap_text(text: str, width: int = 80) -> List[str]:
    tokens = text.split()
    if not tokens:
        return [""]
    lines: List[str] = []
    current = ""
    for token in tokens:
        candidate = token if not current else f"{current} {token}"
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        if len(token) > width:
            lines.append(token[:width])
            current = token[width:]
        else:
            current = token
    if current:
        lines.append(current)
    return lines


def short_token(value: Any) -> str:
    token = str(value or "-")
    if len(token) <= 16:
        return token
    return f"{token[:6]}...{token[-6:]}"


def mask_value(value: str, visible: int = 4) -> str:
    if not value:
        return "-"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def prompt_text(prompt: str, default: Optional[str] = None, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        return default or ""
    try:
        raw = getpass(prompt) if secret else input(prompt)
    except EOFError:
        return default or ""
    value = raw.strip()
    if not value and default is not None:
        return default
    return value


def prompt_runtime_setup(args: argparse.Namespace, paper_mode: bool) -> None:
    if args.no_prompts or not sys.stdin.isatty():
        return

    if not args.llm_api_key:
        args.llm_api_key = prompt_text("Enter LLM API key (input hidden): ", secret=True)

    if not args.wallet.strip():
        wallet_prompt = "Enter Solana public wallet (base58"
        if paper_mode:
            wallet_prompt += ", optional for paper mode"
        wallet_prompt += "): "
        args.wallet = prompt_text(wallet_prompt, default="")

    if not args.cookie:
        args.cookie = prompt_text("Enter PumpPerps session cookie (optional unless live mode): ", default="")

    aggr = prompt_text(
        "Kelly aggressiveness [low/medium/high/custom 0-1] (default medium): ",
        default="medium",
    ).strip().lower()
    presets = {
        "low": 0.15,
        "conservative": 0.15,
        "medium": 0.25,
        "balanced": 0.25,
        "high": 0.4,
        "aggressive": 0.4,
    }
    if aggr in presets:
        args.kelly_fraction = presets[aggr]
    else:
        try:
            args.kelly_fraction = clamp(float(aggr), 0.0, 1.0)
        except ValueError:
            pass


def parse_cycle_output(cycle_output: str) -> Tuple[str, Dict[str, int], List[str]]:
    lines = [ln.strip() for ln in cycle_output.splitlines() if ln.strip()]
    activity = {"opened": 0, "closed": 0, "skipped": 0, "errors": 0}
    events: List[str] = []

    for line in lines:
        low = line.lower()
        if "opened simulated position" in low:
            activity["opened"] += 1
            events.append(line)
        elif "closed simulated trade" in low or "closed positions this cycle" in low:
            activity["closed"] += 1
            events.append(line)
        elif "skipping" in low or "already open" in low:
            activity["skipped"] += 1
            events.append(line)
        elif "failure" in low or "error" in low:
            activity["errors"] += 1
            events.append(line)
        elif line.startswith("llm decision:"):
            events.append(line)

    headline = events[-1] if events else (lines[-1] if lines else "")
    return headline, activity, events[-6:]


def render_dashboard(
    args: argparse.Namespace,
    state: Dict[str, Any],
    paper_mode: bool,
    wallet: str,
    cycle_index: int,
    total_cycles: int,
    last_event: str = "",
    last_error: str = "",
    halted: bool = False,
    cycle_activity: Optional[Dict[str, int]] = None,
    recent_events: Optional[List[str]] = None,
) -> None:
    open_positions = load_paper_positions(PAPER_POSITIONS_PATH)
    history = load_history(HISTORY_PATH)
    closed = [x for x in history if x.get("status") == "closed"]

    wins = sum(1 for x in closed if safe_float(x.get("pnl_usd"), 0.0) > 0)
    losses = sum(1 for x in closed if safe_float(x.get("pnl_usd"), 0.0) <= 0)
    total_closed = len(closed)
    win_rate = (wins / total_closed) if total_closed else 0.0

    recent_closed = closed[-8:]
    latest_rationale = ""
    for source in (reversed(open_positions), reversed(recent_closed)):
        for row in source:
            rationale = str(row.get("llm_rationale") or "").strip()
            if rationale:
                latest_rationale = rationale
                break
        if latest_rationale:
            break

    if supports_color():
        print("[2J[H", end="")

    render_brand_block()

    mode_label = "paper" if paper_mode else "live"
    if cycle_activity is None:
        cycle_activity = {"opened": 0, "closed": 0, "skipped": 0, "errors": 0}

    session_rows = [
        ["mode", mode_label],
        ["cycle", f"{cycle_index}/{total_cycles}"],
        ["timestamp", now_iso()],
        ["wallet", mask_value(wallet)],
        ["llm model", args.llm_model],
        ["kelly", state.get("kelly_fraction", args.kelly_fraction)],
        ["risk bps", state.get("risk_per_trade_bps")],
        ["open positions", len(open_positions)],
        ["closed trades", total_closed],
        ["win/loss", f"{wins}/{losses}"],
        ["win rate", f"{win_rate:.1%}"],
    ]
    render_table("Session", ["Field", "Value"], session_rows, max_col_width=56)

    activity_rows = [
        ["opened", cycle_activity.get("opened", 0)],
        ["closed", cycle_activity.get("closed", 0)],
        ["skipped", cycle_activity.get("skipped", 0)],
        ["errors", cycle_activity.get("errors", 0)],
    ]
    render_table("Cycle Activity", ["Metric", "Count"], activity_rows, aligns=["left", "right"])

    event_items = list(recent_events or [])[-8:]
    if last_error:
        event_items.append(f"ERROR: {last_error}")
    elif last_event:
        event_items.append(f"INFO: {last_event}")
    elif halted:
        event_items.append("ERROR: loop halted due to consecutive failures")

    event_rows = [[idx + 1, ev] for idx, ev in enumerate(event_items[-8:])]
    event_colors = []
    for _, ev in event_rows:
        low = str(ev).lower()
        if "error" in low or "failure" in low:
            event_colors.append("red")
        elif "opened" in low or "take_profit" in low or "win" in low:
            event_colors.append("green")
        else:
            event_colors.append("cyan")
    if not event_rows:
        event_rows = [[1, "(no events yet)"]]
        event_colors = ["dim"]
    render_table(
        "Event Stream",
        ["#", "Event"],
        event_rows,
        aligns=["right", "left"],
        row_colors=event_colors,
        max_col_width=80,
    )

    open_rows: List[List[Any]] = []
    open_colors: List[str] = []
    for pos in open_positions[-10:]:
        pnl_bps = safe_float(pos.get("unrealized_pnl_bps"), 0.0)
        pnl_usd = safe_float(pos.get("unrealized_pnl_usd"), 0.0)
        open_rows.append(
            [
                short_token(pos.get("tokenMint")),
                pos.get("side"),
                pos.get("leverage"),
                pos.get("age_seconds", "-"),
                f"{pnl_bps:.2f}",
                f"{pnl_usd:.2f}",
                pos.get("opened_at", "-"),
            ]
        )
        open_colors.append("green" if pnl_usd >= 0 else "red")
    if not open_rows:
        open_rows = [["-", "-", "-", "-", "-", "-", "(none)"]]
        open_colors = ["dim"]
    render_table(
        "Open Positions",
        ["Token", "Side", "Lev", "Age(s)", "uPnL bps", "uPnL USD", "Opened"],
        open_rows,
        aligns=["left", "left", "right", "right", "right", "right", "left"],
        row_colors=open_colors,
        max_col_width=30,
    )

    closed_rows: List[List[Any]] = []
    closed_colors: List[str] = []
    for row in recent_closed:
        pnl_usd = safe_float(row.get("pnl_usd"), 0.0)
        pnl_bps = safe_float(row.get("pnl_bps"), 0.0)
        reason = str(row.get("close_reason") or "live_close")
        result = "WIN" if pnl_usd > 0 else "LOSS"
        closed_rows.append(
            [
                result,
                short_token(row.get("tokenMint")),
                reason,
                f"{pnl_bps:.2f}",
                f"{pnl_usd:.2f}",
                row.get("opened_at", "-"),
                row.get("closed_at", "-"),
            ]
        )
        closed_colors.append("green" if pnl_usd > 0 else "red")
    if not closed_rows:
        closed_rows = [["-", "-", "-", "-", "-", "-", "(none)"]]
        closed_colors = ["dim"]
    render_table(
        "Recent Closed Positions",
        ["Result", "Token", "Reason", "PnL bps", "PnL USD", "Opened", "Closed"],
        closed_rows,
        aligns=["left", "left", "left", "right", "right", "left", "left"],
        row_colors=closed_colors,
        max_col_width=30,
    )

    reasoning = latest_rationale if latest_rationale else "(no rationale captured yet)"
    reasoning_lines = wrap_text(reasoning, width=78)
    reasoning_rows = [[idx + 1, line] for idx, line in enumerate(reasoning_lines[:8])]
    render_table(
        "Latest LLM Reasoning",
        ["Ln", "Text"],
        reasoning_rows,
        aligns=["right", "left"],
        max_col_width=78,
    )



def request_json(
    base_url: str,
    route: str,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    cookie: Optional[str] = None,
    timeout_seconds: float = 20.0,
    retries: int = 2,
    backoff_seconds: float = 1.25,
) -> Any:
    url = base_url.rstrip("/") + route
    headers = {
        "Accept": "application/json",
        "User-Agent": "perpcrab/1.4",
    }
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie

    retryable_status = {408, 425, 429, 500, 502, 503, 504}
    max_attempts = max(1, int(retries) + 1)

    for attempt in range(max_attempts):
        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            can_retry = exc.code in retryable_status and attempt < (max_attempts - 1)
            if can_retry:
                time.sleep(backoff_seconds * (2**attempt))
                continue
            raise RuntimeError(f"HTTP {exc.code} {method} {route}: {raw[:400]}") from exc
        except (TimeoutError, error.URLError) as exc:
            if attempt < (max_attempts - 1):
                time.sleep(backoff_seconds * (2**attempt))
                continue
            if isinstance(exc, TimeoutError):
                raise RuntimeError(f"Timeout for {method} {route}: {exc}") from exc
            raise RuntimeError(f"Network error for {method} {route}: {exc}") from exc

    raise RuntimeError(f"Failed {method} {route} after {max_attempts} attempts")


def decode_base58(value: str) -> bytes:
    n = 0
    for ch in value:
        idx = BASE58_ALPHABET.find(ch)
        if idx == -1:
            raise ValueError(f"invalid base58 character: {ch}")
        n = n * 58 + idx

    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return (b"\x00" * leading_zeros) + raw


def is_probably_solana_pubkey(value: str) -> bool:
    if not (32 <= len(value) <= 44):
        return False
    try:
        decoded = decode_base58(value)
    except ValueError:
        return False
    return len(decoded) == 32


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_pool(pool: Dict[str, Any]) -> float:
    volume = float(pool.get("volume24h") or pool.get("volume") or 0.0)
    long_oi = float(pool.get("longOi") or 0.0)
    short_oi = float(pool.get("shortOi") or 0.0)
    tvl = float(pool.get("tvl") or 0.0)

    liquidity_term = min(volume / 20000.0, 1.5)
    imbalance = abs(long_oi - short_oi) / max(long_oi + short_oi, 1.0)
    depth_term = min(tvl / 50000.0, 1.2)
    return liquidity_term * 0.45 + (1.0 - imbalance) * 0.25 + depth_term * 0.30


def rank_candidates(pools: List[Dict[str, Any]], min_signal_score: float) -> List[Dict[str, Any]]:
    scored = []
    for pool in pools:
        s = score_pool(pool)
        if s >= min_signal_score:
            row = dict(pool)
            row["signal_score"] = s
            scored.append(row)
    scored.sort(key=lambda p: float(p["signal_score"]), reverse=True)
    return scored


def improve(state: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    lookback = int(state.get("lookback_trades", 30))
    target_win_rate = float(state.get("target_win_rate", 0.55))
    max_dd_bps = float(state.get("max_drawdown_bps", 1200))

    kelly_fraction = clamp(float(state.get("kelly_fraction", 0.25)), 0.0, 1.0)
    min_risk_bps = max(1.0, float(state.get("min_risk_bps", 25.0)))
    max_risk_bps = max(min_risk_bps, float(state.get("max_risk_bps", 1000.0)))

    closed = [x for x in history if x.get("status") == "closed"]
    sample = closed[-lookback:]
    if len(sample) < 5:
        state["updated_at"] = now_iso()
        state["kelly_fraction"] = round(kelly_fraction, 4)
        state["min_risk_bps"] = round(min_risk_bps, 2)
        state["max_risk_bps"] = round(max_risk_bps, 2)
        return state

    pnl_bps_values = [safe_float(x.get("pnl_bps"), 0.0) for x in sample]
    wins_bps = [x for x in pnl_bps_values if x > 0.0]
    losses_bps_abs = [abs(x) for x in pnl_bps_values if x < 0.0]

    win_rate = len(wins_bps) / len(sample)
    avg_pnl_bps = sum(pnl_bps_values) / len(pnl_bps_values)
    worst_pnl_bps = min(pnl_bps_values)
    avg_win_bps = sum(wins_bps) / len(wins_bps) if wins_bps else 0.0
    avg_loss_bps_abs = sum(losses_bps_abs) / len(losses_bps_abs) if losses_bps_abs else 0.0

    payoff_ratio = (avg_win_bps / avg_loss_bps_abs) if avg_loss_bps_abs > 0 else 0.0
    if payoff_ratio > 0:
        kelly_full = win_rate - ((1.0 - win_rate) / payoff_ratio)
    else:
        kelly_full = 0.0
    kelly_full = clamp(kelly_full, -1.0, 1.0)

    kelly_fractional = max(0.0, kelly_full) * kelly_fraction
    kelly_risk_bps = clamp(kelly_fractional * 10000.0, min_risk_bps, max_risk_bps)

    max_leverage = int(state.get("max_leverage", 3))
    current_risk_bps = float(state.get("risk_per_trade_bps", 100))
    signal = float(state.get("min_signal_score", 0.55))
    risk_bps = kelly_risk_bps

    if win_rate < target_win_rate or worst_pnl_bps < -max_dd_bps:
        max_leverage = max(1, max_leverage - 1)
        risk_bps = clamp(min(risk_bps, current_risk_bps * 0.9), min_risk_bps, max_risk_bps)
        signal = clamp(signal + 0.03, 0.40, 0.95)
    elif win_rate >= target_win_rate and avg_pnl_bps > 0 and worst_pnl_bps > -(max_dd_bps * 0.7):
        max_leverage = min(10, max_leverage + 1)
        risk_bps = clamp((current_risk_bps * 0.4) + (risk_bps * 0.6), min_risk_bps, max_risk_bps)
        signal = clamp(signal - 0.02, 0.35, 0.95)

    state["kelly_fraction"] = round(kelly_fraction, 4)
    state["min_risk_bps"] = round(min_risk_bps, 2)
    state["max_risk_bps"] = round(max_risk_bps, 2)
    state["max_leverage"] = int(max_leverage)
    state["risk_per_trade_bps"] = round(float(risk_bps), 2)
    state["min_signal_score"] = round(float(signal), 4)
    state["updated_at"] = now_iso()
    state["last_metrics"] = {
        "sample_size": len(sample),
        "win_rate": round(win_rate, 4),
        "avg_pnl_bps": round(avg_pnl_bps, 2),
        "worst_pnl_bps": round(worst_pnl_bps, 2),
        "avg_win_bps": round(avg_win_bps, 2),
        "avg_loss_bps_abs": round(avg_loss_bps_abs, 2),
        "payoff_ratio": round(payoff_ratio, 4),
        "kelly_full": round(kelly_full, 4),
        "kelly_fractional": round(kelly_fractional, 4),
        "kelly_risk_bps": round(kelly_risk_bps, 2),
    }
    return state


def get_pools(base_url: str, request_timeout: float, request_retries: int) -> List[Dict[str, Any]]:
    try:
        data = request_json(
            base_url,
            "/api/pools",
            "GET",
            timeout_seconds=request_timeout,
            retries=request_retries,
        )
    except RuntimeError as exc:
        print(f"failed to fetch pools: {exc}")
        if POOLS_CACHE_PATH.exists():
            try:
                with POOLS_CACHE_PATH.open("r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list):
                    print("using cached pools data")
                    return [x for x in cached if isinstance(x, dict)]
            except (OSError, json.JSONDecodeError):
                pass
        return []

    if not isinstance(data, list):
        return []

    save_json(POOLS_CACHE_PATH, data)
    return [x for x in data if isinstance(x, dict)]


def get_positions(
    base_url: str,
    wallet: str,
    cookie: Optional[str],
    request_timeout: float,
    request_retries: int,
) -> List[Dict[str, Any]]:
    route = f"/api/positions/{parse.quote(wallet)}"
    data = request_json(
        base_url,
        route,
        "GET",
        cookie=cookie,
        timeout_seconds=request_timeout,
        retries=request_retries,
    )
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return [x for x in data["positions"] if isinstance(x, dict)]
    return []


def parse_json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\\n", "", raw)
        raw = re.sub(r"\\n```$", "", raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response was not a JSON object")
    return parsed


def llm_trade_decision(
    args: argparse.Namespace,
    state: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    paper_mode: bool,
) -> Dict[str, Any]:
    api_key = args.llm_api_key
    if not api_key:
        raise RuntimeError("LLM decision required but PERPCRAB_OPENAI_API_KEY / PUMPCRAB_OPENAI_API_KEY / OPENAI_API_KEY is missing")

    if not candidates:
        raise RuntimeError("No candidates available for LLM decision")

    compact = []
    for c in candidates:
        compact.append(
            {
                "tokenMint": c.get("tokenMint"),
                "tokenTicker": c.get("tokenTicker"),
                "signal_score": round(float(c.get("signal_score") or 0.0), 4),
                "volume24h": float(c.get("volume24h") or c.get("volume") or 0.0),
                "longOi": float(c.get("longOi") or 0.0),
                "shortOi": float(c.get("shortOi") or 0.0),
                "tvl": float(c.get("tvl") or 0.0),
            }
        )

    system_prompt = (
        "You are a strict trading policy model. Choose exactly one candidate and one side. "
        "Return ONLY JSON with keys: tokenMint, side, confidence, rationale. "
        "side must be long or short. confidence must be 0..1."
    )
    user_payload = {
        "mode": "paper" if paper_mode else "live",
        "strategy_state": {
            "max_leverage": state.get("max_leverage"),
            "risk_per_trade_bps": state.get("risk_per_trade_bps"),
            "min_signal_score": state.get("min_signal_score"),
            "target_win_rate": state.get("target_win_rate"),
        },
        "candidates": compact,
        "instruction": "Pick one candidate tokenMint from candidates and a side.",
    }

    body = {
        "model": args.llm_model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
    }

    endpoint = args.llm_api_base.rstrip("/") + "/chat/completions"
    req = request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "perpcrab/1.4",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=args.llm_timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {exc.code}: {raw[:500]}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
        decision = parse_json_object(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid LLM response shape: {exc}") from exc

    token_mint = decision.get("tokenMint")
    side = str(decision.get("side") or "").lower()
    confidence = decision.get("confidence")
    rationale = str(decision.get("rationale") or "")

    allowed_mints = {str(c.get("tokenMint")) for c in candidates if c.get("tokenMint")}
    if token_mint not in allowed_mints:
        raise RuntimeError("LLM selected tokenMint outside allowed candidate set")
    if side not in {"long", "short"}:
        raise RuntimeError("LLM side must be long or short")

    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        raise RuntimeError("LLM confidence must be numeric")
    if conf < 0.0 or conf > 1.0:
        raise RuntimeError("LLM confidence must be between 0 and 1")

    return {
        "tokenMint": token_mint,
        "side": side,
        "confidence": round(conf, 4),
        "rationale": rationale[:400],
    }


def maybe_close_positions(
    base_url: str,
    wallet: str,
    cookie: str,
    dry_run: bool,
    request_timeout: float,
    request_retries: int,
) -> int:
    closed = 0
    positions = get_positions(base_url, wallet, cookie, request_timeout, request_retries)
    for p in positions:
        position_id = p.get("id") or p.get("positionId")
        pnl_bps = float(p.get("pnlBps") or p.get("pnl_bps") or 0.0)
        if position_id is None:
            continue
        should_close = pnl_bps >= 900 or pnl_bps <= -700
        if not should_close:
            continue
        route = f"/api/positions/{parse.quote(str(position_id))}?wallet={parse.quote(wallet)}"
        if dry_run:
            print(f"[paper] close position id={position_id} pnl_bps={pnl_bps}")
        else:
            request_json(
                base_url,
                route,
                "DELETE",
                cookie=cookie,
                timeout_seconds=request_timeout,
                retries=request_retries,
            )
            print(f"closed position id={position_id} pnl_bps={pnl_bps}")
            record_live_close_trade(p)
        closed += 1
    return closed


def resolve_pool_id(base_url: str, candidate: Dict[str, Any]) -> Optional[str]:
    _ = base_url
    for key in ("poolId", "id", "pool_id"):
        value = candidate.get(key)
        if value is not None and str(value).strip() != "":
            return str(value)

    token_mint = candidate.get("tokenMint") or candidate.get("mint")
    if token_mint is not None and str(token_mint).strip() != "":
        return str(token_mint)

    return None


def open_position(
    base_url: str,
    wallet: str,
    side: str,
    cookie: Optional[str],
    candidate: Dict[str, Any],
    state: Dict[str, Any],
    dry_run: bool,
    request_timeout: float,
    request_retries: int,
) -> Dict[str, Any]:
    collateral = round(max(5.0, float(state.get("risk_per_trade_bps", 100)) / 10.0), 2)
    leverage = int(state.get("max_leverage", 3))
    pool_id = resolve_pool_id(base_url, candidate)

    if not pool_id:
        raise RuntimeError("Could not resolve poolId for selected candidate")

    payload = {
        "wallet": wallet,
        "side": side,
        "collateral": collateral,
        "leverage": leverage,
        "poolId": pool_id,
        "tokenMint": candidate.get("tokenMint"),
    }

    if dry_run:
        print("[paper] open position payload=", json.dumps(payload, sort_keys=True))
        return payload

    if not cookie:
        raise RuntimeError("PERPCRAB_COOKIE is required for live trade execution")

    response = request_json(
        base_url,
        "/api/positions",
        "POST",
        payload,
        cookie=cookie,
        timeout_seconds=request_timeout,
        retries=request_retries,
    )
    print("opened position:", json.dumps(response, sort_keys=True)[:600])
    return payload


def build_paper_position(
    candidate: Dict[str, Any],
    payload: Dict[str, Any],
    decision: Dict[str, Any],
    llm_model: str,
) -> Dict[str, Any]:
    opened_at = now_iso()
    score = safe_float(candidate.get("signal_score"), 0.5)
    leverage = max(1.0, safe_float(payload.get("leverage"), 1.0))
    collateral = max(0.0, safe_float(payload.get("collateral"), 0.0))
    notional = collateral * leverage

    return {
        "mode": "paper",
        "status": "open",
        "opened_at": opened_at,
        "last_eval_at": opened_at,
        "tokenMint": payload.get("tokenMint"),
        "poolId": payload.get("poolId"),
        "side": str(payload.get("side") or "long").lower(),
        "collateral": round(collateral, 2),
        "leverage": round(leverage, 4),
        "notional_usd": round(notional, 2),
        "entry_signal_score": round(score, 4),
        "unrealized_pnl_bps": 0.0,
        "unrealized_pnl_usd": 0.0,
        "llm_model": llm_model,
        "llm_confidence": decision.get("confidence"),
        "llm_rationale": decision.get("rationale"),
    }


def simulate_paper_unrealized_pnl_bps(position: Dict[str, Any], current_signal_score: float, paper_noise_bps: float) -> float:
    entry_signal_score = safe_float(position.get("entry_signal_score"), 0.5)
    market_move_bps = (current_signal_score - entry_signal_score) * 1800.0 + random.gauss(0.0, paper_noise_bps)
    side = str(position.get("side") or "long").lower()
    pnl_bps = market_move_bps if side == "long" else -market_move_bps
    return clamp(pnl_bps, -2500.0, 2500.0)


def evaluate_and_close_paper_positions(
    args: argparse.Namespace,
    open_positions: List[Dict[str, Any]],
    pool_by_token: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    now_dt = datetime.now(timezone.utc)
    take_profit_bps = abs(safe_float(args.paper_take_profit_bps, 600.0))
    stop_loss_bps = -abs(safe_float(args.paper_stop_loss_bps, -400.0))
    min_hold_seconds = max(0.0, safe_float(args.paper_min_hold_seconds, 90.0))
    max_hold_seconds = max(min_hold_seconds, safe_float(args.paper_max_hold_seconds, 1800.0))

    remaining: List[Dict[str, Any]] = []
    closed_count = 0

    for position in open_positions:
        token_mint = str(position.get("tokenMint") or "")
        opened_at = position.get("opened_at") or now_iso()
        opened_dt = parse_iso_timestamp(opened_at)
        age_seconds = max(0.0, (now_dt - opened_dt).total_seconds())

        pool = pool_by_token.get(token_mint)
        if pool is None:
            current_signal_score = safe_float(position.get("entry_signal_score"), 0.5)
        else:
            current_signal_score = score_pool(pool)

        pnl_bps = simulate_paper_unrealized_pnl_bps(position, current_signal_score, args.paper_noise_bps)
        notional = max(0.0, safe_float(position.get("notional_usd"), 0.0))
        pnl_usd = round(notional * (pnl_bps / 10000.0), 2)

        position["last_eval_at"] = now_iso()
        position["age_seconds"] = round(age_seconds, 2)
        position["current_signal_score"] = round(current_signal_score, 4)
        position["unrealized_pnl_bps"] = round(pnl_bps, 2)
        position["unrealized_pnl_usd"] = pnl_usd

        hit_take_profit = pnl_bps >= take_profit_bps
        hit_stop_loss = pnl_bps <= stop_loss_bps
        hit_time_stop = age_seconds >= max_hold_seconds
        hold_matured = age_seconds >= min_hold_seconds
        hard_stop = pnl_bps <= (stop_loss_bps * 1.5)

        should_close = (hold_matured and (hit_take_profit or hit_stop_loss or hit_time_stop)) or hard_stop
        if not should_close:
            remaining.append(position)
            continue

        if hard_stop:
            close_reason = "hard_stop"
        elif hit_take_profit:
            close_reason = "take_profit"
        elif hit_stop_loss:
            close_reason = "stop_loss"
        elif hit_time_stop:
            close_reason = "time_stop"
        else:
            close_reason = "rule_close"

        row = {
            "mode": "paper",
            "status": "closed",
            "opened_at": opened_at,
            "closed_at": now_iso(),
            "tokenMint": position.get("tokenMint"),
            "poolId": position.get("poolId"),
            "side": position.get("side"),
            "collateral": round(safe_float(position.get("collateral"), 0.0), 2),
            "leverage": round(safe_float(position.get("leverage"), 1.0), 4),
            "notional_usd": round(notional, 2),
            "signal_score": round(safe_float(position.get("entry_signal_score"), 0.5), 4),
            "exit_signal_score": round(current_signal_score, 4),
            "pnl_bps": round(pnl_bps, 2),
            "pnl_usd": pnl_usd,
            "close_reason": close_reason,
            "holding_seconds": round(age_seconds, 2),
            "llm_model": position.get("llm_model"),
            "llm_confidence": position.get("llm_confidence"),
            "llm_rationale": position.get("llm_rationale"),
        }
        append_history(HISTORY_PATH, row)
        closed_count += 1
        print(
            "[paper] closed simulated trade",
            f"side={row['side']}",
            f"token={row['tokenMint']}",
            f"pnl_bps={row['pnl_bps']}",
            f"pnl_usd={row['pnl_usd']}",
            f"reason={close_reason}",
        )

    return remaining, closed_count


def record_live_close_trade(position: Dict[str, Any]) -> None:
    def _f(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    pnl_bps = _f(position.get("pnlBps") or position.get("pnl_bps"), 0.0)
    leverage = max(1.0, _f(position.get("leverage"), 1.0))
    collateral = _f(position.get("collateral") or position.get("collateralUsd"), 0.0)
    notional = _f(position.get("notionalUsd") or position.get("notional_usd"), collateral * leverage)

    pnl_usd_value = position.get("pnlUsd")
    if pnl_usd_value is None:
        pnl_usd_value = position.get("pnl_usd")
    pnl_usd = round(_f(pnl_usd_value, notional * (pnl_bps / 10000.0)), 2)

    row = {
        "mode": "live",
        "status": "closed",
        "opened_at": position.get("openedAt") or position.get("opened_at"),
        "closed_at": now_iso(),
        "tokenMint": position.get("tokenMint") or position.get("token_mint"),
        "poolId": position.get("poolId") or position.get("pool_id"),
        "side": position.get("side"),
        "collateral": round(collateral, 2),
        "leverage": round(leverage, 4),
        "notional_usd": round(notional, 2),
        "pnl_bps": round(pnl_bps, 2),
        "pnl_usd": pnl_usd,
    }
    append_history(HISTORY_PATH, row)
    print(
        "[live] recorded closed trade",
        f"token={row['tokenMint']}",
        f"pnl_bps={row['pnl_bps']}",
        f"pnl_usd={row['pnl_usd']}",
    )


def cycle(args: argparse.Namespace, state: Dict[str, Any], paper_mode: bool, wallet: str) -> None:
    pools = get_pools(args.base_url, args.request_timeout, args.request_retries)
    if not pools:
        raise RuntimeError("no pools returned from API")

    ranked = rank_candidates(pools, float(state.get("min_signal_score", 0.55)))
    if not ranked:
        raise RuntimeError("no pool passed min_signal_score")

    pool_by_token: Dict[str, Dict[str, Any]] = {}
    for pool in pools:
        token_mint = pool.get("tokenMint") or pool.get("mint")
        if token_mint:
            pool_by_token[str(token_mint)] = pool

    open_positions: List[Dict[str, Any]] = []
    if paper_mode:
        open_positions = load_paper_positions(PAPER_POSITIONS_PATH)
        open_positions, closed_count = evaluate_and_close_paper_positions(args, open_positions, pool_by_token)
        if closed_count:
            print(f"[paper] closed positions this cycle: {closed_count}")
        save_paper_positions(PAPER_POSITIONS_PATH, open_positions)

    candidates = ranked[: max(1, args.llm_candidate_count)]
    decision = llm_trade_decision(args, state, candidates, paper_mode)

    selected = None
    for c in candidates:
        if str(c.get("tokenMint")) == decision["tokenMint"]:
            selected = c
            break
    if selected is None:
        raise RuntimeError("LLM selected candidate not found after validation")

    print(
        "llm decision:",
        f"token={decision['tokenMint']}",
        f"side={decision['side']}",
        f"confidence={decision['confidence']}",
    )

    if paper_mode:
        max_open_positions = max(1, int(args.paper_max_open_positions))
        selected_token = str(selected.get("tokenMint") or "")
        open_tokens = {str(p.get("tokenMint")) for p in open_positions if p.get("tokenMint")}

        if len(open_positions) >= max_open_positions:
            print(f"[paper] max open positions reached ({len(open_positions)}/{max_open_positions}); skipping entry")
            return

        if selected_token and selected_token in open_tokens:
            alternate = None
            for candidate in candidates:
                candidate_token = str(candidate.get("tokenMint") or "")
                if candidate_token and candidate_token not in open_tokens:
                    alternate = candidate
                    break
            if alternate is None:
                print(f"[paper] token already open token={selected_token}; no alternate candidate available")
                return
            selected = alternate
            selected_token = str(selected.get("tokenMint") or "")
            print(f"[paper] selected token already open; using alternate token={selected_token}")

    opened_payload = open_position(
        base_url=args.base_url,
        wallet=wallet,
        side=decision["side"],
        cookie=args.cookie,
        candidate=selected,
        state=state,
        dry_run=paper_mode,
        request_timeout=args.request_timeout,
        request_retries=args.request_retries,
    )

    if paper_mode:
        open_position_row = build_paper_position(selected, opened_payload, decision, args.llm_model)
        open_positions.append(open_position_row)
        save_paper_positions(PAPER_POSITIONS_PATH, open_positions)
        print(
            "[paper] opened simulated position",
            f"side={open_position_row['side']}",
            f"token={open_position_row['tokenMint']}",
            f"open_count={len(open_positions)}",
        )
        return

    if args.cookie and not paper_mode:
        maybe_close_positions(
            args.base_url,
            wallet,
            args.cookie,
            paper_mode,
            args.request_timeout,
            args.request_retries,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Perpcrab trading loop for PumpPerps with mandatory LLM decisions")
    parser.add_argument("--base-url", default=os.getenv("PERPCRAB_BASE_URL", os.getenv("PUMPCRAB_BASE_URL", os.getenv("PUMPPERPS_BASE_URL", "https://pumpperps.com"))))
    parser.add_argument("--cookie", default=os.getenv("PERPCRAB_COOKIE", os.getenv("PUMPCRAB_COOKIE", os.getenv("PUMPPERPS_COOKIE"))))
    parser.add_argument("--wallet", default=os.getenv("PERPCRAB_WALLET", os.getenv("PUMPCRAB_WALLET", os.getenv("PUMPPERPS_WALLET", ""))))
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=int, default=5)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--live", action="store_true", help="Enable live order placement")
    parser.add_argument("--dry-run", action="store_true", help="Force paper mode even when --live is passed")
    parser.add_argument("--improve-only", action="store_true")
    parser.add_argument("--record-sample", action="store_true", help="append a synthetic closed trade sample for testing adaptation")
    parser.add_argument("--dashboard", action="store_true", help="render a live colored CLI dashboard")
    parser.add_argument("--no-prompts", action="store_true", help="disable interactive prompts for key/wallet/kelly")

    parser.add_argument("--llm-model", default=os.getenv("PERPCRAB_LLM_MODEL", os.getenv("PUMPCRAB_LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))))
    parser.add_argument("--llm-api-base", default=os.getenv("PERPCRAB_LLM_API_BASE", os.getenv("PUMPCRAB_LLM_API_BASE", "https://api.openai.com/v1")))
    parser.add_argument("--llm-api-key", default=os.getenv("PERPCRAB_OPENAI_API_KEY", os.getenv("PUMPCRAB_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))))
    parser.add_argument("--llm-timeout", type=float, default=float(os.getenv("PERPCRAB_LLM_TIMEOUT", os.getenv("PUMPCRAB_LLM_TIMEOUT", "25"))))
    parser.add_argument("--llm-candidate-count", type=int, default=int(os.getenv("PERPCRAB_LLM_CANDIDATE_COUNT", os.getenv("PUMPCRAB_LLM_CANDIDATE_COUNT", "12"))))

    parser.add_argument("--kelly-fraction", type=float, default=float(os.getenv("PERPCRAB_KELLY_FRACTION", os.getenv("PUMPCRAB_KELLY_FRACTION", "0.25"))))
    parser.add_argument("--min-risk-bps", type=float, default=float(os.getenv("PERPCRAB_MIN_RISK_BPS", os.getenv("PUMPCRAB_MIN_RISK_BPS", "25"))))
    parser.add_argument("--max-risk-bps", type=float, default=float(os.getenv("PERPCRAB_MAX_RISK_BPS", os.getenv("PUMPCRAB_MAX_RISK_BPS", "1000"))))

    parser.add_argument("--paper-max-open-positions", type=int, default=int(os.getenv("PERPCRAB_PAPER_MAX_OPEN_POSITIONS", os.getenv("PUMPCRAB_PAPER_MAX_OPEN_POSITIONS", "3"))))
    parser.add_argument("--paper-min-hold-seconds", type=float, default=float(os.getenv("PERPCRAB_PAPER_MIN_HOLD_SECONDS", os.getenv("PUMPCRAB_PAPER_MIN_HOLD_SECONDS", "90"))))
    parser.add_argument("--paper-max-hold-seconds", type=float, default=float(os.getenv("PERPCRAB_PAPER_MAX_HOLD_SECONDS", os.getenv("PUMPCRAB_PAPER_MAX_HOLD_SECONDS", "1800"))))
    parser.add_argument("--paper-take-profit-bps", type=float, default=float(os.getenv("PERPCRAB_PAPER_TAKE_PROFIT_BPS", os.getenv("PUMPCRAB_PAPER_TAKE_PROFIT_BPS", "600"))))
    parser.add_argument("--paper-stop-loss-bps", type=float, default=float(os.getenv("PERPCRAB_PAPER_STOP_LOSS_BPS", os.getenv("PUMPCRAB_PAPER_STOP_LOSS_BPS", "-400"))))
    parser.add_argument("--paper-noise-bps", type=float, default=float(os.getenv("PERPCRAB_PAPER_NOISE_BPS", os.getenv("PUMPCRAB_PAPER_NOISE_BPS", "90"))))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paper_mode = (not args.live) or args.dry_run
    prompt_runtime_setup(args, paper_mode)

    args.kelly_fraction = clamp(args.kelly_fraction, 0.0, 1.0)
    args.min_risk_bps = max(1.0, args.min_risk_bps)
    args.max_risk_bps = max(args.min_risk_bps, args.max_risk_bps)

    args.paper_take_profit_bps = abs(args.paper_take_profit_bps)
    args.paper_stop_loss_bps = -abs(args.paper_stop_loss_bps)
    args.paper_min_hold_seconds = max(0.0, args.paper_min_hold_seconds)
    args.paper_max_hold_seconds = max(args.paper_min_hold_seconds, args.paper_max_hold_seconds)
    args.paper_max_open_positions = max(1, args.paper_max_open_positions)

    auto_fast_profile = False
    if (
        args.dashboard
        and args.paper_min_hold_seconds >= 90
        and args.paper_max_hold_seconds >= 1800
        and args.paper_take_profit_bps >= 600
        and abs(args.paper_stop_loss_bps) >= 400
        and args.paper_max_open_positions <= 3
    ):
        args.paper_min_hold_seconds = 20
        args.paper_max_hold_seconds = 300
        args.paper_take_profit_bps = 250
        args.paper_stop_loss_bps = -180
        args.paper_max_open_positions = 5
        auto_fast_profile = True

    state = load_json(
        STATE_PATH,
        {
            "version": 1,
            "max_leverage": 3,
            "risk_per_trade_bps": 100,
            "min_signal_score": 0.55,
            "target_win_rate": 0.55,
            "max_drawdown_bps": 1200,
            "lookback_trades": 30,
            "kelly_fraction": 0.25,
            "min_risk_bps": 25,
            "max_risk_bps": 1000,
            "updated_at": None,
        },
    )

    state["kelly_fraction"] = round(args.kelly_fraction, 4)
    state["min_risk_bps"] = round(args.min_risk_bps, 2)
    state["max_risk_bps"] = round(args.max_risk_bps, 2)

    history = load_history(HISTORY_PATH)

    if args.record_sample:
        sample = {
            "closed_at": now_iso(),
            "status": "closed",
            "pnl_usd": round(random.uniform(-8, 12), 2),
            "pnl_bps": round(random.uniform(-900, 1100), 2),
        }
        append_history(HISTORY_PATH, sample)
        history.append(sample)
        if not args.dashboard:
            print("recorded sample trade:", sample)

    if args.improve_only:
        new_state = improve(state, history)
        save_json(STATE_PATH, new_state)
        print("improved strategy state:", json.dumps(new_state, indent=2, sort_keys=True))
        return 0

    wallet = args.wallet.strip()
    if paper_mode:
        if wallet and not is_probably_solana_pubkey(wallet):
            if not args.dashboard:
                print("paper mode: ignoring invalid wallet string and using placeholder wallet")
            wallet = PAPER_WALLET
        elif not wallet:
            wallet = PAPER_WALLET
            if not args.dashboard:
                print("paper mode: no wallet provided, using placeholder wallet")
    else:
        if not wallet:
            raise RuntimeError("PERPCRAB_WALLET (or --wallet) is required for live trading")
        if not is_probably_solana_pubkey(wallet):
            raise RuntimeError(
                "PERPCRAB_WALLET must be a Solana public address (base58, 32-byte). "
                "Do not pass a private key or seed value."
            )
        if not args.cookie:
            raise RuntimeError("PERPCRAB_COOKIE (or --cookie) is required for live trading")

    total_cycles = max(1, args.cycles)
    if not args.dashboard:
        print(f"mode={'paper' if paper_mode else 'live'}")
    else:
        initial_event = "interactive setup complete"
        if auto_fast_profile:
            initial_event += " | dashboard fast profile enabled"
        render_dashboard(
            args,
            state,
            paper_mode,
            wallet,
            cycle_index=0,
            total_cycles=total_cycles,
            last_event=initial_event,
        )

    consecutive_failures = 0
    halted = False
    last_event = ""
    last_error = ""
    current_cycle = 0
    cycle_activity = {"opened": 0, "closed": 0, "skipped": 0, "errors": 0}
    recent_events: List[str] = []

    for i in range(total_cycles):
        current_cycle = i + 1
        if not args.dashboard:
            print(f"cycle {current_cycle}/{args.cycles} @ {now_iso()}")

        last_error = ""
        cycle_activity = {"opened": 0, "closed": 0, "skipped": 0, "errors": 0}
        try:
            if args.dashboard:
                cycle_buffer = io.StringIO()
                with contextlib.redirect_stdout(cycle_buffer):
                    cycle(args, state, paper_mode, wallet)
                cycle_output = cycle_buffer.getvalue().strip()
                if cycle_output:
                    headline, cycle_activity, cycle_events = parse_cycle_output(cycle_output)
                    last_event = headline
                    recent_events.extend(cycle_events)
                    recent_events = recent_events[-20:]
            else:
                cycle(args, state, paper_mode, wallet)
        except RuntimeError as exc:
            consecutive_failures += 1
            last_error = str(exc)
            cycle_activity["errors"] += 1
            if not args.dashboard:
                print(f"cycle failure {consecutive_failures}/3: {exc}")
            else:
                recent_events.append(f"cycle failure {consecutive_failures}/3: {exc}")
                recent_events = recent_events[-20:]
            if consecutive_failures > 2:
                if not args.dashboard:
                    print("stopping trader loop: more than two consecutive failures")
                halted = True
        else:
            consecutive_failures = 0

        if args.dashboard:
            render_dashboard(
                args,
                state,
                paper_mode,
                wallet,
                cycle_index=current_cycle,
                total_cycles=total_cycles,
                last_event=last_event,
                last_error=last_error,
                halted=halted,
                cycle_activity=cycle_activity,
                recent_events=recent_events,
            )

        if halted:
            break

        if i + 1 < total_cycles:
            time.sleep(max(args.sleep_seconds, 0))

    new_state = improve(state, load_history(HISTORY_PATH))
    save_json(STATE_PATH, new_state)
    if args.dashboard:
        recent_events.append("saved strategy state")
        recent_events = recent_events[-20:]
        render_dashboard(
            args,
            new_state,
            paper_mode,
            wallet,
            cycle_index=current_cycle,
            total_cycles=total_cycles,
            last_event="saved strategy state",
            halted=halted,
            cycle_activity=cycle_activity,
            recent_events=recent_events,
        )
    else:
        print("saved strategy state")

    return 1 if halted else 0


if __name__ == "__main__":
    raise SystemExit(main())
