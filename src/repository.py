from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import BatchConflictError, ConflictError, NotFoundError
from .rules import ENTITY, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    resource_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """为旧库补齐离线合并所需的resource_id列（新建库上为空操作）。"""
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(records)").fetchall()
        }
        if "resource_id" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN resource_id TEXT")
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS ix_records_resource_open
               ON records(resource_id, item_id) WHERE resource_id IS NOT NULL""")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   resource_id: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       resource_id, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref,
                     resource_id, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            event = self._insert_audit(action, entity_type, entity_id, actor, detail)
        return event

    def _insert_audit(self, action: str, entity_type: str, entity_id: int,
                      actor: str, detail: dict) -> Dict[str, Any]:
        """调用方负责持有self._lock并处于打开的事务中。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        event["id"] = int(cur.lastrowid)
        return event

    def merge_offline_records(self, item_id: int, entries: List[Dict[str, Any]],
                              actor: str) -> Dict[str, Any]:
        """在单个事务内合并一批离线记录。

        - 同一事件下client_ref已存在（含本批较早条目）：返回原记录，不新增审计；
        - resource_id在别的未关闭事件仍有open分配：整批退回并说明冲突事件与队员；
        - 否则新增记录，每条写一条record审计。
        冲突时通过异常让事务回滚，不写入任何记录或审计。
        """
        self.get_item(item_id)
        now = utc_now()
        with self._lock, self.conn:
            existing_rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=?", (item_id,)
            ).fetchall()
            by_ref: Dict[str, Dict[str, Any]] = {}
            for row in existing_rows:
                if row["external_ref"] is not None:
                    by_ref[row["external_ref"]] = dict(row)

            inserted: List[Dict[str, Any]] = []
            duplicates: List[Dict[str, Any]] = []
            conflicts: List[Dict[str, Any]] = []

            for entry in entries:
                client_ref = entry["client_ref"]
                if client_ref in by_ref:
                    duplicates.append(by_ref[client_ref])
                    continue
                conflict = None
                if entry["resource_id"] is not None and entry["status"] == "open":
                    conflict = self.conn.execute(
                        """SELECT i.id AS item_id, i.title AS title, r.resource_id AS resource_id
                           FROM records r JOIN items i ON i.id = r.item_id
                           WHERE r.resource_id=? AND r.status='open'
                             AND r.item_id<>? AND i.status<>'closed'
                           ORDER BY r.id LIMIT 1""",
                        (entry["resource_id"], item_id),
                    ).fetchone()
                if conflict is not None:
                    conflicts.append({
                        "client_ref": client_ref,
                        "resource_id": entry["resource_id"],
                        "conflict_item_id": int(conflict["item_id"]),
                        "conflict_item_title": conflict["title"],
                    })
                    continue
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       resource_id, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, entry["kind"], entry["detail"], entry["status"],
                     client_ref, entry["resource_id"], actor, now),
                )
                record = dict(self.conn.execute(
                    "SELECT * FROM records WHERE id=?", (int(cur.lastrowid),)
                ).fetchone())
                by_ref[client_ref] = record
                self._insert_audit("record", ENTITY, item_id, actor, {
                    "record_id": record["id"], "kind": record["kind"],
                    "status": record["status"], "client_ref": client_ref,
                    "resource_id": record["resource_id"], "offline_merge": True,
                })
                inserted.append(record)

            if conflicts:
                payload = {
                    "error": "BatchConflictError",
                    "message": "资源仍分配在别的未关闭事件，整批退回",
                    "inserted_count": 0,
                    "duplicate_count": len(duplicates),
                    "rejected_count": len(inserted) + len(conflicts),
                    "conflicts": conflicts,
                    "inserted": [],
                    "duplicates": [],
                }
                raise BatchConflictError(payload["message"], payload)

        return {
            "inserted_count": len(inserted),
            "duplicate_count": len(duplicates),
            "rejected_count": 0,
            "inserted": inserted,
            "duplicates": duplicates,
            "conflicts": [],
        }

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
