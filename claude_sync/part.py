"""The interface of a phase 2 part (history, mcp, plugins, git). See docs/contract.md, Phase 2.
A part module has NAME, plan(ctx) and apply(ctx, plan). plan only reads."""
from dataclasses import dataclass, field


@dataclass
class Ctx:
    side: dict          # "here" | "there" -> (Runner, Machine)
    state: dict         # the state at the start of the run (read only)
    info: dict          # "here" | "there" -> the helper's info output
    confirm: str | None
    keep: tuple
    skip_repos: tuple
    applying: bool
    run_id: str
    result: dict        # the cli output: a part writes result["parts"][NAME] and adds to result["warnings"]
    review: dict = field(default_factory=dict)      # reason -> [[side, item]]; part of the confirm token
    stops: list = field(default_factory=list)       # reasons that no token can confirm (for example "git")
    part_state: dict = field(default_factory=dict)  # NAME -> the part's new state, saved in state["parts"]
