#!/usr/bin/env python3
"""
mp_sync.py — MemPalace bidirectional sync via a shared folder (Dropbox, network drive, git, etc.)

MemPalace stores memories in ChromaDB (binary HNSW index + SQLite). Syncing those raw
files via Dropbox/OneDrive is unsafe — non-atomic transfers corrupt the index silently.

This tool writes per-machine snapshot exports into a shared folder instead. Each snapshot
contains JSONL exports of the relevant Chroma collections plus supporting palace state
files. On pull, a machine merges snapshots from all OTHER machines' export folders.
Result: every machine converges to the union of all memories, additively, in any order.

Usage:
  python mp_sync.py setup          Interactive setup — writes ~/.mempalace/mp_sync_config.json
    python mp_sync.py push           Write this machine's snapshot → sync_folder/export-{name}/
    python mp_sync.py pull           Merge all other machines' snapshots into local palace
  python mp_sync.py sync           push + pull in one step (recommended daily driver)
  python mp_sync.py status         Show last sync times, drawer counts per machine
    python mp_sync.py sync --quiet   Silent sync with sync.log entries for automation

Config file: ~/.mempalace/mp_sync_config.json
  Edit directly or run `setup`. See config.example.json for all options.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".mempalace" / "mp_sync_config.json"
SYNC_META_FILE = ".mp_sync_meta.json"


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def require(cfg: dict, key: str) -> str:
    val = cfg.get(key)
    if not val:
        print("[mp_sync] Not configured. Run:  python mp_sync.py setup")
        sys.exit(1)
    return val


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _find_drift_dirs(palace_path: str) -> list[Path]:
    """Return all .drift-* quarantined HNSW segment directories in the palace."""
    palace = Path(palace_path).expanduser().resolve()
    return sorted(palace.glob("*.drift-*"))


def _sqlite_collection_counts(palace_path: str) -> dict[str, int]:
    """Read authoritative embedding counts per collection directly from ChromaDB's SQLite.

    Returns {} on any error so callers can treat it as an optional cross-check.
    """
    db = Path(palace_path).expanduser().resolve() / "chroma.sqlite3"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT c.name, COUNT(e.id) "
            "FROM embeddings e "
            "JOIN segments s ON e.segment_id = s.id "
            "JOIN collections c ON s.collection = c.id "
            "WHERE s.scope = 'METADATA' "
            "GROUP BY c.name"
        ).fetchall()
        conn.close()
        return {name: count for name, count in rows}
    except Exception:
        return {}


def _check_hnsw_health(palace_path: str) -> dict[str, str]:
    """Run `mempalace repair-status` without opening a ChromaDB client.

    Returns {collection_name: status} where status is one of OK / DRIFTED / UNKNOWN.
    Returns {} if the mempalace binary is not on PATH or the command fails.
    """
    mempalace_bin = shutil.which("mempalace")
    if not mempalace_bin:
        return {}
    try:
        r = subprocess.run(
            [mempalace_bin, "--palace", palace_path, "repair-status"],
            capture_output=True, text=True, timeout=30,
        )
        statuses: dict[str, str] = {}
        current: str | None = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
            elif line.startswith("status:") and current:
                statuses[current] = line.split(":", 1)[1].strip()
        return statuses
    except Exception:
        return {}


def _repair_hnsw_if_drifted(palace_path: str, quiet: bool) -> bool:
    """Check HNSW health and run `mempalace repair --yes` for any DRIFTED segment.

    Returns True if no repair was needed, or repair ran and succeeded.
    Returns False if repair was needed but failed (caller should still export from SQLite
    and rely on count validation to catch any data loss).
    """
    health = _check_hnsw_health(palace_path)
    if not health:
        return True  # binary not available — skip, runtime will auto-heal on client open

    drifted = [col for col, status in health.items() if status == "DRIFTED"]
    if not drifted:
        return True

    if not quiet:
        print(
            f"[mp_sync] HNSW drift detected for: {', '.join(drifted)} — "
            f"running `mempalace repair --yes` before export"
        )

    mempalace_bin = shutil.which("mempalace")
    if not mempalace_bin:
        return False

    try:
        r = subprocess.run(
            [mempalace_bin, "--palace", palace_path, "repair", "--yes"],
            timeout=180,
        )
        if r.returncode == 0:
            if not quiet:
                print("[mp_sync] ✓ HNSW rebuilt from SQLite")
            return True
        if not quiet:
            print(f"[mp_sync] Warning: `mempalace repair` exited {r.returncode} — export will proceed from SQLite")
        return False
    except Exception as exc:
        if not quiet:
            print(f"[mp_sync] Warning: `mempalace repair` failed: {exc} — export will proceed from SQLite")
        return False


def _cleanup_old_drift_dirs(palace_path: str, keep: int = 2, quiet: bool = False) -> int:
    """Remove old .drift-* dirs, keeping only the `keep` most recent per segment UUID.

    Returns the number of directories removed.
    """
    palace = Path(palace_path).expanduser().resolve()
    by_segment: dict[str, list[Path]] = {}
    for d in palace.glob("*.drift-*"):
        uuid = d.name.split(".drift-")[0]
        by_segment.setdefault(uuid, []).append(d)

    removed = 0
    for _uuid, dirs in by_segment.items():
        dirs.sort()  # lexicographic = chronological for the timestamp suffix
        to_remove = dirs[:-keep] if len(dirs) > keep else []
        for d in to_remove:
            try:
                shutil.rmtree(d)
                removed += 1
            except Exception as exc:
                if not quiet:
                    print(f"[mp_sync] Warning: could not remove drift dir {d.name}: {exc}")

    if removed and not quiet:
        print(f"[mp_sync] Cleaned up {removed} old HNSW drift segment(s)")
    return removed


def run(cmd: list, check=True, quiet=False) -> subprocess.CompletedProcess:
    if not quiet:
        print(f"[mp_sync] $ {' '.join(str(c) for c in cmd)}")
    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None
    return subprocess.run(cmd, check=check, stdout=stdout, stderr=stderr)


def _fatal(msg: str, code: int = 1):
    print(f"[mp_sync] {msg}")
    sys.exit(code)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def _json_dumps_stable(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _machine_name_from_export_dir(path: Path) -> str:
    name = path.name
    if name.startswith("export-"):
        return name.replace("export-", "", 1) or "unknown"
    return "unknown"


def _safe_read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        for row in rows:
            tmp.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _read_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _load_mempalace_runtime():
    try:
        from mempalace.backends.chroma import ChromaBackend
        from mempalace.config import MempalaceConfig
    except Exception as exc:
        hint = ""
        if _has_mempalace_tool():
            hint = (
                "\n[mp_sync] Detected a mempalace CLI tool on PATH. "
                "If this came from uv tool install, run sync via: "
                "uv run --with mempalace python mp_sync.py <command>"
            )
        _fatal(
            "MemPalace runtime import failed. Install/repair mempalace in this Python environment."
            f"\n[mp_sync] Details: {exc}{hint}",
            code=2,
        )
    return ChromaBackend, MempalaceConfig


def _has_mempalace_runtime() -> bool:
    return (
        importlib.util.find_spec("mempalace") is not None
        and importlib.util.find_spec("mempalace.config") is not None
        and importlib.util.find_spec("mempalace.backends.chroma") is not None
    )


def _has_mempalace_tool() -> bool:
    return shutil.which("mempalace") is not None


def _ensure_mempalace_available_for_setup():
    if _has_mempalace_runtime():
        return

    if _has_mempalace_tool():
        if shutil.which("uv") is not None:
            print("[mp_sync] Found mempalace on PATH.")
            print("[mp_sync] Setup can continue using a uv tool install workflow.")
            return
        print("[mp_sync] Found mempalace on PATH, but uv is not available for runner resolution.")
        print("[mp_sync] Installing mempalace into this Python environment is recommended for this setup.")

    if shutil.which("uv") is not None:
        print("[mp_sync] MemPalace is not importable in this Python environment.")
        print("[mp_sync] Tip: if you use uv tools, run: uv tool install mempalace")
    else:
        print("[mp_sync] MemPalace is not installed in this Python environment.")
    print(f"[mp_sync] Python: {sys.executable}")
    answer = input("[mp_sync] Install/upgrade mempalace now? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        _fatal(
            "Setup stopped. Install mempalace into the same Python environment that runs mp_sync.py, then rerun setup."
        )

    try:
        run([sys.executable, "-m", "pip", "install", "--upgrade", "mempalace"], check=True, quiet=False)
    except subprocess.CalledProcessError:
        _fatal("MemPalace installation failed. Fix pip/Python environment and rerun setup.")

    if not _has_mempalace_runtime():
        _fatal("MemPalace still could not be imported after install. Check this Python environment.")

    print("[mp_sync] ✓ MemPalace is installed and ready for setup\n")


def _looks_like_mempalace_palace(palace_path: str) -> bool:
    palace = Path(palace_path).expanduser().resolve()
    if not palace.exists() or not palace.is_dir():
        return False

    root = palace.parent
    evidence_files = (
        root / "config.json",
        root / "knowledge_graph.sqlite3",
        palace / ".mempalace" / "origin.json",
    )
    return any(path.exists() for path in evidence_files)


def _prompt_for_palace_path(default_palace: str) -> str:
    if default_palace and not Path(default_palace).expanduser().exists():
        print("[mp_sync] The detected palace path does not exist yet. You can still use it for a fresh palace.")
    elif default_palace and not _looks_like_mempalace_palace(default_palace):
        print(
            "[mp_sync] The detected palace path does not look populated yet. This is OK for a fresh install."
        )

    while True:
        if default_palace:
            palace_path = prompt_with_default("Palace path", default_palace)
        else:
            palace_path = prompt_with_default(
                "Palace path",
                "",
                example=r"C:\Users\you\.mempalace\palace",
            )

        if not palace_path:
            print("[mp_sync] A palace path is required.")
            continue

        palace = Path(palace_path).expanduser().resolve()
        if palace.exists() and not palace.is_dir():
            print("[mp_sync] Palace path must be a directory.")
            continue
        if not palace.exists():
            try:
                palace.mkdir(parents=True, exist_ok=True)
                print(f"[mp_sync] Created palace directory: {palace}")
            except Exception as exc:
                print(f"[mp_sync] Could not create palace directory: {exc}")
                continue

        if _looks_like_mempalace_palace(palace_path):
            return str(palace)

        print("[mp_sync] Palace path accepted (fresh or not yet populated).")
        return str(palace)


def _recommended_hook_command() -> str:
    return _hook_launcher_details()[0]


def _hook_launcher_details() -> tuple[str, str]:
    script_path = Path(__file__).resolve()
    if _has_mempalace_runtime():
        return (
            f'python "{script_path}" sync --quiet',
            "Detected MemPalace as a Python package import in this interpreter.",
        )
    if _has_mempalace_tool() and shutil.which("uv") is not None:
        return (
            f'uv run --with mempalace python "{script_path}" sync --quiet',
            "Detected MemPalace as a CLI tool on PATH with uv available.",
        )
    return (
        f'python "{script_path}" sync --quiet',
        "Falling back to the Python launcher because no uv-based tool workflow was detected.",
    )


def _mempalace_paths(palace_path: str) -> dict[str, Path]:
    palace = Path(palace_path).expanduser().resolve()
    root = palace.parent
    return {
        "palace": palace,
        "drawers_name_file": root / "config.json",
        "knowledge_graph": root / "knowledge_graph.sqlite3",
        "tunnels": root / "tunnels.json",
        "hallways": root / "hallways.json",
        "known_entities": root / "known_entities.json",
        "entity_registry": root / "entity_registry.json",
        "origin": palace / ".mempalace" / "origin.json",
    }


def _snapshot_dir(export_dir: Path) -> Path:
    return export_dir / "snapshot"


def _snapshot_manifest_path(export_dir: Path) -> Path:
    return _snapshot_dir(export_dir) / "manifest.json"


def _snapshot_collections_dir(export_dir: Path) -> Path:
    return _snapshot_dir(export_dir) / "collections"


def _snapshot_files_dir(export_dir: Path) -> Path:
    return _snapshot_dir(export_dir) / "files"


def _export_collection_records(backend, palace_path: str, collection_name: str) -> tuple[list[dict], int | None]:
    """Export all records from a ChromaDB collection.

    Returns (rows, expected_count) where expected_count is from col.count() taken
    before iteration — a mismatch signals the collection changed mid-export.
    """
    try:
        col = backend.get_collection(palace_path, collection_name, create=False)
    except Exception:
        return [], None

    try:
        expected = col.count()
    except Exception:
        expected = None

    rows: list[dict] = []
    offset = 0
    batch = 1000
    while True:
        result = col.get(limit=batch, offset=offset, include=["documents", "metadatas"])
        ids = result.get("ids") or []
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        if not ids:
            break
        for doc_id, doc, meta in zip(ids, docs, metas):
            rows.append({"id": doc_id, "document": doc, "metadata": meta or {}})
        if len(ids) < batch:
            break
        offset += len(ids)

    return rows, expected


def _copy_if_exists(src: Path, dst: Path):
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _normalize_json_for_merge(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return {"value": value}


def _merge_json(existing, incoming):
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        changed = False
        for key, val in incoming.items():
            if key not in merged:
                merged[key] = val
                changed = True
                continue
            new_val, sub_changed = _merge_json(merged[key], val)
            if sub_changed:
                merged[key] = new_val
                changed = True
        return merged, changed

    if isinstance(existing, list) and isinstance(incoming, list):
        seen = {json.dumps(v, sort_keys=True, ensure_ascii=False) for v in existing}
        out = list(existing)
        changed = False
        for item in incoming:
            sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(item)
            changed = True
        return out, changed

    if existing == incoming:
        return existing, False
    # Preserve local scalar/type to avoid breaking schema-sensitive files.
    return existing, False


def _merge_json_file_additive(target_path: Path, incoming_path: Path):
    incoming = _safe_read_json(incoming_path)
    if incoming is None:
        return False
    if not target_path.exists():
        _atomic_write_text(target_path, json.dumps(incoming, indent=2, ensure_ascii=False))
        return True

    existing = _safe_read_json(target_path)
    if existing is None:
        return False
    merged, changed = _merge_json(existing, incoming)
    if changed:
        _atomic_write_text(target_path, json.dumps(merged, indent=2, ensure_ascii=False))
    return changed


def _merge_named_list_file(target_path: Path, incoming_path: Path, list_key: str):
    incoming_raw = _safe_read_json(incoming_path)
    if incoming_raw is None:
        return False
    target_raw = _safe_read_json(target_path) if target_path.exists() else None

    def extract_list(raw):
        if isinstance(raw, list):
            return raw, {"schema_version": 1}
        if isinstance(raw, dict):
            return raw.get(list_key, []) or [], {
                "schema_version": raw.get("schema_version", 1),
            }
        return [], {"schema_version": 1}

    in_list, in_meta = extract_list(incoming_raw)
    cur_list, cur_meta = extract_list(target_raw)

    by_key: dict[str, dict] = {}
    for row in cur_list:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id") or _sha8(_json_dumps_stable(row))
        by_key[row_id] = row

    changed = False
    for row in in_list:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id") or _sha8(_json_dumps_stable(row))
        if row_id in by_key:
            continue
        by_key[row_id] = row
        changed = True

    if not target_path.exists() or changed:
        schema_version = max(int(cur_meta.get("schema_version", 1)), int(in_meta.get("schema_version", 1)))
        payload = {
            "schema_version": schema_version,
            list_key: list(by_key.values()),
        }
        _atomic_write_text(target_path, json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    return False


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _merge_knowledge_graph_additive(target_db: Path, incoming_db: Path):
    if not incoming_db.exists():
        return 0
    if not target_db.exists():
        target_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(incoming_db, target_db)
        return 1

    inserted_tables = 0
    conn = sqlite3.connect(str(target_db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("ATTACH DATABASE ? AS incoming", (str(incoming_db),))

        src_tables = conn.execute(
            "SELECT name, sql FROM incoming.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        for table_name, create_sql in src_tables:
            if not table_name:
                continue

            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not exists and create_sql:
                conn.execute(create_sql)

            cols = [r[1] for r in conn.execute(f"PRAGMA incoming.table_info('{table_name}')").fetchall()]
            if not cols:
                continue
            quoted_cols = ", ".join([_quote_ident(c) for c in cols])
            quoted_table = _quote_ident(table_name)
            conn.execute(
                f"INSERT OR IGNORE INTO {quoted_table} ({quoted_cols}) "
                f"SELECT {quoted_cols} FROM incoming.{quoted_table}"
            )
            inserted_tables += 1

        conn.commit()
    finally:
        try:
            conn.execute("DETACH DATABASE incoming")
        except Exception:
            pass
        conn.close()

    return inserted_tables


def _collection_record_conflict_id(base_id: str, source_machine: str, document: str) -> str:
    suffix = _sha8(f"{source_machine}|{document}")
    return f"{base_id}__from_{source_machine}_{suffix}"


def _backup_impacted_state(palace_path: str) -> dict:
    """Create a temporary backup for all local state this sync may mutate."""
    root = Path(tempfile.mkdtemp(prefix="mp_sync_pull_backup_"))
    paths = _mempalace_paths(palace_path)

    sources = {
        "palace_dir": paths["palace"],
        "knowledge_graph": paths["knowledge_graph"],
        "tunnels": paths["tunnels"],
        "hallways": paths["hallways"],
        "known_entities": paths["known_entities"],
        "entity_registry": paths["entity_registry"],
        "origin": paths["origin"],
    }

    manifest = {"root": str(root), "items": {}}
    for key, src in sources.items():
        src = Path(src)
        if not src.exists():
            continue

        dst = root / key
        if src.is_dir():
            shutil.copytree(src, dst)
            manifest["items"][key] = {
                "kind": "dir",
                "source": str(src),
                "backup": str(dst),
            }
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            manifest["items"][key] = {
                "kind": "file",
                "source": str(src),
                "backup": str(dst),
            }

    return manifest


def _restore_impacted_state(backup: dict):
    """Restore local state from a backup created by _backup_impacted_state."""
    items = (backup or {}).get("items", {})
    for _, spec in items.items():
        src = Path(spec["source"])
        bak = Path(spec["backup"])
        kind = spec.get("kind")
        if not bak.exists():
            continue

        if src.exists():
            if src.is_dir():
                shutil.rmtree(src, ignore_errors=True)
            else:
                try:
                    src.unlink()
                except Exception:
                    pass

        if kind == "dir":
            shutil.copytree(bak, src)
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, src)


def _cleanup_backup(backup: dict):
    root = (backup or {}).get("root")
    if root:
        shutil.rmtree(root, ignore_errors=True)


def _post_pull_health_check(palace_path: str) -> tuple[bool, str]:
    """Run lightweight integrity checks after pull merge."""
    try:
        ChromaBackend, MempalaceConfig = _load_mempalace_runtime()
        backend = ChromaBackend()
        cfg = MempalaceConfig()
        collection_name = getattr(cfg, "collection_name", "mempalace_drawers") or "mempalace_drawers"

        col = backend.get_collection(palace_path, collection_name, create=False)
        _ = col.count()

        try:
            from mempalace.backends.chroma import hnsw_capacity_status

            hnsw = hnsw_capacity_status(palace_path, collection_name)
            if isinstance(hnsw, dict) and hnsw.get("diverged"):
                return False, f"HNSW divergence detected: {hnsw.get('message', 'unknown')}"
        except Exception:
            pass

        kg_path = _mempalace_paths(palace_path)["knowledge_graph"]
        if kg_path.exists():
            conn = sqlite3.connect(str(kg_path))
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if row and row[0] not in ("ok", "OK"):
                    return False, f"knowledge_graph quick_check failed: {row[0]}"
            finally:
                conn.close()

    except Exception as exc:
        return False, str(exc)

    return True, "ok"


def _merge_collection_records_additive(
    backend,
    palace_path: str,
    collection_name: str,
    incoming_records: list[dict],
    source_machine: str,
) -> tuple[int, int, int]:
    if not incoming_records:
        return 0, 0, 0

    col = backend.get_collection(palace_path, collection_name, create=True)
    created = 0
    skipped = 0
    conflicts = 0

    batch_size = 500
    for i in range(0, len(incoming_records), batch_size):
        batch = incoming_records[i : i + batch_size]
        requested_ids = [str(r.get("id", "")) for r in batch if r.get("id")]
        existing_map: dict[str, str] = {}
        if requested_ids:
            current = col.get(ids=requested_ids, include=["documents"])
            cur_ids = current.get("ids") or []
            cur_docs = current.get("documents") or []
            existing_map = {cid: cdoc for cid, cdoc in zip(cur_ids, cur_docs)}

        upsert_ids: list[str] = []
        upsert_docs: list[str] = []
        upsert_metas: list[dict] = []

        for row in batch:
            base_id = str(row.get("id", "")).strip()
            doc = row.get("document")
            meta = row.get("metadata") or {}
            if not base_id or not isinstance(doc, str):
                skipped += 1
                continue

            existing_doc = existing_map.get(base_id)
            if existing_doc is None:
                final_id = base_id
                created += 1
            elif existing_doc == doc:
                skipped += 1
                continue
            else:
                final_id = _collection_record_conflict_id(base_id, source_machine, doc)
                conflicts += 1
                created += 1
                meta = dict(meta)
                meta["mp_sync_conflict_from"] = source_machine
                meta["mp_sync_original_id"] = base_id

            upsert_ids.append(final_id)
            upsert_docs.append(doc)
            upsert_metas.append(meta)

        if upsert_ids:
            col.upsert(ids=upsert_ids, documents=upsert_docs, metadatas=upsert_metas)

    return created, skipped, conflicts


def _collect_snapshot(export_dir: Path, palace_path: str, machine: str) -> dict:
    ChromaBackend, MempalaceConfig = _load_mempalace_runtime()
    backend = ChromaBackend()
    cfg = MempalaceConfig()

    drawers_name = getattr(cfg, "collection_name", "mempalace_drawers") or "mempalace_drawers"
    closets_name = "mempalace_closets"

    snap_dir = _snapshot_dir(export_dir)
    cols_dir = _snapshot_collections_dir(export_dir)
    files_dir = _snapshot_files_dir(export_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    cols_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    drawers_rows, drawers_expected = _export_collection_records(backend, palace_path, drawers_name)
    closets_rows, closets_expected = _export_collection_records(backend, palace_path, closets_name)

    _write_jsonl(cols_dir / "drawers.jsonl", drawers_rows)
    _write_jsonl(cols_dir / "closets.jsonl", closets_rows)

    paths = _mempalace_paths(palace_path)
    copied_files = {}
    file_map = {
        "knowledge_graph": paths["knowledge_graph"],
        "tunnels": paths["tunnels"],
        "hallways": paths["hallways"],
        "known_entities": paths["known_entities"],
        "entity_registry": paths["entity_registry"],
        "origin": paths["origin"],
    }
    for key, src in file_map.items():
        dst_name = f"{key}{src.suffix if src.suffix else '.json'}"
        copied_files[key] = _copy_if_exists(src, files_dir / dst_name)

    # Cross-check exported count against col.count() (taken before iteration)
    # and against the authoritative SQLite embedding table.
    export_warnings: list[str] = []
    sqlite_counts = _sqlite_collection_counts(palace_path)

    for col_name, rows, expected in [
        (drawers_name, drawers_rows, drawers_expected),
        (closets_name, closets_rows, closets_expected),
    ]:
        exported = len(rows)
        if expected is not None and exported != expected:
            export_warnings.append(
                f"{col_name}: exported {exported} but col.count()={expected} — collection changed during export"
            )
        sqlite_n = sqlite_counts.get(col_name)
        if sqlite_n is not None and exported != sqlite_n:
            export_warnings.append(
                f"{col_name}: exported {exported} but SQLite has {sqlite_n} — snapshot may be incomplete"
            )

    for w in export_warnings:
        print(f"[mp_sync] WARNING: {w}")

    manifest = {
        "format_version": 2,
        "created_at": iso_now(),
        "machine_name": machine,
        "palace_path": str(Path(palace_path).expanduser()),
        "collections": {
            "drawers": {
                "name": drawers_name,
                "records": len(drawers_rows),
                "sqlite_count": sqlite_counts.get(drawers_name),
                "file": "collections/drawers.jsonl",
            },
            "closets": {
                "name": closets_name,
                "records": len(closets_rows),
                "sqlite_count": sqlite_counts.get(closets_name),
                "file": "collections/closets.jsonl",
            },
        },
        "files": copied_files,
        "export_warnings": export_warnings,
    }
    _atomic_write_text(_snapshot_manifest_path(export_dir), json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


def _import_snapshot(src_export_dir: Path, palace_path: str, local_machine: str, quiet: bool):
    manifest_path = _snapshot_manifest_path(src_export_dir)
    if not manifest_path.exists():
        if not quiet:
            print(f"[mp_sync] Skipping {src_export_dir.name}: no snapshot manifest found")
        return

    manifest = _safe_read_json(manifest_path)
    if not isinstance(manifest, dict):
        if not quiet:
            print(f"[mp_sync] Skipping {src_export_dir.name}: invalid snapshot manifest")
        return

    source_machine = manifest.get("machine_name") or _machine_name_from_export_dir(src_export_dir)
    if source_machine == local_machine:
        return

    # Warn if the incoming snapshot was itself flagged as potentially incomplete
    incoming_warnings = manifest.get("export_warnings") or []
    if incoming_warnings and not quiet:
        print(f"[mp_sync] Warning: snapshot from {source_machine} was pushed with export warnings:")
        for w in incoming_warnings:
            print(f"[mp_sync]   • {w}")

    ChromaBackend, _ = _load_mempalace_runtime()
    backend = ChromaBackend()

    cols_dir = _snapshot_collections_dir(src_export_dir)
    files_dir = _snapshot_files_dir(src_export_dir)

    drawers_name = (((manifest.get("collections") or {}).get("drawers") or {}).get("name")) or "mempalace_drawers"
    closets_name = (((manifest.get("collections") or {}).get("closets") or {}).get("name")) or "mempalace_closets"

    drawers_records = list(_read_jsonl(cols_dir / "drawers.jsonl"))
    closets_records = list(_read_jsonl(cols_dir / "closets.jsonl"))

    d_created, d_skipped, d_conflicts = _merge_collection_records_additive(
        backend, palace_path, drawers_name, drawers_records, source_machine
    )
    c_created, c_skipped, c_conflicts = _merge_collection_records_additive(
        backend, palace_path, closets_name, closets_records, source_machine
    )

    paths = _mempalace_paths(palace_path)
    kg_tables = _merge_knowledge_graph_additive(paths["knowledge_graph"], files_dir / "knowledge_graph.sqlite3")
    tun_changed = _merge_named_list_file(paths["tunnels"], files_dir / "tunnels.json", "tunnels")
    hall_changed = _merge_named_list_file(paths["hallways"], files_dir / "hallways.json", "hallways")
    known_changed = _merge_json_file_additive(paths["known_entities"], files_dir / "known_entities.json")
    reg_changed = _merge_json_file_additive(paths["entity_registry"], files_dir / "entity_registry.json")

    origin_src = files_dir / "origin.json"
    origin_merged = False
    if origin_src.exists():
        if not paths["origin"].exists():
            _copy_if_exists(origin_src, paths["origin"])
            origin_merged = True
        else:
            local_origin = _safe_read_json(paths["origin"])
            incoming_origin = _safe_read_json(origin_src)
            if local_origin != incoming_origin and incoming_origin is not None:
                archive_dir = paths["origin"].parent / "origin.imports"
                archive_dir.mkdir(parents=True, exist_ok=True)
                _copy_if_exists(origin_src, archive_dir / f"origin.from-{source_machine}.json")

    if not quiet:
        print(
            f"[mp_sync] Merged {source_machine}: "
            f"drawers +{d_created} (={d_skipped}, conflicts={d_conflicts}), "
            f"closets +{c_created} (={c_skipped}, conflicts={c_conflicts}), "
            f"kg_tables={kg_tables}, "
            f"files changed: tunnels={int(tun_changed)}, hallways={int(hall_changed)}, "
            f"known_entities={int(known_changed)}, entity_registry={int(reg_changed)}, origin={int(origin_merged)}"
        )


def detect_dropbox() -> "Optional[str]":
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "Dropbox",
        Path("E:/Dropbox"),
        Path("C:/Dropbox"),
        Path.home() / "Dropbox",
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return str(c)
    return None


def read_meta(sync_folder: Path) -> dict:
    p = sync_folder / SYNC_META_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_meta(sync_folder: Path, meta: dict):
    (sync_folder / SYNC_META_FILE).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def export_dir_for(sync_folder: Path, machine: str) -> Path:
    """Each machine gets its own subfolder so exports never collide."""
    return sync_folder / f"export-{machine}"


def other_export_dirs(sync_folder: Path, machine: str) -> list[Path]:
    """All export-* subdirs in sync_folder that don't belong to this machine."""
    return [
        d for d in sync_folder.iterdir()
        if d.is_dir()
        and d.name.startswith("export-")
        and d.name != f"export-{machine}"
    ]


