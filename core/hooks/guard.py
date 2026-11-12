#!/usr/bin/env python3
"""PreToolUse guard - deterministic rules for the project's agents.

Why this exists: CLAUDE.md and AGENTS_RULES.md are advisory. They only work if
the agent chooses to read and honour them, and over a long session that decays.
Every rule below was agreed, written down, and then broken anyway. Hooks run
regardless of intent, prompt, or conversational drift.

Costs zero tokens. Shell-level enforcement, not another model call.

Exit codes (Claude Code contract):
  0  allow
  2  block, and feed stderr back to the agent as the reason

Overrides - both logged to .ai-teammates/events.jsonl:
  pushes:   AI_TEAMMATES_ALLOW='<why>' git push ...   (per command, from inside)
  anything: AI_TEAMMATES_OVERRIDE='<why>' set by the HUMAN when launching the
            session. It is the harness's environment, so an agent cannot set it
            from inside - prefixing a command with it does nothing.
"""
import json
import os
import re
import shlex
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _config
import loopguard
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = _config.repo_root()   # the worktree we are IN, not the file's parent
GIT = _config.GIT
EVENTS = _config.state_dir() / "events.jsonl"


def log(kind, detail):
    """Append an event. Never let logging break the hook."""
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "agent": os.environ.get("CLAUDE_AGENT_NAME", "main"),
        }
        rec.update(detail)
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


SESSION_ID = ""      # set from the hook payload in main(); loopguard keys its counters on it
CWD = os.getcwd()    # where the tool runs: the payload's cwd when it says


def block(rule, message, fix):
    log("blocked", {"rule": rule, "message": message})
    # Counted: a block that fires before the tool runs produces no failure event,
    # so a retry-rephrase-retry loop against a guard was invisible. On the pilot project a
    # block led to the human being asked 1 time in 10, and once to an edit of
    # guard.py itself.
    try:
        loopguard.note_guard_block(SESSION_ID, rule, message)
    except Exception:
        pass
    sys.stderr.write(
        "BLOCKED [%s]\n\n%s\n\nWhat to do instead:\n%s\n\n"
        "Do NOT rephrase this command, try another spelling, or edit guard.py, the "
        "hooks or the settings to get past it. Either do what is described above, or "
        "stop and ask the human in one line:  BLOCKED: guard:%s | <what you need decided>\n"
        "Only the human can override a guard: they launch a session with "
        "AI_TEAMMATES_OVERRIDE='<why>' (logged). Prefixing your own command with it "
        "does nothing. A second block in a row ends your autonomy until they reply.\n"
        % (rule, message, fix, rule)
    )
    sys.exit(2)


def ask(rule, reason):
    """Hand the decision to the human. Interactive: a permission prompt with the
    reason. Headless there is no one to answer, so it becomes a denial that
    loopguard turns into a BLOCKED report."""
    log("ask", {"rule": rule, "message": reason[:200]})
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "ask",
                                              "permissionDecisionReason": reason}}))
    sys.exit(0)


# --- rules ----------------------------------------------------------------

def check_blocked_gate(tool, ti):
    """While loopguard has this session blocked, nothing that could change state
    runs without the human. Reads stay open so the agent can still explain itself."""
    reason = loopguard.gate(SESSION_ID, tool)
    if reason:
        ask("loopguard-gate", reason)


def check_card_scope(tool, ti):
    """A seat running a card may change only the files the card names. Enforced here,
    while it works, because hooks fire in Claude Code seats; cardrun checks the diff
    again afterwards. Fails closed: an unreadable card means no edits."""
    cp = os.environ.get("AI_TEAMMATES_CARD")
    if not cp or tool not in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return
    p = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
    if not p:
        return
    try:
        import card as cardmod
        c = json.loads(Path(cp).read_text(encoding="utf-8"))
        edits = cardmod.edit_scope(c)
        allowed = cardmod.allowed_edit(c, rel_path(p))
    except Exception as e:
        block("card-unreadable", "The card this seat is running could not be read (%s), so no "
              "edit can be checked against its scope." % type(e).__name__,
              "Stop. Report that the card is unreadable.")
        return
    if not allowed:
        block("card-scope", "%s is outside this card's scope. You may change only: %s."
              % (rel_path(p), ", ".join(edits) or "(nothing)"),
              "Stay inside those files. If the card truly needs this one, stop and say so: "
              "BLOCKED: scope | add %s to the card" % rel_path(p))


def _unquote(cmd):
    """The command with quoted text blanked, so a '>' or ';' inside a string is not read as syntax."""
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", lambda m: " " * len(m.group(0)), cmd)


# Wrappers that run the command after them. FOUND 2026-10-28 (repo analysis): `sudo -u root rm -rf`
# and `nice -n 5 git push` read as the commands `root` and `5`.
LEADERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "nice", "timeout", "stdbuf", "caffeinate"}
LEADER_VALUE_OPTS = {"sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"},
                     "env": {"-u", "-C", "-S", "-P"}, "nice": {"-n"}, "stdbuf": {"-i", "-o", "-e"}}
GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env")
_FD = re.compile(r"\d*[<>]&\d+|&>>?")
_SEP = re.compile(r"\|\||&&|[;|&\n]")
# Shell words that stand in front of a command without being one. FOUND 2026-11-12 (issue #29): the
# command word was read only as the first word of a simple command, so `then sed -i ...`,
# `do rm ...`, `{ rm ...; }`, `(sed -i ...)`, `f() { rm ...; }` and a case arm each hid a write
# from every rule that reads the shell.
KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}


def _words(text):
    """The words the shell would see: quotes and backslashes removed, so `"git"`, `\\git` and `g''it`
    are all `git`. FOUND 2026-10-28 (repo analysis): each of those walked past the deny list."""
    try:
        return shlex.split(text, posix=True)
    except ValueError:                              # an unclosed quote: still read the words
        return text.split()


def _strip_leaders(w):
    while w:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
            continue
        if w[0] in KEYWORDS:
            w.pop(0)
            continue
        if w[0] == "function":                      # function name { ... }
            del w[:2]
            continue
        lead = os.path.basename(w[0])
        if lead not in LEADERS:
            break
        w.pop(0)
        takes = LEADER_VALUE_OPTS.get(lead, set())
        while w and w[0].startswith("-"):
            if w.pop(0) in takes and w:
                w.pop(0)
        if lead == "timeout" and w:
            w.pop(0)                                # its duration
    return w


def _ungroup(blank, raw):
    """Grouping syntax made plain, at the same offsets in both strings: the ( and ) of a subshell
    become spaces; the () of a function definition and the ) that ends a case pattern become a
    separator. $(...), <(...), >(...) and name=(...) are left as they are. Also returns the
    offsets where a subshell opens and where one closes: what `cd` does inside ends there."""
    b, r, stack, start, i = list(blank), list(raw), [], True, 0
    opens, closes = [], []
    while i < len(b):
        c = b[i]
        if c == "(":
            j = i + 1
            while j < len(b) and b[j] in " \t":
                j += 1
            k = j + 1
            while k < len(b) and b[k] in " \t\n":
                k += 1
            if i and b[i - 1] in "$<>=":
                stack.append(False)
            elif not start and j < len(b) and b[j] == ")" and k < len(b) and b[k] == "{":
                b[i] = r[i] = " "                    # name() { ... }
                b[j] = r[j] = ";"
                i, start = j + 1, True
                continue
            elif start:
                b[i] = r[i] = " "
                stack.append(True)
                opens.append(i)
            else:
                stack.append(False)
        elif c == ")":
            if not stack:
                b[i] = r[i] = ";"                    # the end of a case pattern
                start = True
            elif stack.pop():
                b[i] = r[i] = " "
                closes.append(i)
        elif c in ";|&\n":
            start = True
        elif c not in " \t":
            start = False
        i += 1
    return "".join(b), "".join(r), opens, closes


