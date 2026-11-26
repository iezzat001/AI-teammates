"""Atomic daily admission for concurrently supervised Codex runs (shared across worktrees)."""
from contextlib import contextmanager
from datetime import datetime
import math
import sqlite3
import time

import _config


class DailyLimit(ValueError):
    pass


def caps():
    cfg = _config.load().get("budget") or {}
    tokens = cfg.get("daily_tokens", 2_000_000)
    minutes = cfg.get("daily_minutes", 240)
    if type(tokens) is not int or tokens < 0 or type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0:
        raise ValueError("daily_tokens must be a nonnegative integer and daily_minutes must be finite and nonnegative")
    return tokens, minutes


def connect():
    db = sqlite3.connect(str(_config.state_dir() / "usage.sqlite"), timeout=15)
    db.execute("CREATE TABLE IF NOT EXISTS runs (day TEXT, name TEXT, model TEXT, tokens INTEGER, seconds REAL, "
               "input INTEGER, output INTEGER, complete INTEGER DEFAULT 0)")
    return db


def today():
    return datetime.now().astimezone().date().isoformat()


def totals():
    db = connect()
    try:
        return db.execute("SELECT COALESCE(SUM(tokens),0),COALESCE(SUM(seconds),0) FROM runs WHERE day=?", (today(),)).fetchone()
    finally:
        db.close()


@contextmanager
def reserve(name, model, budget):
    token_cap, minute_cap = caps()
    tokens, minutes = budget["tokens"], budget["minutes"]
    if type(tokens) is not int or tokens <= 0 or type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("a run requires positive finite time and token caps")
    seconds = minutes * 60
    db = connect()
    result = {"started_turn": False}
    try:
        db.execute("BEGIN IMMEDIATE")
        day = today()
        used, elapsed = db.execute("SELECT COALESCE(SUM(tokens),0),COALESCE(SUM(seconds),0) FROM runs WHERE day=?", (day,)).fetchone()
        if (token_cap and used + tokens > token_cap) or (minute_cap and elapsed + seconds > minute_cap * 60):
            raise DailyLimit(f"daily budget would be exceeded: {used} tokens and {elapsed/60:.1f} minutes used or reserved")
        row = db.execute("INSERT INTO runs(day,name,model,tokens,seconds) VALUES(?,?,?,?,?)",
                         (day, name, model, tokens, seconds)).lastrowid
        db.commit()
        started = time.monotonic()
        try:
            yield result
        finally:
            settle(db, row, result, tokens, started)
    finally:
        db.close()


def settle(db, row, result, tokens, started):
    usage = result.get("tokens") or {}
    observed = usage.get("in", 0) + usage.get("out", 0)
    # An interrupted request can still consume tokens after the latest notification.
    charge = max(tokens, observed) if result.get("started_turn") and not result.get("complete_usage") else observed
    elapsed = result.get("seconds", time.monotonic() - started if result["started_turn"] else 0)
    db.execute("UPDATE runs SET tokens=?,seconds=?,input=?,output=?,complete=1 WHERE rowid=?",
               (charge, elapsed, usage.get("in"), usage.get("out"), row))
    db.commit()
