"""Every repository in the agent's session history, found in one pass over its logs.

`history.plan` picks one repository's logs out of the whole history, reading every log's head to
do it; learning from the history of every repository at once would read each head once per
repository. `discover` reads each head once and groups the logs by repository, so each group can
be planned with `history.plan(ctx, logs=group["logs"])` without listing or reading again.

A log's group is decided as `history.is_this_repository` decides it: its recorded directory,
where that still exists, keyed with `repo_identity` as a live session's is (two checkouts of one
remote are one repository). A log whose directory is gone joins the live repository whose root
the host files its sessions under EXACTLY that log's folder (`host.folder_matches`: never a
prefix, or `widget-shell` would be taken for `widget`); one matching no live repository is left
out and counted in `skipped["gone"]`. Subagent logs are never listed (`host.history`).

Order: the repository of `first_cwd` (the session that started the pass) first, then by number
of logs, most first, ties by key.

`run_all` imports every repository in that order, one at a time, each through `history.run` as
a session start in it would (`history.json` marked as `history.maybe_start` marks it: a
repository with a session already folded records `skipped`; one whose import is `done`,
`skipped` or `stopped` is not touched). `te history import --all` runs it by hand, and
`spawn` in a detached process (`te history import --all --auto`, its output in
`<state_root>/history-all.log`). The whole run has its own time budget (`budget_s`, checked
before each repository) and its own state, `<state_root>/history-all.json`:

    {"state": "running" | "partial" | "done" | "stopped", "queue": [keys], "cwds": {key: cwd},
     "next", "done": [keys], "skipped": {why: n}, "started", "heartbeat", "finished", "pid"}

A run stopped by the budget is `partial` and the next one resumes at `next`; a repository its
own budget left `partial` stays out of `done`, and once the queue is through, the next run
lists the history again and takes it up. A run whose heartbeat is older than
`history.HEARTBEAT_STALE_S` has died and is taken over; one alive makes another go away
(`busy`). An exception in one repository is a line in `errors.log`, counted in
`skipped["error"]`, and the run goes on.
"""
import os
import subprocess
import sys
import time

from . import history
from .config import load_config
from .context import LiveContext
from .paths import repo_identity, state_root
from .store import Store

STATE = "history-all.json"
LOG = "history-all.log"
LOCK = "history-all"
LOCK_S = 10.0
BEAT_S = 60.0                      # how often a long repository refreshes the run's heartbeat
FINAL = ("done", "skipped", "stopped")    # a repository's import that is not taken up again
_clock = time.monotonic            # the whole run's budget clock


def _identity(cwd, env, cache):
    """`repo_identity(cwd)`, once per directory; None where it cannot be told."""
    if cwd not in cache:
        try:
            cache[cwd] = repo_identity(cwd, env)
        except (OSError, ValueError):
            cache[cwd] = None
    return cache[cwd]