def _parts_text(cmd):
    """_parts, and for each simple command: its own text (to read a redirect's target), how many
    subshells open in front of it and how many close behind it."""
    blank, raw = _unquote(cmd), cmd
    # fd duplications and &> are not separators: neutralised at the same offsets in both strings
    for m in reversed(list(_FD.finditer(blank))):
        rep = " " * len(m.group(0)) if "&>" not in m.group(0) else " " + m.group(0)[1:]
        blank = blank[:m.start()] + rep + blank[m.end():]
        raw = raw[:m.start()] + rep + raw[m.end():]
    blank, raw, opens, closes = _ungroup(blank, raw)
    out, pos = [], 0
    for m in list(_SEP.finditer(blank)) + [None]:
        end = m.start() if m else len(blank)
        b, text, rw = blank[pos:end], raw[pos:end], _words(raw[pos:end])
        pos = m.end() if m else end
        w = _strip_leaders(list(rw))
        if w:
            w[0] = os.path.basename(w[0])
            if w[0] == "git":
                i = 1
                while i < len(w) and w[i].startswith("-"):
                    i += 2 if w[i] in GIT_VALUE_OPTS else 1
                w = ["git"] + w[i:]
        within = lambda offsets: sum(1 for o in offsets if end - len(b) <= o < end)
        out.append((b, w, rw, text, within(opens), within(closes)))
    return out


def _parts(cmd):
    """Each simple command in a line (split at ; && || | & and newlines) as (blank, argv, raw):

    blank  its text with quoted strings blanked and fd duplications (2>&1) removed: for SYNTAX,
           so a '>' or ';' inside quotes is not a redirect or a separator
    argv   its words dequoted, leading VAR=value and wrappers (sudo, env, ...) dropped, the command
           word as a basename (/usr/bin/ssh is ssh), git's global options dropped: for WHICH
           command runs (`git -C x push` is `git push`)
    raw    its dequoted words before anything was dropped

    Shell keywords and grouping in front of a command are dropped too (KEYWORDS, _ungroup)."""
    return [p[:3] for p in _parts_text(cmd)]


def _segments(cmd):
    """Each simple command as one normalised string (see _parts)."""
    return [" ".join(w) for _, w, _ in _parts(cmd) if w]