def prompt_with_default(label: str, default: str = "", example: str = "") -> str:
    """Prompt with [default] and return that default when user presses Enter.

    If no default is available, show an optional example and require explicit input.
    """
    if default:
        raw = input(f"{label} [{default}]: ").strip()
        return raw or default

    shown = f"example: {example}" if example else "enter value"
    return input(f"{label} [{shown}]: ").strip()


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_setup(args):
    cfg = load_config()
    print("\n── MemPalace Sync Setup ────────────────────────────────")
    print(f"Config will be saved to: {CONFIG_PATH}\n")

    detected = detect_dropbox()
    detected_sync = str(Path(detected) / "mempalace-sync") if detected else ""
    default_sync = cfg.get("sync_folder", "") or detected_sync
    cfg["sync_folder"] = prompt_with_default(
        "Sync folder",
        default_sync,
        example=r"E:\Dropbox\mempalace-sync",
    )
    if not cfg["sync_folder"]:
        print("[mp_sync] A sync folder is required.")
        sys.exit(1)

    _ensure_mempalace_available_for_setup()

    detected_palace = str(Path.home() / ".mempalace" / "palace")
    default_palace = cfg.get("palace_path", "") or detected_palace
    cfg["palace_path"] = _prompt_for_palace_path(default_palace)

    import socket
    detected_name = socket.gethostname()
    default_name = cfg.get("machine_name", "") or detected_name
    cfg["machine_name"] = prompt_with_default("This machine's name", default_name)

    save_config(cfg)
    Path(cfg["sync_folder"]).mkdir(parents=True, exist_ok=True)

    print(f"\n  sync_folder:  {cfg['sync_folder']}")
    print(f"  palace_path:  {cfg['palace_path']}")
    print(f"  machine_name: {cfg['machine_name']}")
    print(f"\n[mp_sync] ✓ Saved to {CONFIG_PATH}")
    launcher, launcher_reason = _hook_launcher_details()
    print("\nHook launcher:")
    print(f"  {launcher_reason}")
    print(f"  {launcher}")
    print("  Use this exact command in hook config.")
    print("\nNext steps:")
    print(f"  1. On your source machine (the one with existing memories), run:\n     {launcher.replace('sync --quiet', 'push')}")
    print(f"  2. On a new machine (even with an empty palace), run:\n     {launcher.replace('sync --quiet', 'pull')}")
    print("  3. For daily usage after bootstrap, run sync on each machine.")