def discover(host, env, dirs, first_cwd=None):
    """Group every log in `host`'s history by repository.

    Returns {"repos": [{"key", "label", "cwd", "logs"}] in order, "skipped": {why: n}}; each log
    is `host.history`'s item plus its "head"."""
    skipped = {}
    if not getattr(host, "keeps_history", False):
        return {"repos": [], "skipped": skipped}
    groups, roots, orphans, cache = {}, {}, [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1
    for log in host.history(env, dirs):
        item = dict(log, head=host.log_head(log["path"]))
        cwd = item["head"].get("cwd")
        if not (cwd and os.path.isdir(cwd)):
            orphans.append(item)
            continue
        rid = _identity(cwd, env, cache)
        if rid is None:
            skip("unkeyed")
            continue
        group = groups.get(rid.key)
        if group is None:
            group = groups[rid.key] = {"key": rid.key, "label": rid.label, "cwd": cwd,
                                       "logs": []}
            roots[rid.key] = []
        if rid.root not in roots[rid.key]:
            roots[rid.key].append(rid.root)
        group["logs"].append(item)
    for item in orphans:
        key = next((k for k, rs in roots.items()
                    if any(host.folder_matches(r, item["folder"]) for r in rs)), None)
        if key is None:
            skip("gone")
        else:
            groups[key]["logs"].append(item)

    first = None
    if first_cwd and os.path.isdir(first_cwd):
        rid = _identity(first_cwd, env, cache)
        first = rid.key if rid else None
    repos = sorted(groups.values(),
                   key=lambda g: (g["key"] != first, -len(g["logs"]), g["key"]))
    return {"repos": repos, "skipped": skipped}


# ---- the run ---------------------------------------------------------------------------------
def _say(log, text):
    if log is not None:
        try:
            log(text)
        except Exception:
            pass


def _save(store, doc):
    with store.locked(LOCK, timeout=LOCK_S):
        store.write(STATE, doc)


def _take(store, auto, on):
    """Take the whole run's state for this process: the doc, or {"state": "busy" | "stopped"}
    when there is nothing to run."""
    with store.locked(LOCK, timeout=LOCK_S) as got:
        if not got:
            return {"state": "busy"}
        doc = store.read(STATE)
        doc = doc if isinstance(doc, dict) else {}
        now = time.time()
        if doc.get("state") == "running" and not history._stale(doc, now) \
                and not (auto and _claimed(doc)):
            return {"state": "busy"}
        if auto and not on:
            doc.update(state="stopped", reason="history_import is off", at=now)
            store.write(STATE, doc)
            return {"state": "stopped"}
        doc["prev"] = doc.get("state")
        doc.pop("claimed", None)
        doc.update(state="running", heartbeat=now, pid=os.getpid())
        store.write(STATE, {k: v for k, v in doc.items() if k != "prev"})
        return doc


def _claimed(doc):
    """A run `maybe_start` claimed for the process it starts, which no process has taken yet."""
    return doc.get("claimed") is True and "pid" not in doc


def _mark(ctx, auto, now):
    """Mark the repository's `history.json` as `history.maybe_start` does; returns None when
    its import is to run, else why not (a `skipped` key)."""
    if os.path.exists(ctx.store.path(history.STATE)) and history.read_state(ctx) is None:
        return "unreadable"
    out = {}

    def decide(doc):
        state = doc.get("state")
        off = auto and not ctx.config.history_import
        if not doc:
            if off:
                out["why"] = "off"
                return False
            if not history._nothing_folded(ctx.store):
                doc.update(state="skipped", at=now,
                           reason=f"{ctx.repo.label} already had folded sessions")
                out["why"] = "already_folded"
                return None
            doc.update(state="running", started=now, heartbeat=now, noticed=False)
        elif state == "partial" or (state == "running" and history._stale(doc, now)):
            if off:
                doc.update(state="stopped", reason="history_import is off", at=now)
                out["why"] = "off"
                return None
            doc["heartbeat"] = now
        else:
            out["why"] = "running" if state == "running" else str(state)
            return False
        return None
    if history._update(ctx, decide, timeout=history.HISTORY_LOCK_S) is None:
        return "running"
    return out.get("why")


def _unmark(ctx):
    """After an exception outside `history.run`'s own handling: not left `running`, or live
    folds would wait for it."""
    def stop(doc):
        if doc.get("state") not in ("running", "partial"):
            return False
        doc.update(state="stopped", reason="the import failed (see errors.log)", at=time.time())
        return None
    try:
        history._update(ctx, stop)
    except Exception:
        history._record_error(ctx.root)


def run_all(host, env, first_cwd=None, auto=False, budget_s=history.BUDGET_S * 4, log=None):
    """Import every repository in `host`'s history, in `discover`'s order, resuming the queue
    where the last run left it. `auto`: started by itself, which `history_import=off` stops.
    Returns {"state", "done", "skipped", "left"}: state "busy" when another run is alive,
    "refused" when the host keeps no history."""
    if not getattr(host, "keeps_history", False):
        return {"state": "refused", "reason": f"{host.name} keeps no session history "
                                              f"the plugin reads"}
    env = dict(os.environ if env is None else env)
    root = state_root(env)
    store = Store(root)
    cfg = load_config(root, env=env)
    doc = _take(store, auto, cfg.history_import)
    if doc.get("state") != "running":
        return doc
    try:
        return _run_taken(host, env, root, store, cfg, doc, first_cwd, auto, budget_s, log)
    except Exception:
        history._record_error(root)
        doc = store.read(STATE) or {}
        doc.update(state="stopped", reason="the run failed (see errors.log)", at=time.time())
        _save(store, doc)
        return {"state": "error", "reason": doc["reason"]}


def _run_taken(host, env, root, store, cfg, doc, first_cwd, auto, budget_s, log):
    prev = doc.pop("prev", None)
    queue = doc.get("queue")
    queue = [k for k in queue if isinstance(k, str)] if isinstance(queue, list) else []
    i = int(history._num(doc.get("next")))
    groups = {}
    if not queue or prev in ("done", "stopped") or i >= len(queue):
        found = discover(host, env, cfg.history_dirs, first_cwd)
        groups = {g["key"]: g for g in found["repos"]}
        queue = [g["key"] for g in found["repos"]]
        cwds = dict(doc.get("cwds") if isinstance(doc.get("cwds"), dict) else {})
        cwds.update({g["key"]: g["cwd"] for g in found["repos"]})
        doc.update(queue=queue, cwds=cwds, next=0, skipped=dict(found["skipped"]),
                   started=time.time())
        doc.pop("finished", None)
        i = 0
    doc["done"] = done = [k for k in (doc.get("done") or []) if isinstance(k, str)]
    skipped = doc["skipped"] = dict(doc["skipped"] if isinstance(doc.get("skipped"), dict)
                                    else {})
    cwds = doc.get("cwds") if isinstance(doc.get("cwds"), dict) else {}
    doc["cwds"] = cwds
    doc["heartbeat"] = time.time()
    _save(store, doc)

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1
    last = [time.time()]

    def beat(text):
        """`history.run`'s lines, and the heartbeat kept fresh while one repository runs."""
        _say(log, text)
        if time.time() - last[0] >= BEAT_S:
            last[0] = doc["heartbeat"] = time.time()
            _save(store, doc)
    deadline = _clock() + max(0.0, float(budget_s))
    left = False
    while i < len(queue):
        if _clock() >= deadline:
            break
        key = queue[i]
        if key not in done:
            left = _one(host, env, root, key, cwds.get(key), groups.get(key), auto, beat,
                        done, skip, log) or left
        i += 1
        doc.update(next=i, heartbeat=time.time())
        _save(store, doc)
    if i < len(queue):
        doc["state"] = "partial"            # the next run resumes at `next`
    else:
        doc.update(state="partial" if left else "done", finished=time.time())
    _save(store, doc)
    _say(log, f"{doc['state']}: {len(done)} of {len(queue)} repositories read"
         + ("; left out: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
            if skipped else ""))
    return {"state": doc["state"], "done": list(done), "skipped": dict(skipped),
            "left": len(queue) - i}


def _one(host, env, root, key, cwd, group, auto, beat, done, skip, log):
    """Import one repository of the queue; True when it is to be taken up again (left
    `partial`, or its import busy elsewhere)."""
    if not (isinstance(cwd, str) and os.path.isdir(cwd)):
        skip("gone")
        return False
    ctx = None
    try:
        ctx = LiveContext.build(host, cwd, env)
        state = (history.read_state(ctx) or {}).get("state")
        if state in FINAL:
            if state == "done":
                done.append(key)
            else:
                skip(state)
            return False
        why = _mark(ctx, auto, time.time())
        if why == "running":
            _say(log, f"{ctx.repo.label}: its import is running elsewhere; taken up next time")
            return True
        if why:
            skip(why)
            _say(log, f"{ctx.repo.label}: not imported ({why})")
            return False
        res = history.run(ctx, auto=auto, log=beat,
                          logs=group["logs"] if group else None)
    except Exception:
        history._record_error(root)
        skip("error")
        if ctx is not None:
            _unmark(ctx)
        return False
    label, state = ctx.repo.label, res.get("state")
    if state == "done":
        done.append(key)
        _say(log, f"{label}: imported {res.get('imported', 0)} earlier session(s)")
        return False
    if state in ("partial", "busy"):
        _say(log, f"{label}: imported {res.get('imported', 0)} so far"
             + (f", {res['left']} to go" if res.get("left") else f" ({res.get('reason')})")
             + "; taken up next time")
        return True
    skip(str(state))
    _say(log, f"{label}: not imported ({res.get('reason') or state})")
    return False


# ---- `te history import --all` and the detached run ------------------------------------------
def dry_run(host, env, first_cwd=None, out=print):
    """What `run_all` would read, per repository; reads, never writes."""
    env = dict(os.environ if env is None else env)
    cfg = load_config(state_root(env), env=env)
    found = discover(host, env, cfg.history_dirs, first_cwd)
    for g in found["repos"]:
        ctx = LiveContext.build(host, g["cwd"], env, settle=False)
        state = (history.read_state(ctx) or {}).get("state")
        if state in FINAL:
            out(f"{g['label']}: import already {state}, in {g['cwd']}")
            continue
        n = len(history.plan(ctx, logs=g["logs"])["sessions"])
        out(f"{g['label']}: {n} earlier session(s) to read, in {g['cwd']}")
    if found["skipped"]:
        out("left out: " + ", ".join(f"{k} {v}" for k, v in sorted(found["skipped"].items())))
    return found


def cli(args, env=None):
    """`te history import --all [--dry-run]`; the exit code."""
    from . import hosts
    host = hosts.HOSTS[args.host]()
    env = dict(os.environ if env is None else env)
    if not host.keeps_history:
        print(f"{host.name}: no session history the plugin reads")
        return 1
    if args.dry_run:
        dry_run(host, env, args.cwd)
        return 0
    say = (lambda line: print(line, flush=True))
    if args.auto:
        try:
            run_all(host, env, first_cwd=args.cwd, auto=True, log=say)
        except Exception:
            history._record_error(state_root(env))
        return 0
    res = run_all(host, env, first_cwd=args.cwd, log=say)
    if res["state"] in ("busy", "refused", "error"):
        print(f"not imported: {res.get('reason') or 'another run is reading the history'}")
        return 1
    return 0


def spawn(host, env, first_cwd):
    """Run `te history import --all --auto` in a detached process, as `LiveContext.spawn` runs
    a repository's jobs, its output appended to `<state_root>/history-all.log`."""
    env = dict(os.environ if env is None else env)
    core = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    root = state_root(env)
    child = dict(env)
    child["PYTHONPATH"] = core + (os.pathsep + child["PYTHONPATH"] if child.get("PYTHONPATH")
                                  else "")
    child["PYTHONUTF8"] = "1"
    cmd = [sys.executable, "-m", "tokeneconomizer.plugin.cli", "history", "import", "--all",
           "--auto", "--host", host.name, "--cwd", first_cwd]
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, LOG)
    if os.name == "nt":
        from . import winspawn
        return winspawn.spawn(cmd, first_cwd, child, path)
    out = open(path, "a", encoding="utf-8")
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         cwd=first_cwd, env=child, start_new_session=True)
    finally:
        out.close()
    return True