def _substitutions(cmd):
    """What $(...) and backticks would run. Single-quoted text does not expand; double-quoted does."""
    text = re.sub(r"'[^']*'", lambda m: " " * len(m.group(0)), cmd)
    return [a or b for a, b in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", text)]


_SH_C = re.compile(r"""(?:^|[\s;&|(])(?:\S*/)?(?:ba|z|da|k|fi)?sh\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*c\s+(['"])(.*?)\1""")
_EVAL = re.compile(r"""(?:^|[\s;&|(])eval\s+(['"])(.*?)\1""")
XARGS_VALUE_OPTS = ("-I", "-n", "-P", "-L", "-s", "-d", "-E", "-p", "--package")


def _commands(cmd, depth=0):
    """Every command this line would RUN: each simple command, plus what a shell (-c), eval,
    xargs, npx, find -exec or a $(...) substitution is told to run. Arguments are not commands.

    FOUND 2026-10-04 by the pilot project's CTO: the first version tried every word as a possible
    command start, with quoted text read as code. With `supabase*` in the project's denies,
    `grep -rn "supabase" src/` became the command `supabase src/`, and the read-only audit seat
    could not read the product's whole auth layer: refused three times per run, no verdict.
    A guard that fires on ordinary work gets worked around (failure-catalogue #8). The bypass
    that version was written against, `bash -c 'git push'`, is caught here by reading what the
    shell is told to run, not by treating every argument as a command."""
    out = []
    for seg in _segments(cmd):
        out.append(seg)
        w = seg.split()
        v = w[0]
        if v in ("xargs", "npx", "bunx", "eval"):
            i = 1
            while i < len(w) and w[i].startswith("-"):
                i += 2 if w[i] in XARGS_VALUE_OPTS else 1
            if i < len(w):
                out.append(" ".join(w[i:]))
        if v == "find":
            for j, x in enumerate(w):
                if x in ("-exec", "-execdir", "-ok", "-okdir") and j + 1 < len(w):
                    out.append(" ".join(w[j + 1:]))
    if depth < 3:
        for m in _SH_C.finditer(cmd):
            out += _commands(m.group(2), depth + 1)
        for m in _EVAL.finditer(cmd):
            out += _commands(m.group(2), depth + 1)
        for sub in _substitutions(cmd):
            out += _commands(sub, depth + 1)
    return out


# FOUND 2026-10-28 (repo analysis): curl -o, tar, unzip, sort -o, an editor, ed, and git's index and
# plumbing writers all changed files while the guard said nothing.
SHELL_WRITERS = {"mv", "cp", "rm", "ln", "dd", "tee", "truncate", "touch", "chmod", "chown", "install",
                 "rsync", "mkdir", "rmdir", "unlink", "patch", "curl", "wget", "tar", "unzip", "zip", "gzip",
                 "gunzip", "bzip2", "bunzip2", "xz", "unxz", "ditto", "cpio", "split", "csplit", "ed", "ex",
                 "vi", "vim", "nvim", "nano", "emacs", "pico", "shred", "mkfifo", "mknod", "scp", "sftp"}
GIT_WRITES = {"checkout", "restore", "reset", "apply", "am", "stash", "clean", "rm", "mv", "commit", "merge",
              "rebase", "cherry-pick", "revert", "push", "pull", "switch", "branch", "tag", "worktree", "config",
              "add", "update-index", "update-ref", "checkout-index", "read-tree", "symbolic-ref", "notes", "gc",
              "prune", "replace", "filter-branch", "fast-import", "init", "clone", "submodule", "sparse-checkout"}


# A shell or interpreter runs whatever its argument says, and that argument is quoted text this
# parser does not read: it cannot be shown not to write, so in a card run it counts as a write.
OPAQUE = {"sh", "bash", "zsh", "fish", "dash", "ksh", "eval", "exec", "xargs", "python", "python3", "node",
          "deno", "bun", "npx", "perl", "ruby", "php", "osascript", "awk"}
SELF_CHECK = "python3 .ai-teammates/hooks/card.py accept"     # the one interpreter call a card seat is given


def _write_reason(blank, w):
    """Why one simple command would change files, or ''."""
    # Any unquoted '>' that is not 2>/dev/null writes a file. FOUND 2026-10-28: the pattern wanted a
    # space before it, so `cat a>b` passed, and the self-check was exempted before this was looked at.
    for m in re.finditer(r">>?\|?\s*(\S*)", blank):
        if m.group(1) != "/dev/null":
            return "it redirects output to %s" % (m.group(1) or "a file")
    if not w:
        return ""
    seg = " ".join(w)
    if seg == SELF_CHECK or seg.startswith(SELF_CHECK + " "):
        return ""
    verb = w[0]
    if verb in SHELL_WRITERS:
        return "it runs %s" % verb
    if verb in OPAQUE:
        return "it runs %s, which can change anything its argument says" % verb
    if verb in ("sed", "perl", "ruby") and any(x.startswith("-i") or x in ("-pi", "-pie") for x in w[1:]):
        return "it edits in place with %s -i" % verb
    if verb == "sort" and any(x.startswith("-o") or x.startswith("--output") for x in w[1:]):
        return "it writes a file with sort -o"
    if verb == "git" and len(w) > 1 and w[1] in GIT_WRITES:
        return "it runs git %s" % w[1]
    return ""


def shell_write(cmd, parts=None):
    """Why this command line would change files, or '' if it would not."""
    if _substitutions(cmd):
        return "it runs a command substitution ($(...) or backticks), which this check cannot read"
    for blank, w, _ in (parts if parts is not None else _parts(cmd)):
        why = _write_reason(blank, w)
        if why:
            return why
    return ""


def _scoped_delete(blank, w, raw, card, cardmod):
    """True for `git rm [-q] [--] <files>` where every file is one this card may change:
    no recursion, no globs, no redirect, no -C or --work-tree (FOUND 2026-10-28: `git -C .. rm a.txt`
    deleted another checkout's a.txt and passed as this card's), nothing outside the card."""
    if ">" in blank or len(raw) < 3 or os.path.basename(raw[0]) != "git" or raw[1] != "rm":
        return False
    paths = []
    for x in raw[2:]:
        if x in ("-q", "--quiet", "--"):
            continue
        if x.startswith("-") or any(ch in x for ch in "*?[]{}~$`"):
            return False
        paths.append(x)
    return bool(paths) and all(cardmod.allowed_edit(card, rel_path(p)) for p in paths)


def check_card_shell(tool, ti):
    """In a card run, files change only through Edit/Write on the card's own files, where the
    scope check above can see them. A shell write cannot be scope-checked while it happens, so
    it is refused (the runner's diff afterwards is the second line, not the only one).
    FOUND 2026-10-03 by the security-audit seat: the live scope check watched edit tools only."""
    cp = os.environ.get("AI_TEAMMATES_CARD")
    if tool != "Bash" or not cp:
        return
    cmd = str(ti.get("command") or "")
    # Deleting a file IS a change to it, and no edit tool can do it, so a seat could not finish
    # a card that removes a file (DOC-4, 2026-10-04: the CTO did the work itself for that reason).
    # `git rm` of files the card names is allowed; anything else that writes is refused below.
    parts = _parts(cmd)
    try:
        import card as cardmod
        c = json.loads(Path(cp).read_text(encoding="utf-8"))
        parts = [p for p in parts if not _scoped_delete(*p, c, cardmod)]
    except Exception:
        pass                                        # an unreadable card deletes nothing
    why = shell_write(cmd, parts)
    if why:
        block("card-shell-write", "This command would change files outside the scope check: %s." % why,
              "Make the change with Edit or Write on a file the card names. Read-only commands "
              "(grep, cat, ls, git diff, the acceptance checks) are fine.")


def _canon_rm(c):
    """`rm -fr`, `rm -r -f`, `rm --recursive --force` and `rm -Rf` are all `rm -rf`."""
    w = c.split()
    if not w or w[0] != "rm":
        return c
    flags, rest = set(), []
    for x in w[1:]:
        if x.startswith("--"):
            flags.add({"--recursive": "r", "--force": "f"}.get(x, ""))
        elif x.startswith("-") and len(x) > 1:
            flags |= set(x[1:].replace("R", "r"))
        else:
            rest.append(x)
    return "rm -rf " + " ".join(rest) if {"r", "f"} <= flags else c


def check_seat_denies(tool, ti):
    """What config.json required_denies names (git push, rm -rf, ssh by default) a SEAT never
    runs. Until 2026-10-03 the list was asserted by the test suite and enforced only by each
    seat's allowlist; nothing in the hooks backed it up (security-audit finding). The CTO and
    the owner are not seats: their pushes go through the branch rules and the pre-push hook."""
    if tool != "Bash" or not os.environ.get("AI_TEAMMATES_SEAT"):
        return
    import fnmatch
    pats = _config.required_denies()
    for c in _commands(str(ti.get("command") or "")):
        hit = next((p for p in pats if fnmatch.fnmatchcase(c, p) or fnmatch.fnmatchcase(_canon_rm(c), p)), None)
        if hit:
            block("seat-deny", "A seat never runs '%s' (config.json required_denies)." % hit,
                  "Stop and say what you needed it for: BLOCKED: seat-deny | <why>. The CTO or the "
                  "owner does it if it is right.")


def rel_path(p):
    """Repo-relative path however the agent spelled it. A plain string replace of
    the repo prefix missed a path written through a symlink (macOS /tmp is
    /private/tmp) or a relative one, so such a path walked past every path rule."""
    try:
        pp = Path(p) if os.path.isabs(p) else REPO / p
        return str(pp.resolve().relative_to(REPO.resolve()))
    except Exception:
        return p.replace(str(REPO) + "/", "")


def _locate(p):
    """(checkout, path inside it, that checkout's branch) for the checkout the file is IN, or None when
    it is in no checkout of this repository.

    FOUND 2026-10-28 (repo analysis): every path rule judged a file against the checkout the SESSION
    runs in. The CTO's session runs in the development checkout, so a product file in the production
    checkout, written by its absolute path, was "outside the repo" and passed; so did that checkout's
    hooks. A worktree has its own branch: the file's own checkout is the one that matters."""
    pp = (Path(p) if os.path.isabs(p) else REPO / p).resolve()
    d = pp.parent
    while not d.exists() and d != d.parent:          # a new file in a new folder
        d = d.parent
    if d not in _CHECKOUTS:                          # one shell line can name many files in a folder
        _CHECKOUTS[d] = _checkout(d)
    if _CHECKOUTS[d] is None:
        return None
    top, branch = _CHECKOUTS[d]
    try:
        rel = str(pp.relative_to(top))
    except ValueError:
        return None
    return top, rel, branch


_CHECKOUTS = {}


def _checkout(d):
    """(top, branch) of the checkout of THIS repository that folder d is in, else None."""
    run = lambda *a: subprocess.run([GIT, "-C", str(d), *a], capture_output=True, text=True, timeout=10)
    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    common = run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if not common or Path(common).resolve() != _config.common_dir().resolve():
        return None                                  # another repository: not this project's rules
    return Path(top.stdout.strip()).resolve(), run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


# Editing the guardrails is a decision for the owner, never a way past a block. config.json is one of
# them (FOUND 2026-10-28, repo analysis): it holds the card size limits, the protected branches, the
# product paths, the seats' denied commands and the CTO's own pre-approved commands, and a single edit
# to "cards" let a card of any size through.
GUARDRAIL_FILES = (".ai-teammates/hooks/", ".claude/settings.json", ".claude/settings.local.json",
                   ".ai-teammates/config.json")
_GUARDRAIL_TEXT = re.compile(r"\.ai-teammates/(?:hooks/|config\.json)|\.claude/settings(?:\.local)?\.json")


def _is_guardrail(rel):
    return rel.startswith(GUARDRAIL_FILES[0]) or rel in GUARDRAIL_FILES[1:]


def _guardrail_rel(word):
    """The guardrail file a shell word names, or ''."""
    if not _GUARDRAIL_TEXT.search(word):
        return ""
    try:
        loc = _locate(word)
    except Exception:
        loc = None
    rel = loc[1] if loc else rel_path(word)
    return rel if _is_guardrail(rel) else ""


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|$)", re.S)


