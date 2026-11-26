"""Supervise the Codex CLI app-server over its stdio protocol."""
from collections import deque
import json
import os
import queue
import signal
import subprocess
import sys
from pathlib import Path
import threading
import time

import codex_runtime
from codex_hooks import REGISTRATION


def launch_args(model, effort):
    return [codex_runtime.executable(), "--enable", "hooks", "--disable", "multi_agent",
            "--disable", "unified_exec", "-c", "web_search=disabled",
            "-c", "apps._default.enabled=false", "-c", "plugins._default.enabled=false",
            "-c", f"model={json.dumps(model)}", "-c", f"model_reasoning_effort={json.dumps(effort)}"]


def require_hooks(entries, cwd):
    for entry in entries:
        if Path(entry["cwd"]).resolve() != Path(cwd).resolve() or entry.get("errors"):
            continue
        live = {(h["eventName"], h.get("command")) for h in entry["hooks"]
                if h["enabled"] and h["trustStatus"] in ("trusted", "managed")
                and not h.get("matcher") and not h.get("async") and h.get("handlerType") == "command"}
        if all((k[:1].lower() + k[1:], cmd) in live for k, cmd in REGISTRATION.items()):
            return
    raise ValueError("Required Codex hooks are missing, disabled, or untrusted. "
                     "Run ai-teammates install, then review the project and its hooks in Codex (/hooks).")


def stop(proc):
    # macOS can return EPERM for a group whose leader is already a zombie.
    proc.poll()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.terminate()
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.kill()
    proc.wait()


