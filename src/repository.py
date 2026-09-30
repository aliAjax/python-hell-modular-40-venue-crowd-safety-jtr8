import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def admit_people(self, zone_id, count, gate_id, admitted_at, order_id):
        """Atomically admit ``count`` people into a zone on behalf of an order.

        The capacity check and the occupancy update happen inside one write
        transaction, so two concurrent orders cannot both pass the check and
        over-occupy the zone.  The ``order_id`` is recorded on the zone so a
        retry of the same order is a no-op instead of occupying the same
        people twice.

        Returns ``(zone, applied)`` where ``applied`` is False when the order
        had already been applied (idempotent retry).
        """
        if not gate_id:
            raise ValidationError("gate_id is required for admission")
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("zone not found: " + zone_id)
            zone = self._entity_from_row(row)
            data = zone["data"]
            transfers = data.get("diversion_transfers") or {}
            if order_id in transfers:
                connection.rollback()
                return zone, False
            if zone["status"] not in ("open", "limited"):
                raise ConflictError(
                    "zone %s is not accepting admission (status %s)"
                    % (zone_id, zone["status"])
                )
            occupancy = int(data.get("current_occupancy", 0))
            capacity = int(data.get("capacity", 0))
            if occupancy + int(count) > capacity:
                raise ConflictError(
                    "zone remaining capacity conflict: %d + %d > %d"
                    % (occupancy, count, capacity)
                )
            if zone["status"] == "limited":
                limit = int(data.get("admit_limit", capacity))
                if occupancy + int(count) > limit:
                    raise ConflictError(
                        "zone admission limit conflict: %d + %d > %d"
                        % (occupancy, count, limit)
                    )
            gate_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (gate_id,)
            ).fetchone()
            if not gate_row:
                raise ValidationError("gate does not exist")
            gate = self._entity_from_row(gate_row)
            if gate["status"] != "open":
                raise ConflictError("entry gate is not open")
            if zone_id not in (gate["data"].get("zone_ids") or []):
                raise ValidationError("gate does not serve this zone")
            data["current_occupancy"] = occupancy + int(count)
            transfers[order_id] = {
                "count": int(count),
                "at": admitted_at,
                "gate_id": gate_id,
            }
            data["diversion_transfers"] = transfers
            data["last_admission_at"] = admitted_at
            data["last_gate_id"] = gate_id
            new_version = int(zone["version"]) + 1
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (zone["status"], new_version, payload, now, zone_id, int(zone["version"])),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(zone_id), True

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
