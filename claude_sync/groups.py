"""Sidebar groups of the Code tab: a three-way merge of one part of claude_desktop_config.json.
The rest of that file holds per-machine settings, so it never changes. Pure functions."""
from claude_sync.merge import merge_json

KEY = "desktop/claude_desktop_config.json"


def subtree(cfg: dict) -> dict:
    """{scope: {"groups": [...], "assignments": {...}, "order": {...}}}"""
    return cfg.get("preferences", {}).get("epitaxyPrefs", {}).get("dframe-group-scopes", {})


def put(cfg: dict, scopes: dict) -> dict:
    out = dict(cfg)
    prefs = dict(out.get("preferences", {}))
    ep = dict(prefs.get("epitaxyPrefs", {}))
    ep["dframe-group-scopes"] = scopes
    prefs["epitaxyPrefs"] = ep
    out["preferences"] = prefs
    return out


def merge(base: dict, here: dict, there: dict, here_is_newer: bool) -> dict:
    """Merge every scope. base is the last subtree that both machines had, or {}."""
    return {s: _scope((base or {}).get(s, {}), here.get(s, {}), there.get(s, {}), here_is_newer)
            for s in sorted(set(here) | set(there))}


def _scope(base, here, there, newer):
    by_id = lambda sc: {g["id"]: g for g in sc.get("groups", [])}
    groups, _ = merge_json(by_id(base), by_id(here), by_id(there), newer)
    assign, _ = merge_json(base.get("assignments", {}), here.get("assignments", {}),
                           there.get("assignments", {}), newer)
    assign = {s: g for s, g in assign.items() if g in groups}  # a session in a deleted group is ungrouped
    order = {}
    for gid in groups:
        seen = []
        for s in here.get("order", {}).get(gid, []) + there.get("order", {}).get(gid, []):
            if assign.get(s) == gid and s not in seen:
                seen.append(s)
        seen += sorted(s for s, g in assign.items() if g == gid and s not in seen)
        order[gid] = seen
    # Here's group order first, then groups that exist only there.
    ids = [g["id"] for g in here.get("groups", [])] + [g["id"] for g in there.get("groups", [])]
    listed = list(dict.fromkeys(i for i in ids if i in groups))
    return {"groups": [groups[i] for i in listed], "assignments": assign, "order": order}

