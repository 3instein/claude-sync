# claude-sync build contract (phase 1)

This file fixes the interfaces between the parts. The PRD gives the reasons:
https://claude.ai/code/artifact/fb6210c4-da47-473c-aef3-c2dadf763388

## Rules for every agent

- Standard library only. The cli and merge run on Python 3.12 or later (both machines have 3.14). The helper program (`model.py`, `paths.py`, `helper.py`) must also run on Python 3.9, because a non-interactive SSH login on the Mac gets `/usr/bin/python3` 3.9.6. `remote.program()` adds `from __future__ import annotations`.
- Never read or write the real `~/.claude`, the real desktop data folder, or `~/.cache/claude-sync`. Never run `ssh`. Tests use temporary folders and the `Machine` values in the tests.
- Change only the files that your part owns (see the table). `claude_sync/model.py` and this file belong to the orchestrator. If the contract is wrong, stop and say so in your report.
- `python3 -m unittest discover -s tests` must pass for your test file before you finish.
- Code style: short functions, no classes where a function works, comments only for a rule and its reason.

| Part | Owner | Files |
| --- | --- | --- |
| paths | Sonnet agent | `claude_sync/paths.py` |
| transport | Sonnet agent | `claude_sync/helper.py`, `claude_sync/remote.py` |
| merge | Opus agent | `claude_sync/merge.py` |
| skill and install | Sonnet agent | `skill/SKILL.md`, `install.sh`, `tests/test_install.sh` |
| cli | orchestrator | `claude_sync/cli.py`, `claude-sync` |
| tests | orchestrator | `tests/test_paths.py`, `tests/test_merge.py`, `tests/test_helper.py` |

## Neutral paths

A neutral path replaces the machine-specific prefix with a token:

- the desktop data folder becomes `{desktop}` (checked first, because it is inside home on the Mac)
- the home folder becomes `~`

A prefix matches only at a path boundary: `/Users/r` matches `/Users/r` and `/Users/r/x`, not `/Users/r2`. Other paths do not change.

## Keys

A key names one synced file the same way on both machines: `<root>/<rel>`, where `root` is `cli` or `desktop` and `rel` is the path under that root with `/` separators.

Project folders are the exception. `cli/projects/<folder>/<rest>` becomes one of:

- `cli/projects/{<neutral cwd>}/<rest>`, when the cwd of the folder is known. Example: `cli/projects/{~/dev/x}/abc.jsonl`.
- `cli/projects/[<neutral slug>]/<rest>`, when the cwd is unknown and the folder name is 200 characters or less. The neutral slug replaces the slug form of the desktop prefix, then of the home prefix, with the tokens. Example: `-Users-r-dev-x` becomes `[~-dev-x]`.
- no key (skip with a warning) when the cwd is unknown and the name is longer than 200 characters.

The cwd of a folder comes from the first transcript line with a `cwd` field in any `.jsonl` file directly in the folder, or else from the state (`folders`).

`paths.folder_name(cwd)` gives Claude Code's folder name: every character that is not `[A-Za-z0-9]` becomes `-`. If the result is longer than 200 characters, it is cut to 200 characters plus `-` plus the base-36 form of `abs(java_hash_code(cwd))`. `java_hash_code` is Java's `String.hashCode` over the characters of the full cwd, in 32-bit signed arithmetic.

## File kinds

`paths.classify(key)`:

| Kind | Keys |
| --- | --- |
| `transcript` | any `.jsonl` under `cli/projects/` |
| `session` | `desktop/claude-code-sessions/*/*/local_*.json` |
| `json` | `cli/settings.json`, `desktop/claude-code-sessions/*/*/archived-sessions.idx` |
| `raw` | everything else |

`paths.is_exec(key)` is true for `cli/settings.json` and any key under `cli/skills/`, `cli/agents/`, `cli/commands/`.

## Path fields

Only these values change between machines:

- `transcript`, per line: `cwd`; `serverClassifierContext.context.live_cwd`; `serverClassifierContext.context.git_state.cwd`; `attachment.snapshot.workingDirectory`; `trackingPath`; `backup.realParentDir`; in `snapshot.trackedFileBackups`, each key that is an absolute path and each `realParentDir`.
- `session`: `cwd`, `originCwd`, each item of `scratchPromptRecents`.
- `json` (`cli/settings.json` only): every string value, at any depth, where the home or desktop prefix occurs (replace each occurrence at a path boundary).
- `raw`: nothing.

A transcript line that has none of these fields keeps its exact bytes. A line that changes is written again with `json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`.

## Hashes

`FileInfo.hash` is the sha256 of `paths.normalize_bytes(key, data, machine)`: the content with every path field in neutral form, JSON written with `sort_keys=True` and compact separators (for `transcript`, line by line, and lines without path fields keep their bytes). So two copies that differ only in path fields have the same hash. For `.jsonl`, `data` stops after the last `\n`: an incomplete last line is not hashed, counted, or sent.

## State file

`~/.cache/claude-sync/state.json`, mode 600, the same content on both machines:

```json
{
  "version": 1,
  "run_id": "20260927T164000Z-1a2b",
  "files": {"<key>": {"hash": "<sha256>", "stat": {"<hostname>": [1790500000, 1234]}}},
  "folders": {"<hostname>": {"<folder name>": "<neutral cwd>"}},
  "base": {"<key of kind session or json>": {"...": "neutral JSON object"}}
}
```

`base` holds the last synced content of JSON files, for the key-by-key merge.

## Merge (merge.py)

`plan(state_files, here, there, opts) -> Plan` is pure. `state_files` is the `files` object of the state, or `{}` on the first run. `here` and `there` are inventories. Rules, per key:

1. Same hash on both sides: nothing.
2. On both sides with a state entry: a side changed when its hash differs from the state hash. One side changed: `copy` to the other side. Both changed: `transcript`, `json_merge`, or `conflict` by kind, with `to` = `there` (the executor decides the final direction).
3. On one side only, with a state entry: if the side that has it did not change, it was deleted on the other side, so `delete` it. If it changed, `copy` it (a change wins over a deletion).
4. On one side only, no state entry, not first run: new file, `copy`.
5. First run (`state_files` is empty): a copy is unchanged since the migration when `mtime <= opts.first_run_cutoff[host]`.
   - On both sides, transcript: `transcript`.
   - On both sides, one copy unchanged since the migration: `copy` the other copy.
   - On both sides, both changed: as in rule 2.
   - On one side only and unchanged since the migration: it was probably deleted on the other side. Add it to `review["first_run"]` and `delete` it, unless a `keep` pattern matches, then `copy`.
   - On one side only and changed: `copy`.
6. Deletion limit: if the `delete` actions not in `first_run` are more than `opts.deletion_limit`, list them in `review["deletions"]`. A transcript whose mtime is older than `opts.now - opts.cleanup_days * 86400` does not count toward the limit and is not listed.
7. Exec config: every action that writes a key where `paths.is_exec(key)` goes in `review["exec_config"]`.
8. `review` holds only non-empty lists. Each item is `[side, key]`, where `side` is the side that the action writes, and each list is sorted. `token = review_token(review)`: the first 12 hex characters of sha256 over the canonical JSON of `review` (sorted keys, compact). A stop reason is in `stops` when its review list is not empty and `opts.confirm != token`. When `stops` is not empty, `actions` still lists every action, and the executor writes nothing.
9. Live files: an action that writes a `transcript` to `here` when `here[key].mtime > opts.now - opts.live_window` is removed from `actions` and its key goes in `skipped_live`.

`Opts` fields: `here_host`, `there_host`, `now`, `first_run_cutoff` (dict host to epoch seconds), `cleanup_days=30`, `deletion_limit=50`, `live_window=60`, `confirm=None`, `keep=()`.

`keep` patterns match a key with `fnmatch`, or when the key starts with the pattern plus `/`.

Content-level helpers, all on neutral bytes or neutral objects:

- `resolve_transcript(here: bytes, there: bytes) -> "here" | "there" | "split"`: if one copy's lines start with all lines of the other, the longer copy wins. Equal copies return `"here"`. Otherwise `"split"`.
- `split_transcript(data: bytes, old_id: str, new_id: str) -> bytes`: set `sessionId` to `new_id` in each line where it equals `old_id`. Other lines keep their bytes.
- `merge_json(base, here, there, here_is_newer: bool) -> (merged, overridden)`: merge top-level keys against `base` (`None` on the first run means an empty object). A key changed on one side takes that side's value. A key changed on both sides takes the newer file's value, and its name goes in `overridden`. A key deleted on one side and unchanged on the other is deleted.

## Helper (helper.py) and runner (remote.py)

`helper.py` runs on both machines. The runner sends `model.py`, `paths.py` and `helper.py`, joined in that order, as one base64 program:

```
python3 -c "import base64;exec(base64.b64decode('<source>'))" <command> '<json args>'
```

Over SSH the runner quotes the whole remote command with `shlex.quote`. Local calls use the same program without SSH. The combined source must stay under 90 KB (Linux limits one argument to 128 KB). `paths.py` and `helper.py` import `model` and `paths` in a `try` block, because in the combined program the names are already defined.

Every command takes one JSON object as `argv[1]` with at least `{"home": ..., "desktop": ...}`, and writes one JSON object to stdout, except `pack`. Bulk input comes on stdin.

| Command | Args | stdin | stdout |
| --- | --- | --- | --- |
| `info` | none | none | `{"hostname", "home", "desktop", "platform", "claude_version", "app_running", "cli_pids"}` (real environment; the only command that reads it. Find `claude` on PATH or in `~/.local/bin`, because a non-interactive SSH shell has a short PATH) |
| `inventory` | `folders` (folder name to neutral cwd) | cache JSON: `{key: [mtime, size, hash]}` | `{"files": {key: [hash, mtime, size]}, "folders": {folder name: neutral cwd}, "warnings": [...]}` |
| `pack` | `keys` | none | a `tar` stream (`w|gz`). Member name = key, member mtime = file mtime. For `.jsonl`, only up to the last full line. |
| `apply` | `run_id`, `src` (`home`, `desktop` of the machine that sent the files) | `tar` stream from `pack` | `{"written": {key: [mtime, size]}, "errors": [...]}` |
| `delete` | `run_id`, `keys` | none | `{"deleted": [...], "errors": [...]}` |
| `lock_read` / `lock_write` / `lock_remove` | `lock` for write: `{"host", "pid", "start", "run_id"}` | none | `{"lock": obj or null}` |
| `proc_alive` | `pid`, `start` | none | `{"alive": bool}`; true only if the process runs and `ps -o lstart= -p <pid>` (stripped) equals `start` |
| `state_read` / `state_write` | none | state JSON for write | `{"state": obj or null}` |
| `undo` | `run_id`, or null for the run with the newest manifest | none | `{"restored": [paths], "removed": [paths]}` |
| `prune` | `days` | none | `{"removed": [run ids]}`: backup runs whose folder mtime is older than `days` |

`apply` details: for each member, map the content from `src` to this machine with `paths.localize_bytes`, find the local path with `paths.key_to_path`, copy an existing file to `~/.cache/claude-sync/backup/<run_id>/` first, write through a temporary file and `os.replace`, set mode 600 and the member's mtime. A member name that starts with `conflicts/` is written under `~/.cache/claude-sync/conflicts/<run_id>/`. Each run keeps one `backup/<run_id>/manifest.json` with each path it touched and whether a backup exists, so `undo` can restore or remove it. A run can call `apply` and `delete` more than once and write one key twice: the manifest keeps the first backup of each key, so `undo` returns to the state before the run.

The inventory lists regular files under the phase 1 items (`model.CLI_ITEMS`, `model.DESKTOP_ITEMS`), skips symlinks and `model.SKIP_NAMES`, and reuses a cached hash when mtime and size are the same.

`~/.cache/claude-sync` and each folder in it have mode 700. Each file that the helper writes has mode 600.

