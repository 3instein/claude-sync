"""The mcp part: mcpServers and, per project, the trust and tool-permission fields
of ~/.claude.json. See docs/contract.md, Phase 2 parts, mcp, and Phase 2 fix round, 6."""
import hashlib
import json
import os
import re

from claude_sync.model import HOME_TOKEN, DESKTOP_TOKEN

NAME = "mcp"

# A secret by name, unless the value is a path (for example TOKEN_PATH holds a file path).
NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS|PAT|AUTH|CREDENTIAL", re.IGNORECASE)
# A --api-key / --token / --secret / --password / --auth style option, with a value after it.
FLAG_RE = re.compile(r"^--?((api[-_]?)?key|token|secret|password|passwd|auth"
                     r"|(access|client|refresh|auth)[-_]?(token|secret|key))(?P<value>=.+)?$", re.IGNORECASE)
# scheme://user:pass@host
URL_AUTH_RE = re.compile(r"[a-zA-Z][\w+.-]*://[^/\s:@]+:[^/\s@]+@")
# a token=, key= or secret= query value
QUERY_SECRET_RE = re.compile(r"[?&](token|key|secret)=[^&\s]+", re.IGNORECASE)


def _looks_like_path(v):
    return isinstance(v, str) and (v.startswith("/") or v.startswith("~"))


def _value_leaks_secret(v):
    return isinstance(v, str) and bool(URL_AUTH_RE.search(v) or QUERY_SECRET_RE.search(v))


def _has_secret(server):
    """A secret in an env or header name (not a path value), or a value that carries one:
    a password in a URL, a token/key/secret query value, or a flag followed by its value."""
    for field in ("env", "headers"):
        for k, v in (server.get(field) or {}).items():
            if (NAME_RE.search(k) and not _looks_like_path(v)) or _value_leaks_secret(v):
                return True
    args = server.get("args") or []
    for i, a in enumerate(args):
        if not isinstance(a, str):
            continue
        if _value_leaks_secret(a):
            return True
        flag = FLAG_RE.match(a)
        if flag and (flag.group("value") or i + 1 < len(args)):
            return True
    return _value_leaks_secret(server.get("url"))


def _map_server(server, fn):
    out = dict(server)
    if isinstance(out.get("command"), str):
        out["command"] = fn(out["command"])
    if isinstance(out.get("args"), list):
        out["args"] = [fn(a) if isinstance(a, str) else a for a in out["args"]]
    if isinstance(out.get("env"), dict):
        out["env"] = {k: (fn(v) if isinstance(v, str) else v) for k, v in out["env"].items()}
    return out


def _is_abs_like(cmd):
    """An OS-absolute command, or its neutral ~/... or {desktop}/... form."""
    if not isinstance(cmd, str):
        return False
    return (os.path.isabs(cmd) or cmd in (HOME_TOKEN, DESKTOP_TOKEN)
            or cmd.startswith(HOME_TOKEN + "/") or cmd.startswith(DESKTOP_TOKEN + "/"))


def _basename_if_abs(cmd):
    return cmd.rsplit("/", 1)[-1] if _is_abs_like(cmd) else cmd


def _for_merge(server):
    """The server as merge_json sees it: command reduced to its base name, so resolving
    it to a different absolute path per machine is not read as a change."""
    return dict(server, command=_basename_if_abs(server.get("command")))


def _content_hash(obj):
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:8]


def _plan_servers(local_servers, merged, abs_names, skip, r, m, side, result):
    """This side's full mcpServers dict (h_mcp replaces the whole key), the review items,
    and the neutral values actually written, for result["parts"]["mcp"]. `skip` is the
    secret-holding names: left exactly as this side already has them, on every side."""
    from claude_sync import cli, paths
    final = dict(local_servers)
    review, written = [], {}
    names = (set(local_servers) | set(merged)) - skip
    to_resolve = sorted({merged[n]["command"] for n in names if n in merged and n in abs_names
                          and isinstance(merged[n].get("command"), str)})
    resolved = {}
    if to_resolve:
        resolved = cli.call_json(r, "which", {**cli.roots(m), "names": to_resolve})["names"]
    for name in sorted(names):
        if name not in merged:
            if local_servers.get(name) is not None:
                final.pop(name, None)
                review.append([side, f"mcp:{name}@{_content_hash({})}"])
            continue
        candidate = merged[name]
        local_cmd = (local_servers.get(name) or {}).get("command")
        if name in abs_names and _basename_if_abs(local_cmd) == candidate.get("command"):
            found = local_cmd  # this side's command works here already (for example an nvm npx): keep it
        elif name in abs_names:
            found = resolved.get(candidate.get("command"))
        if name in abs_names and not found:
                result["warnings"].append(f"skipped on {m.name}: mcp command "
                                          f"'{candidate.get('command')}' for server '{name}' not found")
                continue
        if name in abs_names:
            new_local = dict(_map_server(candidate, lambda p: paths.localize(p, m)), command=found)
        else:
            new_local = _map_server(candidate, lambda p: paths.localize(p, m))
        if new_local != local_servers.get(name):
            final[name] = new_local
            shown = _map_server(new_local, lambda p: paths.neutral(p, m))  # the command as written on this side
            review.append([side, f"mcp:{name}@{_content_hash(shown)}"])
            written[f"{name} ({m.name})"] = shown
    return final, review, written