# ---- the first session start after installing -----------------------------------------------
def _alive(doc, now):
    return doc.get("state") in ("running", "partial") and not history._stale(doc, now)


def maybe_start(ctx, now=None):
    """At a session start of Claude Code: start the whole run when there is none yet, resume one
    left `partial`, or take over one whose process died -- from the session's repository, in a
    detached `te history import --all --auto` (inline when TE_FOLD_SYNC=1, for tests).

    The run is claimed here (`running`, a fresh heartbeat, `claimed` and no `pid`) so that
    another session start meanwhile starts no second one; the process started takes the claim
    (`_take`), and one that never does leaves it to go stale and be taken over."""
    if ctx.host.name != "claude" or not ctx.config.history_import:
        return None
    now = time.time() if now is None else now
    store = Store(ctx.root)
    with store.locked(LOCK, timeout=history.HISTORY_LOCK_S) as got:
        if not got:
            return None
        doc = store.read(STATE)
        doc = doc if isinstance(doc, dict) else {}      # as `_take` reads it
        state = doc.get("state")
        if state is not None and not (state == "partial"
                                      or (state == "running" and history._stale(doc, now))):
            return None
        doc.pop("pid", None)
        doc.update(state="running", heartbeat=now, claimed=True)
        store.write(STATE, doc)
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return run_all(ctx.host, ctx.env, first_cwd=ctx.repo.root, auto=True)
    return spawn(ctx.host, ctx.env, ctx.repo.root)


def covers(ctx, now=None):
    """Does a live whole run stand in for this repository's own import: its queue from `next`
    lists it, or it was just claimed and has no queue yet (it lists every repository in the
    history, the session's first)."""
    if ctx.host.name != "claude":
        return False
    doc = Store(ctx.root).read(STATE)
    if not isinstance(doc, dict) or not _alive(doc, time.time() if now is None else now):
        return False
    queue = doc.get("queue")
    if not isinstance(queue, list) or not queue:
        return doc.get("state") == "running"
    return ctx.repo.key in queue[int(history._num(doc.get("next"))):]