`remote.Runner(host)`: `host=None` runs locally. `call(command, args, stdin=b"") -> bytes` raises `RemoteError` with the stderr on a non-zero exit. SSH options: `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no`.

## JSON output of the cli

See the PRD, section Commands. Exit codes: 0 done or in sync, 1 error, 2 not in sync (`status` only), 3 a decision is needed, 4 the other machine is not reachable, 5 another sync holds the lock.

## Fix round 1 (after the adversarial code review)

These rules replace the earlier text where they differ.

1. **Loose hash.** The migration replaced the home path in all text, not only in path fields (checked on real transcripts: 73 of 336 lines differ, all only in the home path). So `normalize_bytes(key, data, m, prefixes=())` takes `prefixes`, a list of `[home, desktop]` pairs of all machines. After the path-field mapping, it replaces every occurrence of each desktop folder with `{desktop}` and then of each home folder with `~` in the whole content (longest prefix first, at a path boundary as in `settings.json`). This applies to every kind, `raw` included. It is only for hashes and for `resolve_transcript` input; written content does not change. `inventory` takes `prefixes` in its args.
2. **Key tokens.** A cwd can contain `}` or `{desktop}`. In a `{...}` or `[...]` token, the characters `%`, `{`, `}`, `[`, `]` are written as `%25`, `%7B`, `%7D`, `%5B`, `%5D`. `key_to_path` decodes them. So `cli/projects/{%7Bdesktop%7D/scratch-workspaces/x}/s.jsonl` is the key of a Code tab scratch session.
3. **Safe keys.** `paths.safe_key(key) -> bool`: false for an empty segment, `.` or `..` segments, a leading `/`, a NUL, or a root that is not `cli` or `desktop`. A `conflicts/` member name must be `conflicts/` plus a safe key. `pack`, `apply`, `delete` and the cli refuse unsafe keys. `apply` also checks that the real path of the target stays under its root.
4. **Keys on stdin.** `pack` and `delete` read `{"keys": [...]}` from stdin, not from args, because one SSH argument is limited to 128 KB. The cli sends at most 500 keys per call.
5. **Apply precondition.** The first tar member may be `.claude-sync-expect.json`: `{key: [mtime, size] or null}`. `apply` skips a key whose target exists with a different mtime or size (or exists when `null` is expected), and reports it in `skipped`. `delete` takes `expect` in its stdin object the same way.
6. **Modes.** A file whose source had any execute bit is written with mode 700, else 600. The tar member mode carries it.
7. **Inventory warnings.** An item in `CLI_ITEMS` or `DESKTOP_ITEMS` that is a symlink, an unreadable folder, or an `os.walk` error adds a warning. A missing item does not (a new machine can lack `agents/`), but the cli treats a missing `projects` folder as a warning.
8. **Atomic lock.** `lock_write` creates the file with `O_CREAT | O_EXCL` and returns `{"ok": false, "lock": existing}` when a lock exists.
9. **Undo.** `undo` requires a `run_id`; the cli uses the run id in the state and undoes it on both machines. The manifest records the mtime and size that the run wrote. `undo` skips a file that changed after the run, backs up the current file before it restores or removes, and reports skipped files.
10. **Deletions.** Every deletion counts toward the limit: no exemption for old transcripts. A `keep` pattern also applies to rule 3: a kept deletion becomes a `copy` back.
11. **Splits.** Before it writes anything, the cli resolves every `transcript` action. More than 10 splits in one run is a stop reason, `splits`, whose list is part of the review and the token.

## Fix round 2 (real data, 2026-09-27)

Read-only runs and the first real sync found these rules. They replace the earlier text where they differ.

