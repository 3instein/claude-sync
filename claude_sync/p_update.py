"""The update part: a real sync runs `claude update` on both machines, so both have the
latest Claude Code. status only reports the versions. See docs/contract.md, Phase 2 parts."""

NAME = "update"


def plan(ctx):
    versions = {ctx.side[s][1].name: ctx.info[s].get("claude_version") for s in ctx.side}
    ctx.result["parts"][NAME] = {"versions": versions}
    # Only a real sync updates. A status run returns None, so it never counts as out of sync.
    return {"update": True} if ctx.applying else None


def apply(ctx, plan):
    from claude_sync import cli
    out = {}
    for s, (r, m) in ctx.side.items():
        got = cli.call_json(r, "claude_update", cli.roots(m))
        if not got.get("found"):
            ctx.result["warnings"].append(f"note: no ~/.local/bin/claude on {m.name}, Claude Code not updated there")
            continue
        if got.get("code") != 0:
            ctx.result["warnings"].append(f"error: claude update failed on {m.name}: {got.get('output', '')[-200:]}")
        out[m.name] = {"before": got.get("before"), "after": got.get("after")}
    ctx.result["parts"][NAME] = {"versions": out}
    after = {v["after"] for v in out.values()}
    if len(out) == 2 and len(after) == 1:
        # The version warning at the start of the run is out of date now.
        ctx.result["warnings"] = [w for w in ctx.result["warnings"] if not w.startswith("Claude Code versions differ")]