def run(cwd, env, model, effort, prompt, read_only, budget, check_only=False, idle_seconds=None):
    """One fresh thread. Usage notifications are cumulative and can include a final overshoot."""
    start = time.monotonic()
    deadline = start + budget["minutes"] * 60
    result = {"status": "failed", "reason": "", "text": "", "tools": 0, "tokens": {"in": 0, "out": 0},
              "usage_known": False, "started_turn": False}
    inbox = queue.Queue()
    stderr = deque(maxlen=16)
    proc = None
    child_env = {**env, "AI_TEAMMATES_RUNTIME": "codex", "PYTHONDONTWRITEBYTECODE": "1",
                 "AI_TEAMMATES_SUPERVISED": "1", "AI_TEAMMATES_READ_ONLY": "1" if read_only else "0"}

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def pump(stream):
        try:
            for line in stream:
                inbox.put(line)
        finally:
            inbox.put(None)

    try:
        proc = subprocess.Popen(launch_args(model, effort) + ["app-server", "--stdio"], cwd=cwd,
                                env=child_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        threading.Thread(target=pump, args=(proc.stdout,), daemon=True).start()
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ai_teammates",
              "version": "1"}, "capabilities": {"experimentalApi": True}}})
        thread = None
        last = time.monotonic()
        while True:
            now = time.monotonic()
            remaining = min(deadline - now, last + idle_seconds - now if idle_seconds else deadline - now)
            if remaining <= 0:
                result.update(status="timeout", reason="wall-time or idle-time cap reached")
                break
            try:
                line = inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise ValueError("Codex closed the stream before completing the run. " + "".join(stderr)[-300:])
            msg = json.loads(line)
            last = time.monotonic()
            if "error" in msg:
                raise ValueError(str(msg["error"].get("message", msg["error"])))
            method, data = msg.get("method"), msg.get("params") or {}
            if "id" in msg and method:
                # A supervised run has no operator to grant escalations or answer tool requests.
                send({"id": msg["id"], "error": {"code": -32603, "message": "Unattended run: request denied"}})
            elif msg.get("id") == 1:
                send({"method": "initialized"})
                send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}})
            elif msg.get("id") == 2:
                require_hooks(msg["result"]["data"], cwd)
                if check_only:
                    result["status"] = "done"
                    break
                send({"id": 3, "method": "thread/start", "params": {"cwd": str(cwd), "model": model,
                      "approvalPolicy": "never", "sandbox": "read-only" if read_only else "workspace-write",
                      "ephemeral": True}})
            elif msg.get("id") == 3:
                if msg["result"]["model"] != model:
                    raise ValueError("Codex started a thread on a different model than requested")
                thread = msg["result"]["thread"]["id"]
                result["started_turn"] = True
                send({"id": 4, "method": "turn/start", "params": {"threadId": thread, "effort": effort,
                      "input": [{"type": "text", "text": prompt}]}})
            elif thread and data.get("threadId") == thread:
                if method == "thread/tokenUsage/updated":
                    totals = data["tokenUsage"]["total"]
                    values = [totals[k] for k in ("inputTokens", "outputTokens", "totalTokens")]
                    if any(type(v) is not int or v < 0 for v in values) or values[2] != sum(values[:2]):
                        raise ValueError("Codex returned invalid cumulative token usage")
                    result["tokens"] = {"in": values[0], "out": values[1]}
                    result["usage_known"] = True
                    if values[2] >= budget["tokens"]:
                        result.update(status="over_budget", reason=f"token cap reached ({values[2]} of {budget['tokens']})")
                        break
                elif method == "item/started" and data["item"]["type"] in (
                    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch", "imageView"
                ):
                    result["tools"] += 1
                    if result["tools"] > budget["tool_calls"]:
                        result.update(status="timeout", reason="tool-call cap reached")
                        break
                elif method == "item/completed" and data["item"]["type"] == "commandExecution":
                    # The PostToolUse hook only receives shell output. This event carries the exit code.
                    item = data["item"]
                    failed = item.get("exitCode") != 0
                    payload = {"session_id": thread, "tool_name": "Bash", "tool_input": {"command": item["command"]},
                               "error": item.get("aggregatedOutput") or item.get("status")}
                    event = "PostToolUseFailure" if failed else "PostToolUse"
                    check = subprocess.run([sys.executable, str(Path(__file__).with_name("loopguard.py")), event],
                                           input=json.dumps(payload), env=child_env, cwd=cwd, capture_output=True,
                                           text=True, timeout=max(0.01, deadline - time.monotonic()))
                    if check.returncode:
                        raise ValueError("Could not check the command outcome")
                    if failed and check.stdout.strip():
                        result.update(status="blocked", reason="Repeated command failures need owner action")
                        break
                elif method == "hook/completed" and data["run"]["eventName"] in ("preToolUse", "permissionRequest"):
                    hook = data["run"]
                    if hook["status"] == "failed":
                        raise ValueError("A Codex guard hook failed")
                    if hook["status"] in ("blocked", "stopped"):
                        import loopguard
                        state = loopguard.load_state(thread)
                        limit = loopguard.cfg()["seat_interrupt_after" if env.get("AI_TEAMMATES_CARD") else "interrupt_after"]
                        if state.get("blocked") and (not state.get("from_denial") or state.get("denials", 0) >= limit):
                            result.update(status="blocked", reason=state["blocked"]["need"])
                            break
                elif method == "item/completed" and data["item"]["type"] == "agentMessage":
                    result["text"] = data["item"]["text"]
                elif method == "turn/completed":
                    turn = data["turn"]
                    result["complete_usage"] = result["usage_known"] and turn["status"] == "completed"
                    if turn["status"] != "completed":
                        raise ValueError(str(turn.get("error") or turn["status"]))
                    if not result["usage_known"]:
                        raise ValueError("Codex completed without reporting token usage")
                    result["status"] = "done"
                    break
    except subprocess.TimeoutExpired:
        result.update(status="timeout", reason="wall-time cap reached")
    except (OSError, ValueError, TypeError, KeyError) as e:
        result["reason"] = str(e)
    finally:
        if proc is not None:
            stop(proc)
    result["seconds"] = time.monotonic() - start
    return result
