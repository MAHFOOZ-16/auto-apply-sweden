"""SQLite database layer – single source of truth for the agent."""

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import States


def _now() -> str:
    return datetime.utcnow().isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class AgentDB:
    """Thread-safe wrapper around agent.sqlite."""

    DDL = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id          TEXT PRIMARY KEY,
        url             TEXT NOT NULL,
        url_hash        TEXT NOT NULL,
        title           TEXT,
        company         TEXT,
        location        TEXT,
        language_hint   TEXT,
        description     TEXT,
        date_found      TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'DISCOVERED',
        fit_score       REAL DEFAULT 0,
        company_title_loc_hash TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

    CREATE TABLE IF NOT EXISTS applications (
        app_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          TEXT NOT NULL REFERENCES jobs(job_id),
        status          TEXT NOT NULL,
        attempt_count   INTEGER DEFAULT 1,
        artifact_dir    TEXT,
        resume_path     TEXT,
        cover_path      TEXT,
        applied_at      TEXT,
        last_error      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_app_job ON applications(job_id);

    CREATE TABLE IF NOT EXISTS queue (
        job_id      TEXT PRIMARY KEY REFERENCES jobs(job_id),
        priority    INTEGER DEFAULT 50,
        queued_at   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT NOT NULL,
        job_id          TEXT,
        state           TEXT,
        message         TEXT,
        screenshot_path TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);

    CREATE TABLE IF NOT EXISTS daily_stats (
        date_str    TEXT PRIMARY KEY,
        applied     INTEGER DEFAULT 0,
        failed      INTEGER DEFAULT 0,
        skipped     INTEGER DEFAULT 0
    );
    """

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn.executescript(self.DDL)
        self._conn.commit()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    # ───────── JOBS ─────────
    def job_exists(self, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def url_exists(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE url_hash = ?", (_hash(url),)
        ).fetchone()
        return row is not None

    def repost_exists(self, company: str, title: str, location: str) -> bool:
        h = _hash(f"{company}|{title}|{location}")
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE company_title_loc_hash = ? AND status = ?",
            (h, States.SUBMITTED),
        ).fetchone()
        return row is not None

    def insert_job(self, job: Dict[str, Any]) -> bool:
        """Insert a new job; return False if duplicate."""
        url_h = _hash(job["url"])
        ctl_h = _hash(
            f"{job.get('company', '')}|{job.get('title', '')}|{job.get('location', '')}"
        )
        try:
            self._conn.execute(
                """INSERT INTO jobs
                   (job_id, url, url_hash, title, company, location,
                    language_hint, description, date_found, status,
                    company_title_loc_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job["job_id"], job["url"], url_h,
                    job.get("title"), job.get("company"), job.get("location"),
                    job.get("language_hint"), job.get("description"),
                    _now(), States.DISCOVERED, ctl_h,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_job_status(self, job_id: str, status: str,
                          fit_score: Optional[float] = None):
        if fit_score is not None:
            self._conn.execute(
                "UPDATE jobs SET status=?, fit_score=? WHERE job_id=?",
                (status, fit_score, job_id),
            )
        else:
            self._conn.execute(
                "UPDATE jobs SET status=? WHERE job_id=?", (status, job_id)
            )
        self._conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_jobs_by_status(self, status: str) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status=?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ───────── QUEUE ─────────
    def enqueue(self, job_id: str, priority: int = 50):
        self._conn.execute(
            "INSERT OR REPLACE INTO queue (job_id, priority, queued_at) "
            "VALUES (?,?,?)",
            (job_id, priority, _now()),
        )
        self._conn.commit()

    def dequeue_next(self) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT q.job_id, q.priority, j.* FROM queue q "
            "JOIN jobs j ON q.job_id = j.job_id "
            "ORDER BY q.priority DESC, q.queued_at ASC LIMIT 1"
        ).fetchone()
        if row:
            d = dict(row)
            self._conn.execute(
                "DELETE FROM queue WHERE job_id=?", (d["job_id"],)
            )
            self._conn.commit()
            return d
        return None

    def queue_size(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) c FROM queue").fetchone()
        return row["c"]

    # ───────── APPLICATIONS ─────────
    def insert_application(self, job_id: str, status: str,
                           artifact_dir: str = "", resume_path: str = "",
                           cover_path: str = "") -> int:
        cur = self._conn.execute(
            """INSERT INTO applications
               (job_id, status, attempt_count, artifact_dir,
                resume_path, cover_path, applied_at)
               VALUES (?,?,1,?,?,?,?)""",
            (job_id, status, artifact_dir, resume_path, cover_path, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_application(self, app_id: int, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [app_id]
        self._conn.execute(
            f"UPDATE applications SET {sets} WHERE app_id=?", vals
        )
        self._conn.commit()

    def get_application_for_job(self, job_id: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE job_id=? "
            "ORDER BY app_id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None

    # ───────── EVENTS ─────────
    def log_event(self, job_id: str, state: str, message: str,
                  screenshot_path: str = ""):
        self._conn.execute(
            "INSERT INTO events "
            "(timestamp, job_id, state, message, screenshot_path) "
            "VALUES (?,?,?,?,?)",
            (_now(), job_id, state, message, screenshot_path),
        )
        self._conn.commit()

    # ───────── DAILY STATS ─────────
    def _today(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def increment_daily(self, column: str):
        today = self._today()
        self._conn.execute(
            f"INSERT INTO daily_stats (date_str, {column}) VALUES (?, 1) "
            f"ON CONFLICT(date_str) DO UPDATE SET {column} = {column} + 1",
            (today,),
        )
        self._conn.commit()

    def get_daily_applied(self) -> int:
        row = self._conn.execute(
            "SELECT applied FROM daily_stats WHERE date_str=?",
            (self._today(),),
        ).fetchone()
        return row["applied"] if row else 0

    def get_daily_failed(self) -> int:
        row = self._conn.execute(
            "SELECT failed FROM daily_stats WHERE date_str=?",
            (self._today(),),
        ).fetchone()
        return row["failed"] if row else 0

    # ───────── COMPANY COOLDOWN ─────────
    def company_applications_since(self, company: str, days: int) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            """SELECT COUNT(*) c FROM applications a
               JOIN jobs j ON a.job_id = j.job_id
               WHERE j.company = ? AND a.applied_at >= ? AND a.status = ?""",
            (company, cutoff, States.SUBMITTED),
        ).fetchone()
        return row["c"]

    # ───────── CONSECUTIVE FAILURES ─────────
    def recent_consecutive_failures(self) -> int:
        rows = self._conn.execute(
            "SELECT state FROM events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        count = 0
        for r in rows:
            if r["state"] in (States.FAILED_RETRYABLE, States.FAILED_PERMANENT):
                count += 1
            else:
                break
        return count

    # ───────── RESUMABLE JOBS ─────────
    def get_resumable_jobs(self) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?,?,?,?)",
            (States.READY_TO_APPLY, States.APPLYING,
             States.WAITING_FOR_HUMAN, States.ASSIST),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_retryable_jobs(self, max_retries: int = 3) -> List[Dict]:
        rows = self._conn.execute(
            """SELECT j.* FROM jobs j
               LEFT JOIN applications a ON j.job_id = a.job_id
               WHERE j.status = ?
               GROUP BY j.job_id
               HAVING COALESCE(MAX(a.attempt_count), 0) < ?""",
            (States.FAILED_RETRYABLE, max_retries),
        ).fetchall()
        return [dict(r) for r in rows]

    def stable_days_count(self) -> int:
        """Count consecutive past days with at least 1 application and <50% failure rate."""
        rows = self._conn.execute(
            "SELECT date_str, applied, failed FROM daily_stats "
            "ORDER BY date_str DESC LIMIT 10"
        ).fetchall()
        count = 0
        for r in rows:
            applied = r["applied"] or 0
            failed = r["failed"] or 0
            # Day counts as stable if it had applications and failure rate < 50%
            if applied > 0 and (failed / max(applied, 1)) < 0.5:
                count += 1
            else:
                break
        return count

    def consecutive_run_days(self) -> int:
        """Count consecutive days the agent has run (any applications)."""
        rows = self._conn.execute(
            "SELECT date_str, applied FROM daily_stats "
            "ORDER BY date_str DESC LIMIT 30"
        ).fetchall()
        count = 0
        for r in rows:
            if (r["applied"] or 0) > 0:
                count += 1
            else:
                break
        return count