def _do_push(sync_folder: Path, palace_path: str, machine: str, quiet: bool):
    my_export = export_dir_for(sync_folder, machine)
    my_export.mkdir(parents=True, exist_ok=True)

    if not quiet:
        print(f"\n── Push: {machine} → {my_export.name}/ ──────────────────")

    _repair_hnsw_if_drifted(palace_path, quiet)
    drift_before = len(_find_drift_dirs(palace_path))
    manifest = _collect_snapshot(my_export, palace_path, machine)
    drift_after = _find_drift_dirs(palace_path)
    new_quarantines = len(drift_after) - drift_before

    # Auto-clean old drift dirs — keep only the 2 most recent per segment
    cleaned = _cleanup_old_drift_dirs(palace_path, keep=2, quiet=quiet)

    meta = read_meta(sync_folder)
    meta.setdefault("machines", {}).setdefault(machine, {})
    meta["machines"][machine]["last_push"] = iso_now()
    write_meta(sync_folder, meta)

    if not quiet:
        drawers_count = (((manifest.get("collections") or {}).get("drawers") or {}).get("records")) or 0
        export_warnings = manifest.get("export_warnings", [])
        if new_quarantines > 0:
            print(
                f"[mp_sync] Note: {new_quarantines} HNSW segment(s) were auto-rebuilt by the MemPalace runtime "
                f"(SQLite is authoritative — all records exported from SQLite, not HNSW)"
            )
        if export_warnings:
            print(f"[mp_sync] ✗ Push completed with warnings — snapshot may be incomplete:")
            for w in export_warnings:
                print(f"[mp_sync]   • {w}")
        else:
            print(f"[mp_sync] ✓ Pushed snapshot to {my_export} ({drawers_count} drawer records, counts verified)")

    if quiet:
        log_path = sync_folder / "sync.log"
        warnings = manifest.get("export_warnings", [])
        status = "push-warning" if warnings else "push"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{iso_now()} | {machine} | {status}\n")
            for w in warnings:
                f.write(f"  WARNING: {w}\n")


