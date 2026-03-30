#!/usr/bin/env python3
import argparse
import json
import os
import random
import re
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


def record_paper_trade(candidate: Dict[str, Any], payload: Dict[str, Any], decision: Dict[str, Any], llm_model: str) -> None:
    score = float(candidate.get("signal_score") or 0.5)
    leverage = float(payload.get("leverage") or 1.0)
    collateral = float(payload.get("collateral") or 0.0)
    side = str(payload.get("side") or "long")

    edge_bps = (score - 0.5) * 350.0
    noise_bps = random.gauss(0.0, 220.0)
    direction_bps = 20.0 if side == "long" else -20.0
    pnl_bps = clamp(edge_bps + noise_bps + direction_bps, -1500.0, 1500.0)

    notional = collateral * leverage
    pnl_usd = round(notional * (pnl_bps / 10000.0), 2)

    row = {
        "mode": "paper",
        "status": "closed",
        "opened_at": now_iso(),
        "closed_at": now_iso(),
        "tokenMint": payload.get("tokenMint"),
        "poolId": payload.get("poolId"),
        "side": side,
        "collateral": collateral,
        "leverage": leverage,
        "notional_usd": round(notional, 2),
        "signal_score": round(score, 4),
        "pnl_bps": round(pnl_bps, 2),
        "pnl_usd": pnl_usd,
        "llm_model": llm_model,
        "llm_confidence": decision.get("confidence"),
        "llm_rationale": decision.get("rationale"),
    }
    append_history(HISTORY_PATH, row)
    print(
        "[paper] closed simulated trade",
        f"side={side}",
        f"token={payload.get('tokenMint')}",
        f"pnl_bps={row['pnl_bps']}",
        f"pnl_usd={row['pnl_usd']}",
    )


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
        record_paper_trade(selected, opened_payload, decision, args.llm_model)

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

    parser.add_argument("--llm-model", default=os.getenv("PERPCRAB_LLM_MODEL", os.getenv("PUMPCRAB_LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))))
    parser.add_argument("--llm-api-base", default=os.getenv("PERPCRAB_LLM_API_BASE", os.getenv("PUMPCRAB_LLM_API_BASE", "https://api.openai.com/v1")))
    parser.add_argument("--llm-api-key", default=os.getenv("PERPCRAB_OPENAI_API_KEY", os.getenv("PUMPCRAB_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))))
    parser.add_argument("--llm-timeout", type=float, default=float(os.getenv("PERPCRAB_LLM_TIMEOUT", os.getenv("PUMPCRAB_LLM_TIMEOUT", "25"))))
    parser.add_argument("--llm-candidate-count", type=int, default=int(os.getenv("PERPCRAB_LLM_CANDIDATE_COUNT", os.getenv("PUMPCRAB_LLM_CANDIDATE_COUNT", "12"))))
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
            raise RuntimeError("PERPCRAB_WALLET (or --wallet) is required for live trading")
        if not is_probably_solana_pubkey(wallet):
            raise RuntimeError(
                "PERPCRAB_WALLET must be a Solana public address (base58, 32-byte). "
                "Do not pass a private key or seed value."
            )
        if not args.cookie:
            raise RuntimeError("PERPCRAB_COOKIE (or --cookie) is required for live trading")

    print(f"mode={'paper' if paper_mode else 'live'}")

    consecutive_failures = 0
    halted = False
    for i in range(max(1, args.cycles)):
        print(f"cycle {i + 1}/{args.cycles} @ {now_iso()}")
        try:
            cycle(args, state, paper_mode, wallet)
        except RuntimeError as exc:
            consecutive_failures += 1
            print(f"cycle failure {consecutive_failures}/3: {exc}")
            if consecutive_failures > 2:
                print("stopping trader loop: more than two consecutive failures")
                halted = True
                break
        else:
            consecutive_failures = 0
        time.sleep(max(args.sleep_seconds, 0))

    new_state = improve(state, load_history(HISTORY_PATH))
    save_json(STATE_PATH, new_state)
    print("saved strategy state")
    return 1 if halted else 0


if __name__ == "__main__":
    raise SystemExit(main())
