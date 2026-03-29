#!/usr/bin/env python3
import argparse
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "strategy_state.json"
HISTORY_PATH = DATA_DIR / "trade_history.jsonl"
POOLS_CACHE_PATH = DATA_DIR / "pools_cache.json"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PAPER_WALLET = "PAPER_TRADING_WALLET"


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
        "User-Agent": "pumpcrab/1.1",
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


def pick_side(pool: Dict[str, Any]) -> str:
    long_oi = float(pool.get("longOi") or 0.0)
    short_oi = float(pool.get("shortOi") or 0.0)
    if long_oi > short_oi:
        return "short"
    if short_oi > long_oi:
        return "long"
    return random.choice(["long", "short"])


def choose_candidate(pools: List[Dict[str, Any]], min_signal_score: float) -> Optional[Dict[str, Any]]:
    scored = []
    for pool in pools:
        s = score_pool(pool)
        if s >= min_signal_score:
            row = dict(pool)
            row["signal_score"] = s
            scored.append(row)
    if not scored:
        return None
    scored.sort(key=lambda p: float(p["signal_score"]), reverse=True)
    return scored[0]


def improve(state: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    lookback = int(state.get("lookback_trades", 30))
    target_win_rate = float(state.get("target_win_rate", 0.55))
    max_dd_bps = float(state.get("max_drawdown_bps", 1200))

    closed = [x for x in history if x.get("status") == "closed"]
    sample = closed[-lookback:]
    if len(sample) < 5:
        state["updated_at"] = now_iso()
        return state

    wins = sum(1 for x in sample if float(x.get("pnl_usd", 0.0)) > 0)
    win_rate = wins / len(sample)
    avg_pnl_bps = sum(float(x.get("pnl_bps", 0.0)) for x in sample) / len(sample)
    worst_pnl_bps = min(float(x.get("pnl_bps", 0.0)) for x in sample)

    max_leverage = int(state.get("max_leverage", 3))
    risk_bps = float(state.get("risk_per_trade_bps", 100))
    signal = float(state.get("min_signal_score", 0.55))

    if win_rate < target_win_rate or worst_pnl_bps < -max_dd_bps:
        max_leverage = max(1, max_leverage - 1)
        risk_bps = clamp(risk_bps * 0.90, 25, 1000)
        signal = clamp(signal + 0.03, 0.40, 0.95)
    elif win_rate >= target_win_rate and avg_pnl_bps > 0 and worst_pnl_bps > -(max_dd_bps * 0.7):
        max_leverage = min(10, max_leverage + 1)
        risk_bps = clamp(risk_bps * 1.05, 25, 1000)
        signal = clamp(signal - 0.02, 0.35, 0.95)

    state["max_leverage"] = int(max_leverage)
    state["risk_per_trade_bps"] = round(float(risk_bps), 2)
    state["min_signal_score"] = round(float(signal), 4)
    state["updated_at"] = now_iso()
    state["last_metrics"] = {
        "sample_size": len(sample),
        "win_rate": round(win_rate, 4),
        "avg_pnl_bps": round(avg_pnl_bps, 2),
        "worst_pnl_bps": round(worst_pnl_bps, 2),
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
    cookie: Optional[str],
    candidate: Dict[str, Any],
    state: Dict[str, Any],
    dry_run: bool,
    request_timeout: float,
    request_retries: int,
) -> None:
    collateral = round(max(5.0, float(state.get("risk_per_trade_bps", 100)) / 10.0), 2)
    leverage = int(state.get("max_leverage", 3))
    side = pick_side(candidate)
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
        return

    if not cookie:
        raise RuntimeError("PUMPCRAB_COOKIE is required for live trade execution")

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


def cycle(args: argparse.Namespace, state: Dict[str, Any], paper_mode: bool, wallet: str) -> None:
    pools = get_pools(args.base_url, args.request_timeout, args.request_retries)
    if not pools:
        print("no pools returned from API")
        return

    candidate = choose_candidate(pools, float(state.get("min_signal_score", 0.55)))
    if not candidate:
        print("no pool passed min_signal_score")
        return

    print(
        "candidate:",
        candidate.get("tokenTicker") or candidate.get("tokenName") or candidate.get("tokenMint"),
        "signal_score=",
        round(float(candidate.get("signal_score", 0.0)), 4),
    )

    open_position(
        base_url=args.base_url,
        wallet=wallet,
        cookie=args.cookie,
        candidate=candidate,
        state=state,
        dry_run=paper_mode,
        request_timeout=args.request_timeout,
        request_retries=args.request_retries,
    )

    if args.cookie:
        maybe_close_positions(
            args.base_url,
            wallet,
            args.cookie,
            paper_mode,
            args.request_timeout,
            args.request_retries,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pumpcrab trading loop for PumpPerps with adaptive parameter tuning")
    parser.add_argument("--base-url", default=os.getenv("PUMPCRAB_BASE_URL", os.getenv("PUMPPERPS_BASE_URL", "https://pumpperps.com")))
    parser.add_argument("--cookie", default=os.getenv("PUMPCRAB_COOKIE", os.getenv("PUMPPERPS_COOKIE")))
    parser.add_argument("--wallet", default=os.getenv("PUMPCRAB_WALLET", os.getenv("PUMPPERPS_WALLET", "")))
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=int, default=5)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--live", action="store_true", help="Enable live order placement")
    parser.add_argument("--dry-run", action="store_true", help="Force paper mode even when --live is passed")
    parser.add_argument("--improve-only", action="store_true")
    parser.add_argument("--record-sample", action="store_true", help="append a synthetic closed trade sample for testing adaptation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
            "updated_at": None,
        },
    )

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
        print("recorded sample trade:", sample)

    if args.improve_only:
        new_state = improve(state, history)
        save_json(STATE_PATH, new_state)
        print("improved strategy state:", json.dumps(new_state, indent=2, sort_keys=True))
        return 0

    paper_mode = (not args.live) or args.dry_run

    wallet = args.wallet.strip()
    if paper_mode:
        if wallet and not is_probably_solana_pubkey(wallet):
            print("paper mode: ignoring invalid wallet string and using placeholder wallet")
            wallet = PAPER_WALLET
        elif not wallet:
            wallet = PAPER_WALLET
            print("paper mode: no wallet provided, using placeholder wallet")
    else:
        if not wallet:
            raise RuntimeError("PUMPCRAB_WALLET (or --wallet) is required for live trading")
        if not is_probably_solana_pubkey(wallet):
            raise RuntimeError(
                "PUMPCRAB_WALLET must be a Solana public address (base58, 32-byte). "
                "Do not pass a private key or seed value."
            )
        if not args.cookie:
            raise RuntimeError("PUMPCRAB_COOKIE (or --cookie) is required for live trading")

    print(f"mode={'paper' if paper_mode else 'live'}")

    for i in range(max(1, args.cycles)):
        print(f"cycle {i + 1}/{args.cycles} @ {now_iso()}")
        cycle(args, state, paper_mode, wallet)
        time.sleep(max(args.sleep_seconds, 0))

    new_state = improve(state, load_history(HISTORY_PATH))
    save_json(STATE_PATH, new_state)
    print("saved strategy state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
