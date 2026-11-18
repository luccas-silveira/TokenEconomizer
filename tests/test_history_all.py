"""Finding every repository in Claude Code's history in one pass (`history_all.discover`), and
planning one repository's import from the logs that pass found (`history.plan(..., logs=)`).

Logs are written where Claude Code keeps them (`history_log`); each names the directory the
session ran in. A repository is a git checkout with an `origin`, keyed as a live session's is.
"""
import collections
import json
import os
import re
import subprocess
import time

import pytest

from test_history_import import (claude_home, ctx_for, env, history_log, ledger_ids,  # noqa: F401
                                 live_session, looked, trap)
from test_live_hooks import store_for, trap_session
from tokeneconomizer import trace
from tokeneconomizer.plugin import cli, fold, history, history_all, hosts
from tokeneconomizer.plugin.paths import repo_identity, state_root


def checkout(path, remote):
    """A git checkout at `path` whose `origin` is `remote`."""
    os.makedirs(os.path.join(path, ".git", "info"), exist_ok=True)
    with open(os.path.join(path, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    with open(os.path.join(path, ".git", "config"), "w") as fh:
        fh.write(f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {remote}\n')
    return str(path)


def sessions(claude_home, cwd, n, prefix, **kw):
    """`n` finished sessions run in `cwd`; their log paths."""
    return {history_log(claude_home, cwd, f"{prefix}{i}", looked(cwd), **kw) for i in range(n)}


def by_key(found):
    return {g["key"]: g for g in found["repos"]}


def paths(group):
    return {item["path"] for item in group["logs"]}


def found_paths(found):
    return [item["path"] for g in found["repos"] for item in g["logs"]]


# ---- grouping ---------------------------------------------------------------------------------
def test_three_repositories_in_the_history_are_three_groups_each_with_its_own_logs(
        env, claude_home, tmp_path):
    a = checkout(tmp_path / "a", "https://github.com/acme/alpha.git")
    b = checkout(tmp_path / "b", "https://github.com/acme/beta.git")
    c = checkout(tmp_path / "c", "https://github.com/acme/gamma.git")
    logs = {a: sessions(claude_home, a, 5, "a"), b: sessions(claude_home, b, 2, "b"),
            c: sessions(claude_home, c, 3, "c")}

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    groups = by_key(found)
    assert len(found["repos"]) == 3
    for root, want in logs.items():
        rid = repo_identity(root, env)
        g = groups[rid.key]
        assert paths(g) == want
        assert g["label"] == rid.label and g["cwd"] == root
        for item in g["logs"]:
            # each item is `host.history`'s, plus the head the pass read
            assert {"path", "folder", "session_id", "mtime", "size", "head"} <= set(item)
            assert item["folder"] == trace.claude_project_folder(root)
            assert item["session_id"] == os.path.basename(item["path"])[:-len(".jsonl")]
            assert item["head"]["cwd"] == root
    assert not found["skipped"].get("gone")


def test_two_checkouts_of_one_repository_are_one_group(env, claude_home, tmp_path):
    remote = "https://github.com/acme/widget.git"
    one = checkout(tmp_path / "widget", remote)
    two = checkout(tmp_path / "widget-copy", remote)
    want = sessions(claude_home, one, 2, "one") | sessions(claude_home, two, 3, "two")

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert len(found["repos"]) == 1
    g = found["repos"][0]
    assert g["key"] == repo_identity(one, env).key == repo_identity(two, env).key
    assert paths(g) == want and len(g["logs"]) == 5
    assert g["cwd"] in (one, two)


# ---- logs whose directory is gone -------------------------------------------------------------
def test_a_gone_directorys_log_joins_the_live_repository_whose_folder_is_exactly_its_own(
        env, claude_home, tmp_path):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    live = sessions(claude_home, widget, 2, "w")
    # Filed under widget's own folder, naming a directory that no longer exists.
    moved = history_log(claude_home, str(tmp_path / "gone" / "widget"), "moved", looked(widget),
                        folder=trace.claude_project_folder(widget))
    # Filed under `widget-shell`, which is gone: its folder starts with widget's, but is not it.
    shell = history_log(claude_home, str(tmp_path / "widget-shell"), "shell", looked(widget))
    assert trace.claude_project_folder(str(tmp_path / "widget-shell")).startswith(
        trace.claude_project_folder(widget))

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert len(found["repos"]) == 1
    g = found["repos"][0]
    assert g["key"] == repo_identity(widget, env).key
    assert paths(g) == live | {moved}
    assert shell not in found_paths(found)
    assert found["skipped"]["gone"] == 1


def test_a_gone_directorys_log_does_not_join_a_repository_whose_folder_only_starts_like_it(
        env, claude_home, tmp_path):
    shell = checkout(tmp_path / "widget-shell", "https://github.com/acme/widget-shell.git")
    live = sessions(claude_home, shell, 2, "s")
    # `widget` is gone; its folder is a prefix of the live `widget-shell`'s.
    old = history_log(claude_home, str(tmp_path / "widget"), "old", looked(str(tmp_path / "w")))

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert [paths(g) for g in found["repos"]] == [live]
    assert old not in found_paths(found)
    assert found["skipped"]["gone"] == 1


def test_a_gone_directorys_log_matches_the_repositorys_root_folder_not_a_subdirectorys(
        env, claude_home, tmp_path):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    sub = os.path.join(widget, "pkg")
    os.makedirs(sub)
    live = sessions(claude_home, sub, 1, "w")             # the live log ran in a subdirectory
    moved = history_log(claude_home, str(tmp_path / "gone"), "moved", looked(widget),
                        folder=trace.claude_project_folder(widget))

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert len(found["repos"]) == 1
    assert paths(found["repos"][0]) == live | {moved}
    assert not found["skipped"].get("gone")


def test_a_log_of_a_gone_directory_matching_no_live_repository_is_left_out_and_counted(
        env, claude_home, tmp_path):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    live = sessions(claude_home, widget, 2, "w")
    gone = sessions(claude_home, str(tmp_path / "deleted"), 3, "d")
    gone.add(history_log(claude_home, str(tmp_path / "elsewhere" / "x"), "x",
                         looked(widget)))

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert [paths(g) for g in found["repos"]] == [live]
    assert not gone & set(found_paths(found))
    assert found["skipped"]["gone"] == 4


def test_subagent_logs_are_not_sessions_of_any_repository(env, claude_home, tmp_path):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    helper = checkout(tmp_path / "helper", "https://github.com/acme/helper.git")
    live = sessions(claude_home, widget, 1, "w")
    folder = trace.claude_project_folder(widget)
    # Both layouts Claude Code has used, each naming a live repository that has no session.
    history_log(claude_home, helper, "agent-x", looked(helper),
                folder=os.path.join(folder, "w0", "subagents"))
    history_log(claude_home, helper, "agent-old", looked(helper), folder=folder)

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert [g["key"] for g in found["repos"]] == [repo_identity(widget, env).key]
    assert paths(found["repos"][0]) == live
    assert not any("agent-" in os.path.basename(p) for p in found_paths(found))
    assert not found["skipped"].get("gone")


def test_a_host_that_keeps_no_history_finds_no_repositories(env, claude_home, tmp_path):
    class NoHistory(hosts.ClaudeCode):
        keeps_history = False

    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    sessions(claude_home, widget, 2, "w")
    assert history_all.discover(NoHistory(), env, [])["repos"] == []


# ---- order ------------------------------------------------------------------------------------
def test_repositories_come_first_cwds_first_then_by_number_of_logs_then_by_key(
        env, claude_home, tmp_path):
    # Folder (listing) order and key order both disagree with the order asked for.
    zeta = checkout(tmp_path / "m_zeta", "https://github.com/acme/zeta.git")      # 5 logs
    alpha = checkout(tmp_path / "n_alpha", "https://github.com/acme/alpha.git")   # 2 logs
    gamma = checkout(tmp_path / "a_gamma", "https://github.com/acme/gamma.git")   # 3 logs
    beta = checkout(tmp_path / "z_beta", "https://github.com/acme/beta.git")      # 3 logs
    for root, n in ((zeta, 5), (alpha, 2), (gamma, 3), (beta, 3)):
        sessions(claude_home, root, n, os.path.basename(root))
    key = {r: repo_identity(r, env).key for r in (zeta, alpha, gamma, beta)}
    assert key[beta] < key[gamma]

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    assert [g["key"] for g in found["repos"]] == [key[zeta], key[beta], key[gamma], key[alpha]]

    inside = os.path.join(alpha, "src")                   # a session started in a subdirectory
    os.makedirs(inside)
    found = history_all.discover(hosts.ClaudeCode(), env, [], first_cwd=inside)
    assert [g["key"] for g in found["repos"]] == [key[alpha], key[zeta], key[beta], key[gamma]]

    found = history_all.discover(hosts.ClaudeCode(), env, [], first_cwd=gamma)
    assert [g["key"] for g in found["repos"]] == [key[gamma], key[zeta], key[beta], key[alpha]]


# ---- cost -------------------------------------------------------------------------------------
def test_each_logs_head_is_read_once_in_the_whole_pass(env, claude_home, tmp_path, monkeypatch):
    remote = "https://github.com/acme/widget.git"
    widget = checkout(tmp_path / "widget", remote)
    copy = checkout(tmp_path / "widget-copy", remote)
    other = checkout(tmp_path / "other", "https://github.com/acme/other.git")
    sessions(claude_home, widget, 3, "w")
    sessions(claude_home, copy, 2, "c")
    sessions(claude_home, other, 2, "o")
    history_log(claude_home, str(tmp_path / "gone"), "moved", looked(widget),
                folder=trace.claude_project_folder(widget))          # joins widget
    history_log(claude_home, str(tmp_path / "deleted"), "lost", looked(widget))   # gone

    host = hosts.ClaudeCode()
    listed = [log["path"] for log in host.history(env, [])]
    reads = collections.Counter()
    real = trace.claude_log_head

    def counting(path):
        reads[path] += 1
        return real(path)
    monkeypatch.setattr(trace, "claude_log_head", counting)
    monkeypatch.setattr(host, "log_head", counting)

    found = history_all.discover(host, env, [], first_cwd=other)
    assert len(listed) == 9
    assert sorted(reads) == sorted(listed)
    assert set(reads.values()) == {1}
    assert len(found_paths(found)) == 8 and found["skipped"]["gone"] == 1


# ---- planning one repository from the pass ----------------------------------------------------
def test_plan_from_the_passs_logs_is_the_plan_from_the_history_without_listing_it(
        env, claude_home, tmp_path, monkeypatch):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    other = checkout(tmp_path / "other", "https://github.com/acme/other.git")
    for i in range(3):
        history_log(claude_home, widget, f"old{i}", trap(widget, i), age_s=3600 * (10 - i))
    history_log(claude_home, widget, "running", trap(widget, 9), age_s=5)          # recent
    history_log(claude_home, widget, "scripted", trap(widget, 8), entrypoint="sdk-cli")
    history_log(claude_home, widget, "empty", [])                                   # no prompt
    history_log(claude_home, str(tmp_path / "gone"), "moved", trap(widget, 7),
                age_s=3600 * 12, folder=trace.claude_project_folder(widget))
    sessions(claude_home, other, 2, "o")

    now = time.time()
    found = history_all.discover(hosts.ClaudeCode(), env, [])
    group = by_key(found)[repo_identity(widget, env).key]
    ctx = ctx_for(env, widget)
    want = history.plan(ctx, now=now)
    assert [s["id"] for s in want["sessions"]] == ["moved", "old0", "old1", "old2"]

    def listed(*a, **k):
        raise AssertionError("host.history called with the logs given")

    def head(*a, **k):
        raise AssertionError("log_head read again with the heads given")
    monkeypatch.setattr(ctx.host, "history", listed)
    monkeypatch.setattr(ctx.host, "log_head", head)
    assert history.plan(ctx, now=now, logs=group["logs"]) == want


def test_plan_from_the_passs_logs_still_leaves_out_another_repositorys(
        env, claude_home, tmp_path, monkeypatch):
    widget = checkout(tmp_path / "widget", "https://github.com/acme/widget.git")
    other = checkout(tmp_path / "other", "https://github.com/acme/other.git")
    for i in range(2):
        history_log(claude_home, widget, f"w{i}", trap(widget, i), age_s=3600 * (10 - i))
        history_log(claude_home, other, f"o{i}", trap(other, i), age_s=3600 * (10 - i))

    found = history_all.discover(hosts.ClaudeCode(), env, [])
    every = [item for g in found["repos"] for item in g["logs"]]
    ctx = ctx_for(env, widget)
    monkeypatch.setattr(ctx.host, "history", lambda *a, **k: pytest.fail("listed"))
    got = history.plan(ctx, logs=every)
    assert [s["id"] for s in got["sessions"]] == ["w0", "w1"]


# ---- importing every repository: `run_all` and `te history import --all` -----------------------
def all_state(env):
    """`<state_root>/history-all.json`, the whole run's state; None when there is none."""
    path = os.path.join(state_root(env), "history-all.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def write_all_state(env, doc):
    os.makedirs(state_root(env), exist_ok=True)
    with open(os.path.join(state_root(env), "history-all.json"), "w") as fh:
        json.dump(doc, fh)


def with_history(claude_home, tmp_path, name, n):
    """A checkout of acme/<name> with `n` earlier sessions, `<name>0` the oldest."""
    root = checkout(tmp_path / name, f"https://github.com/acme/{name}.git")
    for i in range(n):
        history_log(claude_home, root, f"{name}{i}", trap(root, i), age_s=3600 * (10 - i))
    return root


def key(env, root):
    return repo_identity(root, env).key


def imported(env, root):
    """The sessions folded into `root`'s ledger, in order."""
    return ledger_ids(store_for(env, root))


def repo_state(env, root):
    return store_for(env, root).read("history.json")


def watch_runs(monkeypatch, fail=None, after=None):
    """The repositories `history.run` is called for, in order. For the key `fail` it raises;
    `after(ctx)` runs once each call returns (time passing while a repository is read)."""
    calls, real = [], history.run

    def run(ctx, *a, **kw):
        calls.append(ctx.repo.key)
        if ctx.repo.key == fail:
            raise RuntimeError("a log this import cannot read")
        try:
            return real(ctx, *a, **kw)
        finally:
            if after:
                after(ctx)
    monkeypatch.setattr(history, "run", run)
    return calls


def test_run_all_imports_every_repository_in_the_order_of_its_queue(env, claude_home, tmp_path,
                                                                     monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 4)
    b = with_history(claude_home, tmp_path, "beta", 3)
    c = with_history(claude_home, tmp_path, "gamma", 2)
    order = [key(env, r) for r in (c, a, b)]     # the first session's repository, then by logs
    calls = watch_runs(monkeypatch)

    res = history_all.run_all(hosts.ClaudeCode(), env, first_cwd=c)
    assert res["state"] == "done"
    assert calls == order
    for root, n in ((a, 4), (b, 3), (c, 2)):
        name = os.path.basename(root)
        assert imported(env, root) == [f"{name}{i}" for i in range(n)]
        assert repo_state(env, root)["state"] == "done"
    doc = all_state(env)
    assert doc["state"] == "done" and doc["queue"] == order and doc["done"] == order
    assert doc["cwds"] == {key(env, r): r for r in (a, b, c)}


def test_a_repository_with_a_session_learned_live_is_skipped_and_the_run_goes_on(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)             # first in the queue
    b = with_history(claude_home, tmp_path, "beta", 2)
    ctx = ctx_for(env, a)
    live_session(ctx, "live0", trap_session(str(tmp_path / "live0.jsonl"), a, 5))
    assert fold.fold_session(ctx, "live0") is not None

    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert res["state"] == "done"
    doc = repo_state(env, a)
    assert doc["state"] == "skipped" and "already" in doc["reason"]
    assert imported(env, a) == ["live0"]                    # its earlier sessions not read
    assert imported(env, b) == ["beta0", "beta1"] and repo_state(env, b)["state"] == "done"
    assert all_state(env)["state"] == "done" and key(env, b) in all_state(env)["done"]


def test_a_repository_whose_import_is_done_skipped_or_stopped_is_not_touched(
        env, claude_home, tmp_path, monkeypatch):
    done = with_history(claude_home, tmp_path, "alpha", 2)
    skipped = with_history(claude_home, tmp_path, "beta", 2)
    stopped = with_history(claude_home, tmp_path, "gamma", 2)
    fresh = with_history(claude_home, tmp_path, "delta", 1)
    # A plan stopped by `history_import=off`: a manual run would resume it, a run by itself not.
    plan = history.plan(ctx_for(env, stopped))["sessions"]
    assert len(plan) == 2
    before = {
        done: {"state": "done", "imported": 7, "finished": 1.0, "noticed": True},
        skipped: {"state": "skipped", "reason": "acme/beta already had folded sessions",
                  "at": 1.0},
        stopped: {"state": "stopped", "reason": "history_import is off", "at": 1.0,
                  "plan": plan, "next": 0, "imported": 0, "skipped": {}},
    }
    for root, doc in before.items():
        store_for(env, root).write("history.json", doc)
    calls = watch_runs(monkeypatch)

    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert res["state"] == "done"
    for root, doc in before.items():
        assert repo_state(env, root) == doc, os.path.basename(root)
        assert imported(env, root) == [], os.path.basename(root)
    assert calls == [key(env, fresh)]
    assert imported(env, fresh) == ["delta0"]


def test_an_error_in_one_repository_is_logged_counted_and_the_others_are_imported(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)             # first: the one that fails
    b = with_history(claude_home, tmp_path, "beta", 2)
    c = with_history(claude_home, tmp_path, "gamma", 1)
    calls = watch_runs(monkeypatch, fail=key(env, a))

    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert calls == [key(env, r) for r in (a, b, c)]
    assert imported(env, a) == []
    assert imported(env, b) == ["beta0", "beta1"] and imported(env, c) == ["gamma0"]
    doc = all_state(env)
    assert doc["skipped"]["error"] == 1
    assert res["state"] == doc["state"] == "done"
    assert {key(env, b), key(env, c)} <= set(doc["done"])
    log = open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log")).read()
    assert "a log this import cannot read" in log


def test_a_run_stopped_by_the_whole_budget_resumes_at_the_next_repository(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)
    c = with_history(claude_home, tmp_path, "gamma", 1)
    order = [key(env, r) for r in (a, b, c)]
    now = [0.0]
    monkeypatch.setattr(history, "_clock", lambda: now[0])
    monkeypatch.setattr(history_all, "_clock", lambda: now[0], raising=False)

    def ten_minutes(ctx):
        now[0] += 600
    calls = watch_runs(monkeypatch, after=ten_minutes)

    # 15 minutes: alpha (0-10) and beta (10-20) start inside it, gamma would start after it.
    res = history_all.run_all(hosts.ClaudeCode(), env, budget_s=900)
    assert res["state"] == "partial"
    assert calls == order[:2]
    doc = all_state(env)
    assert doc["state"] == "partial" and doc["queue"] == order
    assert doc["next"] == 2 and doc["queue"][doc["next"]] == key(env, c)
    assert doc["done"] == order[:2]
    assert repo_state(env, c) is None and imported(env, c) == []    # not marked, not blocking
    first = {r: imported(env, r) for r in (a, b)}
    assert first == {a: ["alpha0", "alpha1", "alpha2"], b: ["beta0", "beta1"]}

    del calls[:]
    res = history_all.run_all(hosts.ClaudeCode(), env, budget_s=900)
    assert res["state"] == "done"
    assert calls == [key(env, c)]
    assert {r: imported(env, r) for r in (a, b)} == first
    assert imported(env, c) == ["gamma0"] and repo_state(env, c)["state"] == "done"
    doc = all_state(env)
    assert doc["state"] == "done" and doc["done"] == order


def test_a_repository_left_partial_by_its_own_budget_is_resumed_by_the_next_run(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)
    ka, kb = key(env, a), key(env, b)
    monkeypatch.setattr(history, "_clock", lambda: 0.0)          # the whole run's budget holds
    monkeypatch.setattr(history_all, "_clock", lambda: 0.0, raising=False)
    calls, real = [], history.run
    cut = [True]

    def run(ctx, *args, **kw):
        calls.append(ctx.repo.key)
        if ctx.repo.key != ka or not cut:
            return real(ctx, *args, **kw)
        cut.clear()
        # alpha's own budget runs out after its first session.
        ticks = iter([0.0, 1.0] + [1e12] * 100)
        saved = history._clock
        history._clock = lambda: next(ticks)
        try:
            return real(ctx, *args, **kw)
        finally:
            history._clock = saved
    monkeypatch.setattr(history, "run", run)

    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert calls == [ka, kb]
    assert repo_state(env, a)["state"] == "partial" and imported(env, a) == ["alpha0"]
    assert repo_state(env, b)["state"] == "done" and imported(env, b) == ["beta0", "beta1"]
    doc = all_state(env)
    assert res["state"] == doc["state"] == "partial"
    assert ka not in doc["done"] and kb in doc["done"]

    del calls[:]
    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert calls == [ka]
    assert repo_state(env, a)["state"] == "done"
    assert imported(env, a) == ["alpha0", "alpha1", "alpha2"]
    assert imported(env, b) == ["beta0", "beta1"]
    doc = all_state(env)
    assert res["state"] == doc["state"] == "done"
    assert ka in doc["done"] and kb in doc["done"]


def test_a_running_whole_run_is_left_alone_while_alive_and_taken_over_once_its_heartbeat_is_old(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 1)
    order = [key(env, a), key(env, b)]
    alive = {"state": "running", "queue": order, "cwds": {order[0]: a, order[1]: b}, "next": 0,
             "done": [], "skipped": {}, "started": time.time() - 60,
             "heartbeat": time.time() - 5, "pid": os.getpid()}
    write_all_state(env, alive)
    calls = watch_runs(monkeypatch)

    assert history_all.run_all(hosts.ClaudeCode(), env)["state"] == "busy"
    assert all_state(env) == alive and calls == []
    assert repo_state(env, a) is None and repo_state(env, b) is None

    dead = dict(alive, heartbeat=time.time() - history.HEARTBEAT_STALE_S - 1, pid=4_000_000)
    write_all_state(env, dead)
    res = history_all.run_all(hosts.ClaudeCode(), env)
    assert res["state"] == "done" and calls == order
    assert imported(env, a) == ["alpha0", "alpha1"] and imported(env, b) == ["beta0"]
    doc = all_state(env)
    assert doc["state"] == "done" and doc["done"] == order
    assert time.time() - doc["heartbeat"] < 60


def test_with_history_import_off_a_run_by_itself_does_nothing_and_records_stopped(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    off = dict(env, TE_HISTORY_IMPORT="off")
    calls = watch_runs(monkeypatch)

    res = history_all.run_all(hosts.ClaudeCode(), off, auto=True)
    assert res["state"] == "stopped"
    assert all_state(off)["state"] == "stopped"
    assert calls == []
    assert repo_state(env, a) is None and imported(env, a) == []


def _line(out, root, label, *words):
    """The one line of `out` naming `label` (and `words`), with the label and `root` cut out."""
    lines = [ln for ln in out.splitlines() if label in ln and all(w in ln for w in words)]
    assert len(lines) == 1, out
    return lines[0], lines[0].replace(root, "").replace(label, "")


def test_te_history_import_all_dry_run_lists_each_repository_and_writes_nothing(
        env, claude_home, tmp_path, monkeypatch, capsys):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)
    history_log(claude_home, str(tmp_path / "deleted"), "lost", trap(a, 9))   # gone, unmatched
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    assert cli.main(["--cwd", a, "--host", "claude", "history", "import", "--all",
                     "--dry-run"]) == 0
    out = capsys.readouterr().out
    for root, n in ((a, 3), (b, 2)):
        line, rest = _line(out, root, repo_identity(root, env).label)
        assert root in line                                     # the repository's directory
        assert re.search(rf"\b{n}\b", rest), line               # its sessions to read
    assert "left out: gone 1" in out
    home = env["TOKENECONOMIZER_HOME"]
    assert [os.path.join(d, f) for d, _, fs in os.walk(home) for f in fs] == []
    assert all_state(env) is None


def test_te_history_import_all_says_how_many_each_repository_imported(
        env, claude_home, tmp_path, monkeypatch, capsys):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    assert cli.main(["--cwd", a, "--host", "claude", "history", "import", "--all"]) == 0
    out = capsys.readouterr().out
    for root, n in ((a, 3), (b, 2)):
        line, rest = _line(out, root, repo_identity(root, env).label, "imported")
        assert re.search(rf"\b{n}\b", rest), line
    assert imported(env, a) == ["alpha0", "alpha1", "alpha2"]
    assert imported(env, b) == ["beta0", "beta1"]
    assert all_state(env)["state"] == "done"


@pytest.mark.skipif(os.name == "nt", reason="the detached process goes through winspawn there")
def test_the_whole_runs_detached_process_logs_to_history_all_log_in_the_state_root(
        env, claude_home, tmp_path, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 1)
    started = []

    class Popen:
        def __init__(self, cmd, **kw):
            started.append((list(cmd), kw, getattr(kw.get("stdout"), "name", None),
                            getattr(kw.get("stderr"), "name", None)))
    monkeypatch.setattr(subprocess, "Popen", Popen)

    history_all.spawn(hosts.ClaudeCode(), env, a)
    assert len(started) == 1
    cmd, kw, out, err = started[0]
    i = cmd.index("history")
    assert cmd[i:i + 4] == ["history", "import", "--all", "--auto"]
    log = os.path.join(state_root(env), "history-all.log")
    assert out == err == log and os.path.exists(log)
    assert kw.get("cwd") == a
    assert not os.path.exists(store_for(env, a).path("history.log"))


# ---- the first session start after installing: `history_all.maybe_start` ----------------------
# A session start of Claude Code starts the whole run when there is none yet, resumes one left
# `partial`, and takes over one whose heartbeat is old: inline under `TE_FOLD_SYNC=1`, else in a
# detached `te history import --all --auto`. The detached processes are recorded, not started:
# every `te ...` the hook would run is `subprocess.Popen` of `tokeneconomizer.plugin.cli`.
from test_droid import TEST_CALLS, _write_session  # noqa: E402
from test_history_import import CLAIM, detached, start_hook  # noqa: E402
from test_live_hooks import call  # noqa: E402
from tokeneconomizer.plugin.context import LiveContext  # noqa: E402

nt_spawn = pytest.mark.skipif(os.name == "nt", reason="detached processes go through winspawn")


@pytest.fixture
def started(monkeypatch):
    """The argv after `-m tokeneconomizer.plugin.cli` of every `te ...` process started; any
    other process (git) runs."""
    procs, real = [], subprocess.Popen

    class Recorded:
        pid = 4_000_001

    def popen(cmd, *a, **kw):
        cmd = list(cmd) if isinstance(cmd, (list, tuple)) else cmd
        if isinstance(cmd, list) and "tokeneconomizer.plugin.cli" in cmd:
            procs.append(cmd[cmd.index("tokeneconomizer.plugin.cli") + 1:])
            return Recorded()
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return procs


def opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def whole_runs(procs):
    """The `te history import --all --auto` processes started."""
    return [a for a in procs if a[:4] == ["history", "import", "--all", "--auto"]]


def repo_imports(procs):
    """The one-repository `te history import --auto` processes started."""
    return [a for a in procs if a[:2] == ["history", "import"] and "--all" not in a]


def history_procs(procs):
    return [a for a in procs if a[:1] == ["history"]]


def run_detached(argv, env, monkeypatch):
    """Run a recorded `te` process here, as the detached process would, in its environment."""
    for k, v in detached(env).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TE_FOLD_SYNC", raising=False)
    return cli.main(list(argv))


def fresh(doc):
    return time.time() - doc["heartbeat"] < 60


def test_the_first_session_start_imports_every_repository_inline_starting_with_its_own(
        env, claude_home, tmp_path):
    a = with_history(claude_home, tmp_path, "alpha", 4)
    b = with_history(claude_home, tmp_path, "beta", 3)
    c = with_history(claude_home, tmp_path, "gamma", 2)
    assert all_state(env) is None

    start_hook(env, c, "new1", tmp_path)                       # TE_FOLD_SYNC=1: inline
    doc = all_state(env)
    assert doc is not None and doc["state"] == "done"
    assert doc["queue"][0] == key(env, c)                      # the session's repository first
    assert doc["queue"] == [key(env, r) for r in (c, a, b)]
    assert set(doc["done"]) == {key(env, r) for r in (a, b, c)}
    for root, n in ((a, 4), (b, 3), (c, 2)):
        name = os.path.basename(root)
        assert imported(env, root) == [f"{name}{i}" for i in range(n)], name
        assert repo_state(env, root)["state"] == "done", name


@nt_spawn
def test_the_first_session_start_detached_starts_the_whole_run_once_from_its_repository(
        env, claude_home, tmp_path, started, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)

    start_hook(detached(env), b, "new1", tmp_path)
    runs = whole_runs(started)
    assert len(runs) == 1
    assert opt(runs[0], "--host") == "claude" and opt(runs[0], "--cwd") == b
    doc = all_state(env)
    assert doc["state"] == "running" and fresh(doc)            # claimed before it was started

    # Another session start meanwhile, in the same repository or another: not a second run.
    start_hook(detached(env), b, "new2", tmp_path)
    start_hook(detached(env), a, "new3", tmp_path)
    assert len(whole_runs(started)) == 1

    # The process it started does the work: the claim does not make it find the run busy.
    assert run_detached(runs[0], env, monkeypatch) == 0
    doc = all_state(env)
    assert doc["state"] in ("done", "partial") and doc["queue"][0] == key(env, b)
    assert set(doc["queue"]) == {key(env, a), key(env, b)}
    assert imported(env, a) + imported(env, b) != []


@nt_spawn
def test_the_first_session_start_opens_one_process_not_a_second_for_its_own_repository(
        env, claude_home, tmp_path, started, monkeypatch):
    """The whole run starts with the session's repository, so the session start does not also
    start that repository's own import beside it (which would leave the run `partial`)."""
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 3)
    start_hook(detached(env), a, "new1", tmp_path)
    runs = whole_runs(started)
    assert len(runs) == 1
    assert repo_imports(started) == []
    assert repo_state(env, a) is None

    assert run_detached(runs[0], env, monkeypatch) == 0
    doc = all_state(env)
    assert doc["state"] == "done" and doc["done"] == [key(env, a), key(env, b)]
    assert imported(env, a) == ["alpha0", "alpha1"]
    assert imported(env, b) == ["beta0", "beta1", "beta2"]


@nt_spawn
def test_a_session_start_after_the_whole_run_is_done_starts_nothing(
        env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 1)
    start_hook(env, a, "new1", tmp_path)                       # inline: the run is done
    doc = all_state(env)
    assert doc["state"] == "done"

    for root, sid in ((a, "new2"), (b, "new3"), (a, "new4")):
        start_hook(detached(env), root, sid, tmp_path)
    assert history_procs(started) == []
    assert all_state(env) == doc
    assert imported(env, a) == ["alpha0", "alpha1"] and imported(env, b) == ["beta0"]


@nt_spawn
def test_a_session_start_resumes_a_partial_whole_run_where_it_left_off(
        env, claude_home, tmp_path, started, monkeypatch):
    a = with_history(claude_home, tmp_path, "alpha", 3)
    b = with_history(claude_home, tmp_path, "beta", 2)
    order = [key(env, a), key(env, b)]
    partial = {"state": "partial", "queue": order, "cwds": {order[0]: a, order[1]: b},
               "next": 1, "done": [order[0]], "skipped": {}, "started": time.time() - 7200,
               "heartbeat": time.time() - 3600, "pid": 4_000_000}
    write_all_state(env, partial)

    start_hook(detached(env), b, "new1", tmp_path)
    runs = whole_runs(started)
    assert len(runs) == 1 and opt(runs[0], "--cwd") == b
    assert repo_imports(started) == []                         # beta is in its queue
    doc = all_state(env)
    assert doc["state"] == "running" and fresh(doc)
    assert doc["queue"] == order and doc["next"] == 1 and doc["done"] == [order[0]]
    start_hook(detached(env), a, "new2", tmp_path)
    assert len(whole_runs(started)) == 1                       # once, not once per start

    assert run_detached(runs[0], env, monkeypatch) == 0
    assert imported(env, b) == ["beta0", "beta1"]
    assert imported(env, a) == []                              # before `next`: not read again
    doc = all_state(env)
    assert doc["state"] == "done" and doc["done"] == order


@nt_spawn
def test_a_session_start_takes_over_a_whole_run_whose_heartbeat_is_old_and_leaves_a_live_one(
        env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 1)
    order = [key(env, a), key(env, b)]
    alive = {"state": "running", "queue": order, "cwds": {order[0]: a, order[1]: b},
             "next": 0, "done": [], "skipped": {}, "started": time.time() - 60,
             "heartbeat": time.time() - 5, "pid": 4_000_000}
    write_all_state(env, alive)
    start_hook(detached(env), a, "new1", tmp_path)
    assert history_procs(started) == []                        # alive: left to run
    assert all_state(env) == alive

    write_all_state(env, dict(alive, heartbeat=time.time() - history.HEARTBEAT_STALE_S - 1))
    start_hook(detached(env), a, "new2", tmp_path)
    runs = whole_runs(started)
    assert len(runs) == 1 and opt(runs[0], "--cwd") == a
    assert repo_imports(started) == []
    doc = all_state(env)
    assert doc["state"] == "running" and fresh(doc)
    assert doc["queue"] == order and doc["next"] == 0
    start_hook(detached(env), b, "new3", tmp_path)
    assert len(whole_runs(started)) == 1                       # one takeover, not one per start


@nt_spawn
def test_while_the_whole_run_is_alive_a_repository_in_its_queue_starts_no_import_of_its_own(
        env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 2)
    order = [key(env, a), key(env, b)]
    alive = {"state": "running", "queue": order, "cwds": {order[0]: a, order[1]: b},
             "next": 0, "done": [], "skipped": {}, "started": time.time() - 60,
             "heartbeat": time.time() - 5, "pid": 4_000_000}
    write_all_state(env, alive)

    start_hook(detached(env), b, "new1", tmp_path)
    assert history_procs(started) == []
    assert repo_state(env, b) is None                          # not marked: the run takes it
    assert all_state(env) == alive
    ctx = ctx_for(detached(env), b)
    assert history_all.covers(ctx) is True


def test_a_live_session_that_ends_in_a_repository_still_ahead_in_the_whole_runs_queue_waits_for_its_import(
        env, claude_home, tmp_path):
    """A repository the live whole run has yet to reach has no `history.json` of its own: a
    session of it that ends meanwhile is left pending, as under its own running import. Folded
    then, the run would find the repository already learning and skip its earlier sessions."""
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 2)
    order = [key(env, a), key(env, b)]
    alive = {"state": "running", "queue": order, "cwds": {order[0]: a, order[1]: b},
             "next": 0, "done": [], "skipped": {}, "started": time.time() - 60,
             "heartbeat": time.time() - 5, "pid": 4_000_000}
    write_all_state(env, alive)

    t = trap_session(str(tmp_path / "live1.jsonl"), b, 7)
    call(env, {"hook_event_name": "SessionStart", "session_id": "live1", "source": "startup",
               "transcript_path": t, "cwd": b})
    assert repo_state(env, b) is None                          # the whole run takes it
    call(env, {"hook_event_name": "SessionEnd", "session_id": "live1", "transcript_path": t,
               "cwd": b})
    assert imported(env, b) == []                              # not learned before its history
    assert fold.pending_sessions(ctx_for(env, b)) == ["live1"]

    # The whole run goes on (here: a run that takes it over, from the same `next`).
    write_all_state(env, dict(alive, heartbeat=time.time() - history.HEARTBEAT_STALE_S - 1))
    res = history_all.run_all(hosts.ClaudeCode(), env, auto=True)
    assert res["state"] == "done"
    doc = repo_state(env, b)
    assert doc["state"] == "done", doc
    assert imported(env, b) == ["beta0", "beta1", "live1"]     # its history first, then the live one
    folded = store_for(env, b).read("folded.json")
    assert folded["beta0"].get("imported") and folded["beta1"].get("imported")
    assert not folded["live1"].get("imported")
    assert imported(env, a) == ["alpha0", "alpha1"]
    assert all_state(env)["done"] == order


@nt_spawn
def test_a_repository_the_whole_run_has_passed_imports_on_its_own_as_before(
        env, claude_home, tmp_path, started):
    """Only the queue from `next` on is the run's: a repository before it starts its own."""
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 2)
    order = [key(env, a), key(env, b)]
    write_all_state(env, {"state": "running", "queue": order,
                          "cwds": {order[0]: a, order[1]: b}, "next": 1, "done": [],
                          "skipped": {"gone": 1}, "started": time.time() - 60,
                          "heartbeat": time.time() - 5, "pid": 4_000_000})
    assert history_all.covers(ctx_for(detached(env), a)) is False

    start_hook(detached(env), a, "new1", tmp_path)
    assert whole_runs(started) == []
    own = repo_imports(started)
    assert own == [["history", "import", "--auto", "--host", "claude", "--cwd", a]]
    assert repo_state(env, a)["state"] == "running"


@nt_spawn
def test_a_repository_outside_the_whole_runs_queue_imports_on_its_own_as_before(
        env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    new = checkout(tmp_path / "newer", "https://github.com/acme/newer.git")   # no history then
    order = [key(env, a)]
    write_all_state(env, {"state": "running", "queue": order, "cwds": {order[0]: a},
                          "next": 0, "done": [], "skipped": {}, "started": time.time() - 60,
                          "heartbeat": time.time() - 5, "pid": 4_000_000})
    history_log(claude_home, new, "newer0", trap(new, 0))      # its first session since
    assert history_all.covers(ctx_for(detached(env), new)) is False

    start_hook(detached(env), new, "new1", tmp_path)
    assert whole_runs(started) == []
    assert repo_imports(started) == [["history", "import", "--auto", "--host", "claude",
                                      "--cwd", new]]
    assert repo_state(env, new)["state"] == "running"


def test_a_repository_outside_a_done_whole_run_is_imported_inline_as_before(
        env, claude_home, tmp_path):
    a = with_history(claude_home, tmp_path, "alpha", 1)
    start_hook(env, a, "new1", tmp_path)
    assert all_state(env)["state"] == "done"
    new = with_history(claude_home, tmp_path, "newer", 2)      # a checkout made afterwards
    start_hook(env, new, "new2", tmp_path)
    assert imported(env, new) == ["newer0", "newer1"]
    assert repo_state(env, new)["state"] == "done"
    assert all_state(env)["state"] == "done"


@nt_spawn
def test_with_history_import_off_neither_import_starts(env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 1)
    off = dict(env, TE_HISTORY_IMPORT="off")

    start_hook(detached(off), a, "new1", tmp_path)
    start_hook(off, b, "new2", tmp_path)                       # inline as well
    assert history_procs(started) == []
    assert all_state(env) is None
    assert repo_state(env, a) is None and repo_state(env, b) is None
    assert imported(env, a) == [] and imported(env, b) == []

    # A whole run left partial is not taken up while the setting is off.
    order = [key(env, a), key(env, b)]
    partial = {"state": "partial", "queue": order, "cwds": {order[0]: a, order[1]: b},
               "next": 0, "done": [], "skipped": {}, "started": time.time() - 7200,
               "heartbeat": time.time() - 3600, "pid": 4_000_000}
    write_all_state(env, partial)
    start_hook(detached(off), a, "new3", tmp_path)
    assert whole_runs(started) == []
    assert all_state(env)["state"] != "running"


@nt_spawn
@pytest.mark.parametrize("name", ["codex", "copilot"])
def test_a_session_start_of_another_agent_starts_no_whole_run(name, env, claude_home, tmp_path,
                                                               started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    t = str(tmp_path / f"{name}.jsonl")
    open(t, "w").close()
    for e in (env, detached(env)):
        call(e, {"hook_event_name": "SessionStart", "session_id": f"{name}-{len(started)}",
                 "source": "startup", "transcript_path": t, "cwd": a}, name)
    assert history_procs(started) == []
    assert all_state(env) is None
    assert imported(env, a) == [] and repo_state(env, a) is None


@nt_spawn
def test_a_droid_session_start_starts_no_whole_run_and_imports_its_own_history_as_before(
        env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)        # Claude Code's history too
    sessions_dir = tmp_path / "droid-sessions"
    _write_session(sessions_dir, "d0", a, TEST_CALLS)
    denv = dict(detached(env), DROID_SESSIONS_DIR=str(sessions_dir))
    # Even with a live whole run of Claude Code listing the same repository.
    order = [key(env, a)]
    alive = {"state": "running", "queue": order, "cwds": {order[0]: a}, "next": 0,
             "done": [], "skipped": {}, "started": time.time() - 60,
             "heartbeat": time.time() - 5, "pid": 4_000_000}
    write_all_state(env, alive)

    call(denv, {"hook_event_name": "SessionStart", "session_id": "d1", "source": "startup",
                "transcript_path": str(sessions_dir / "ws" / "d1.jsonl"), "cwd": a}, "droid")
    assert whole_runs(started) == []
    assert repo_imports(started) == [["history", "import", "--auto", "--host", "droid",
                                      "--cwd", a]]
    assert store_for(env, a, host="droid").read("history.json")["state"] == "running"
    assert all_state(env) == alive
    assert imported(env, a) == []                              # Claude Code's: not touched


@nt_spawn
@pytest.mark.parametrize("name", sorted(set(hosts.HOSTS) - {"claude"}))
def test_history_all_leaves_every_other_agent_alone(name, env, claude_home, tmp_path, started):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    ctx = LiveContext.build(hosts.HOSTS[name](), a, detached(env))
    history_all.maybe_start(ctx)
    assert history_procs(started) == []
    assert all_state(env) is None
    order = [key(env, a)]
    write_all_state(env, {"state": "running", "queue": order, "cwds": {order[0]: a},
                          "next": 0, "done": [], "skipped": {}, "started": time.time() - 60,
                          "heartbeat": time.time() - 5, "pid": 4_000_000})
    assert not history_all.covers(ctx)          # Claude Code's run never stands in for theirs


def _learned(env, repo, tmp_path):
    """Six sessions folded live, with the import off: the start's context carries `CLAIM`."""
    off = dict(env, TE_HISTORY_IMPORT="off")
    for i in range(6):
        t = trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i)
        call(off, {"hook_event_name": "SessionStart", "session_id": f"s{i}",
                   "source": "startup", "transcript_path": t, "cwd": repo})
        call(off, {"hook_event_name": "SessionEnd", "session_id": f"s{i}",
                   "transcript_path": t, "cwd": repo})


@nt_spawn
@pytest.mark.parametrize("where", ["run_all", "spawn"])
def test_an_exception_in_the_whole_runs_start_never_changes_the_session_starts_output(
        where, env, claude_home, tmp_path, started, monkeypatch):
    a = checkout(tmp_path / "alpha", "https://github.com/acme/alpha.git")
    _learned(env, a, tmp_path)
    with_history(claude_home, tmp_path, "beta", 2)
    e = env if where == "run_all" else detached(env)
    # What the start says when neither import is started at all.
    before = start_hook(dict(e, TE_HISTORY_IMPORT="off"), a, "n0", tmp_path)
    assert all_state(env) is None

    def boom(*args, **kw):
        raise RuntimeError(f"boom in history_all.{where}")
    monkeypatch.setattr(history_all, where, boom)
    out = start_hook(e, a, "n1", tmp_path)
    assert CLAIM in out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["additionalContext"] == \
        before["hookSpecificOutput"]["additionalContext"]
    log = open(os.path.join(env["TOKENECONOMIZER_HOME"], "errors.log")).read()
    assert f"RuntimeError: boom in history_all.{where}" in log


def test_the_notice_is_said_once_per_repository_after_the_whole_run_imported_it(
        env, claude_home, tmp_path):
    a = with_history(claude_home, tmp_path, "alpha", 2)
    b = with_history(claude_home, tmp_path, "beta", 3)
    said = ("TokenEconomizer read {n} earlier session(s) of {label} from Claude Code's "
            "history.")
    la, lb = repo_identity(a, env).label, repo_identity(b, env).label

    first = start_hook(env, a, "a1", tmp_path)                 # the whole run, inline
    assert all_state(env)["state"] == "done"
    assert "earlier session" not in first.get("systemMessage", "")

    second = start_hook(env, a, "a2", tmp_path)
    assert said.format(n=2, label=la) in second["systemMessage"]
    assert lb not in second["systemMessage"]
    assert "earlier session" not in start_hook(env, a, "a3", tmp_path).get("systemMessage", "")

    other = start_hook(env, b, "b1", tmp_path)                 # read by the same run
    assert said.format(n=3, label=lb) in other["systemMessage"]
    assert "earlier session" not in start_hook(env, b, "b2", tmp_path).get("systemMessage", "")
