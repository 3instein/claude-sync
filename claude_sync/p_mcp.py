"""The mcp part: mcpServers and, per project, the trust and tool-permission fields
of ~/.claude.json. See docs/contract.md, Phase 2 parts, mcp."""
import os
import re

NAME = "mcp"
SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.IGNORECASE)


def _has_secret(server):
    """A server whose env or headers has a secret-named value is never copied."""
    for field in ("env", "headers"):
        if any(SECRET_RE.search(k) for k in (server.get(field) or {})):
            return True
    return False


def _map_server(server, fn):
    out = dict(server)
    if isinstance(out.get("command"), str):
        out["command"] = fn(out["command"])
    if isinstance(out.get("args"), list):
        out["args"] = [fn(a) if isinstance(a, str) else a for a in out["args"]]
    if isinstance(out.get("env"), dict):
        out["env"] = {k: (fn(v) if isinstance(v, str) else v) for k, v in out["env"].items()}
    return out


def _plan_servers(local_servers, merged, skip, m, side):
    """Full mcpServers dict for this side (h_mcp replaces the whole key), and the
    server names that change, for review and for command resolution. `skip` is the
    secret-holding names: left exactly as this side already has them, on every side."""
    from claude_sync import paths
    final = dict(local_servers)
    review, changed = [], set()
    for name in (set(local_servers) | set(merged)) - skip:
        new_local = _map_server(merged[name], lambda p: paths.localize(p, m)) if name in merged else None
        if new_local != local_servers.get(name):
            if new_local is None:
                final.pop(name, None)
            else:
                final[name] = new_local
            review.append([side, f"mcp:{name}"])
            changed.add(name)
    return final, review, changed


def _resolve_commands(r, m, changed, final_servers, result):
    """A server whose command is an absolute path missing on this side uses the same
    command name from this side's PATH, ~/.local/bin, /opt/homebrew/bin or /usr/local/bin."""
    from claude_sync import cli
    candidates = {name: final_servers[name]["command"] for name in changed
                  if name in final_servers and isinstance(final_servers[name].get("command"), str)
                  and os.path.isabs(final_servers[name]["command"])}
    if not candidates:
        return
    names = sorted({os.path.basename(c) for c in candidates.values()})
    out = cli.call_json(r, "which", {**cli.roots(m), "names": names, "paths": sorted(set(candidates.values()))})
    for name, cmd in candidates.items():
        if out["paths"].get(cmd):
            continue
        found = out["names"].get(os.path.basename(cmd))
        if found:
            final_servers[name] = dict(final_servers[name], command=found)
        else:
            result["warnings"].append(f"mcp: command '{cmd}' for server '{name}' not found on {m.name}")


def _plan_projects(local_projects, merged, m, side):
    """{local path: fields} only for projects whose fields change (h_mcp updates
    just those keys, so unaffected projects are left out entirely)."""
    from claude_sync import paths
    updates, review = {}, []
    for npath in set(paths.neutral(p, m) for p in local_projects) | set(merged):
        local_path = paths.localize(npath, m)
        new_val = merged.get(npath, {})
        if new_val != local_projects.get(local_path, {}):
            updates[local_path] = new_val
            review.append([side, f"mcp-project:{npath}"])
    return updates, review


def plan(ctx):
    """Read ~/.claude.json's mcp keys on both machines, merge in neutral form
    against the saved base, and report per side what needs writing. Reads only."""
    from claude_sync import cli, paths
    from claude_sync.merge import merge_json
    read, m_of = {}, {}
    for s, (r, m) in ctx.side.items():
        read[s] = cli.call_json(r, "mcp_read", cli.roots(m))
        m_of[s] = m
    neutral_servers = {s: {n: _map_server(v, lambda p, m=m_of[s]: paths.neutral(p, m))
                            for n, v in read[s]["mcpServers"].items()} for s in read}
    neutral_projects = {s: {paths.neutral(p, m_of[s]): v for p, v in read[s]["projects"].items()}
                         for s in read}

    if neutral_servers["here"] == neutral_servers["there"] and neutral_projects["here"] == neutral_projects["there"]:
        # The base moves only when both machines agree: an open app can write its old copy back.
        ctx.part_state[NAME] = {"mcpServers": neutral_servers["here"], "projects": neutral_projects["here"]}
        ctx.result["parts"][NAME] = "in sync"
        return None

    base = ctx.state.get("parts", {}).get(NAME) or {}
    newer = (read["here"].get("mtime") or 0) >= (read["there"].get("mtime") or 0)
    merged_servers, _ = merge_json(base.get("mcpServers", {}), neutral_servers["here"], neutral_servers["there"], newer)
    merged_projects, _ = merge_json(base.get("projects", {}), neutral_projects["here"], neutral_projects["there"], newer)

    secret_names = set()
    for name in list(merged_servers):
        if _has_secret(merged_servers[name]):
            if neutral_servers["here"].get(name) != neutral_servers["there"].get(name):
                ctx.result["warnings"].append(f"mcp: server '{name}' has a secret value, not synced")
            del merged_servers[name]
            secret_names.add(name)

    write, review = {}, []
    for s, m in m_of.items():
        r = ctx.side[s][0]
        final_servers, srv_review, changed = _plan_servers(read[s]["mcpServers"], merged_servers, secret_names, m, s)
        _resolve_commands(r, m, changed, final_servers, ctx.result)
        project_updates, proj_review = _plan_projects(read[s]["projects"], merged_projects, m, s)
        review += srv_review + proj_review
        if final_servers != read[s]["mcpServers"] or project_updates:
            write[s] = {"mcpServers": final_servers, "projects": project_updates,
                        "expect": None if read[s]["mtime"] is None else [read[s]["mtime"], read[s]["size"]]}

    if review:
        ctx.review.setdefault("exec_config", []).extend(sorted(review))
    ctx.result["parts"][NAME] = f"updates {', '.join(sorted(write))}" if write else "no changes"
    return write or None


def apply(ctx, plan):
    from claude_sync import cli
    for s, data in plan.items():
        r, m = ctx.side[s]
        args = {**cli.roots(m), "run_id": ctx.run_id, "mcpServers": data["mcpServers"],
                "projects": data["projects"], "expect": data["expect"]}
        out = cli.call_json(r, "mcp_write", args)
        if out.get("skipped"):
            ctx.result["warnings"].append(f"mcp: skipped on {m.name}, changed during the run")