def shell_guardrail_write(cmd):
    """The guardrail file this shell line would change, or ''.

    A file the line merely NAMES is not a write (`cat config.json`, `python3 .ai-teammates/hooks/card.py`
    runs a hook, it does not change one). It counts when the same simple command writes (a redirect,
    sed -i, cp, git checkout, ...) or is an interpreter, and when an interpreter is fed a heredoc that
    names one: that is how the pilot project's CTO edited JSON files."""
    for m in _HEREDOC.finditer(cmd):
        line = cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start()]
        head = [w for _, w, _ in _parts(line) if w]
        if head and head[-1][0] in OPAQUE and _GUARDRAIL_TEXT.search(m.group(3)):
            return _GUARDRAIL_TEXT.search(m.group(3)).group(0)
    for blank, w, raw in _parts(_HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], cmd)):
        if not w:
            continue
        # `cto setup` rewrites config.json and the seat specs, and running a hook script is otherwise
        # exempt below: an agent could have changed every setting through it without asking
        if (w[0] in ("cto", "ai-teammates") and w[1:2] == ["setup"]) \
                or any(x.endswith(".ai-teammates/hooks/setup.py") for x in raw):
            return ".ai-teammates/config.json"
        words = list(raw)
        if w[0] in ("python", "python3") and len(w) > 1 and w[1].startswith(".ai-teammates/hooks/") \
                and w[1].endswith(".py"):
            words = [x for x in words if x != w[1]]      # running a hook script is not writing it
        named = next((r for r in (_guardrail_rel(x) for x in words) if r), "")
        if named and (_write_reason(blank, w) or w[0] in OPAQUE):
            return named
    return ""


def check_guardrail_files(tool, ti):
    if tool == "Bash":
        rel = shell_guardrail_write(str(ti.get("command") or ""))
    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        p = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        if not p:
            return
        try:
            loc = _locate(p)
        except Exception:
            loc = None
        rel = loc[1] if loc else rel_path(p)
        rel = rel if _is_guardrail(rel) else ""
    else:
        return
    if rel:
        what = ("holds the guardrail settings: card size limits, protected branches, product paths, the "
                "seats' denied commands and the CTO's pre-approved commands" if rel.endswith("config.json")
                else "is part of the guardrails")
        ask("guardrail-files",
            "%s %s. Changing it is the owner's decision; it is not a way past a block. If a guard fires "
            "on correct behaviour, say so and propose the fix instead." % (rel, what))


def check_frontend_edit(tool, ti):
    """Product code is edited on a feature branch, never in the main checkout.

    SUPERSEDED 2026-09-06: the old rule was "UI work routes through the
    UI builder's MCP". The UI builder was dropped from production on 2026-08-25; the block stayed
    and its advice pointed at a tool that no longer exists.

    What the rule protects: guarded paths (config "guarded_paths") get edited
    on a feature branch in a worktree, so the change is reviewable and the
    pre-push hook can gate it. Editing them while a protected branch is checked
    out is how product code reaches it without review - which happened four times.

    So: allowed on a feature branch, blocked on protected branches. This mirrors
    prepush.py, one layer earlier. Delegation to a seat is a preference, not a
    wall - the CTO deploys and sometimes must fix directly.
    """
    if tool not in ("Edit", "Write", "NotebookEdit"):
        return
    p = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
    if not p:
        return
    # Which checkout is the FILE in, and which branch is that checkout on? A worktree has its own HEAD,
    # and that is the whole point - seats and the CTO work on feature branches there.
    try:
        loc = _locate(p)
    except Exception:
        loc = False                      # could not tell: judge it as this checkout's, below
    if loc is None:
        return                           # in no checkout of this repository
    if loc:
        top, rel, branch = loc
    else:
        top, rel = REPO, rel_path(p)
        branch = _config.branches()[0]   # fail closed: unknown = production
    if not rel.startswith(_config.guarded()):
        return
    # Mechanical files inside guarded paths (generated code, tool config).
    # Project-specific: config "edit_exempt", a list of regexes.
    if any(re.search(x, rel) for x in _config.load()["edit_exempt"]):
        return

    if branch not in _config.protected():
        return                   # feature branch: this is where product work belongs

    block(
        "product-code-on-protected",
        "%s is product code in the checkout %s, which is on '%s'. Product code is "
        "edited on a feature branch in a worktree, so the change is reviewable "
        "and the pre-push hook can gate it." % (rel, top.name, branch),
        "Work in a feature worktree (git worktree add ../<project>-<name> -b "
        "feat/<name>), or delegate to a seat: seat.py run <seat> \"<task>\".",
    )


# --- the framework's own source ----------------------------------------------
# FOUND 2026-10-29: the pilot project's CTO, asked to fix a failing setup check, fixed the framework's
# test in its project copy, then read the AI-teammates repository's AGENTS.md and set out to branch and
# open a pull request there. The owner: AI-teammates is not the project's work; its fixes are made and
# reviewed in its own repository. A project's agents read it and run it; they report what is wrong.
FRAMEWORK_MARKERS = ("install.sh", "bin/cto", "core/hooks/guard.py")
COPY_VERBS = {"cp", "rsync", "install", "ditto", "scp", "ln"}       # only the destination is written
GH_READS = {"view", "list", "status", "checks", "diff", "checkout"}  # gh pr/issue verbs that change nothing there


def _framework_root(p):
    """The AI-teammates source checkout p is in (the dev repository, or the stable copy `cto` runs
    from), else None. Known by its files, not its name or remote: a fork or a clone elsewhere too."""
    try:
        d = Path(os.path.expanduser(str(p))).resolve()
    except Exception:
        return None
    for c in (d, *d.parents):
        if all((c / m).is_file() for m in FRAMEWORK_MARKERS):
            return c
    return None


def _at(base, word):
    return Path(base) / os.path.expanduser(word) if not os.path.isabs(os.path.expanduser(word)) \
        else Path(os.path.expanduser(word))


def framework_write(cmd, cwd):
    """(what, framework root) for the first part of this shell line that would change the framework's
    source, else None. A `cd` earlier in the line moves where the rest runs."""
    here = Path(cwd)
    for blank, w, raw in _parts(cmd):
        if not w:
            continue
        if w[0] in ("cd", "pushd"):
            here = _at(here, w[1]) if len(w) > 1 and not w[1].startswith("-") else Path.home()
            continue
        at = here
        if w[0] == "git":                                # git -C <dir> (each one relative to the last)
            for i, x in enumerate(raw[:-1]):
                if x == "-C":
                    at = _at(at, raw[i + 1])
        root = _framework_root(at)
        if root and w[0] == "git" and len(w) > 1 and w[1] in GIT_WRITES:
            return "git %s in %s" % (w[1], root), root
        if root and w[0] == "gh" and len(w) > 2 and w[1] in ("pr", "issue", "release", "repo") \
                and w[2] not in GH_READS:
            return "gh %s %s from %s" % (w[1], w[2], root), root
        if w[0] in OPAQUE or not _write_reason(blank, w):
            continue                                     # running the framework's scripts is using it
        targets = [m.group(1) for m in re.finditer(r">>?\|?\s*(\S+)", blank)]
        if _write_reason("", w):                         # the command itself writes, not only a redirect
            args = [x for x in w[1:] if not x.startswith("-") and not re.match(r"^\d*[<>]", x)]
            targets += args[-1:] if w[0] in COPY_VERBS else args
        for t in targets:
            root = _framework_root(_at(here, t))
            if root:
                return "%s (%s)" % (t, _write_reason(blank, w)), root
    return None


