# claude-sync

claude-sync is a two-way sync of Claude Code state between two machines over SSH. It syncs the CLI (`~/.claude`) and the Code tab of the Claude desktop app. It also checks the git repos in `~/dev` and tells you what to do with them.

The main user is Claude. You say "sync with the PC", and the `claude-sync` skill runs the tool, asks you about each decision, and reports the result. You can also run it yourself in a terminal.

- PRD: https://claude.ai/code/artifact/fb6210c4-da47-473c-aef3-c2dadf763388
- Build rules and the full merge rules: [docs/contract.md](docs/contract.md)
- Status: phase 1 and phase 2 are done and run on real data.

## Contents

- [Quick start](#quick-start)
- [What syncs](#what-syncs)
- [Commands](#commands)
- [Stops and confirm tokens](#stops-and-confirm-tokens)
- [Exit codes](#exit-codes)
- [How a sync works](#how-a-sync-works)
- [Safety](#safety)
- [Backups, undo and the lock](#backups-undo-and-the-lock)
- [Setup](#setup)
- [Development](#development)
- [Limits](#limits)

## Quick start

Ask Claude on either machine:

> sync with the PC

On the Ubuntu PC, say "sync with the Mac". The skill finds the other machine from `uname -s` (`Darwin` syncs with `ubuntu-pc`, `Linux` syncs with `mac`).

To run it yourself:

```bash
claude-sync status ubuntu-pc
```

```bash
claude-sync ubuntu-pc
```

After a sync, restart the Claude desktop app on both machines. The app reads new sessions and sidebar groups only when it starts.

## What syncs

A sync is two-way. Each file goes in the direction of the change: a file changed on the Mac goes to the PC, and a file changed on the PC comes to the Mac. It does not matter which machine runs the command.

### Files

| Area | Items |
| --- | --- |
| CLI (`~/.claude`) | `CLAUDE.md`, `settings.json`, `skills/`, `agents/`, `commands/`, `plans/`, `projects/` (transcripts), `file-history/` (rewind checkpoints), `uploads/`, `plugins/data/`, and the mode flag files `.i-have-adhd-always` and `.ponytail-active` |
| Desktop app | `claude-code-sessions/` (Code tab sessions) and `scratch-workspaces/` |
| `~/dev` | Files outside git repos (for example notes and proposals in a folder with no `.git`), and `.claude/settings.local.json` in each repo |

Never synced:

- `skills/synced` (the desktop app syncs it from claude.ai for each machine)
- `scheduled-tasks.json` and `.DS_Store`
- secret files (see [Secrets](#secrets))
- in `~/dev`: git repos (git syncs them), symlinks, and dependency or build folders (`node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.next`, `.turbo`, `target`, `.worktrees`, `google-cloud-sdk`)

### Parts

Parts sync data that is not a whole file.

| Part | What it does |
| --- | --- |
| `groups` | Merges the Code tab sidebar groups (`preferences.epitaxyPrefs["dframe-group-scopes"]` in `claude_desktop_config.json`). The other settings in that file stay on each machine. |
| `history` | Merges `~/.claude/history.jsonl` (your prompt history). It keeps the lines of both machines, removes duplicates, and sorts by time. Nothing is deleted. |
| `mcp` | Merges `mcpServers` and the per-project trust and tool-permission fields of `~/.claude.json`. All other keys of that file stay on each machine. |
| `plugins` | Syncs installed plugins and marketplaces as lists. It runs `claude plugin install`, `uninstall`, `marketplace add` or `marketplace remove` on the other machine. |
| `git` | Reads only. Checks each repo in `~/dev` (two levels deep) and its worktrees. See [Git check](#git-check). |
| `secrets` | Reads only. Reports secret files that differ. See [Secrets](#secrets). |
| `update` | On a real sync, runs `~/.local/bin/claude update` on both machines and reports the versions before and after. `status` only shows the versions. |

### Paths between machines

The machines have different home folders (`/Users/reynaldikindarto` and `/home/idkman`) and desktop data folders (`~/Library/Application Support/Claude` and `~/.config/Claude`). claude-sync compares files in a neutral form, where the home becomes `~` and the desktop folder becomes `{desktop}`. When it writes a file, it changes the path fields (for example the `cwd` of a transcript line) to the paths of the target machine.

A project folder name is made from its path, so the same project has different folder names on the two machines. claude-sync finds the matching folder through the `cwd` in the transcripts and writes the file into the correct folder on each machine.

### Git check

The git check fetches each repo on both machines and reads its state. It never commits, pushes or pulls by itself. It gives a `next` command for the skill or for you to run.

| State | What happens |
| --- | --- |
| Clean and in sync | Nothing. |
| Clean and only behind | A `next` fast-forward pull. The skill runs it without asking. |
| Ahead | Stop `git`, with a `next` push command. |
| Uncommitted changes | Stop `git`, with the list of files. |
| Diverged | Stop `git`. |
| Branch with no upstream | Stop `git`. |
| On one machine only | Stop `git`, with a `next` clone command. |
| No remote | A warning only. |

A worktree is reported only when its branch is not pushed. Use `--skip-repo PATH` to leave one repo out, for example a repo with work in progress that you do not want to commit yet.

### Secrets

A secret file is a file whose name matches a pattern such as `.env*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `credentials*.json`, `.npmrc`, `.netrc`, `.git-credentials`, `*.tfvars` or `auth.json`. The full list is `_SECRET_PATTERNS` in [claude_sync/paths.py](claude_sync/paths.py).

claude-sync never copies a secret file. The `secrets` part lists each secret file in `~/dev` or `~/.claude` that is missing on one machine or different, with an `scp` command to copy it. It never prints the contents or a hash. A secret difference does not stop a sync and does not change `in_sync`.

The `mcp` part also skips a server that holds a secret: an `env` value or header named like `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `AUTH`, a password in a URL, a `token=` query value, or a `--token VALUE` style argument.

## Commands

```
claude-sync status HOST [--json] [--all] [--confirm TOKEN] [--keep PATH]... [--skip-repo PATH]...
claude-sync HOST [--json] [--all] [--dry-run] [--confirm TOKEN] [--keep PATH]... [--skip-repo PATH]...
claude-sync undo HOST [--json]
claude-sync unlock HOST [--json]
```

`HOST` is the SSH host of the other machine: `ubuntu-pc` from the Mac, `mac` from the Ubuntu PC.

| Command | What it does |
| --- | --- |
| `status` | Reads both machines and shows what a sync would do. It writes nothing. Exit code 0 when in sync, 2 when not. |
| (no command) | Runs the sync. It writes only when there are no stops. |
| `undo` | Reverts the last run on both machines from its backups. |
| `unlock` | Removes a lock that a crashed run left behind. |

| Flag | Meaning |
| --- | --- |
| `--json` | Prints the full result as JSON. The skill always uses it. |
| `--all` | Lists every file in the review lists. The default shows the first 20 of each list. |
| `--dry-run` | Plans a sync but writes nothing. |
| `--confirm TOKEN` | Confirms the review lists of the last `status`. See [Stops](#stops-and-confirm-tokens). |
| `--keep PATH` | Keeps a file that the sync would delete, and copies it back. `PATH` is a key or a glob from the JSON output, for example `desktop/scratch-workspaces/*`. |
| `--skip-repo PATH` | Leaves one repo out of the git check, for example `~/dev/atomic-platform/atomic-pistons`. |

### JSON output

The main fields of `--json`:

| Field | Meaning |
| --- | --- |
| `in_sync` | `true` when the machines match after the run. |
| `stopped` | The stop reasons. Empty when the run could continue. |
| `files` | Counts: `to_here`, `to_host`, `deletions`, `both_changed`. |
| `review` | For each stop reason, the list of `[side, key]` items to confirm. |
| `token` | The confirm token for the current review lists. |
| `conflicts` | Files that changed on both machines and could not merge. |
| `parts` | The result of each part (`git`, `mcp`, `plugins`, `secrets`, `update` and more). |
| `groups` | The result of the sidebar group merge. |
| `warnings` | Notes. A warning that starts with `error` or `skipped` makes `in_sync` false. |
| `run_id` | The id of the run, for backups and `undo`. |

A key names a file the same way on both machines. It starts with `cli/`, `desktop/` or `dev/`. For a project folder, the key holds the neutral path of the project: `cli/projects/{~/dev/claude-sync}/<session>.jsonl`.

## Stops and confirm tokens

A sync stops with exit code 3 and writes nothing when a decision is needed. The skill asks you, then runs again with the flags you agree to.

| Stop | Cause | How to continue |
| --- | --- | --- |
| `first_run` | No sync state yet. Files on one machine only may be deleted on the other. | Check the list, then `--confirm TOKEN`, with `--keep PATH` for each file to keep. |
| `deletions` | More than 50 deletions, or any deletion in `~/dev`. | Check the list, then `--confirm TOKEN` and `--keep` as needed. |
| `exec_config` | A change to something that runs code: hooks, permission rules, MCP servers, plugins, skills, agents, commands, or a repo's `settings.local.json`. | Check the diff, then `--confirm TOKEN`. |
| `splits` | More than 10 transcripts were continued on both machines. Each one becomes two sessions. | Check the list, then `--confirm TOKEN`. |
| `git` | A repo needs a decision. See [Git check](#git-check). | Run the `next` commands you agree to, or `--skip-repo PATH`. No token clears this stop. |
| `app_open` | The desktop app runs on the other machine. | Quit the app there. |
| `session_open` | A Claude Code CLI session runs on the other machine. | Close the session there. |

The token is the first 12 hex characters of a sha256 hash over all review lists. When a list changes, the token changes, so a token confirms only the exact lists you saw.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done, or in sync. |
| 1 | Error. The `error` field has the cause. |
| 2 | Not in sync (`status` only). |
| 3 | Stopped: a decision is needed. |
| 4 | The other machine is not reachable (off, or not on Tailscale). |
| 5 | Another sync holds the lock. |

## How a sync works

1. Take a lock on both machines.
2. Read the shared state (`~/.cache/claude-sync/state.json`, the same on both machines). It holds the hash of each file at the last sync.
3. List the files on both machines. Hashes use the neutral form, so two copies that differ only in home paths are equal. The list reuses cached hashes, so a `status` takes about 5 seconds.
4. Compare each file to the state (three-way merge):
   - Changed on one machine: copy it to the other.
   - On one machine only, and in the state: it was deleted on the other machine, so delete it. A change wins over a deletion.
   - New on one machine: copy it.
   - Changed on both machines: merge it (see below).
5. Plan each part, and collect the review lists and stops.
6. If there is a stop, report and exit with code 3.
7. Copy the files (tar over SSH), with a backup of each file that is replaced.
8. Apply the parts and the sidebar groups, then save the new state on both machines.

A file changed on both machines:

| Kind | Rule |
| --- | --- |
| Transcript (`.jsonl`) | If one copy starts with all lines of the other, the longer copy wins. Otherwise the session splits: the other copy becomes a new session with a new id, so no work is lost. |
| Session and JSON files | Merged key by key. For a key changed on both machines, the newer file's value wins, with a warning. |
| Other files | The newer copy wins. The other copy goes to `~/.cache/claude-sync/conflicts/<run_id>/` on the machine that runs the sync. |

A transcript that changed in the last 60 seconds is not overwritten, because a session may still write to it.

### Transport

Each helper command runs as `python3 -c` on the other machine over SSH. The code is `model.py`, `paths.py`, `helper.py` and the `h_*.py` files, joined, compressed with zlib and base64-encoded. Nothing needs to be installed on the other machine for this, and it always runs the same code as the local copy. The helper must run on Python 3.9, because a non-interactive SSH login on the Mac gets `/usr/bin/python3` 3.9.6.

## Safety

- **Confirm before change.** A change to deletions, executable config or split sessions needs a token that matches the lists you saw.
- **The skill asks you.** It never adds `--confirm`, `--keep` or `--skip-repo` without your yes in the conversation. It runs `undo` or `unlock` only when you ask.
- **Secrets stay put.** Secret files are reported only. See [Secrets](#secrets).
- **Safe keys.** `pack`, `apply` and `delete` refuse a key with `..`, a leading `/`, or a root other than `cli`, `desktop` or `dev`. `apply` checks that the real target path stays under its root.
- **Values are quoted.** Each value in a `next` git command is quoted with `shlex.quote`. A value from the other machine (a clone URL, a plugin id) comes after `--`, so it cannot be read as an option.
- **SSH options.** `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ForwardAgent=no`.
- **No races.** Each write checks that the target still has the mtime and size that the plan read. A file that changed during the run is skipped with a warning.
- **One permission rule.** The installer allows only `Bash(claude-sync status:*)` in `~/.claude/settings.json`. A real sync always needs your approval in the permission prompt.

## Backups, undo and the lock

| Path | Content |
| --- | --- |
| `~/.cache/claude-sync/state.json` | The shared state. |
| `~/.cache/claude-sync/backup/<run_id>/` | The old copy of each file that a run replaced or deleted, and `manifest.json`. Kept for 14 days. |
| `~/.cache/claude-sync/conflicts/<run_id>/` | The losing copy of each conflict. |
| `~/.cache/claude-sync/lock.json` | The lock of a running sync. |

The folder has mode 700, and each file in it has mode 600.

`claude-sync undo HOST` restores the files of the last run on both machines. It skips a file that changed after the run, and backs up the current file before it restores. For `~/.claude.json`, undo works only if Claude Code has not written that file since the run.

The lock holds the pid and start time of the run. A run that finds a lock from a dead process takes it. Use `claude-sync unlock HOST` only when a run crashed and the lock stays.

## Setup

Requirements on both machines:

- Python 3.12 or later for the CLI (both machines have 3.14). The other machine also needs a `python3` of 3.9 or later on its SSH PATH.
- Key-based SSH in both directions, with the host key of the other machine in `known_hosts`. Here: `ssh ubuntu-pc` from the Mac and `ssh mac` from the Ubuntu PC, over Tailscale. On the Mac, turn on Remote Login.
- Claude Code installed at `~/.local/bin/claude` (for the `update` and `plugins` parts).
- A clone of this repo at `~/dev/claude-sync`.

Install on each machine:

```bash
~/dev/claude-sync/install.sh
```

The installer can run more than one time. It:

1. Links `~/.local/bin/claude-sync` to the `claude-sync` script.
2. Links `~/.claude/skills/claude-sync` to `skill/`.
3. Makes `~/.cache/claude-sync` with mode 700.
4. Adds `Bash(claude-sync status:*)` to the allow list in `~/.claude/settings.json`. It changes no other key.

To update the tool, run `git pull` in the repo on both machines. The links point into the repo, so no new install is needed.

## Development

Standard library only. Run the tests:

```bash
python3 -m unittest discover -s tests
```

The tests use temporary folders and fake machines. They never touch the real `~/.claude`, `~/.cache`, desktop data or `~/dev`, never run `ssh`, and never run the real `claude` binary. The `CLAUDE_SYNC_TEST_ROOTS` environment variable points the CLI at test folders.

| File | Role |
| --- | --- |
| `claude-sync` | Entry point. |
| `claude_sync/cli.py` | Commands, the run order, stops, output. |
| `claude_sync/merge.py` | Three-way plan, transcript resolution, JSON merge. |
| `claude_sync/paths.py` | Neutral paths, keys, file kinds, hashes, secret patterns. |
| `claude_sync/model.py` | Shared types and the lists of synced items. |
| `claude_sync/helper.py` | Helper commands that run on each machine: inventory, pack, apply, delete, lock, state, undo, prune. |
| `claude_sync/remote.py` | Runs a helper command locally or over SSH. |
| `claude_sync/groups.py` | Sidebar group merge. |
| `claude_sync/part.py` | The `Ctx` object that each part gets. |
| `claude_sync/p_<name>.py` | A part: `plan(ctx)` reads, `apply(ctx, plan)` writes. |
| `claude_sync/h_<name>.py` | The helper commands of a part. |
| `skill/SKILL.md` | The steps Claude follows. |
| `docs/contract.md` | The full rules. |

To add a part, see "Phase 2 parts" in [docs/contract.md](docs/contract.md). Add the module to `cli.PART_MODULES`. A new helper file must run on Python 3.9.

## Limits

- Two machines only. The tool syncs "here" with one host.
- Manual only. There is no background sync and no hook.
- The desktop app must be closed on the other machine, and the app must restart to show new sessions and groups.
- Transcripts and file-history can hold secrets that you typed or that a tool read. They sync as they are.
- An edit that only changes the form of a home path (for example `/Users/reynaldikindarto/dev/x` to `~/dev/x`) has the same hash and does not sync.
- In a transcript resolved by metadata only, the losing copy's extra metadata lines (for example a session rename) are dropped.
- `update` updates only the native CLI at `~/.local/bin/claude`. The desktop app updates its own copy of Claude Code.
