"""
step_runner.py — one step runner for both orchestrators (issue #35)

run.py and pol_refresh.py used to judge steps differently: run.py ran scripts as
subprocesses and read exit codes; pol_refresh.py called functions in-process and
caught exceptions. This module unifies them.

A `Step` is either:
  - kind="subprocess": run `script` (+ args) as `sys.executable script args`;
    failure is a non-zero exit code.
  - kind="call":       call `fn(**kwargs)` in-process; failure is an exception.

`run_steps(steps, from_step=None, log=...)` runs the enabled steps in order,
continues past failures, logs each step with its duration in one format to
config.RUN_LOG, and returns (ok, results) where results maps name -> bool.
"""
import os, subprocess, sys, time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DIV = "=" * 58


@dataclass
class Step:
    name: str
    kind: str                                   # "subprocess" | "call"
    script: Optional[str] = None                # subprocess: script path/name
    args: list = field(default_factory=list)    # subprocess: extra argv
    fn: Optional[Callable] = None               # call: function to invoke
    kwargs: dict = field(default_factory=dict)  # call: keyword args
    enabled: bool = True


def default_log(msg):
    """Append to config.RUN_LOG (under DATA_DIR, survives on the Fly volume) and echo."""
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(config.RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_one(step, log):
    """Run a single step; return True on success, False on failure."""
    log(f"▶  Starting {step.name} …")
    t0 = time.time()
    ok = False
    try:
        if step.kind == "subprocess":
            path = step.script
            if not os.path.isabs(path):
                path = os.path.join(SCRIPT_DIR, path)
            if not os.path.exists(path):
                log(f"❌ {step.name} not found at {path}")
                return False
            result = subprocess.run([sys.executable, path, *step.args], cwd=SCRIPT_DIR)
            ok = result.returncode == 0
            detail = "" if ok else f" (exit {result.returncode})"
        elif step.kind == "call":
            step.fn(**step.kwargs)
            ok, detail = True, ""
        else:
            raise ValueError(f"unknown step kind: {step.kind!r}")
    except Exception as e:
        ok, detail = False, f" ({e})"
    dur = round(time.time() - t0, 1)
    status = "OK" if ok else "FAILED"
    log(f"{'✅' if ok else '❌'} {step.name:<20} {status:<6} {dur}s{detail}")
    return ok


def run_steps(steps, from_step=None, log=default_log):
    """Run enabled steps in order. Returns (ok, results: {name: bool}).

    `from_step` skips every enabled step before the one named; an unknown name
    raises ValueError before anything runs.
    """
    active = [s for s in steps if s.enabled]

    if from_step is not None:
        names = [s.name for s in active]
        if from_step not in names:
            raise ValueError(f"unknown step: {from_step!r} (have {names})")
        active = active[names.index(from_step):]

    results = {}
    for step in active:
        log(DIV)
        ok = _run_one(step, log)
        results[step.name] = ok
        if not ok:
            log(f"⚠️  {step.name} failed — continuing")

    ok_all = all(results.values())
    return ok_all, results