def check_framework_source(tool, ti):
    if _framework_root(REPO):
        return                                           # a session working ON the framework
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        f = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        root = _framework_root(_at(CWD, f)) if f else None
        hit = (f, root) if root else None
    elif tool == "Bash":
        hit = framework_write(str(ti.get("command") or ""), CWD)
    else:
        return
    if hit:
        block("framework-source",
              "This would change the AI-teammates source itself: %s. The framework is not this project's "
              "work; its fixes are made and reviewed in its own repository, by the owner." % hit[0],
              "Report it to the owner and stop: the framework file, the check or behaviour that is wrong, "
              "the evidence, and the fix you propose. Reading the framework source and running its scripts "
              "is fine. A workaround in this project's own .ai-teammates/ copy is the owner's call (the "
              "guard asks), and the next upgrade replaces it.")


def _product_file(p):
    """(checkout, path) when p is a product file (guarded, not edit_exempt) of this repository, else None."""
    try:
        loc = _locate(p)
    except Exception:
        loc = False
    if loc is None:
        return None
    top, rel = (loc[0], loc[1]) if loc else (REPO, rel_path(p))
    # `rm -rf src` names the product folder itself: "src" is not under "src/" by prefix
    if not (rel.startswith(_config.guarded()) or rel + "/" in _config.guarded()) \
            or any(re.search(x, rel) for x in _config.load()["edit_exempt"]):
        return None
    return top, rel


# --- where a shell write lands ------------------------------------------------
# FOUND 2026-11-11 by the pilot project's CTO, reproduced 2026-11-12 (issue #29): the delegation rule
# resolved every word of a writing command against the repository root. `cd src && sed -i ... a.ts`,
# `git -C src checkout -- a.ts`, the same write from a session already in src/, `F=src/a.ts; rm $F`,
# `bash -c '...'`, `find src -exec sed -i ...` and `rm -rf src` all changed product code unseen.
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
DECLARE = {"export", "local", "readonly", "declare", "typeset"}
GIT_PATH_VERBS = {"checkout", "restore", "rm", "mv", "apply", "clean", "stash"}   # their words can be files
TRANSFER = {"curl", "wget", "scp", "sftp"}                 # their words are mostly URLs and headers
_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _plain(words):
    """The words that can name a file: no options, but the value of --option=value; a word glued to a
    redirect (>out.txt) without the redirect."""
    out = []
    for x in words:
        if x.startswith("-"):
            x = x.split("=", 1)[1] if "=" in x else ""
        out += [y for y in re.split(r"[<>|&]+", x) if y]
    return out


def _redirect_targets(blank, text):
    """The files this simple command redirects its output to."""
    out = []
    for m in re.finditer(r">>?\|?", blank):
        first = _words(text[m.end():])[:1]
        if first and first[0] != "/dev/null" and not first[0].startswith("("):
            out.append(first[0])
    return out


def _writes_paths(w):
    """True when this command's own words name the files it changes (rm, cp, sed -i, git checkout ...).
    An interpreter's words are a script and its arguments, curl's are URLs and headers, git commit's
    are a message: those are judged only where they do name a product file."""
    if w[0] == "git":
        return len(w) > 1 and w[1] in GIT_PATH_VERBS
    return bool(_write_reason("", w)) and w[0] not in OPAQUE and w[0] not in TRANSFER


def _written(blank, w, text):
    """The words of one simple command that name what it writes."""
    own = []
    if _writes_paths(w):
        if w[0] == "git":
            own = _plain(w[w.index("--") + 1:] if "--" in w else w[2:])
        elif w[0] == "sed":                              # its script is not a file
            rest, script, i = [], False, 1
            while i < len(w):
                if w[i] in ("-e", "-f", "--expression", "--file"):
                    script, i = True, i + 1
                elif w[i] and not w[i].startswith("-"):
                    rest.append(w[i])
                i += 1
            own = _plain(rest if script else rest[1:])
        else:
            own = _plain(w[1:])
            own = own[-1:] if w[0] in COPY_VERBS else own
    return own + _redirect_targets(blank, text)


def _lift(cmd):
    """(cmd with each $(...) and `...` replaced by one word, what those run). mktemp's output becomes
    a path in the temporary folder; any other output is unknown here, and reads as a variable
    nothing can resolve."""
    bodies = []
    for _ in range(4):                                   # innermost first
        text = re.sub(r"'[^']*'", lambda m: " " * len(m.group(0)), cmd)
        found = list(_SUBST.finditer(text))
        if not found:
            break
        for m in reversed(found):
            g = 1 if m.group(1) is not None else 2
            body = cmd[m.start(g):m.end(g)]
            bodies.append(body)
            first = next((w for _, w, _ in _parts(body) if w), [""])
            cmd = cmd[:m.start()] + (os.path.join(tempfile.gettempdir(), "mktemp") if first[0] == "mktemp"
                                     else "$?") + cmd[m.end():]
    return cmd, bodies