def _do_pull(sync_folder: Path, palace_path: str, machine: str, quiet: bool):
    others = other_export_dirs(sync_folder, machine)

    if not others:
        if not quiet:
            print(f"[mp_sync] No other machines have pushed yet — nothing to pull.")
            print(f"[mp_sync] Expected folders like  export-<other-machine>/  inside {sync_folder}")
        return

    _repair_hnsw_if_drifted(palace_path, quiet)

    backup = None
    pull_ok = False

    try:
        backup = _backup_impacted_state(palace_path)
        if not quiet:
            print("[mp_sync] Created temporary safety backup before merge")

        for src in others:
            if not quiet:
                print(f"\n── Pull: {src.name}/ → {machine} ──────────────────")
            _import_snapshot(src, palace_path, machine, quiet)
            if not quiet:
                print(f"[mp_sync] ✓ Merged {src.name}")

        ok, reason = _post_pull_health_check(palace_path)
        if not ok:
            raise RuntimeError(reason)

        pull_ok = True
    except Exception as exc:
        if backup is not None:
            _restore_impacted_state(backup)
        if not quiet:
            print(f"[mp_sync] Pull failed, restored local state from backup: {exc}")
        raise
    finally:
        if backup is not None and pull_ok:
            _cleanup_backup(backup)
            if not quiet:
                print("[mp_sync] Backup removed after successful health check")

    meta = read_meta(sync_folder)
    meta.setdefault("machines", {}).setdefault(machine, {})
    meta["machines"][machine]["last_pull"] = iso_now()
    meta["machines"][machine]["pulled_from"] = [d.name for d in others]
    write_meta(sync_folder, meta)

    if quiet:
        log_path = sync_folder / "sync.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{iso_now()} | {machine} | pull from {[d.name for d in others]}\n")