1. **Loose hash boundary.** The loose replacement uses a right boundary only (the path ends at `/`, the end, or a character that is not `[A-Za-z0-9_.-]`). Inside JSON strings a path often follows `\n` or `file://`, and a left boundary missed it: 194 transcripts would have split.
2. **Both desktop layouts.** `cli.loose_prefixes` lists `<home>/Library/Application Support/Claude` and `<home>/.config/Claude` under each home, besides each machine's real desktop folder. The migration turned the Mac desktop folder into `/home/idkman/Library/Application Support/Claude`.
3. **Metadata lines.** When the plain prefix check fails, `resolve_transcript` compares only the lines whose `type` is not in `merge.META_TYPES` (`cost-state`, `bridge-session`, `last-prompt`, `queue-operation`, `custom-title`, `agent-name`, `mode`, `atis-latch`, `pr-link`). If one copy's conversation starts with all of the other's, that copy wins, and the other copy's extra metadata lines are dropped.
4. **Folder cwd.** `helper._folder_cwd` takes a folder's cwd only from a transcript line where `folder_name(cwd)` equals the folder name: first from the first 1 MB of each `.jsonl`, then from whole files. A session moved to another folder keeps its old cwd on its first lines. Without a match, the folder gets a `[slug]` key and the cli remaps it.
5. **Skipped paths.** `model.SKIP_PATHS` holds paths under a root that are never synced: `skills/synced` (the desktop app syncs it itself from claude.ai for each machine).
6. **Undo and the state.** `undo` also returns the `keys` it touched, and the cli removes them from the state on both machines, so the next run treats the two copies as a conflict. Before any write, the cli writes the new run id to the state, so `undo` finds a run that crashed.
7. **Deterministic splits.** A split's new session id is `uuid5` of the key and the hash of the other machine's copy, so a run that crashed after a split does not split again.
8. **Robust transfers.** `pack` skips a file deleted since the file list. The `apply` precondition compares a `.jsonl` size up to its last full line. A missing `projects` or `claude-code-sessions` folder is a warning only when the state had files there.
9. **Raw conflicts: the newer copy wins.** For a `raw` file changed on both machines, the copy with the newer mtime wins, and the other copy goes to the conflicts folder on the machine that runs the sync. Before, the machine that runs the sync always won, and a sync after `undo` could put an older copy back.
10. **Splits never overwrite.** The files of a split session are written only where they do not exist yet, because an existing split session may have been continued.
11. **Folder cwd from the state first.** `_folder_cwd` uses the cwd from the state's `folders` for this machine when its folder name matches, and scans the transcripts only when it does not. So a new session in a folder whose name two cwds share (`~/dev/my-app` and `~/dev/my.app`) cannot change the folder's key.
12. **Precondition size.** Only a `transcript` compares its size up to the last full line. Other `.jsonl` files compare their full size.
13. **Accepted limits.** An edit that only changes a home path form (for example `/Users/reynaldikindarto/dev/x` to `~/dev/x`) has the same loose hash and does not sync. The losing copy's extra metadata lines (for example a `custom-title` rename) are dropped in a metadata-only resolution.

## Phase 2 parts

A part is a module `claude_sync/p_<name>.py` with `NAME`, `plan(ctx)` and `apply(ctx, plan)` (see `claude_sync/part.py`). The cli calls `plan` for every part in `cli.PART_MODULES` before it decides on stops, and `apply` after the file sync, only for a part whose plan is not `None`. `plan` must only read. A part:

- reads and writes the other machine only through helper commands. New helper commands go in `claude_sync/h_<name>.py`, which the runner adds to the combined program after `helper.py`. Such a file imports from `claude_sync.helper` in a `try` block, defines `cmd_<x>(args, stdin)` functions, and ends with `COMMANDS.update({...})`. It must run on Python 3.9.
- writes a summary to `ctx.result["parts"][NAME]` and warnings to `ctx.result["warnings"]`.
- adds items that need a confirm to `ctx.review[reason]` as `[side, item]` (side = the side that changes). They become part of the token. A change that runs code (MCP servers, plugins, marketplaces, permission rules) uses the reason `exec_config`.
- adds reasons that no token can confirm to `ctx.stops` (only `git`).
- saves its own state in `ctx.part_state[NAME]`; the cli stores it in `state["parts"][NAME]`. The old value is `ctx.state.get("parts", {}).get(NAME)`.
- writes a file on a machine with a backup and a precondition: with `cli.apply(runner, machine, run_id, src_machine, {key: (data, mtime, mode)}, {key: FileInfo(hash, mtime, size)}, result)` for keys under the roots, or with its own helper command that uses `helper._backup` and `helper._atomic_write` and skips a file whose mtime changed since the plan read it.
- updates a base in its state only when both machines show the same content at the start of a run (the same rule as `groups`), because an open app or session can write its old copy back.
- has its own tests in `tests/test_<name>.py`, run with fake machines as in `tests/test_cli.py`.