def shell_product_write(cmd, cwd):
    """(checkout, product file, why) for the first write to product code in this shell line;
    (None, what could not be read, why) for a write this check cannot place; else None.

    It follows where each command runs: `cd`, `pushd` and `popd`, a subshell that ends, `git -C`,
    variables set earlier in the line (W=...; cd "$W"), loop variables and the environment. What
    `bash -c`, `eval`, `$(...)` and a heredoc fed to a shell run is judged from every folder the
    line visits; what `find -exec` and `xargs` run, from where they stand.

    A guard that cannot decide refuses: a file named through a variable or a command's output it
    cannot read, a relative file after a `cd` it cannot follow, and files that arrive on xargs'
    input. Only for commands whose words are the files they change (_writes_paths): an
    interpreter given `"$ID"` is not refused. MEASURED 2026-11-12 on 351 shell commands of real CTO
    sessions: 21 of 31 `cd`s and 33 of 41 `git -C`s go through a variable set in the same line.

    Known limits: what an interpreter's own script writes (python3 -c, a heredoc fed to python), a
    script written to a file and then run, a function's body is read where it is defined, and a
    variable set by `read` or in an earlier call is unknown."""
    known, visited, ever, product_folder = {}, [], {"lost": False}, {}

    def value(m):
        name = m.group(1) or m.group(2)
        return known[name] if name in known else [os.environ[name]] if name in os.environ else None

    def expand(word):
        """Every value this word can have, or None when part of it cannot be read."""
        outs, pos = [""], 0
        for m in _VAR.finditer(word):
            vals = value(m)
            if vals is None:
                return None
            outs = [o + word[pos:m.start()] + v for o in outs for v in vals]
            pos = m.end()
            if len(outs) > 64:
                return None
        outs = [o + word[pos:] for o in outs]
        return None if any("$" in o or "`" in o for o in outs) else outs

    def start_of(word):
        """What can be read of a word that expand() cannot: its values up to the first unknown part."""
        outs, pos = [""], 0
        for m in list(_VAR.finditer(word)) + [None]:
            text = word[pos:m.start() if m else len(word)]
            cut = re.search(r"[$`]", text)
            if cut:
                return [o + text[:cut.start()] for o in outs]
            outs = [o + text for o in outs]
            vals = value(m) if m else None
            if vals is None:
                return outs
            outs, pos = [o + v for o in outs for v in vals][:64], m.end()

    def reach(bases, lost, start):
        """For a path known only up to `start`: a product folder it is in, True when it could be
        product code, False when its folder is outside the project or holds no product code.
        `/tmp/out.$(date +%s).txt` is not a product file whatever the date is."""
        folder = start[:start.rfind("/") + 1]
        if not folder or (lost and not os.path.isabs(os.path.expanduser(folder))):
            return True
        for b in bases:
            probe = str(_at(b, folder) / "x")
            try:
                loc = _locate(probe)
            except Exception:
                return True
            if loc is None:
                continue                                 # outside this repository
            rel = os.path.dirname(loc[1])
            if _product_file(probe):
                return loc[0], rel + "/..."
            if not rel or any(g.startswith(rel + "/") for g in _config.guarded()):
                return True                              # product code lies under it
        return False

    def in_product(folder):
        if folder not in product_folder:
            product_folder[folder] = _product_file(str(folder)) is not None
        return product_folder[folder]

    def assign(word):
        m = _ASSIGN.match(word)
        if m:
            vals = expand(m.group(2))
            old = known.get(m.group(1), [])
            known[m.group(1)] = None if vals is None or old is None else old + vals
        return bool(m)

    def stand(at, dirs, lost=False):
        """The line now runs in `dirs` (more than one only when a variable has several values)."""
        at["dirs"], at["lost"] = dirs, lost or at["lost"]
        visited.extend(d for d in dirs if d not in visited)
        ever["lost"] = ever["lost"] or at["lost"]

    def move(at, word):
        vals = expand(word)
        if vals is None or len(at["dirs"]) * len(vals) > 32:
            stand(at, at["dirs"], lost=True)
        else:
            stand(at, [Path(os.path.normpath(str(_at(d, v)))) for d in at["dirs"] for v in vals])

    def one(at, blank, w, raw, text, strict, depth):
        for x in raw:                                    # VAR=value in front of the command, or alone
            if not assign(x):
                break
        if not w:
            return None
        verb = w[0]
        if verb in DECLARE:
            for x in w[1:]:
                assign(x)
            return None
        if verb == "for" and len(w) > 3 and w[2] == "in":
            vals = [expand(x) for x in w[3:]]
            known[w[1]] = None if None in vals else [v for vs in vals for v in vs]
            return None
        if verb in ("cd", "pushd", "popd"):
            to = [x for x in w[1:] if x == "-" or not x.startswith("-")]
            was = (list(at["dirs"]), at["lost"])
            if verb == "popd" or to[:1] == ["-"] or (verb == "pushd" and not to):
                back = at["stack"].pop() if verb == "popd" and at["stack"] else at.get("old")
                at["dirs"], at["lost"] = back if back else (at["dirs"], True)    # nowhere known to go back to
                stand(at, at["dirs"])
            else:
                move(at, to[0] if to else str(Path.home()))
            if verb == "pushd":
                at["stack"].append(was)
            at["old"] = was
            return None
        if verb == "find":                               # what -exec runs, on each place find starts from
            starts = []
            for x in w[1:]:
                if x.startswith("-") or x in ("(", "!"):
                    break
                starts.append(x)
            for j, x in enumerate(w):
                if x in ("-exec", "-execdir", "-ok", "-okdir"):
                    inner = []
                    for y in w[j + 1:]:
                        if y in ("+", ";") or y.startswith("-exec") or y.startswith("-ok"):
                            break
                        inner.append(y)
                    for st in starts or ["."]:
                        hit = walk(shlex.join([y.replace("{}", st) for y in inner]), strict, depth + 1,
                                   at["dirs"], at["lost"])
                        if hit:
                            return hit
        if verb == "xargs":
            i, into = 1, ""
            while i < len(w) and w[i].startswith("-"):
                if w[i] in ("-I", "-J") and i + 1 < len(w):
                    into = w[i + 1]
                i += 2 if w[i] in XARGS_VALUE_OPTS + ("-J",) else 1
            inner = [iw for _, iw, _ in _parts(shlex.join(w[i:])) if iw][:1]
            if inner:
                hit = walk(shlex.join(w[i:]), strict, depth + 1, at["dirs"], at["lost"])
                if hit:
                    return hit
                iw = inner[0]
                # cp -I{} {} dest/ names its destination; without -I what arrives is the last word
                placed = iw[0] in COPY_VERBS and into and into not in iw[-1]
                if strict and _writes_paths(iw) and not placed:
                    return None, "the files arrive on xargs' input", _write_reason("", iw) or "it runs %s" % iw[0]
            return None
        why = _write_reason(blank, w)
        if not why:
            return None
        bases, lost = at["dirs"], at["lost"]
        if verb == "git":                                # git -C <dir> (each one relative to the last)
            for i, x in enumerate(raw[:-1]):
                if x == "-C":
                    vals = expand(raw[i + 1])
                    if vals is None:
                        lost = True
                    else:
                        bases = [_at(b, v) for b in bases for v in vals]
        written = _written(blank, w, text)
        unsure = ""
        for word in dict.fromkeys(_plain(w[2:] if verb == "git" else w[1:]) + written):
            vals = expand(word)
            if vals is None:
                for start in start_of(word) if word in written else ():
                    where = reach(bases, lost, start)
                    if isinstance(where, tuple):
                        return where + (why,)
                    if where:
                        unsure = unsure or ("%s is named through a variable or a command's output it "
                                            "cannot read" % word)
                continue
            for v in vals:
                if lost and word in written and not os.path.isabs(os.path.expanduser(v)):
                    unsure = unsure or ("%s is relative to a folder it cannot follow (a cd or git -C through a "
                                        "variable or a command's output it cannot read)" % v)
                for b in bases:
                    # Standing in a product folder, every word is a path inside it (`git commit -m msg`,
                    # an awk script). Only a word the command writes, or a file that is there, counts.
                    if word not in written and in_product(b) and not os.path.lexists(str(_at(b, v))):
                        continue
                    f = _product_file(str(_at(b, v)))
                    if f:
                        return f[0], f[1], why
        return (None, unsure, why) if unsure and strict else None

    def walk(text, strict, depth, dirs, lost):
        if depth > 4:                                    # bash -c in bash -c in ...: not read, so not allowed
            return (None, "it nests commands deeper than this check reads", "of what they may run") if strict else None
        at = {"dirs": [], "lost": False, "stack": []}
        stand(at, list(dirs), lost)
        heredocs, scopes = [], []

        def body(m):
            head = text[text.rfind("\n", 0, m.start()) + 1:m.start()] + m.group(0).split("\n", 1)[0]
            heredocs.append((m.group(3), any(w and w[0] in SHELLS for _, w, _ in _parts(head))))
            return m.group(0).split("\n", 1)[0]
        text = _HEREDOC.sub(body, text)
        runs = []
        if strict:
            text, runs = _lift(text)
            runs += [m.group(2) for m in _SH_C.finditer(text)] + [m.group(2) for m in _EVAL.finditer(text)]
        for blank, w, raw, own, opens, closes in _parts_text(text):
            scopes += [(list(at["dirs"]), at["lost"])] * opens
            hit = one(at, blank, w, raw, own, strict, depth)
            if hit:
                return hit
            for _ in range(closes):                      # a subshell ends: so does its cd
                if scopes:
                    at["dirs"], at["lost"] = scopes.pop()
        # what these run is not placed in the line's order: judged from every folder the line visits
        for sub in runs:
            hit = walk(sub, strict, depth + 1, visited, ever["lost"])
            if hit:
                return hit
        for sub, is_shell in heredocs:
            # a heredoc fed to a shell runs; any other is text (a commit message, a script for an
            # interpreter), read as before: only for a line that plainly writes a product file
            hit = walk(sub, strict and is_shell, depth + 1, visited, ever["lost"])
            if hit:
                return hit
        return None

    return walk(cmd, True, 0, [Path(cwd)], False)


