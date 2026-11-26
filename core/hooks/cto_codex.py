"""Launch the supervised headless CTO or the interactive Codex CLI."""
import os
import re
from pathlib import Path
import subprocess
import sys

import _config
import card
import codex_runtime
import codex_session
import run_budget


def main():
    action = sys.argv[1]
    cfg = _config.load().get("cto") or {}
    if cfg.get("provider") not in (None, "openai"):
        sys.exit("The CTO runs on Codex CLI. Run ai-teammates install to migrate the old provider.")
    model, effort = codex_runtime.configured_cto()
    budget = card.budget({})
    limits = card.limits()
    budget.update(minutes=cfg.get("max_minutes", limits["max_minutes"]), tokens=cfg.get("max_tokens", limits["max_tokens"]))
    if action == "doctor":
        print(f"  Codex CLI: {codex_runtime.executable()} · {model} {effort}")
        print(f"  headless CTO: {budget['minutes']} minutes / {budget['tokens']} tokens")
        codex_runtime.verify(model, effort, codex_runtime.catalog())
        return subprocess.run([codex_runtime.executable(), "login", "status"]).returncode
    codex_runtime.verify(model, effort, codex_runtime.catalog())
    cwd = Path.cwd()
    if action == "interactive":
        check = codex_session.run(cwd, os.environ, model, effort, "", False,
                                  {"minutes": 0.5, "tokens": 1, "tool_calls": 1}, check_only=True)
        if check["status"] != "done":
            sys.exit(check["reason"])
        cmd = codex_session.launch_args(model, effort) + ["--sandbox", "workspace-write", "--ask-for-approval", "on-request", "--", sys.argv[2]]
        os.execvp(cmd[0], cmd)
    prompt = sys.argv[2]
    with run_budget.reserve("cto", model, budget) as usage:
        if action == "card":
            cid = sys.argv[3]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cid):
                raise ValueError("Supply a card ID")
            # The owner named this card at launch. Model output never selects host commands.
            seat = subprocess.run([sys.executable, str(Path(__file__).with_name("seat.py")), "run", "--card", cid],
                                  cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print(seat.stdout, flush=True)
            if seat.returncode:
                return seat.returncode
            prompt += f"\n\nReview the completed card {cid}. Verify one claim against its branch.\n{seat.stdout[-8000:]}"
        usage["started_turn"] = True
        usage.update(codex_session.run(cwd, os.environ, model, effort, prompt, False, budget))
    print(usage["text"])
    if usage["reason"]:
        print(usage["reason"], file=sys.stderr)
    print(f"  {sum(usage['tokens'].values())} tokens · {usage['seconds']:.1f}s · {usage['status']}")
    return 0 if usage["status"] == "done" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as e:
        sys.exit(str(e))
