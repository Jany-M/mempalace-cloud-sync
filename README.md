# MemPalace Cloud Sync

Version 1.0.0

Bidirectional sync for [MemPalace](https://github.com/MemPalace/mempalace) across multiple machines, via your preferred Cloud Storage provider (Dropbox, Google Drive, OneDrive etc.).

> Don't have MemPalace yet?
> Here's a [guide on how to setup MemPalace](https://www.shambix.com/ai-persistent-memory-mempalace-guide-claude-lmstudio-cursor-vscode/) on your machine and connect it to your favorite IDEs and AI tools.

---

## How it works

MemPalace stores memories in ChromaDB - a binary HNSW index plus SQLite.
Dropbox transfers files non-atomically, which can silently corrupt indexes if you sync raw DB files directly.

This tool does **not** sync your live palace DB files. Instead, each `push` writes a machine-local **snapshot export** into the shared folder, and each `pull` merges snapshots from other machines into your local palace.

Current snapshot format:

- Chroma collections exported to JSONL (`drawers`, `closets`)
- Supporting state files copied into the snapshot (`knowledge_graph.sqlite3`, `tunnels.json`, `hallways.json`, `known_entities.json`, `entity_registry.json`, origin metadata)
- `manifest.json` describing the snapshot and record counts

Each machine writes to its own subfolder:

```
E:\Dropbox\mempalace-sync\
├── export-home-pc\
├── export-work-laptop\
└── .mp_sync_meta.json
```

On `pull`, each machine merges from **all other machines' `export-*` folders**.

Merge behavior:

- Existing identical records are skipped
- New records are inserted
- ID collisions with different content are preserved by creating conflict-safe IDs
- JSON state files are merged additively (no destructive overwrite)
- Knowledge graph tables merge with `INSERT OR IGNORE`

**End result: every machine converges to the union of all memories.**

Logging behavior:

- `python mp_sync.py sync` prints progress to terminal (no log append)
- `python mp_sync.py sync --quiet` suppresses terminal output and writes sync log lines to `<sync_folder>/sync.log`
- `sync --quiet` logs push, and logs pull when other machine exports are available to pull

### Pull safety and rollback

Before merge, `pull` creates a temporary backup of all local mempalace local state it may mutate.
After merge, it runs health checks:

- Collection open/count
- Optional HNSW divergence probe
- SQLite `PRAGMA quick_check` for the knowledge graph

If anything fails, local state is restored immediately from backup.
If all checks pass, backup is deleted.

---

## Requirements

- Python 3.13.+
- MemPalace 3.3.5+ installed on the machine, either as a Python package (`pip`) or as a CLI tool (`uv tool install mempalace`).
- A shared folder all machines can access (just the same Dropbox, OneDrive, etc. folder on all machines)

---

## Setup - once per machine

1. Clone or [download](https://github.com/Jany-M/mempalace-cloud-sync/archive/refs/heads/main.zip) this repo, and keep it wherever is convenient, on each machine (doesn't need be shared, but you can if you want).

2. Create a new subfolder in your preferred Cloud Storage directory, once (e.g. `E:\Dropbox\mempalace-sync`), let it sync to all your machines (each one may have its own path to it, that's ok), and make sure it is **always available offline** on all machines.

3. Run the sync setup, on each machine, once:

```
python mp_sync.py setup
```

You will be prompted for:

| Field | What to enter | Example |
|---|---|---|
| `sync_folder` | E.g. shared cloud folder for `export-*` snapshots | `E:\Dropbox\mempalace-sync` |
| `palace_path` | Local MemPalace palace directory | `C:\Users\you\.mempalace\palace` |
| `machine_name` | Short machine label | `home-pc`, `work-laptop` |

Config is then saved to `~/.mempalace/mp_sync_config.json`.

> At the end of setup, make a mental note of what `Hook launcher`says, since the hook command to use below, depends on it.
> If you installed Mempalace through `pip`, then you can use the default python commands below in your hooks, if you installed it with `uv`, then you'll need to replace the command with a different string.

Prompt behavior:

- If a real default is detected, pressing Enter uses it
- If only an example is shown, you must type a value

Important:

- `sync_folder` should point to the same shared cloud folder on all machines, even if the local path differs per machine
- `machine_name` must be unique per machine
- Setup accepts a fresh/empty palace path and will create the palace directory if it does not exist yet
- Setup detects MemPalace either as an importable Python package or as an installed `mempalace` CLI tool (for example from `uv tool install mempalace`)
- Use the hook launcher printed at the end of setup.

### Fresh machine bootstrap (empty .mempalace is OK)

If machine 2 just installed MemPalace and has no palace DB yet, you do not need to initialize a local palace first.

1. On machine 1 (the one with existing memories), run `push`.
2. On machine 2 (fresh/empty palace), run `pull`.
3. After the first pull, use `sync` normally on both machines.

If setup cannot detect a usable MemPalace runtime/launcher, initialize/fix MemPalace first:

- Getting started: https://mempalaceofficial.com/guide/getting-started.html
- Repository: https://github.com/MemPalace/mempalace

---

## Manual sync

One command for full sync, `push` & `pull`.

This prints progress/status to terminal.
```
python mp_sync.py sync
```

This suppresses terminal output and writes sync log lines.
```
python mp_sync.py sync --quiet 
```

Or run separately:

```bash
# After a session - write this machine snapshot to shared folder
python mp_sync.py push

# Before a session - merge snapshots from other machines
python mp_sync.py pull
```

Check state (shows machine folders, record counts, and last push/pull metadata):

```
python mp_sync.py status
```

Remove accumulated `.drift-*` HNSW quarantine dirs (push does this automatically; run manually if needed):

```
python mp_sync.py clean
```

---

## Automation

Depending on what Hook launcher showed during setup (depends on how MemPalace was installed), your hook command will differ from the examples below:

- If you installed MemPalace with Python pip / venv, use: `python C:\PATH\TO\THIS\REPO\mempalace-cloud-sync\mp_sync.py sync --quiet`
- If with uv-managed environment, use: `uv run --with mempalace python C:\PATH\TO\THIS\REPO\mempalace-cloud-sync\mp_sync.py sync --quiet`

### Claude Code hooks

Claude Code uses Claude-style hook config in `~/.claude/settings.json` (Windows: `C:\Users\YOU\.claude\settings.json`).
Edit the command path directly for your machine.

Use the exact command printed by `python mp_sync.py setup` under `Hook launcher:`.
If setup printed a `uv run --with mempalace ...` command, replace the example `python ...` command below with that uv command everywhere in your hook configs.

Example using plain Python:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py sync --quiet"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py sync --quiet"
          }
        ]
      }
    ]
  }
}
```

Notes:

- `SessionStart` and `SessionEnd` are Claude Code session lifecycle events.
- However, `SessionEnd` only fires on a clean Claude Code session exit (for example `/exit`, `Ctrl+D`, or quitting through the menu), and *does not exist* at all in Claude Code *CLI*.
- If you use Claude Code CLI regularly, `SessionEnd` will not fire, so the final push will be skipped.
- Alternative 1: use Claude Code `/schedule` for periodic syncs (works, but each run uses tokens).
- Alternative 2: add a `Stop` hook as a fallback (token-free, but can add small latency because it runs after each agent response).
- Alternative 3: use a OS task scheduler instead of hooks (for Windows, see `Auto Sync every 60 min` in `Alternative Sync Methods` below).
- **Claude Cowork** doesn't have hooks, so look into its *Scheduled Tasks* or use an OS task scheduler.

### Cursor hooks

Use `.cursor/hooks.json` (project) or `~/.cursor/hooks.json` (user):

Replace the example command below with the exact command printed by setup under `Hook launcher:`.
If setup printed a uv command, use that full uv command here instead of the example `python ...` command.

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [
      {
        "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py pull --quiet"
      }
    ],
    "sessionEnd": [
      {
        "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py push --quiet"
      }
    ]
  }
}
```