def check_delegation(tool, ti):
    """The owner's policy for the CTO writing product code itself: config.json
    "delegation": {"cto_product_edits": "block" | "ask" | "allow"}, block when unset
    (_config.delegation_policy): product code reaches the project through cards.

    MEASURED 2026-10-17: on one large owner request the CTO delegated two cards and wrote 982 of
    1,252 product lines itself (78%), in a feature worktree it made, where no rule applied; the
    "delegate when" rules in cto.md are advice to the model. The CTO's session only: a seat works
    inside its card's scope. Resolving a conflict while a merge is in progress is not authoring."""
    policy = _config.delegation_policy()
    if policy == "allow" or os.environ.get("CLAUDE_AGENT_NAME") != "cto" \
            or os.environ.get("AI_TEAMMATES_SEAT") or os.environ.get("AI_TEAMMATES_CARD"):
        return
    hit, how = None, "edits"
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        hit = _product_file(str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or ""))
    elif tool == "Bash":
        try:
            found = shell_product_write(str(ti.get("command") or ""), CWD)
        except Exception as e:                           # a guard that cannot decide refuses
            found = (None, "the command could not be read (%s)" % type(e).__name__, "of what it may run")
        if found:
            hit, how = found[:2], "would change, because %s," % found[2]
    if not hit:
        return
    top, rel = hit
    card = ("Write a card for it: python3 .ai-teammates/hooks/card.py new <ID> --seat <seat> ..., then "
            "seat.py run --card <ID> and seat.py land <ID>. Resolving a merge conflict is allowed.")
    if top is None:
        msg = ("This %s a file this check cannot place: %s. It cannot tell that the file is not product "
               "code, and the owner's delegation policy (config.json delegation.cto_product_edits = %s) says "
               "product code reaches the project through cards." % (how, rel, policy))
        card = ("If the file is not product code, name it by its full path, with no variable, command "
                "output or cd in front that this check cannot read. If it is product code: " + card)
    else:
        if subprocess.run([GIT, "-C", str(top), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode == 0:
            return
        msg = ("This %s %s, product code. The owner's delegation policy (config.json delegation."
               "cto_product_edits = %s) says product code reaches the project through cards." % (how, rel, policy))
    if policy == "ask":
        ask("delegation-policy", msg + " Approve to let the CTO write it itself this time.")
    block("delegation-policy", msg, card)


# `git push`, also as `git -C dir push` / `git -c k=v push`.
_PUSH = r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+push\b"
# git's own commands: an alias of the same name is ignored by git, so these are never looked up
GIT_BUILTINS = {"add", "am", "apply", "archive", "bisect", "blame", "branch", "cat-file", "checkout",
                "cherry-pick", "clean", "clone", "commit", "config", "describe", "diff", "fetch",
                "for-each-ref", "format-patch", "gc", "grep", "help", "init", "log", "ls-files", "ls-remote",
                "merge", "merge-base", "mv", "notes", "pull", "push", "rebase", "reflog", "remote", "reset",
                "restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show", "show-ref", "stash",
                "status", "submodule", "switch", "symbolic-ref", "tag", "update-ref", "version", "worktree"}
MERGE_VALUE_OPTS = {"-m", "-F", "-s", "-X", "--strategy", "--strategy-option", "--file", "--message",
                    "--into-name"}


def _reads_hookspath(w):
    """`git config core.hooksPath` / `--get core.hooksPath` / `--list`: reading it changes nothing."""
    rest = [x.lower() for x in w[2:]]
    return w[:2] == ["git", "config"] and (rest == ["core.hookspath"] or rest in (["--list"], ["-l"])
                                           or (len(rest) == 2 and rest[0] in ("--get", "--get-all")))


def _alias(sub, rw, repo):
    """What `git <sub>` runs when <sub> is an alias: `-c alias.<sub>=...` on this command line first,
    then the repository's config. '' when it is not one."""
    key = "alias.%s=" % sub.lower()
    for i, x in enumerate(rw):
        v = rw[i + 1] if x == "-c" and i + 1 < len(rw) else (x[2:] if x.startswith("-c") else "")
        if v.lower().startswith(key):
            return v.split("=", 1)[1]
    try:
        r = subprocess.run([GIT, "config", "--get", "alias." + sub], cwd=str(repo), capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _expand_aliases(gits, repo):
    """The git commands that git aliases in this line would really run.

    FOUND 2026-10-28 (repo analysis): `git -c alias.ship='push --no-verify' ship origin HEAD:main`
    skipped the pre-push hook and matched nothing here, and so did an alias left in the repo's config.
    An alias may name another alias, or start with '!' and run a shell line."""
    out = []
    for w, rw in gits:
        for _ in range(3):
            if len(w) < 2 or w[1].startswith("-") or w[1] in GIT_BUILTINS:
                break
            val = _alias(w[1], rw, repo)
            if not val:
                break
            line = (val[1:] if val.startswith("!") else "git " + val) + " " + " ".join(shlex.quote(x) for x in w[2:])
            out.append(line)
            nxt = [(p[1], p[2]) for p in _parts(line) if p[1]]
            if not nxt or nxt[0][0][0] != "git":
                break
            w, rw = nxt[0]
    return out


def check_git(tool, ti):
    if tool != "Bash":
        return
    cmd = " ".join((ti.get("command") or "").split())
    if not cmd:
        return

    # `git -C <dir>` and a leading `cd <dir> &&` retarget the command at
    # ANOTHER worktree. Until 2026-09-18 every rule below keyed on literal
    # "git <verb>" adjacency and THIS checkout's branch, which was wrong in
    # both directions at once: `git -C ../main-repo commit` sailed past the
    # flow rule entirely, and `cd ../project-dev && git commit` was blocked
    # while targeting the development branch it was supposed to use. Resolve
    # the real target repo for the branch check, and match verbs with any
    # -C stripped so flag-shaped gaps cannot reappear.
    norm = re.sub(r"\bgit\s+-C\s+\S+", "git", cmd)
    _m = re.search(r"\bgit\s+-C\s+(\S+)", cmd) or re.match(r"\s*cd\s+(\S+)\s*&&", cmd)
    eff_repo = REPO
    if _m:
        _t = _m.group(1)
        try:
            _p = (Path(_t) if os.path.isabs(_t) else REPO / _t).resolve()
            if (_p / ".git").exists():
                eff_repo = _p            # else: cannot see it - guard this checkout
        except Exception:
            pass

    parts = _parts(str(ti.get("command") or ""))
    gits = [(w, rw) for _, w, rw in parts if w and w[0] == "git"]

    # core.hooksPath decides which pre-push hook git runs, so setting it - for one command with -c or
    # GIT_CONFIG_*, or for good with `git config` - is --no-verify by another name. FOUND 2026-10-28 (repo
    # analysis): `git -c core.hooksPath=/dev/null push` put unreviewed product code on a real remote.
    for _, w, rw in parts:
        env_set = any(re.match(r"(?i)^GIT_CONFIG_\w*=.*hookspath", x) for x in rw)
        git_set = bool(w) and w[0] == "git" and "hookspath" in " ".join(rw).lower() and not _reads_hookspath(w)
        if env_set or git_set:
            block("hooks-path",
                  "This changes core.hooksPath, which decides whether git runs the pre-push hook at all - "
                  "the one check that sees where a push lands.",
                  "Leave core.hooksPath alone. If the project needs another hooks folder (husky, lefthook), "
                  "the owner sets it and re-runs install.sh so the pre-push hook is placed there.")

    # What aliases in this line really run is checked like the line itself
    expansions = _expand_aliases(gits, eff_repo)
    if expansions:
        cmd = cmd + " ; " + " ; ".join(expansions)
        norm = re.sub(r"\bgit\s+-C\s+\S+", "git", cmd)
        gits += [(p[1], p[2]) for e in expansions for p in _parts(e) if p[1] and p[1][0] == "git"]

    # WHERE a push lands is judged by the git pre-push hook (prepush.py), which
    # git tells the resolved destination. This used to be a regex here plus a
    # call to prepush about the CURRENT branch: `git push origin feat:main`
    # passed, `git push origin HEAD` and `git -C . push origin main` never
    # matched. Guessing a destination from command text is the failure; so
    # this layer only makes sure the hook can run: it exists, and is not skipped.
    if re.search(_PUSH, cmd):
        # git accepts any unambiguous abbreviation of a long option: --no-verif and --no-veri skip
        # the hook too (FOUND 2026-10-28, executed against a real remote)
        if re.search(r"--no-veri", cmd):
            block("no-verify",
                  "--no-verify skips the pre-push hook - the one check that "
                  "sees where this push actually lands.",
                  "Drop --no-verify. A genuine override is announced instead:\n"
                  "AI_TEAMMATES_ALLOW='<why>' git push ...   (logged)")
        if not _config.prepush_hook_installed():
            block("prepush-hook-missing",
                  "The AI-teammates pre-push hook is not installed in %s, so "
                  "nothing would judge where this push lands." % _config.hooks_dir(),
                  "Re-run install.sh from the project, or add the hook by hand.")

    # -f is the shorthand people actually type; the long form alone was a gap.
    if re.search(_PUSH + r"[^|;&]*(--force(?!-with-lease)|\s-\w*f)", cmd):
        block("force-with-lease",
              "Force-push without a lease can silently discard work that "
              "landed upstream since your last fetch. This catches both "
              "--force and the -f shorthand.",
              "Use --force-with-lease.")

    if re.search(r"\bgit\s+branch\s+-D\b", norm):
        block("verify-before-delete",
              "git branch -D force-deletes without checking whether the "
              "commits live anywhere else. This exact command lost the "
              "URL-validation work once already.",
              "Run: git log --oneline <keep-branch>..<delete-branch>\n"
              "Confirm the count is 0, then delete.")

    if re.search(r"\brm\s+-rf?\b[^|;&]*(\.git\b|\.ai-teammates)", cmd):
        block("destructive-path",
              "Recursive delete aimed at a path holding state we cannot "
              "regenerate.",
              "Archive it first, or ask.")

    # --- flow: nothing is authored on the production branch -----------------
    # Caught at the moment of the commit, not later at push time. prepush found
    # these five times in one day and waved them all through, because they were
    # config or docs and its rule is about file type. The flow rule is about
    # direction: work is authored on a feature branch or the integration
    # branch, and reaches production ONLY by merge.
    production, integration = _config.branches()
    if integration:
        cur = subprocess.run([GIT, "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=str(eff_repo), capture_output=True, text=True).stdout.strip()
        # Concluding a conflicted promotion merge IS a commit on production -
        # and the correct one. Blocking it made the flow unfinishable.
        merging = subprocess.run([GIT, "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                                 cwd=str(eff_repo), capture_output=True).returncode == 0
        if cur == production:
            if re.search(r"\bgit\s+commit\b", norm) and "--amend" not in cmd and not merging:
                block("flow-direction",
                      f"This commits directly onto {production}. The rule is "
                      f"feature -> {integration} -> {production}: {production} "
                      f"advances only by merging {integration}. Five commits "
                      f"broke this in one day because each was 'only config'.",
                      f"git checkout {integration}   (or a feature branch), commit "
                      f"there, then merge into {production} when it is accepted.")
            remotes = set(subprocess.run([GIT, "remote"], cwd=str(eff_repo), capture_output=True,
                                         text=True).stdout.split())

            def is_integration(ref):
                ref = re.sub(r"^(refs/heads/|refs/remotes/|remotes/)", "", ref.lstrip("+"))
                return ref == integration or any(ref == "%s/%s" % (r, integration) for r in remotes)

            for w, _ in gits:
                sub, args = (w[1] if len(w) > 1 else ""), w[2:]
                # Authoring on production by another verb. FOUND 2026-10-28 (repo analysis): only
                # `git commit` was checked, so a cherry-pick, revert or rebase onto it went through.
                if sub in ("cherry-pick", "revert", "am", "rebase") and not {"--abort", "--quit"} & set(args):
                    block("flow-direction",
                          f"`git {sub}` writes new commits onto {production}. The rule is feature -> "
                          f"{integration} -> {production}: {production} advances only by merging {integration}.",
                          f"Do the {sub} on {integration} (or a feature branch), then promote {integration}.")
                if sub == "pull":
                    pos = [a for a in args if not a.startswith("-")]
                    other = [r for r in pos[1:] if not is_integration(r.split(":", 1)[0])
                             and r.split(":", 1)[0].lstrip("+") not in (production, "refs/heads/" + production)]
                    if other:
                        block("flow-direction",
                              f"This pulls {other[0]} into {production}. Feature branches are integrated in "
                              f"{integration} and tested there first.",
                              f"Merge it into {integration}, verify, then merge {integration} into {production}.")
                if sub != "merge" or {"--abort", "--continue", "--quit"} & set(args):
                    continue
                pos, skip = [], False
                for a in args:
                    if skip:
                        skip = False
                    elif a in MERGE_VALUE_OPTS:
                        skip = True
                    elif not a.startswith("-"):
                        pos.append(a)
                # The branch must BE the integration branch. FOUND 2026-10-28 (repo analysis): this
                # was a substring test, so `git merge feat/development-hotfix` passed as a promotion.
                if not pos or not all(is_integration(a) for a in pos):
                    block("flow-direction",
                          f"This merges something other than {integration} into "
                          f"{production}. Feature branches are integrated in "
                          f"{integration} and tested there first.",
                          f"Merge it into {integration}, verify, then merge "
                          f"{integration} into {production}.")
                # Promotion is the last moment an independent review can still
                # happen. The CTO wrote auth in front of a money-spending proxy,
                # reviewed its own work, merged and deployed - the read-only audit
                # seat existed and was never launched.
                # This judges a SPELLING. prepush.py runs the same check on every push
                # that lands on production, so `git push origin development:main` is
                # caught there (2026-09-30).
                rv = REPO / ".ai-teammates" / "hooks" / "review.py"
                if rv.exists():
                    r = subprocess.run([sys.executable, str(rv)], cwd=str(eff_repo),
                                       capture_output=True, text=True)
                    if r.returncode != 0:
                        block("independent-review",
                              "This promotion carries security-touching commits "
                              "that no independent review covers:\n"
                              + r.stdout.strip()[:700],
                              "Launch the read-only audit seat, then file the "
                              "result with review.py --record.")


HANDLERS = (check_blocked_gate, check_card_scope, check_card_shell, check_seat_denies,
            check_framework_source, check_guardrail_files, check_frontend_edit, check_delegation, check_git)


def main():
    global SESSION_ID, CWD
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never break the session on a malformed payload
    SESSION_ID = payload.get("session_id") or ""
    CWD = payload.get("cwd") or CWD

    tool = payload.get("tool_name") or payload.get("tool") or ""
    ti = payload.get("tool_input") or {}

    override = os.environ.get("AI_TEAMMATES_OVERRIDE")
    if override:
        log("override", {"tool": tool, "reason": override[:200]})
        sys.exit(0)

    for handler in HANDLERS:
        handler(tool, ti)

    sys.exit(0)


if __name__ == "__main__":
    main()
