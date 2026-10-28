#!/usr/bin/env python3
"""Run a command in a NEW SESSION so it survives its launcher.

Why this exists: `nohup ./cto "$BRIEF" &` looked right and died silently three
times - the log stopped at the pre-flight banner, no error, no output. nohup
only blocks SIGHUP; it does not detach from the process group, so when the
launching shell's group was torn down the run went with it. The identical
command in the foreground completed fine, which is what made it look like a
flag-parsing problem for two rounds of debugging.

os.setsid() puts the child in its own session with no controlling terminal, so
nothing upstream can take it down.

    detach.py /tmp/run.log ./cto "the brief"
"""
import os
import sys

if len(sys.argv) < 3:
    sys.exit("usage: detach.py LOGFILE COMMAND [ARGS...]")

logfile, argv = sys.argv[1], sys.argv[2:]

if os.fork() > 0:
    os._exit(0)          # launcher returns immediately
os.setsid()              # new session - this is the part nohup does not do
if os.fork() > 0:
    os._exit(0)          # cannot reacquire a controlling terminal

fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)

print(f"[detach] pid {os.getpid()}: {' '.join(argv)}", flush=True)
os.execvp(argv[0], argv)