| Part | Data | Rule |
| --- | --- | --- |
| `history` (`p_history.py`) | `cli/history.jsonl` through `pack` and `apply` | Union of the lines of both machines, with no duplicates (a line's identity is its `timestamp`, `display` and `sessionId`), in `timestamp` order. The `project` field changes to each machine's path (`paths.neutral` and `paths.localize`). Nothing is deleted. No state. |
| `mcp` (`p_mcp.py`, `h_mcp.py`) | In `~/.claude.json`: `mcpServers`, and for each project the fields `allowedTools`, `hasTrustDialogAccepted`, `enabledMcpjsonServers`, `disabledMcpjsonServers` | Three-way merge by server name and by project against `state["parts"]["mcp"]`, in neutral form (home paths in `command`, `args`, `env` and the project keys are neutral). Every other key of `~/.claude.json` never changes. A server whose `command` is absolute and missing on the target uses the same command name from the target's PATH, `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`. A command or path that is still missing gives a warning. An `env` value or header whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `AUTH` is never copied: such a server is reported and skipped. Each server or project change is `exec_config`. |
| `plugins` (`p_plugins.py`, `h_plugins.py`) | `~/.claude/plugins/installed_plugins.json` (user scope) and `known_marketplaces.json` | Three-way against `state["parts"]["plugins"]` (`{"installed": [...], "marketplaces": {name: source}}`). New on one machine: install on the other. Removed on one machine: remove on the other. First run (no state): install what is missing, remove nothing. Marketplaces before plugins. The helper runs only `claude plugin install X`, `claude plugin uninstall X`, `claude plugin marketplace add SOURCE` and `claude plugin marketplace remove NAME`, with the `claude` binary from `info` and a 180 s timeout. Each change is `exec_config`. `plugins/data/` syncs as files. |
| `git` (`p_git.py`, `h_git.py`) | Repos in `~/dev`, two levels deep, and their git worktrees | Read only. The helper runs `git fetch` (on Linux inside `bash -ic`, so the tokens in `~/.bash_env` are loaded; 30 s timeout; a failure is a warning) and reports each repo's path, origin URL, branch, upstream, ahead, behind, changed and untracked files. The table in the PRD (Git check) decides: a clean repo that is only behind gets a `next` fast-forward pull and no stop; ahead, uncommitted, diverged, or on one machine only is the stop `git`, with a `next` command where there is one; no remote is a warning. `--skip-repo PATH` takes a repo out. Every value in a `next` command is quoted with `shlex.quote`, and a command for the other machine is `ssh HOST <quoted "bash -ic ...">`. |
| `secrets` (`p_secrets.py`, `h_secrets.py`) | Secret files (`paths.is_secret`) in `~/dev` (repos included, dependency folders excluded) and in the synced folders of `~/.claude` | Report only: for each file that is missing on one machine or different, `result["parts"]["secrets"]` lists its path and an `scp` command. The helper returns hashes to the cli, and the cli never prints a hash or the contents. Never a stop, and not part of `in_sync`. |
| `dev` (a new root in `paths` and `helper`) | Files in `~/dev` outside git repos, and `.claude/settings.local.json` in each repo | The key is `dev/<path under ~/dev>`. The inventory skips git repos (a folder with `.git`), symlinks, secret files, and the folders `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.next`, `.turbo`, `target`, `.worktrees` and `google-cloud-sdk`. It includes `<repo>/.claude/settings.local.json`, whose kind is `json` with the same string mapping as `cli/settings.json`, and which is exec config. Everything else in `dev` is `raw`. The normal three-way file merge applies. |
