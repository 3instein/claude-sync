# claude-sync build contract (phase 1)

This file fixes the interfaces between the parts. The PRD gives the reasons:
https://claude.ai/code/artifact/fb6210c4-da47-473c-aef3-c2dadf763388

## Rules for every agent

- Python 3.12 or later, standard library only. Both machines run 3.14.
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

`apply` details: for each member, map the content from `src` to this machine with `paths.localize_bytes`, find the local path with `paths.key_to_path`, copy an existing file to `~/.cache/claude-sync/backup/<run_id>/` first, write through a temporary file and `os.replace`, set mode 600 and the member's mtime. A member name that starts with `conflicts/` is written under `~/.cache/claude-sync/conflicts/<run_id>/`. Each run keeps `backup/<run_id>/manifest.json` with each path it touched and whether a backup exists, so `undo` can restore or remove it.

The inventory lists regular files under the phase 1 items (`model.CLI_ITEMS`, `model.DESKTOP_ITEMS`), skips symlinks and `model.SKIP_NAMES`, and reuses a cached hash when mtime and size are the same.

`~/.cache/claude-sync` and each folder in it have mode 700. Each file that the helper writes has mode 600.

`remote.Runner(host)`: `host=None` runs locally. `call(command, args, stdin=b"") -> bytes` raises `RemoteError` with the stderr on a non-zero exit. SSH options: `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no`.

## JSON output of the cli

See the PRD, section Commands. Exit codes: 0 done or in sync, 1 error, 2 not in sync (`status` only), 3 a decision is needed, 4 the other machine is not reachable, 5 another sync holds the lock.
