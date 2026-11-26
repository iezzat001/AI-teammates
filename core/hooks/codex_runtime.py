"""Codex CLI model selection for the CTO and task cards."""
import json
import os
import subprocess

# The seat decides who owns the work. The card's difficulty decides the model.
ROUTES = {
    "cto": ("gpt-6.2-astra", "xhigh"),
    "mechanical": ("gpt-6-luna", "low"),
    "standard": ("gpt-6.2-sol", "medium"),
    "hard": ("gpt-6.2-sol", "high"),
}


def executable():
    return os.environ.get("AI_TEAMMATES_CODEX_BIN") or "codex"


def route(tier):
    if tier not in ROUTES:
        raise ValueError("choose a Codex route from " + ", ".join(ROUTES))
    return ROUTES[tier]


def configured_cto():
    import _config
    model, effort = route("cto")
    cfg = _config.load().get("cto") or {}
    return cfg.get("model") or model, cfg.get("effort") or effort


def card_route(data):
    import _config
    tier = data.get("tier") or "standard"
    if tier not in ("mechanical", "standard", "hard"):
        raise ValueError(f"unknown card tier: {tier}")
    default, effort = route(tier)
    override = data.get("model")
    if isinstance(override, str) and override.startswith("claude-"):
        # Cards written before the provider change keep their tier and historical runs.
        override = None
    model = override or (_config.load().get("seat_models") or {}).get(tier, default)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("the card model must be a nonempty model ID")
    return model, effort


def catalog():
    try:
        p = subprocess.run([executable(), "debug", "models"], capture_output=True,
                           stdin=subprocess.DEVNULL, text=True, timeout=30, check=True)
        rows = json.loads(p.stdout)["models"]
        return {m["slug"]: {level["effort"] for level in m["supported_reasoning_levels"]}
                for m in rows}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read the model catalog from {executable()} debug models. "
                         "Check the CLI version and sign-in, or set AI_TEAMMATES_CODEX_BIN.") from exc


def verify(model, effort, models):
    if effort not in models.get(model, set()):
        raise ValueError(f"{executable()} does not list {model} with {effort} reasoning. "
                         "Set AI_TEAMMATES_CODEX_BIN to a CLI that lists this route.")


def show_route(argv):
    import argparse
    from pathlib import Path
    import card

    parser = argparse.ArgumentParser(prog="seat.py route")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("tier", nargs="?", choices=tuple(ROUTES))
    source.add_argument("--card", metavar="ID")
    parser.add_argument("--verify", action="store_true", help="check the selected CLI's model catalog")
    args = parser.parse_args(argv)
    if args.card is None:
        model, effort = configured_cto() if args.tier == "cto" else card_route({"tier": args.tier})
    else:
        if Path(args.card).name != args.card:
            parser.error("supply a card ID")
        data = card.load(args.card)
        if not isinstance(data, dict):
            raise ValueError("the card must be a JSON object")
        model, effort = card_route(data)
    if args.verify:
        verify(model, effort, catalog())
    print(model, effort)
