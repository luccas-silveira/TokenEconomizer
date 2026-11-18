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
"""
import os

from .paths import repo_identity


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
