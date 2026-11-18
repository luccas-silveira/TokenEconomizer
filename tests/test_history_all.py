"""Finding every repository in Claude Code's history in one pass (`history_all.discover`), and
planning one repository's import from the logs that pass found (`history.plan(..., logs=)`).

Logs are written where Claude Code keeps them (`history_log`); each names the directory the
session ran in. A repository is a git checkout with an `origin`, keyed as a live session's is.
"""
import collections
import os
import time

import pytest

from test_history_import import (claude_home, ctx_for, env, history_log, looked,  # noqa: F401
                                 trap)
from tokeneconomizer import trace
from tokeneconomizer.plugin import history, history_all, hosts
from tokeneconomizer.plugin.paths import repo_identity


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
