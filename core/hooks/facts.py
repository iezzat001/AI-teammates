#!/usr/bin/env python3
"""Canonical project facts, VERIFIED at session start — not scraped from docs.

WHY THIS EXISTS (2026-09-10). The CTO asked the human to run
`ssh root@api.<domain>`. It timed out. Nothing in the docs said that:
they contain `root@<server-ip>` fifteen times and `https://api.<domain>`
seven times, and the CTO combined the ssh USER from one fact with the HOST from
another. Both ingredients were true. The result was not.

A stale doc is one failure. This is a different one: two correct facts of
different TYPES, merged into a plausible third. More docs would not have helped -
the agent had the right answer available and built a wrong one anyway.

So: one file, typed keys, and each is CHECKED before the session starts. An
unverified fact prints as unverified rather than silently looking authoritative,
because a facts file nobody checks is just another doc that can go stale.

    python3 facts.py            # verify and print
    python3 facts.py --quiet    # only print problems
"""
import json
import socket
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

FACTS = _config.repo_root() / ".ai-teammates" / "facts.json"

TEMPLATE = {
    "_comment": "Canonical, typed. ssh_host is a HOST for port 22. api_base is a URL. They are not interchangeable - assuming they were is why this file exists.",
    "ssh_host": "",
    "ssh_user": "root",
    "api_base": "",
    "deploy": {},
    "urls": {},
}


def check_ssh(host: str, user: str, port: int = 22, timeout: float = 4.0, alias: str = ""):
    """The incident was a wrong USER@HOST combination, and an open port 22 says
    nothing about the user. So log in, non-interactively, and run `true`.

    Use the ~/.ssh/config alias when one is given (facts.json "ssh_alias"): it
    carries the right key. FOUND 2026-09-24: this logged in as plain user@host,
    so ssh offered the default key, was rejected, and every session reported
    "login FAILED" for a VPS that the `vps` alias reached fine - weeks of a
    false alarm that also told agents the server was unreachable."""
    if not host:
        return None, "not set"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except Exception as e:
        return False, f"port {port} unreachable ({type(e).__name__})"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}",
                            "-p", str(port), (alias or f"{user}@{host}"), "true"],
                           capture_output=True, text=True, timeout=timeout + 6)
    except Exception as e:
        return False, f"port open, login not checked ({type(e).__name__})"
    return (True, "login ok") if r.returncode == 0 else \
           (False, f"port open, login FAILED: {r.stderr.strip()[:60]}")


def check_url(url: str, timeout: float = 6.0):
    if not url:
        return None, "not set"
    try:
        with urlopen(url, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except Exception as e:
        code = getattr(e, "code", None)
        # A 401/403 means the host is real and answering — that is a pass here.
        # A 5xx is not: a proxy answering 502 for a dead API used to pass too.
        if code:
            return code < 500, f"HTTP {code}"
        return False, f"unreachable ({type(e).__name__})"


def main() -> int:
    quiet = "--quiet" in sys.argv

    if not FACTS.exists():
        FACTS.parent.mkdir(parents=True, exist_ok=True)
        FACTS.write_text(json.dumps(TEMPLATE, indent=2))
        print(f"  facts: created {FACTS.name} — fill in ssh_host / api_base")
        return 0

    try:
        f = json.loads(FACTS.read_text())
    except Exception as e:
        print(f"  facts: {FACTS.name} unreadable ({e})")
        return 1

    rows, problems = [], 0

    user = f.get("ssh_user", "root")
    ok, why = check_ssh(f.get("ssh_host", ""), user, alias=f.get("ssh_alias", ""))
    if ok is not None:
        rows.append((ok, f"ssh        {f.get('ssh_alias') or user + '@' + f['ssh_host']}", why))
        problems += 0 if ok else 1

    ok, why = check_url(f.get("api_base", ""))
    if ok is not None:
        rows.append((ok, f"api        {f['api_base']}", why))
        problems += 0 if ok else 1

    for name, url in (f.get("urls") or {}).items():
        ok, why = check_url(url)
        rows.append((ok, f"{name:<10} {url}", why))
        problems += 0 if ok else 1

    if not rows:
        print("  facts: nothing configured yet")
        return 0

    if not quiet or problems:
        print("\n  facts — verified now, not read from a doc:")
        for ok, label, why in rows:
            mark = "ok  " if ok else "FAIL"
            print(f"  {mark}  {label}   {why}")
        if f.get("deploy"):
            print("  deploy commands (exact, do not improvise):")
            for k, v in f["deploy"].items():
                print(f"        {k:<12} {v}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