def cmd_push(args):
    cfg = load_config()
    quiet = getattr(args, "quiet", False)
    _do_push(
        Path(require(cfg, "sync_folder")),
        require(cfg, "palace_path"),
        cfg.get("machine_name", "unknown"),
        quiet,
    )


def cmd_pull(args):
    cfg = load_config()
    quiet = getattr(args, "quiet", False)
    _do_pull(
        Path(require(cfg, "sync_folder")),
        require(cfg, "palace_path"),
        cfg.get("machine_name", "unknown"),
        quiet,
    )


def cmd_sync(args):
    """push + pull in one step — the recommended daily command."""
    cfg = load_config()
    quiet = getattr(args, "quiet", False)
    sync_folder = Path(require(cfg, "sync_folder"))
    palace_path = require(cfg, "palace_path")
    machine = cfg.get("machine_name", "unknown")

    if not quiet:
        print(f"\n── Sync: {machine} ↔ {sync_folder} ───────────────────")

    _do_push(sync_folder, palace_path, machine, quiet)
    _do_pull(sync_folder, palace_path, machine, quiet)

    if not quiet:
        print(f"\n[mp_sync] ✓ Sync complete at {iso_now()}")


def cmd_clean(args):
    """Remove accumulated .drift-* HNSW quarantine dirs from the palace."""
    cfg = load_config()
    palace_path = require(cfg, "palace_path")
    keep = getattr(args, "keep", 1)

    drift_dirs = _find_drift_dirs(palace_path)
    if not drift_dirs:
        print("[mp_sync] No drift segments found — palace is clean.")
        return

    print(f"[mp_sync] Found {len(drift_dirs)} drift segment dir(s):")
    for d in drift_dirs:
        print(f"  {d.name}")

    removed = _cleanup_old_drift_dirs(palace_path, keep=keep, quiet=False)
    remaining = _find_drift_dirs(palace_path)
    print(f"[mp_sync] Done — removed {removed}, {len(remaining)} kept as recent backup(s).")