Notes:

- Cursor hook names are lowercase, such as `sessionStart` and `sessionEnd`.
- Cursor also supports `stop`, but that is a different hook event from `sessionEnd`.

### VS Code hooks

VS Code can read Claude-style hook files by default, including `.claude/settings.json`, `.claude/settings.local.json`, and `~/.claude/settings.json`.
It also supports workspace hook files such as `.github/hooks/mempalace-cloud-sync.json`.

Replace the example command below with the exact command printed by setup under `Hook launcher:`.
If setup printed a uv command, use that full uv command here instead of the example `python ...` command.

If you want a VS Code-specific hook file, use:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py pull --quiet"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py push --quiet"
      }
    ]
  }
}
```

Notes:

- VS Code does not use `SessionEnd` in its documented preview lifecycle; `Stop` is the end-of-agent-session hook.
- VS Code can often reuse Claude-style hook files, but its preview hook behavior is not identical to Claude Code in every detail.

### Provider references

- Claude Code hooks: <https://code.claude.com/docs/en/hooks>
- Claude Desktop Scheduled Tasks: <https://code.claude.com/docs/en/desktop-scheduled-tasks>
- Cursor hooks: <https://cursor.com/docs/hooks>
- VS Code hooks overview: <https://code.visualstudio.com/learn/customizations/5-hooks>
- VS Code hooks reference (preview): <https://code.visualstudio.com/docs/agent-customization/hooks>

## Alternative Sync Methods examples (Windows Task Scheduler)

### Auto Pull on Windows login

1. Open Task Scheduler -> Create Basic Task
2. Trigger: When I log on
3. Action: Start a program
4. Use the same launcher that setup printed under `Hook launcher`
5. If setup printed a Python command, use `python` with arguments like `C:\PATH\TO\THIS\REPO\mempalace-cloud-sync\mp_sync.py pull --quiet`
6. If setup printed a uv command, use `uv` with arguments like `run --with mempalace python C:\PATH\TO\THIS\REPO\mempalace-cloud-sync\mp_sync.py pull --quiet`

## Auto Sync every 60 min

Run in Powershell, adjust minutes how you want, e.g. 15min with the Python command:

```
schtasks /create /tn "MemPalace Sync" /tr "python C:\PATH\TO\THIS\REPO\mempalace-cloud-sync\mp_sync.py sync --quiet" /sc minute /mo 60 /f
```

---

## Sync contents after first push (on shared cloud folder)

```
E:\Dropbox\mempalace-sync\
├── export-home-pc\
│   └── snapshot\
│       ├── manifest.json
│       ├── collections\
│       │   ├── drawers.jsonl
│       │   └── closets.jsonl
│       └── files\
│           ├── knowledge_graph.sqlite3
│           ├── tunnels.json
│           ├── hallways.json
│           ├── known_entities.json
│           ├── entity_registry.json
│           └── origin.json
├── .mp_sync_meta.json
└── sync.log              (present when hooks use --quiet)
```

`.jsonl` files are plain text (one JSON object per line).

---

## Known Issues

### Recurring HNSW drift after ungraceful process shutdown

**What it is:** MemPalace keeps memories in two places: a SQLite database (crash-safe via WAL) and a binary HNSW vector index on disk. When the MemPalace MCP server is killed without a clean shutdown — e.g. when a Claude Code session ends abruptly or the OS terminates the process — SQLite is safely flushed, but the HNSW binary may not be fully written. On the next startup the runtime detects the mismatch and auto-rebuilds the HNSW from SQLite, quarantining the stale index as a `.drift-<timestamp>` directory. **No memory data is ever lost** — SQLite is always the authoritative source of truth.

**When you see it:** During `push` or `pull` you may see messages like:
```
Quarantined corrupt HNSW segment ... (sqlite 373s newer than HNSW and integrity check failed)
```

**Effect on sync:** `push` exports from SQLite (not HNSW), so the snapshot is always complete regardless of HNSW drift. Export counts are cross-checked against SQLite and surfaced in the manifest. `.drift-*` dirs are cleaned up automatically on every push (keeping the 2 most recent per segment as a rollback backup). You can also run `python mp_sync.py clean` to housekeep them manually.

**Auto-mitigation built into mp_sync:** Before every push and pull, `mp_sync.py` runs `mempalace repair-status` to detect drift without opening a ChromaDB client. If any segment shows `DRIFTED` status, it automatically runs `mempalace repair --yes` to rebuild the HNSW from SQLite before exporting. This means by the time `push` writes the snapshot, HNSW and SQLite are always in sync.

**Root fix:** The MemPalace server needs graceful `SIGTERM`/`SIGINT` handling to flush HNSW before exit. Until that is implemented upstream, the auto-repair step in mp_sync handles it transparently.

---

## Troubleshooting

**Runtime import failed for MemPalace modules**
Install/repair MemPalace in the same Python environment used to run `mp_sync.py`.

**"No other machines have pushed yet"**
Other machines have not run `push`, or shared-folder sync has not completed.

**"Skipping <machine>: no snapshot manifest found"**
That machine folder is incomplete or stale. Re-run `push` on that machine.

**Two machines pushed at the same time**
Safe. Each machine writes only to its own `export-{name}/` folder.

**"Pull failed, restored local state from backup"**
Rollback safety was triggered correctly. Fix incoming snapshot or local environment issues,
then run `pull` again.

**Windows encoding errors**
Set environment variable `PYTHONIOENCODING=utf-8` in the hook command:
```
"command": "cmd /c \"set PYTHONIOENCODING=utf-8 && python C:\\PATH\\TO\\THIS\\REPO\\mempalace-cloud-sync\\mp_sync.py sync --quiet\""
```

---

## Author

Jany Martelli

- https://www.shambix.com
- info@shambix.com
- https://www.linkedin.com/in/janymartelli/

---

Check out our latest product: [Patcherly](https://patcherly.com), catch live production bugs & fix them in real time, in seconds.