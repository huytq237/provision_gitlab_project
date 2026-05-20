"""SQLite layer cho audit findings.

Thread-safe: dùng Lock cho tất cả write operations vì webhook handler
và polling scanner chạy trên các thread riêng biệt.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS findings (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance     TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    full_path    TEXT NOT NULL,
                    violation    TEXT NOT NULL,
                    detail       TEXT,
                    detected_at  TEXT NOT NULL,
                    alerted_at   TEXT,
                    resolved_at  TEXT,
                    UNIQUE(instance, full_path, violation)
                )
            """)
            self._conn.commit()

    def upsert_finding(
        self,
        instance: str,
        kind: str,
        full_path: str,
        violation: str,
        detail: str = "",
    ) -> bool:
        """Insert finding nếu chưa có. Trả về True nếu là finding MỚI (cần alert).

        Nếu finding đã tồn tại và đã được mark resolved trước đây → reopen và trả về True.
        Nếu finding đang mở (chưa resolved) → không làm gì, trả về False.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, resolved_at FROM findings "
                "WHERE instance=? AND full_path=? AND violation=?",
                (instance, full_path, violation),
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO findings "
                    "(instance, kind, full_path, violation, detail, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (instance, kind, full_path, violation, detail, _now()),
                )
                self._conn.commit()
                return True
            if row["resolved_at"] is not None:
                # Finding đã resolved trước đây, nay xuất hiện lại → reopen
                self._conn.execute(
                    "UPDATE findings SET resolved_at=NULL, alerted_at=NULL, "
                    "detected_at=?, detail=? WHERE id=?",
                    (_now(), detail, row["id"]),
                )
                self._conn.commit()
                return True
            return False

    def mark_alerted(self, instance: str, full_path: str, violation: str):
        with self._lock:
            self._conn.execute(
                "UPDATE findings SET alerted_at=? "
                "WHERE instance=? AND full_path=? AND violation=?",
                (_now(), instance, full_path, violation),
            )
            self._conn.commit()

    def mark_resolved(self, instance: str, full_path: str, violation: str):
        with self._lock:
            self._conn.execute(
                "UPDATE findings SET resolved_at=? "
                "WHERE instance=? AND full_path=? AND violation=? AND resolved_at IS NULL",
                (_now(), instance, full_path, violation),
            )
            self._conn.commit()

    def sync_resolved(self, instance: str, actual_paths: set[str]):
        """Mark resolved các findings mà full_path không còn xuất hiện trong actual_paths."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, full_path FROM findings "
                "WHERE instance=? AND resolved_at IS NULL",
                (instance,),
            )
            rows = cur.fetchall()
            for row in rows:
                if row["full_path"] not in actual_paths:
                    self._conn.execute(
                        "UPDATE findings SET resolved_at=? WHERE id=?",
                        (_now(), row["id"]),
                    )
            self._conn.commit()

    def get_unalerted(self) -> list[dict]:
        """Lấy tất cả findings chưa gửi alert và chưa resolved."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM findings WHERE alerted_at IS NULL AND resolved_at IS NULL"
            )
            return [dict(row) for row in cur.fetchall()]

    def summary(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM findings WHERE resolved_at IS NULL"
            )
            open_count = cur.fetchone()[0]

            cur = self._conn.execute(
                "SELECT COUNT(*) FROM findings WHERE resolved_at IS NOT NULL"
            )
            resolved_count = cur.fetchone()[0]

            cur = self._conn.execute(
                "SELECT COUNT(*) FROM findings "
                "WHERE alerted_at IS NOT NULL AND resolved_at IS NULL"
            )
            alerted_count = cur.fetchone()[0]

        return {"open": open_count, "resolved": resolved_count, "alerted": alerted_count}