def _plan_projects(local_projects, merged, r, m, side):
    """{local path: fields} only for projects whose fields change, and only where the
    project's folder exists on this side. h_mcp updates just those project keys."""
    from claude_sync import cli, paths
    updates, review, written = {}, [], {}
    names = set(paths.neutral(p, m) for p in local_projects) | set(merged)
    to_check = sorted({paths.localize(n, m) for n in names if n in merged})
    exists = cli.call_json(r, "which", {**cli.roots(m), "dirs": to_check})["dirs"] if to_check else {}
    for npath in sorted(names):
        local_path = paths.localize(npath, m)
        if npath in merged and not exists.get(local_path):
            continue  # only write a project to a side where its folder exists
        new_val = merged.get(npath, {})
        if new_val != local_projects.get(local_path, {}):
            updates[local_path] = new_val
            review.append([side, f"mcp-project:{npath}@{_content_hash(new_val)}"])
            written[npath] = new_val
    return updates, review, written


def plan(ctx):
    """Read ~/.claude.json's mcp keys on both machines, merge in neutral form
    against the saved base, and report per side what needs writing. Reads only."""
    from claude_sync import cli, paths
    from claude_sync.merge import merge_json
    read, m_of = {}, {}
    for s, (r, m) in ctx.side.items():
        read[s] = cli.call_json(r, "mcp_read", cli.roots(m))
        m_of[s] = m
    neutral_full = {s: {n: _map_server(v, lambda p, m=m_of[s]: paths.neutral(p, m))
                         for n, v in read[s]["mcpServers"].items()} for s in read}
    neutral_projects = {s: {paths.neutral(p, m_of[s]): v for p, v in read[s]["projects"].items()}
                         for s in read}

    if neutral_full["here"] == neutral_full["there"] and neutral_projects["here"] == neutral_projects["there"]:
        # The base moves only when both machines agree: an open app can write its old copy back.
        ctx.part_state[NAME] = {"mcpServers": neutral_full["here"], "projects": neutral_projects["here"]}
        ctx.result["parts"][NAME] = "in sync"
        return None

    base = ctx.state.get("parts", {}).get(NAME) or {}
    base_servers = base.get("mcpServers", {})
    newer = (read["here"].get("mtime") or 0) >= (read["there"].get("mtime") or 0)

    # A secret anywhere (base, here or there) takes the whole server out of the merge, so a
    # newer copy that happens to lack the secret can never overwrite the copy that has it.
    secret_names = set()
    for name in set(base_servers) | set(neutral_full["here"]) | set(neutral_full["there"]):
        copies = [d.get(name) for d in (base_servers, neutral_full["here"], neutral_full["there"])]
        if any(c is not None and _has_secret(c) for c in copies):
            secret_names.add(name)
            if neutral_full["here"].get(name) != neutral_full["there"].get(name):
                ctx.result["warnings"].append(f"note: mcp server '{name}' has a secret value, so it is not synced")

    abs_names = {n for n, v in {**base_servers, **neutral_full["here"], **neutral_full["there"]}.items()
                 if _is_abs_like(v.get("command"))}

    def for_merge(d):
        return {n: _for_merge(v) for n, v in d.items() if n not in secret_names}

    merged_servers, _ = merge_json(for_merge(base_servers), for_merge(neutral_full["here"]),
                                   for_merge(neutral_full["there"]), newer)
    merged_projects, _ = merge_json(base.get("projects", {}), neutral_projects["here"], neutral_projects["there"], newer)

    write, review, written_servers, written_projects = {}, [], {}, {}
    for s, m in m_of.items():
        r = ctx.side[s][0]
        final_servers, srv_review, srv_written = _plan_servers(
            read[s]["mcpServers"], merged_servers, abs_names, secret_names, r, m, s, ctx.result)
        project_updates, proj_review, proj_written = _plan_projects(read[s]["projects"], merged_projects, r, m, s)
        review += srv_review + proj_review
        written_servers.update(srv_written)
        written_projects.update(proj_written)
        if final_servers != read[s]["mcpServers"] or project_updates:
            write[s] = {"mcpServers": final_servers, "projects": project_updates,
                        "expect": None if read[s]["mtime"] is None else [read[s]["mtime"], read[s]["size"]]}

    if review:
        ctx.review.setdefault("exec_config", []).extend(sorted(review))
    ctx.result["parts"][NAME] = ({"mcpServers": written_servers, "projects": written_projects}
                                  if write else "no changes")
    return write or None


def apply(ctx, plan):
    from claude_sync import cli
    for s, data in plan.items():
        r, m = ctx.side[s]
        args = {**cli.roots(m), "run_id": ctx.run_id, "mcpServers": data["mcpServers"],
                "projects": data["projects"], "expect": data["expect"]}
        out = cli.call_json(r, "mcp_write", args)
        if out.get("skipped"):
            ctx.result["warnings"].append(f"skipped on {m.name}: mcp write, changed during the run")
        else:
            ctx.result["warnings"].append(
                f"mcp: wrote ~/.claude.json on {m.name}; undo restores it only if Claude Code has not written it since")
