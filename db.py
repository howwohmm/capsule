"""
db.py — SQLite schema + all query helpers.
Single source of truth. GSheets is monitoring only.
"""
import sqlite3
import uuid
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import pytz

from config import DATABASE_PATH


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def uid() -> str:
    return str(uuid.uuid4())


def now_utc() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    timezone    TEXT DEFAULT 'Asia/Kolkata',
    frequency   TEXT DEFAULT '1x',
    tone        TEXT DEFAULT 'Casual',
    depth       TEXT DEFAULT 'Mix',
    active_days TEXT DEFAULT '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS courses (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    playlist_url        TEXT NOT NULL,
    playlist_title      TEXT,
    status              TEXT DEFAULT 'processing',
    current_video_index INTEGER DEFAULT 0,
    total_videos        INTEGER DEFAULT 0,
    videos_processed    INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id              TEXT PRIMARY KEY,
    course_id       TEXT NOT NULL REFERENCES courses(id),
    youtube_id      TEXT NOT NULL,
    title           TEXT,
    transcript_url  TEXT,
    position        INTEGER,
    status          TEXT DEFAULT 'pending',
    error_msg       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS emails (
    id           TEXT PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    slot         TEXT NOT NULL,
    subject      TEXT,
    html_body    TEXT,
    plain_body   TEXT,
    scheduled_at TEXT,
    sent_at      TEXT,
    status       TEXT DEFAULT 'pending',
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    review_type  TEXT NOT NULL,
    subject      TEXT,
    html_body    TEXT,
    scheduled_at TEXT,
    sent_at      TEXT,
    status       TEXT DEFAULT 'pending',
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS synthesis (
    id           TEXT PRIMARY KEY,
    course_id    TEXT NOT NULL REFERENCES courses(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    video_ids    TEXT NOT NULL,
    subject      TEXT,
    html_body    TEXT,
    scheduled_at TEXT,
    sent_at      TEXT,
    status       TEXT DEFAULT 'pending',
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT DEFAULT 'pending',
    progress   INTEGER DEFAULT 0,
    message    TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_traces (
    id              TEXT PRIMARY KEY,
    model           TEXT,
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0,
    latency_ms      INTEGER DEFAULT 0,
    raw             TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_traces_created ON llm_traces(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_emails_due      ON emails(scheduled_at, status);
CREATE INDEX IF NOT EXISTS idx_reviews_due     ON reviews(scheduled_at, status);
CREATE INDEX IF NOT EXISTS idx_synthesis_due   ON synthesis(scheduled_at, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS otps (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    code       TEXT NOT NULL,
    used       INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_otps_email ON otps(email, created_at);
CREATE INDEX IF NOT EXISTS idx_videos_course   ON videos(course_id, position);
CREATE INDEX IF NOT EXISTS idx_courses_user    ON courses(user_id);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token      TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    details     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS feature_flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '1',
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Safe migrations — ignore errors if column already exists
        for stmt in [
            "ALTER TABLE videos ADD COLUMN transcript_text TEXT",
            "ALTER TABLE users ADD COLUMN paused INTEGER DEFAULT 0",
            "ALTER TABLE courses ADD COLUMN paused INTEGER DEFAULT 0",
            "ALTER TABLE emails ADD COLUMN opened_at TEXT",
            "ALTER TABLE emails ADD COLUMN open_count INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # Seed default feature flags if not present
        defaults = [
            ("groq_fallback", "1"),
            ("spaced_rep", "1"),
            ("synthesis", "1"),
            ("welcome_email", "1"),
        ]
        for key, val in defaults:
            conn.execute("INSERT OR IGNORE INTO feature_flags (key, value) VALUES (?, ?)", (key, val))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def upsert_user(email: str, prefs: dict) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            conn.execute(
                """UPDATE users SET timezone=?, frequency=?, tone=?, depth=?, active_days=?
                   WHERE email=?""",
                (prefs.get("timezone", "Asia/Kolkata"), prefs.get("frequency", "1x"),
                 prefs.get("tone", "Casual"), prefs.get("depth", "Mix"),
                 json.dumps(prefs.get("active_days", ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])),
                 email)
            )
            return row["id"]
        else:
            user_id = uid()
            conn.execute(
                """INSERT INTO users (id, email, timezone, frequency, tone, depth, active_days)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, email,
                 prefs.get("timezone", "Asia/Kolkata"),
                 prefs.get("frequency", "1x"),
                 prefs.get("tone", "Casual"),
                 prefs.get("depth", "Mix"),
                 json.dumps(prefs.get("active_days", ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])))
            )
            return user_id


def get_user(email: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

def create_course(user_id: str, playlist_url: str, playlist_title: str, total_videos: int) -> str:
    course_id = uid()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO courses (id, user_id, playlist_url, playlist_title, total_videos)
               VALUES (?, ?, ?, ?, ?)""",
            (course_id, user_id, playlist_url, playlist_title, total_videos)
        )
    return course_id


def get_user_courses(email: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.*, u.email FROM courses c
               JOIN users u ON c.user_id = u.id
               WHERE u.email = ? ORDER BY c.created_at DESC""",
            (email,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_course_status(course_id: str, status: str):
    with get_db() as conn:
        conn.execute("UPDATE courses SET status=? WHERE id=?", (status, course_id))


def increment_videos_processed(course_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE courses SET videos_processed = videos_processed + 1 WHERE id=?",
            (course_id,)
        )
        # Mark active once first video is done
        conn.execute(
            "UPDATE courses SET status='active' WHERE id=? AND status='processing'",
            (course_id,)
        )


def cancel_course(course_id: str) -> bool:
    with get_db() as conn:
        conn.execute(
            "UPDATE emails SET status='cancelled' WHERE video_id IN (SELECT id FROM videos WHERE course_id=?) AND status='pending'",
            (course_id,)
        )
        conn.execute(
            "UPDATE reviews SET status='cancelled' WHERE video_id IN (SELECT id FROM videos WHERE course_id=?) AND status='pending'",
            (course_id,)
        )
        conn.execute(
            "UPDATE synthesis SET status='cancelled' WHERE course_id=? AND status='pending'",
            (course_id,)
        )
        result = conn.execute(
            "UPDATE courses SET status='cancelled' WHERE id=? AND status != 'cancelled'",
            (course_id,)
        )
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

def insert_video(course_id: str, youtube_id: str, title: str, position: int) -> str:
    video_id = uid()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO videos (id, course_id, youtube_id, title, position)
               VALUES (?, ?, ?, ?, ?)""",
            (video_id, course_id, youtube_id, title, position)
        )
    return video_id


def update_video_status(video_id: str, status: str, error_msg: str = None, transcript_url: str = None):
    with get_db() as conn:
        if transcript_url:
            conn.execute(
                "UPDATE videos SET status=?, error_msg=?, transcript_url=? WHERE id=?",
                (status, error_msg, transcript_url, video_id)
            )
        else:
            conn.execute(
                "UPDATE videos SET status=?, error_msg=? WHERE id=?",
                (status, error_msg, video_id)
            )


def get_video(video_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return dict(row) if row else None


def get_course_videos_ready(course_id: str) -> list:
    """Get all ready videos in a course (for synthesis check)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE course_id=? AND status='ready' ORDER BY position",
            (course_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Email scheduling
# ---------------------------------------------------------------------------

# Slot → hour of day (24h, user's local time)
SLOT_HOURS = {
    "morning":      8,
    "late_morning": 11,
    "midday":       12,
    "afternoon":    14,
    "evening":      17,
    "night":        21,
}

SLOTS_BY_FREQUENCY = {
    "1x": ["morning"],
    "2x": ["morning", "night"],
    "3x": ["morning", "midday", "night"],
    "5x": ["morning", "late_morning", "afternoon", "evening", "night"],
}


def _delivery_date(course_created_at: str, position: int, active_days: list, user_timezone: str) -> datetime:
    """
    Calculate UTC datetime of the delivery day for video at `position` (0-indexed).
    Video 0 delivers on the first active day after course creation, etc.
    """
    tz = pytz.timezone(user_timezone)
    start = datetime.fromisoformat(course_created_at).replace(tzinfo=pytz.utc).astimezone(tz)
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    while True:
        current += timedelta(days=1)
        if current.strftime("%a") in active_days:
            if count == position:
                return current
            count += 1


def schedule_video_emails(
    video_id: str,
    user_id: str,
    course_created_at: str,
    position: int,
    active_days: list,
    user_timezone: str,
    frequency: str,
    email_data: dict,   # slot -> {subject, html_body, plain_body}
):
    """Insert all email rows for a video with correct scheduled_at timestamps."""
    delivery_day = _delivery_date(course_created_at, position, active_days, user_timezone)
    tz = pytz.timezone(user_timezone)
    slots = SLOTS_BY_FREQUENCY.get(frequency, ["morning"])

    with get_db() as conn:
        for slot in slots:
            if slot not in email_data:
                continue
            hour = SLOT_HOURS.get(slot, 8)
            local_dt = delivery_day.replace(hour=hour, minute=0, second=0, microsecond=0)
            utc_dt = local_dt.astimezone(pytz.utc)
            edata = email_data[slot]
            eid = edata.get("email_id") or uid()
            conn.execute(
                """INSERT INTO emails (id, video_id, user_id, slot, subject, html_body, plain_body, scheduled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, video_id, user_id, slot,
                 edata["subject"], edata["html_body"], edata.get("plain_body", ""),
                 utc_dt.isoformat())
            )

    # Schedule spaced repetition reviews
    schedule_reviews(video_id, user_id, delivery_day, user_timezone)

    return delivery_day


def schedule_reviews(video_id: str, user_id: str, delivery_day: datetime, user_timezone: str):
    """
    Placeholder rows for day-3, day-7, day-30 reviews.
    HTML will be generated by the worker before these are due.
    """
    tz = pytz.timezone(user_timezone)
    for review_type, delta_days in [("day3", 3), ("day7", 7), ("day30", 30)]:
        review_day = delivery_day + timedelta(days=delta_days)
        local_dt = review_day.replace(hour=8, minute=0, second=0, microsecond=0)
        utc_dt = local_dt.astimezone(pytz.utc)
        with get_db() as conn:
            conn.execute(
                """INSERT INTO reviews (id, video_id, user_id, review_type, scheduled_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid(), video_id, user_id, review_type, utc_dt.isoformat())
            )


def schedule_synthesis(
    course_id: str,
    user_id: str,
    video_ids: list,
    after_datetime: datetime,
    user_timezone: str,
):
    """Insert a synthesis email row, scheduled 30 minutes after last email of the 5th video."""
    utc_dt = (after_datetime + timedelta(minutes=30)).astimezone(pytz.utc)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO synthesis (id, course_id, user_id, video_ids, scheduled_at)
               VALUES (?, ?, ?, ?, ?)""",
            (uid(), course_id, user_id, json.dumps(video_ids), utc_dt.isoformat())
        )


# ---------------------------------------------------------------------------
# Sending queue
# ---------------------------------------------------------------------------

def get_due_emails(limit: int = 50) -> list:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT e.*, u.email as user_email FROM emails e
               JOIN users u ON e.user_id = u.id
               WHERE e.scheduled_at <= ? AND e.status = 'pending'
               ORDER BY e.scheduled_at LIMIT ?""",
            (now, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_due_reviews(limit: int = 50) -> list:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT r.*, u.email as user_email, v.title as video_title
               FROM reviews r
               JOIN users u ON r.user_id = u.id
               JOIN videos v ON r.video_id = v.id
               WHERE r.scheduled_at <= ? AND r.status = 'pending' AND r.html_body IS NOT NULL
               ORDER BY r.scheduled_at LIMIT ?""",
            (now, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_due_synthesis(limit: int = 20) -> list:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, u.email as user_email FROM synthesis s
               JOIN users u ON s.user_id = u.id
               WHERE s.scheduled_at <= ? AND s.status = 'pending' AND s.html_body IS NOT NULL
               ORDER BY s.scheduled_at LIMIT ?""",
            (now, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_email_sent(email_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE emails SET status='sent', sent_at=? WHERE id=?",
            (now_utc(), email_id)
        )


def mark_review_sent(review_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE reviews SET status='sent', sent_at=? WHERE id=?",
            (now_utc(), review_id)
        )


def mark_synthesis_sent(synthesis_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE synthesis SET status='sent', sent_at=? WHERE id=?",
            (now_utc(), synthesis_id)
        )


def get_pending_reviews_for_generation(limit: int = 20) -> list:
    """Reviews that are due within the next 24h but not yet generated."""
    cutoff = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT r.*, v.title as video_title, v.id as vid_id,
                      u.tone, u.depth
               FROM reviews r
               JOIN videos v ON r.video_id = v.id
               JOIN users u ON r.user_id = u.id
               WHERE r.scheduled_at <= ? AND r.scheduled_at > ?
                 AND r.status = 'pending' AND r.html_body IS NULL
               LIMIT ?""",
            (cutoff, now, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_first_email_body_for_video(video_id: str) -> Optional[str]:
    """Pull the morning email's plain body to use as review context."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT plain_body FROM emails WHERE video_id=? AND slot='morning' LIMIT 1",
            (video_id,)
        ).fetchone()
        return row["plain_body"] if row else None


def update_review_content(review_id: str, subject: str, html_body: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE reviews SET subject=?, html_body=? WHERE id=?",
            (subject, html_body, review_id)
        )


def update_synthesis_content(synthesis_id: str, subject: str, html_body: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE synthesis SET subject=?, html_body=? WHERE id=?",
            (subject, html_body, synthesis_id)
        )


def get_pending_synthesis_for_generation(limit: int = 10) -> list:
    cutoff = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, u.tone, u.depth FROM synthesis s
               JOIN users u ON s.user_id = u.id
               WHERE s.scheduled_at <= ? AND s.scheduled_at > ?
                 AND s.status = 'pending' AND s.html_body IS NULL
               LIMIT ?""",
            (cutoff, now, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_videos_by_ids(video_ids: list) -> list:
    with get_db() as conn:
        placeholders = ",".join("?" * len(video_ids))
        rows = conn.execute(
            f"SELECT * FROM videos WHERE id IN ({placeholders})", video_ids
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

def enqueue_job(job_type: str, payload: dict) -> str:
    job_id = uid()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, payload) VALUES (?, ?, ?)",
            (job_id, job_type, json.dumps(payload))
        )
    return job_id


def claim_next_job() -> Optional[dict]:
    with get_db() as conn:
        # Reset jobs stuck in 'processing' for >30 minutes back to 'pending'
        timeout_cutoff = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        conn.execute(
            """UPDATE jobs SET status='pending', message='Requeued after timeout', updated_at=?
               WHERE status='processing' AND updated_at < ?""",
            (now_utc(), timeout_cutoff)
        )
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
            (now_utc(), row["id"])
        )
        return dict(row)


def update_job(job_id: str, progress: int, message: str, status: str = "processing"):
    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET progress=?, message=?, status=?, updated_at=? WHERE id=?",
            (progress, message, status, now_utc(), job_id)
        )


def get_job(job_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def cancel_job(job_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status NOT IN ('done','cancelled')",
            (now_utc(), job_id)
        )
        return cur.rowcount > 0


def requeue_job(job_id: str) -> bool:
    """Reset any job back to pending so the worker picks it up again."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE jobs SET status='pending', progress=0, message='Manually requeued', updated_at=? WHERE id=?",
            (now_utc(), job_id)
        )
        return True


# ---------------------------------------------------------------------------
# Monitoring helpers (for GSheets sync)
# ---------------------------------------------------------------------------

def get_all_users() -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()]


def get_all_courses() -> list:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.*, u.email FROM courses c
               JOIN users u ON c.user_id = u.id
               ORDER BY c.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schedule view helpers
# ---------------------------------------------------------------------------

def get_course_schedule(course_id: str) -> list:
    """Returns all videos with their email slots for the schedule view."""
    with get_db() as conn:
        videos = conn.execute(
            "SELECT * FROM videos WHERE course_id=? ORDER BY position",
            (course_id,)
        ).fetchall()
        result = []
        for v in videos:
            emails = conn.execute(
                """SELECT id, slot, status, scheduled_at, sent_at, subject,
                          CASE WHEN html_body IS NOT NULL THEN 1 ELSE 0 END as has_body
                   FROM emails WHERE video_id=? ORDER BY scheduled_at""",
                (v["id"],)
            ).fetchall()
            vd = dict(v)
            vd["emails"] = [dict(e) for e in emails]
            result.append(vd)
        return result


def get_video_emails_content(video_id: str) -> list:
    """All emails for a video (sent + generated pending) for consolidated view."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, slot, subject, html_body, status, scheduled_at, sent_at
               FROM emails WHERE video_id=? AND html_body IS NOT NULL ORDER BY scheduled_at""",
            (video_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_email_for_send(email_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT e.*, u.email as user_email FROM emails e JOIN users u ON e.user_id = u.id WHERE e.id=?",
            (email_id,)
        ).fetchone()
        return dict(row) if row else None


def get_first_pending_email_for_course(course_id: str) -> Optional[dict]:
    """First pending+generated email across all videos in a course."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT e.*, u.email as user_email
               FROM emails e
               JOIN videos v ON e.video_id = v.id
               JOIN users u ON e.user_id = u.id
               WHERE v.course_id = ? AND e.status = 'pending' AND e.html_body IS NOT NULL
               ORDER BY e.scheduled_at LIMIT 1""",
            (course_id,)
        ).fetchone()
        return dict(row) if row else None


def get_email_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as n FROM emails").fetchone()["n"]
        sent  = conn.execute("SELECT COUNT(*) as n FROM emails WHERE status='sent'").fetchone()["n"]
        today = datetime.utcnow().date().isoformat()
        sent_today = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE status='sent' AND sent_at LIKE ?",
            (f"{today}%",)
        ).fetchone()["n"]
        pending = conn.execute("SELECT COUNT(*) as n FROM emails WHERE status='pending'").fetchone()["n"]
        return {"total": total, "sent": sent, "sent_today": sent_today, "pending": pending}


def reset_videos_processed(course_id: str, count: int):
    with get_db() as conn:
        conn.execute("UPDATE courses SET videos_processed=? WHERE id=?", (count, course_id))


def reset_course_for_reprocess(course_id: str):
    """Reset a stuck/failed course so the worker will reprocess skipped/failed videos."""
    with get_db() as conn:
        # Reset skipped/failed videos back to pending
        conn.execute(
            "UPDATE videos SET status='pending', error_msg=NULL WHERE course_id=? AND status IN ('skipped', 'failed')",
            (course_id,)
        )
        # Recalculate videos_processed (only count 'ready' ones)
        ready_count = conn.execute(
            "SELECT COUNT(*) as n FROM videos WHERE course_id=? AND status='ready'",
            (course_id,)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE courses SET status='processing', videos_processed=? WHERE id=?",
            (ready_count, course_id)
        )


def get_admin_jobs(limit: int = 100) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, type, status, progress, message, created_at, updated_at, payload FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# OTP verification
# ---------------------------------------------------------------------------

import random as _random

def create_otp(email: str) -> str:
    code = f"{_random.randint(100000, 999999)}"
    otp_id = uid()
    with get_db() as conn:
        conn.execute("DELETE FROM otps WHERE email=?", (email,))
        conn.execute(
            "INSERT INTO otps (id, email, code) VALUES (?, ?, ?)",
            (otp_id, email, code)
        )
    return code


# ---------------------------------------------------------------------------
# Admin sessions
# ---------------------------------------------------------------------------

import secrets as _secrets

def create_admin_session() -> str:
    token = _secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    with get_db() as conn:
        # Clean expired sessions
        conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now_utc(),))
        conn.execute("INSERT INTO admin_sessions (token, expires_at) VALUES (?, ?)", (token, expires))
    return token


def validate_admin_session(token: str) -> bool:
    if not token:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT token FROM admin_sessions WHERE token=? AND expires_at > ?",
            (token, now_utc())
        ).fetchone()
        return row is not None


def delete_admin_session(token: str):
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token=?", (token,))


# ---------------------------------------------------------------------------
# Admin CRUD — Users
# ---------------------------------------------------------------------------

def delete_user(user_id: str) -> bool:
    with get_db() as conn:
        # Cascade: emails, reviews, synthesis, videos, courses, then user
        conn.execute("DELETE FROM emails WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM reviews WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM synthesis WHERE user_id=?", (user_id,))
        course_ids = [r["id"] for r in conn.execute("SELECT id FROM courses WHERE user_id=?", (user_id,)).fetchall()]
        for cid in course_ids:
            conn.execute("DELETE FROM videos WHERE course_id=?", (cid,))
        conn.execute("DELETE FROM courses WHERE user_id=?", (user_id,))
        result = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Admin CRUD — Courses
# ---------------------------------------------------------------------------

def delete_course(course_id: str) -> bool:
    with get_db() as conn:
        video_ids = [r["id"] for r in conn.execute("SELECT id FROM videos WHERE course_id=?", (course_id,)).fetchall()]
        for vid in video_ids:
            conn.execute("DELETE FROM emails WHERE video_id=?", (vid,))
            conn.execute("DELETE FROM reviews WHERE video_id=?", (vid,))
        conn.execute("DELETE FROM synthesis WHERE course_id=?", (course_id,))
        conn.execute("DELETE FROM videos WHERE course_id=?", (course_id,))
        result = conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
        return result.rowcount > 0


def get_course_videos_admin(course_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE course_id=? ORDER BY position",
            (course_id,)
        ).fetchall()
        result = []
        for v in rows:
            vd = dict(v)
            email_count = conn.execute(
                "SELECT COUNT(*) as n FROM emails WHERE video_id=?", (v["id"],)
            ).fetchone()["n"]
            vd["email_count"] = email_count
            vd["has_transcript"] = bool(vd.get("transcript_text") or vd.get("transcript_url"))
            result.append(vd)
        return result


# ---------------------------------------------------------------------------
# Admin CRUD — Jobs
# ---------------------------------------------------------------------------

def cancel_job(job_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE jobs SET status='failed', message='Cancelled by admin', updated_at=? WHERE id=? AND status IN ('pending','processing')",
            (now_utc(), job_id)
        )
        return result.rowcount > 0


def requeue_job(job_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE jobs SET status='pending', progress=0, message='Requeued by admin', updated_at=? WHERE id=?",
            (now_utc(), job_id)
        )
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Admin CRUD — Emails
# ---------------------------------------------------------------------------

def get_all_emails(limit: int = 200, offset: int = 0, status_filter: str = None) -> list:
    with get_db() as conn:
        if status_filter:
            rows = conn.execute(
                """SELECT e.id, e.video_id, e.user_id, e.slot, e.subject,
                          e.scheduled_at, e.sent_at, e.status, e.created_at,
                          e.opened_at, e.open_count,
                          u.email as user_email, v.title as video_title, c.playlist_title
                   FROM emails e
                   JOIN users u ON e.user_id = u.id
                   JOIN videos v ON e.video_id = v.id
                   JOIN courses c ON v.course_id = c.id
                   WHERE e.status=?
                   ORDER BY e.scheduled_at DESC LIMIT ? OFFSET ?""",
                (status_filter, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.id, e.video_id, e.user_id, e.slot, e.subject,
                          e.scheduled_at, e.sent_at, e.status, e.created_at,
                          e.opened_at, e.open_count,
                          u.email as user_email, v.title as video_title, c.playlist_title
                   FROM emails e
                   JOIN users u ON e.user_id = u.id
                   JOIN videos v ON e.video_id = v.id
                   JOIN courses c ON v.course_id = c.id
                   ORDER BY e.scheduled_at DESC LIMIT ? OFFSET ?""",
                (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]


def cancel_email(email_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE emails SET status='cancelled', updated_at=? WHERE id=? AND status='pending'",
            (now_utc(), email_id)
        )
        return result.rowcount > 0


def reset_email_to_pending(email_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE emails SET status='pending', sent_at=NULL WHERE id=?",
            (email_id,)
        )
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def save_transcript(video_id: str, text: str):
    with get_db() as conn:
        conn.execute("UPDATE videos SET transcript_text=? WHERE id=?", (text, video_id))


def get_video_transcript(video_id: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("SELECT transcript_text FROM videos WHERE id=?", (video_id,)).fetchone()
        return row["transcript_text"] if row else None


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------

def get_system_stats() -> dict:
    import os as _os
    db_size = _os.path.getsize(DATABASE_PATH) if _os.path.exists(DATABASE_PATH) else 0
    with get_db() as conn:
        users    = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        courses  = conn.execute("SELECT COUNT(*) as n FROM courses").fetchone()["n"]
        videos   = conn.execute("SELECT COUNT(*) as n FROM videos").fetchone()["n"]
        emails   = conn.execute("SELECT COUNT(*) as n FROM emails").fetchone()["n"]
        reviews  = conn.execute("SELECT COUNT(*) as n FROM reviews").fetchone()["n"]
        jobs     = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
        sessions = conn.execute("SELECT COUNT(*) as n FROM admin_sessions WHERE expires_at > ?", (now_utc(),)).fetchone()["n"]
    return {
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "row_counts": {"users": users, "courses": courses, "videos": videos, "emails": emails, "reviews": reviews, "jobs": jobs},
        "active_admin_sessions": sessions,
    }


def verify_otp(email: str, code: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id FROM otps
               WHERE email=? AND code=? AND used=0
               AND created_at > datetime('now', '-10 minutes')""",
            (email, code)
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
        return True


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

def add_audit(action: str, entity_type: str = None, entity_id: str = None, details: str = None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)",
            (uid(), action, entity_type, entity_id, details)
        )


def get_audit_log(limit: int = 200, offset: int = 0) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Feature Flags
# ---------------------------------------------------------------------------

def get_feature_flags() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM feature_flags").fetchall()
        return {r["key"]: r["value"] == "1" for r in rows}


def set_feature_flag(key: str, enabled: bool):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feature_flags (key, value, updated_at) VALUES (?, ?, ?)",
            (key, "1" if enabled else "0", now_utc())
        )


# ---------------------------------------------------------------------------
# Admin — User controls
# ---------------------------------------------------------------------------

def update_user_settings(user_id: str, **fields) -> bool:
    allowed = {"timezone", "frequency", "tone", "depth", "active_days", "email"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if "active_days" in updates and isinstance(updates["active_days"], list):
        updates["active_days"] = json.dumps(updates["active_days"])
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [user_id]
    with get_db() as conn:
        result = conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", values)
        return result.rowcount > 0


def pause_user(user_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute("UPDATE users SET paused=1 WHERE id=?", (user_id,))
        return result.rowcount > 0


def resume_user(user_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute("UPDATE users SET paused=0 WHERE id=?", (user_id,))
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Admin — Course controls
# ---------------------------------------------------------------------------

def pause_course(course_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute("UPDATE courses SET paused=1 WHERE id=?", (course_id,))
        return result.rowcount > 0


def resume_course(course_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute("UPDATE courses SET paused=0 WHERE id=?", (course_id,))
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Admin — Video controls
# ---------------------------------------------------------------------------

def retry_video(video_id: str) -> bool:
    """Reset a single skipped/failed video to pending so the worker retries it."""
    with get_db() as conn:
        result = conn.execute(
            "UPDATE videos SET status='pending', error_msg=NULL WHERE id=? AND status IN ('skipped','failed')",
            (video_id,)
        )
        return result.rowcount > 0


def update_video_transcript(video_id: str, text: str) -> bool:
    with get_db() as conn:
        result = conn.execute("UPDATE videos SET transcript_text=? WHERE id=?", (text, video_id))
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Admin — Email controls
# ---------------------------------------------------------------------------

def reschedule_email(email_id: str, new_datetime: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE emails SET scheduled_at=? WHERE id=?",
            (new_datetime, email_id)
        )
        return result.rowcount > 0


def edit_email_content(email_id: str, subject: str, html_body: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE emails SET subject=?, html_body=? WHERE id=?",
            (subject, html_body, email_id)
        )
        return result.rowcount > 0


def get_all_transcripts(limit: int = 200, offset: int = 0) -> list:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT v.id, v.title, v.youtube_id, v.status, v.position,
                      c.playlist_title, u.email,
                      CASE WHEN v.transcript_text IS NOT NULL THEN length(v.transcript_text) ELSE 0 END as transcript_len
               FROM videos v
               JOIN courses c ON v.course_id = c.id
               JOIN users u ON c.user_id = u.id
               WHERE v.transcript_text IS NOT NULL
               ORDER BY v.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_user_emails_for_broadcast() -> list:
    """Returns all distinct user emails for broadcast."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT email FROM users WHERE paused=0 ORDER BY email"
        ).fetchall()
        return [r["email"] for r in rows]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def get_analytics() -> dict:
    with get_db() as conn:
        # Enrollment funnel
        total_users    = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        total_courses  = conn.execute("SELECT COUNT(*) as n FROM courses").fetchone()["n"]
        active_courses = conn.execute("SELECT COUNT(*) as n FROM courses WHERE status='active'").fetchone()["n"]
        total_emails   = conn.execute("SELECT COUNT(*) as n FROM emails").fetchone()["n"]
        sent_emails    = conn.execute("SELECT COUNT(*) as n FROM emails WHERE status='sent'").fetchone()["n"]

        # Video status breakdown
        video_statuses = conn.execute(
            "SELECT status, COUNT(*) as n FROM videos GROUP BY status"
        ).fetchall()
        video_by_status = {r["status"]: r["n"] for r in video_statuses}

        # Emails sent per day (last 14 days)
        daily = conn.execute(
            """SELECT DATE(sent_at) as day, COUNT(*) as n FROM emails
               WHERE status='sent' AND sent_at >= datetime('now', '-14 days')
               GROUP BY day ORDER BY day""",
        ).fetchall()

        # Slot breakdown
        slots = conn.execute(
            "SELECT slot, COUNT(*) as n FROM emails WHERE status='sent' GROUP BY slot ORDER BY n DESC"
        ).fetchall()

        # Error log — failed videos
        errors = conn.execute(
            """SELECT v.id, v.title, v.error_msg, v.youtube_id, c.playlist_title, u.email
               FROM videos v
               JOIN courses c ON v.course_id = c.id
               JOIN users u ON c.user_id = u.id
               WHERE v.status IN ('failed','skipped') AND v.error_msg IS NOT NULL
               ORDER BY v.created_at DESC LIMIT 50"""
        ).fetchall()

        # Paused users/courses
        paused_users   = conn.execute("SELECT COUNT(*) as n FROM users WHERE paused=1").fetchone()["n"]
        paused_courses = conn.execute("SELECT COUNT(*) as n FROM courses WHERE paused=1").fetchone()["n"]

    return {
        "funnel": {
            "users": total_users,
            "courses": total_courses,
            "active_courses": active_courses,
            "emails_generated": total_emails,
            "emails_sent": sent_emails,
        },
        "video_statuses": video_by_status,
        "daily_sent": [{"day": r["day"], "n": r["n"]} for r in daily],
        "slot_breakdown": [{"slot": r["slot"], "n": r["n"]} for r in slots],
        "errors": [dict(r) for r in errors],
        "paused_users": paused_users,
        "paused_courses": paused_courses,
    }


def log_email_open(email_id: str):
    """Record an email open event — called from the tracking pixel endpoint."""
    with get_db() as conn:
        conn.execute(
            """UPDATE emails
               SET open_count = COALESCE(open_count, 0) + 1,
                   opened_at  = CASE WHEN opened_at IS NULL THEN ? ELSE opened_at END
               WHERE id = ?""",
            (now_utc(), email_id)
        )


def get_open_stats() -> dict:
    """Returns opens today, 7-day open rate, total unique opens."""
    with get_db() as conn:
        opens_today = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE opened_at >= date('now') AND status='sent'"
        ).fetchone()["n"]
        sent_7d = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE sent_at >= date('now','-7 days')"
        ).fetchone()["n"]
        opened_7d = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE opened_at >= date('now','-7 days') AND status='sent'"
        ).fetchone()["n"]
        total_unique_opens = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE opened_at IS NOT NULL"
        ).fetchone()["n"]
    open_rate_7d = round(opened_7d / sent_7d * 100, 1) if sent_7d > 0 else 0.0
    return {
        "opens_today": opens_today,
        "open_rate_7d": open_rate_7d,
        "total_unique_opens": total_unique_opens,
        "sent_7d": sent_7d,
        "opened_7d": opened_7d,
    }


# ---------------------------------------------------------------------------
# Intelligence layer — activity feed, health scores, slot stats, etc.
# ---------------------------------------------------------------------------

def get_activity_feed(limit: int = 40) -> list:
    """Synthesize recent events from multiple tables into a unified feed."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM (
                SELECT 'enrolled'    AS type,
                       u.email,
                       COALESCE(c.playlist_title,'Untitled') AS title,
                       c.id AS entity_id,
                       c.created_at AS ts
                FROM courses c JOIN users u ON u.id = c.user_id

                UNION ALL

                SELECT 'email_sent'  AS type,
                       u.email,
                       e.subject AS title,
                       e.id AS entity_id,
                       e.sent_at AS ts
                FROM emails e JOIN users u ON u.id = e.user_id
                WHERE e.status = 'sent' AND e.sent_at IS NOT NULL
                  AND e.sent_at >= datetime('now','-48 hours')

                UNION ALL

                SELECT 'email_opened' AS type,
                       u.email,
                       e.subject AS title,
                       e.id AS entity_id,
                       e.opened_at AS ts
                FROM emails e JOIN users u ON u.id = e.user_id
                WHERE e.opened_at IS NOT NULL
                  AND e.opened_at >= datetime('now','-48 hours')

                UNION ALL

                SELECT 'job_done'    AS type,
                       '' AS email,
                       'Processing complete' AS title,
                       j.id AS entity_id,
                       j.updated_at AS ts
                FROM jobs j
                WHERE j.status = 'done'
                  AND j.updated_at >= datetime('now','-48 hours')

                UNION ALL

                SELECT 'job_failed'  AS type,
                       '' AS email,
                       COALESCE(j.message, 'Job failed') AS title,
                       j.id AS entity_id,
                       j.updated_at AS ts
                FROM jobs j
                WHERE j.status = 'failed'
                  AND j.updated_at >= datetime('now','-48 hours')
            )
            ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_slot_stats() -> list:
    """Open rate broken down by email slot."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT slot,
                   COUNT(*) AS sent,
                   SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened
            FROM emails
            WHERE status = 'sent'
            GROUP BY slot
            ORDER BY sent DESC
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["open_rate"] = round(d["opened"] / d["sent"] * 100, 1) if d["sent"] > 0 else 0.0
        result.append(d)
    return result


def get_heartbeat(days: int = 14) -> list:
    """Emails sent per day for the last N days."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT date(sent_at) AS day, COUNT(*) AS count
            FROM emails
            WHERE status = 'sent' AND sent_at IS NOT NULL
              AND sent_at >= datetime('now', ? || ' days')
            GROUP BY day
            ORDER BY day ASC
        """, (f"-{days}",)).fetchall()
    return [dict(r) for r in rows]


def get_user_health() -> list:
    """Per-user health classification: active / at_risk / ghost / new."""
    cutoff_7d = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.id, u.email, u.created_at,
                   COUNT(DISTINCT c.id)  AS course_count,
                   SUM(CASE WHEN e.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS total_opens,
                   SUM(CASE WHEN e.status='sent' THEN 1 ELSE 0 END)         AS total_sent,
                   MAX(e.opened_at)                                          AS last_opened
            FROM users u
            LEFT JOIN courses c ON c.user_id = u.id
            LEFT JOIN emails  e ON e.user_id = u.id
            GROUP BY u.id
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        total_opens = d.get("total_opens") or 0
        total_sent  = d.get("total_sent")  or 0
        last_opened = d.get("last_opened")
        if total_sent == 0:
            health = "new"
        elif total_opens == 0:
            health = "ghost"
        elif last_opened and last_opened < cutoff_7d:
            health = "at_risk"
        elif total_sent > 0 and total_opens / total_sent < 0.20:
            health = "at_risk"
        else:
            health = "active"
        d["health"] = health
        d["open_rate"] = round(total_opens / total_sent * 100, 1) if total_sent > 0 else 0.0
        result.append(d)
    return result


def get_stale_jobs() -> list:
    """Jobs stuck in 'processing' for more than 10 minutes."""
    cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM jobs
            WHERE status = 'processing' AND updated_at < ?
            ORDER BY updated_at ASC
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_dow_heatmap() -> list:
    """Email opens broken down by day of week."""
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    with get_db() as conn:
        rows = conn.execute("""
            SELECT strftime('%w', opened_at) AS dow, COUNT(*) AS opens
            FROM emails
            WHERE opened_at IS NOT NULL
            GROUP BY dow
        """).fetchall()
    data = {str(i): 0 for i in range(7)}
    for r in rows:
        data[r["dow"]] = r["opens"]
    return [{"day": days[i], "opens": data[str(i)]} for i in range(7)]


def get_unit_economics() -> dict:
    """Rough cost estimates based on DeepSeek v3-0324 pricing."""
    # DeepSeek v3-0324: $0.20/M input, $0.77/M output
    # Avg video: ~8K input tokens + ~1.5K output tokens per slot call
    # With 1x freq = 1 slot call; 5x = 5 slot calls
    COST_PER_VIDEO = 0.002  # ~$0.002 per video processed (blended avg)
    with get_db() as conn:
        videos_processed = conn.execute(
            "SELECT COUNT(*) as n FROM videos WHERE status='ready'"
        ).fetchone()["n"]
        emails_sent = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE status='sent'"
        ).fetchone()["n"]
        total_users = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        active_users = conn.execute(
            "SELECT COUNT(*) as n FROM users WHERE paused=0"
        ).fetchone()["n"]
        pending_emails = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE status='pending'"
        ).fetchone()["n"]
    est_llm_cost = round(videos_processed * COST_PER_VIDEO, 3)
    return {
        "videos_processed":   videos_processed,
        "emails_sent":        emails_sent,
        "total_users":        total_users,
        "active_users":       active_users,
        "pending_emails":     pending_emails,
        "est_llm_cost_usd":   est_llm_cost,
        "cost_per_email":     round(est_llm_cost / emails_sent, 5) if emails_sent > 0 else 0,
        "cost_per_user":      round(est_llm_cost / total_users, 3) if total_users > 0 else 0,
    }


def search_all(q: str) -> dict:
    """Global search across users, courses, and emails."""
    if not q or len(q) < 2:
        return {"users": [], "courses": [], "emails": []}
    like = f"%{q}%"
    with get_db() as conn:
        users = conn.execute(
            "SELECT id, email, created_at FROM users WHERE email LIKE ? LIMIT 10",
            (like,)
        ).fetchall()
        courses = conn.execute(
            """SELECT c.id, c.playlist_title, c.status, u.email
               FROM courses c JOIN users u ON u.id = c.user_id
               WHERE c.playlist_title LIKE ? OR c.playlist_url LIKE ? LIMIT 10""",
            (like, like)
        ).fetchall()
        emails = conn.execute(
            """SELECT e.id, e.subject, e.status, e.slot, u.email
               FROM emails e JOIN users u ON u.id = e.user_id
               WHERE e.subject LIKE ? LIMIT 10""",
            (like,)
        ).fetchall()
    return {
        "users":   [dict(r) for r in users],
        "courses": [dict(r) for r in courses],
        "emails":  [dict(r) for r in emails],
    }


# ---------------------------------------------------------------------------
# LLM trace storage (from OpenRouter webhook)
# ---------------------------------------------------------------------------

def save_llm_trace(trace: dict) -> None:
    """Store an OpenRouter observability webhook payload."""
    # OpenRouter sends different shapes — extract what we can defensively
    gen  = trace.get("generation", trace)  # some payloads nest under 'generation'
    usage = gen.get("usage") or gen.get("native_tokens_completion") or {}
    if isinstance(usage, dict):
        prompt_tokens     = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total_tokens      = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    else:
        prompt_tokens = completion_tokens = total_tokens = 0

    cost_usd   = float(gen.get("total_cost") or gen.get("cost") or 0)
    latency_ms = int(gen.get("latency") or gen.get("generation_time") or 0)
    model      = gen.get("model") or gen.get("model_id") or trace.get("model") or ""
    trace_id   = gen.get("id") or trace.get("id") or uid()

    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO llm_traces
               (id, model, prompt_tokens, completion_tokens, total_tokens, cost_usd, latency_ms, raw, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, model, prompt_tokens, completion_tokens, total_tokens,
             cost_usd, latency_ms, None, now_utc())
        )


def prune_old_sessions() -> None:
    """Delete admin sessions older than 7 days."""
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE created_at < datetime('now', '-7 days')")


def get_llm_trace_stats() -> dict:
    """Aggregate stats from stored LLM traces."""
    with get_db() as conn:
        totals = conn.execute("""
            SELECT COUNT(*) as calls,
                   SUM(total_tokens)      as tokens,
                   SUM(cost_usd)          as cost,
                   AVG(latency_ms)        as avg_latency,
                   SUM(prompt_tokens)     as prompt_tokens,
                   SUM(completion_tokens) as completion_tokens
            FROM llm_traces
        """).fetchone()
        by_model = conn.execute("""
            SELECT model, COUNT(*) as calls, SUM(total_tokens) as tokens, SUM(cost_usd) as cost
            FROM llm_traces
            GROUP BY model ORDER BY cost DESC LIMIT 10
        """).fetchall()
        daily = conn.execute("""
            SELECT date(created_at) as day, COUNT(*) as calls, SUM(cost_usd) as cost
            FROM llm_traces
            WHERE created_at >= datetime('now', '-14 days')
            GROUP BY day ORDER BY day ASC
        """).fetchall()
        recent = conn.execute("""
            SELECT id, model, total_tokens, cost_usd, latency_ms, created_at
            FROM llm_traces ORDER BY created_at DESC LIMIT 20
        """).fetchall()
    t = dict(totals)
    return {
        "total_calls":         t.get("calls") or 0,
        "total_tokens":        t.get("tokens") or 0,
        "total_cost_usd":      round(t.get("cost") or 0, 5),
        "avg_latency_ms":      round(t.get("avg_latency") or 0),
        "total_prompt_tokens": t.get("prompt_tokens") or 0,
        "total_comp_tokens":   t.get("completion_tokens") or 0,
        "by_model":            [dict(r) for r in by_model],
        "daily":               [dict(r) for r in daily],
        "recent":              [dict(r) for r in recent],
    }