def cmd_status(args):
    cfg = load_config()
    if not cfg:
        print("[mp_sync] Not configured. Run:  python mp_sync.py setup")
        sys.exit(1)

    sync_folder = Path(cfg.get("sync_folder", ""))
    palace_path = cfg.get("palace_path", "")
    machine = cfg.get("machine_name", "unknown")

    print(f"\n── MemPalace Sync Status ───────────────────────────────")
    print(f"  This machine: {machine}")
    print(f"  Palace:       {palace_path}")
    print(f"  Sync folder:  {sync_folder}")

    if sync_folder.exists():
        meta = read_meta(sync_folder)
        machines_meta = meta.get("machines", {})

        # Per-machine export stats
        export_dirs = [d for d in sync_folder.iterdir() if d.is_dir() and d.name.startswith("export-")]
        if export_dirs:
            print(f"\n  Machines in sync folder:")
            for d in sorted(export_dirs):
                name = d.name.replace("export-", "")
                files = list(d.rglob("*.jsonl"))
                lines = 0
                for f in files:
                    try:
                        lines += sum(1 for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip())
                    except Exception:
                        pass
                m = machines_meta.get(name, {})
                last_push = m.get("last_push", "never")
                marker = " ← you" if name == machine else ""
                print(f"    {name}{marker}:  {lines} records  (last push: {last_push})")

        # This machine's last pull
        my_meta = machines_meta.get(machine, {})
        last_pull = my_meta.get("last_pull", "never")
        pulled_from = my_meta.get("pulled_from", [])
        print(f"\n  Last pull by this machine: {last_pull}")
        if pulled_from:
            print(f"  Pulled from: {', '.join(pulled_from)}")

        # Recent log
        log_path = sync_folder / "sync.log"
        if log_path.exists():
            recent = log_path.read_text(encoding="utf-8").splitlines()[-5:]
            print(f"\n  Recent auto-syncs:")
            for line in recent:
                print(f"    {line}")
    else:
        print(f"\n  [!] Sync folder not found: {sync_folder}")

    print()
    run(["mempalace", "--palace", palace_path, "status"], check=False)


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    # Ensure UTF-8 output on Windows consoles that default to cp1252
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="mp_sync",
        description="MemPalace bidirectional sync — every machine gets the union of all memories",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Interactive setup — writes ~/.mempalace/mp_sync_config.json")

    for name in ("push", "pull", "sync"):
        p = sub.add_parser(name, help={
            "push": "Export this machine's palace → sync folder",
            "pull": "Import all other machines' exports → local palace",
            "sync": "push + pull in one step (recommended)",
        }[name])
        p.add_argument("--quiet", "-q", action="store_true", help="Suppress output (for hooks)")

    sub.add_parser("status", help="Show sync state and drawer counts per machine")

    p_clean = sub.add_parser("clean", help="Remove accumulated .drift-* HNSW quarantine dirs")
    p_clean.add_argument(
        "--keep", type=int, default=1, metavar="N",
        help="Keep the N most recent drift dirs per segment as a backup (default: 1)"
    )

    args = parser.parse_args()
    try:
        {
            "setup":  cmd_setup,
            "push":   cmd_push,
            "pull":   cmd_pull,
            "sync":   cmd_sync,
            "status": cmd_status,
            "clean":  cmd_clean,
        }[args.command](args)
    except KeyboardInterrupt:
        print("\n[mp_sync] Cancelled.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
