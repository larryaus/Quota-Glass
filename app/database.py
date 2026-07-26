import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.models import Meter


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meter_state (
                    key TEXT PRIMARY KEY,
                    last_pct REAL,
                    window_id INTEGER,
                    fired_full_for_window INTEGER,
                    last_event_at INTEGER,
                    missing_polls INTEGER NOT NULL DEFAULT 0,
                    reseed_required INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    meter_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    used_pct REAL,
                    window_id INTEGER,
                    created_at INTEGER NOT NULL,
                    notification_status TEXT NOT NULL DEFAULT 'delivered',
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_error TEXT
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at INTEGER NOT NULL,
                    meter_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    used_pct REAL,
                    stale INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_time
                    ON samples(sampled_at);
                CREATE INDEX IF NOT EXISTS idx_events_time
                    ON events(created_at DESC);
                """
            )
            self._add_column_if_missing(
                "meter_state",
                "missing_polls",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._add_column_if_missing(
                "meter_state",
                "reseed_required",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._add_column_if_missing(
                "events",
                "notification_status",
                "TEXT NOT NULL DEFAULT 'delivered'",
            )
            self._add_column_if_missing(
                "events",
                "notification_attempts",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._add_column_if_missing(
                "events",
                "notification_error",
                "TEXT",
            )

    def _add_column_if_missing(
        self,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(%s)" % table
            ).fetchall()
        }
        if column not in columns:
            self._connection.execute(
                "ALTER TABLE %s ADD COLUMN %s %s"
                % (table, column, declaration)
            )

    def get_meter_state(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM meter_state WHERE key = ?", (key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def put_meter_state(
        self,
        key: str,
        last_pct: float,
        window_id: Optional[int],
        fired_full_for_window: Optional[int],
        last_event_at: Optional[int],
        missing_polls: int = 0,
        reseed_required: bool = False,
    ) -> None:
        with self._lock, self._connection:
            self._put_meter_state(
                key,
                last_pct,
                window_id,
                fired_full_for_window,
                last_event_at,
                missing_polls,
                reseed_required,
            )

    def _put_meter_state(
        self,
        key: str,
        last_pct: float,
        window_id: Optional[int],
        fired_full_for_window: Optional[int],
        last_event_at: Optional[int],
        missing_polls: int,
        reseed_required: bool,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO meter_state
                (
                    key,
                    last_pct,
                    window_id,
                    fired_full_for_window,
                    last_event_at,
                    missing_polls,
                    reseed_required
                )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_pct = excluded.last_pct,
                window_id = excluded.window_id,
                fired_full_for_window = excluded.fired_full_for_window,
                last_event_at = excluded.last_event_at,
                missing_polls = excluded.missing_polls,
                reseed_required = excluded.reseed_required
            """,
            (
                key,
                last_pct,
                window_id,
                fired_full_for_window,
                last_event_at,
                max(0, missing_polls),
                int(reseed_required),
            ),
        )

    def record_events_and_state(
        self,
        event_types: List[str],
        meter: Meter,
        window_id: Optional[int],
        created_at: int,
        fired_full_for_window: Optional[int],
        last_event_at: Optional[int],
    ) -> List[int]:
        event_ids: List[int] = []
        with self._lock, self._connection:
            # The state latch is written first and committed atomically with all
            # pending event rows. Notification delivery happens only afterward.
            self._put_meter_state(
                meter.key,
                float(meter.used_pct),
                window_id,
                fired_full_for_window,
                last_event_at,
                0,
                False,
            )
            for event_type in event_types:
                cursor = self._connection.execute(
                    """
                    INSERT INTO events
                        (
                            event_type,
                            meter_key,
                            provider,
                            label,
                            used_pct,
                            window_id,
                            created_at,
                            notification_status
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        event_type,
                        meter.key,
                        meter.provider,
                        meter.label,
                        meter.used_pct,
                        window_id,
                        created_at,
                    ),
                )
                event_ids.append(int(cursor.lastrowid))
        return event_ids

    def get_pending_notifications(
        self,
        meter_key: str,
        max_attempts: int,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM events
                WHERE meter_key = ?
                  AND notification_status = 'pending'
                  AND notification_attempts < ?
                ORDER BY id ASC
                """,
                (meter_key, max(1, max_attempts)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_delivered(self, event_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE events
                SET notification_status = 'delivered',
                    notification_attempts = notification_attempts + 1,
                    notification_error = NULL
                WHERE id = ? AND notification_status = 'pending'
                """,
                (event_id,),
            )

    def mark_notification_failed(
        self,
        event_id: int,
        error: str,
        max_attempts: int,
    ) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT notification_attempts
                FROM events
                WHERE id = ? AND notification_status = 'pending'
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["notification_attempts"]) + 1
            status = "failed" if attempts >= max(1, max_attempts) else "pending"
            self._connection.execute(
                """
                UPDATE events
                SET notification_status = ?,
                    notification_attempts = ?,
                    notification_error = ?
                WHERE id = ?
                """,
                (status, attempts, error[:2000], event_id),
            )

    def get_latest_notification_error(self) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT notification_error
                FROM events
                WHERE notification_status IN ('pending', 'failed')
                  AND notification_error IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return str(row["notification_error"])

    def mark_meter_presence(
        self,
        seen_keys: List[str],
        polled_providers: List[str],
    ) -> None:
        seen = set(seen_keys)
        providers = set(polled_providers)
        if not providers:
            return
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT key, missing_polls FROM meter_state"
            ).fetchall()
            for row in rows:
                key = str(row["key"])
                provider = key.split(".", 1)[0]
                if provider not in providers:
                    continue
                if key in seen:
                    self._connection.execute(
                        """
                        UPDATE meter_state
                        SET missing_polls = 0
                        WHERE key = ?
                        """,
                        (key,),
                    )
                    continue
                missing_polls = int(row["missing_polls"]) + 1
                reseed_required = missing_polls > 1
                self._connection.execute(
                    """
                    UPDATE meter_state
                    SET missing_polls = ?,
                        reseed_required = ?,
                        fired_full_for_window = CASE
                            WHEN ? THEN NULL
                            ELSE fired_full_for_window
                        END
                    WHERE key = ?
                    """,
                    (
                        missing_polls,
                        int(reseed_required),
                        int(reseed_required),
                        key,
                    ),
                )
                if reseed_required:
                    self._connection.execute(
                        """
                        UPDATE events
                        SET notification_status = 'abandoned',
                            notification_error = COALESCE(
                                notification_error,
                                'Meter disappeared for more than one poll.'
                            )
                        WHERE meter_key = ?
                          AND notification_status = 'pending'
                        """,
                        (key,),
                    )

    def add_samples(self, meters: List[Meter], sampled_at: Optional[int] = None) -> None:
        timestamp = int(time.time()) if sampled_at is None else sampled_at
        rows = [
            (timestamp, meter.key, meter.provider, meter.used_pct, int(meter.stale))
            for meter in meters
        ]
        if not rows:
            return
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO samples
                    (sampled_at, meter_key, provider, used_pct, stale)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(200, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_history(self, hours: int = 24) -> List[Dict[str, Any]]:
        safe_hours = max(1, min(24 * 30, hours))
        cutoff = int(time.time()) - safe_hours * 3600
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sampled_at, meter_key, provider, used_pct, stale
                FROM samples
                WHERE sampled_at >= ?
                ORDER BY sampled_at ASC, id ASC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_history(
        self,
        retention_days: int,
        now: Optional[int] = None,
    ) -> Tuple[int, int]:
        current = int(time.time()) if now is None else now
        cutoff = current - max(1, retention_days) * 24 * 3600
        with self._lock, self._connection:
            sample_cursor = self._connection.execute(
                "DELETE FROM samples WHERE sampled_at < ?",
                (cutoff,),
            )
            event_cursor = self._connection.execute(
                "DELETE FROM events WHERE created_at < ?",
                (cutoff,),
            )
        return int(sample_cursor.rowcount), int(event_cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
