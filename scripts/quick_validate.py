#!/usr/bin/env python3
import json
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def require(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"missing required file: {path}")


def main() -> int:
    required = [
        ROOT / "SKILL.md",
        ROOT / "agents" / "openai.yaml",
        ROOT / "scripts" / "trader_loop.py",
        ROOT / "data" / "strategy_state.json",
        ROOT / "data" / "trade_history.jsonl",
    ]
    for p in required:
        require(p)

    with (ROOT / "data" / "strategy_state.json").open("r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state.get("max_leverage"), int):
        raise ValueError("strategy_state.max_leverage must be int")

    py_compile.compile(str(ROOT / "scripts" / "trader_loop.py"), doraise=True)
    print("validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
