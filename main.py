"""
main.py — FastAPI app. Single entry point.

Endpoints:
  POST /api/enroll          create course + enqueue job
  GET  /api/job/{id}        poll job progress
  GET  /api/job/{id}/stream SSE stream for live progress
  GET  /api/courses         list user's courses
  DELETE /api/courses/{id}  cancel course
  GET  /api/cron            manual trigger for scheduled jobs (Vercel/cron fallback)
  GET  /                    serve index.html
"""
import json
import asyncio
import hmac
import time
import collections
import bcrypt
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

import db
from worker import start_worker
from scheduler_jobs import send_due_emails, generate_upcoming, sync_to_sheets, advance_processing
from transcript import resolve_playlist
from mailer import send_email as do_send_email, send_welcome_email
from config import CRON_SECRET, ADMIN_SECRET, ADMIN_USER, ADMIN_PASSWORD, ADMIN_PASSWORD_HASH, SENTRY_DSN, CORS_ORIGINS

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        traces_sample_rate=0.05,
        send_default_pii=False,
    )

from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI(title="Capsule")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# In-memory rate limiting
# ---------------------------------------------------------------------------

# email -> deque of timestamps
_otp_rate: dict = collections.defaultdict(collections.deque)
_enroll_rate: dict = collections.defaultdict(collections.deque)


_MAX_RATE_KEYS = 10000  # prevent memory leak from enumeration attacks

def _check_rate(store: dict, key: str, max_requests: int, window_seconds: int):
    """Raise 429 if key exceeds max_requests within window_seconds."""
    now = time.time()
    # Evict stale keys if store grows too large
    if len(store) > _MAX_RATE_KEYS:
        stale = [k for k, v in store.items() if not v or now - v[-1] > window_seconds]
        for k in stale:
            del store[k]
    q = store[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= max_requests:
        raise HTTPException(429, "Too many requests — try again later")
    q.append(now)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()

@app.on_event("startup")
def startup():
    db.init_db()
    start_worker()

    scheduler.add_job(send_due_emails,   "interval", minutes=5,  id="send_emails")
    scheduler.add_job(generate_upcoming, "interval", hours=1,    id="generate_upcoming")
    scheduler.add_job(sync_to_sheets,    "interval", hours=1,    id="sync_sheets")
    scheduler.add_job(advance_processing, "cron",    hour=20, minute=30, id="advance_processing")  # 2am IST
    scheduler.start()
    print("[main] ✅ MindOS started")


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    try:
        with open("index.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>index.html not found</h1>"


# ---------------------------------------------------------------------------
# Enroll
# ---------------------------------------------------------------------------

class EnrollRequest(BaseModel):
    email: str
    playlist_url: str
    frequency: str  = "1x"
    tone: str       = "Casual"
    depth: str      = "Mix"
    timezone: str   = "Asia/Kolkata"
    active_days: List[str] = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]


_ALLOWED_YT_PREFIXES = (
    "https://youtube.com/",
    "https://www.youtube.com/",
    "https://youtu.be/",
)


@app.post("/api/enroll")
def enroll(req: EnrollRequest):
    # Rate limit: 5 per email per hour
    _check_rate(_enroll_rate, req.email.strip().lower(), 5, 3600)

    # Validate playlist URL
    if not req.playlist_url.startswith(_ALLOWED_YT_PREFIXES):
        raise HTTPException(400, "Invalid URL — only YouTube playlist URLs are accepted")

    # Resolve playlist first to validate URL and get video count
    playlist_title, videos = resolve_playlist(req.playlist_url)
    if not videos:
        raise HTTPException(400, "Could not resolve playlist. Check the URL and try again.")

    prefs = {
        "timezone":    req.timezone,
        "frequency":   req.frequency,
        "tone":        req.tone,
        "depth":       req.depth,
        "active_days": req.active_days,
    }

    user_id   = db.upsert_user(req.email, prefs)
    course_id = db.create_course(user_id, req.playlist_url, playlist_title, len(videos))

    # Insert video stubs immediately so frontend can show the queue
    for i, v in enumerate(videos):
        db.insert_video(course_id, v["id"], v["title"], i)

    # Enqueue background processing job — JIT: process video 1 now, rest queued daily
    job_id = db.enqueue_job("process_playlist", {
        "user_id":      user_id,
        "course_id":    course_id,
        "playlist_url": req.playlist_url,
        "max_videos":   1,
    })

    # Send welcome email (fire-and-forget — don't block response)
    import threading
    threading.Thread(
        target=send_welcome_email,
        args=(req.email, playlist_title, len(videos), req.frequency, req.timezone),
        daemon=True,
    ).start()

    return {
        "success":        True,
        "job_id":         job_id,
        "course_id":      course_id,
        "playlist_title": playlist_title,
        "total_videos":   len(videos),
        "videos":         videos[:10],  # preview first 10
    }


# ---------------------------------------------------------------------------
# Job status (polling)
# ---------------------------------------------------------------------------

@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ---------------------------------------------------------------------------
# Job progress (SSE stream)
# ---------------------------------------------------------------------------

@app.get("/api/job/{job_id}/stream")
async def stream_job(job_id: str):
    async def generate():
        prev_progress = -1
        prev_message  = ""
        stale_ticks   = 0

        while True:
            job = db.get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'job not found'})}\n\n"
                break

            changed = (job["progress"] != prev_progress or job["message"] != prev_message)
            if changed:
                stale_ticks = 0
                prev_progress = job["progress"]
                prev_message  = job["message"]
                yield f"data: {json.dumps({'progress': job['progress'], 'message': job['message'], 'status': job['status']})}\n\n"

            if job["status"] in ("done", "failed"):
                break

            stale_ticks += 1
            if stale_ticks > 360:   # 30 min timeout
                yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
                break

            await asyncio.sleep(5)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

@app.get("/api/courses")
def get_courses(email: str):
    if not email:
        raise HTTPException(400, "Email required")
    courses = db.get_user_courses(email)
    return {"courses": courses}


@app.delete("/api/courses/{course_id}")
def cancel_course(course_id: str, email: str):
    if not email:
        raise HTTPException(400, "Email required")
    # Verify ownership
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT c.id FROM courses c JOIN users u ON c.user_id = u.id WHERE c.id=? AND u.email=?",
            (course_id, email)
        ).fetchone()
    if not row:
        raise HTTPException(403, "Access denied")
    ok = db.cancel_course(course_id)
    if not ok:
        raise HTTPException(404, "Course not found or already cancelled")
    return {"success": True}


# ---------------------------------------------------------------------------
# Schedule view — per-course video + email breakdown
# ---------------------------------------------------------------------------

@app.get("/api/courses/{course_id}/schedule")
def get_course_schedule(course_id: str, email: str):
    user = db.get_user(email)
    if not user:
        raise HTTPException(404, "User not found")
    videos = db.get_course_schedule(course_id)
    return {"videos": videos, "timezone": user["timezone"], "frequency": user["frequency"]}


@app.get("/api/videos/{video_id}/emails")
def get_video_emails(video_id: str):
    emails = db.get_video_emails_content(video_id)
    return {"emails": emails}


# ---------------------------------------------------------------------------
# Send-now actions
# ---------------------------------------------------------------------------

class SendNowBody(BaseModel):
    email: str


@app.post("/api/emails/{email_id}/send-now")
def send_email_now(email_id: str, body: SendNowBody):
    row = db.get_email_for_send(email_id)
    if not row:
        raise HTTPException(404, "Email not found")
    # Verify the requesting user owns this email
    if row.get("user_email") != body.email:
        raise HTTPException(403, "Access denied")
    if row["status"] == "sent":
        return {"success": True, "already_sent": True}
    # Guard: don't allow sending emails scheduled >48h in the future
    if row.get("scheduled_at"):
        sched = datetime.fromisoformat(row["scheduled_at"])
        if sched > datetime.utcnow() + timedelta(hours=48):
            print(f"[send-now] rejected email {email_id}: scheduled_at {row['scheduled_at']} is >48h in the future")
            raise HTTPException(400, "Email is scheduled too far in the future — wait until closer to its delivery date")
    if not row.get("html_body"):
        raise HTTPException(400, "Email not yet generated — check back shortly")
    ok = do_send_email(body.email, row["subject"] or "Your MindOS lesson", row["html_body"])
    if ok:
        db.mark_email_sent(email_id)
        return {"success": True}
    raise HTTPException(500, "Failed to send — check server logs")


@app.post("/api/courses/{course_id}/send-first")
def send_first_email(course_id: str, body: SendNowBody):
    # Verify the requesting user owns this course
    with db.get_db() as conn:
        owner = conn.execute(
            "SELECT c.id FROM courses c JOIN users u ON c.user_id = u.id WHERE c.id=? AND u.email=?",
            (course_id, body.email)
        ).fetchone()
    if not owner:
        raise HTTPException(403, "Access denied")
    row = db.get_first_pending_email_for_course(course_id)
    if not row:
        raise HTTPException(404, "not_ready")
    # Guard: don't allow sending emails scheduled >48h in the future
    if row.get("scheduled_at"):
        sched = datetime.fromisoformat(row["scheduled_at"])
        if sched > datetime.utcnow() + timedelta(hours=48):
            print(f"[send-first] rejected email {row['id']}: scheduled_at {row['scheduled_at']} is >48h in the future")
            raise HTTPException(400, "Email is scheduled too far in the future — wait until closer to its delivery date")
    ok = do_send_email(body.email, row["subject"] or "Your first MindOS lesson", row["html_body"])
    if ok:
        db.mark_email_sent(row["id"])
        return {"success": True, "subject": row["subject"]}
    raise HTTPException(500, "Failed to send")


# ---------------------------------------------------------------------------
# OTP auth (for My Courses email verification)
# ---------------------------------------------------------------------------

class OtpRequest(BaseModel):
    email: str

class OtpVerify(BaseModel):
    email: str
    code: str


def _otp_email_html(code: str, email: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#FAFAF8;font-family:Helvetica,Arial,sans-serif;">
<table width="100%" bgcolor="#FAFAF8"><tr><td align="center" style="padding:48px 20px;">
<table width="480" style="max-width:480px;width:100%;">
  <tr><td style="padding-bottom:20px;border-bottom:2px solid #E8E2DC;">
    <span style="font-size:11px;font-weight:bold;letter-spacing:3px;text-transform:uppercase;color:#0D0D0D;">MINDOS</span>
  </td></tr>
  <tr><td style="padding:32px 0 12px;">
    <p style="font-size:11px;color:#A09080;text-transform:uppercase;letter-spacing:1.5px;margin:0;">Verification code</p>
  </td></tr>
  <tr><td style="padding-bottom:24px;">
    <div style="font-family:Georgia,serif;font-size:42px;letter-spacing:12px;color:#0D0D0D;font-weight:bold;">{code}</div>
  </td></tr>
  <tr><td style="padding-bottom:24px;">
    <p style="font-family:Georgia,serif;font-size:16px;line-height:1.75;color:#2A2520;margin:0;">
      Enter this code to access your MindOS courses. It expires in 10 minutes.
    </p>
  </td></tr>
  <tr><td style="padding-top:20px;border-top:1px solid #E8E2DC;">
    <p style="font-size:11px;color:#C8C0B8;margin:0;">If you didn't request this, ignore this email.</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


@app.post("/api/auth/send-otp")
def send_otp(req: OtpRequest):
    email = req.email.strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    # Rate limit: 3 per email per 10 minutes
    _check_rate(_otp_rate, email.lower(), 3, 600)
    code = db.create_otp(email)
    ok = do_send_email(email, "Your MindOS verification code", _otp_email_html(code, email))
    if not ok:
        raise HTTPException(500, "Failed to send verification email — check server config")
    return {"success": True}


@app.post("/api/auth/verify-otp")
def verify_otp(req: OtpVerify):
    if not db.verify_otp(req.email.strip(), req.code.strip()):
        raise HTTPException(400, "Invalid or expired code")
    return {"success": True}


# ---------------------------------------------------------------------------
# Manual cron trigger (fallback / Vercel cron)
# ---------------------------------------------------------------------------

@app.get("/api/cron")
def run_cron(request: Request):
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CRON_SECRET}":
            raise HTTPException(401, "Unauthorized")
    try:
        send_due_emails()
        generate_upcoming()
        return {"success": True, "time": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

def _get_admin_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("X-Admin-Token", "") or request.query_params.get("token", "")


def _check_admin(request: Request):
    token = _get_admin_token(request)
    if ADMIN_SECRET and hmac.compare_digest(token, ADMIN_SECRET):
        return
    if db.validate_admin_session(token):
        return
    raise HTTPException(401, "Unauthorized")


def _verify_admin_password(candidate: str) -> bool:
    if not candidate:
        return False

    if not ADMIN_PASSWORD_HASH:
        return False

    try:
        return bcrypt.checkpw(candidate.encode("utf-8"), ADMIN_PASSWORD_HASH.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── Auth ──────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str


# ip -> deque of attempt timestamps
_login_attempts: dict = collections.defaultdict(collections.deque)
_LOGIN_MAX = 10       # attempts
_LOGIN_WINDOW = 60    # seconds


@app.post("/api/admin/login")
def admin_login(req: AdminLoginRequest, request: Request):
    ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    now = time.time()
    attempts = _login_attempts[ip]
    # drop attempts outside the window
    while attempts and now - attempts[0] > _LOGIN_WINDOW:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX:
        raise HTTPException(429, "Too many login attempts. Try again in a minute.")
    attempts.append(now)

    if req.username != ADMIN_USER or not _verify_admin_password(req.password):
        raise HTTPException(401, "Invalid credentials")
    token = db.create_admin_session()
    db.prune_old_sessions()
    _login_attempts.pop(ip, None)  # clear on successful login
    return {"success": True, "token": token}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    token = _get_admin_token(request)
    db.delete_admin_session(token)
    return {"success": True}


# ── Pages ──────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def serve_admin():
    try:
        with open("admin.html", "r") as f:
            content = f.read()
        return HTMLResponse(content=content, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    except FileNotFoundError:
        return "<h1>admin.html not found</h1>"


# ── Data ──────────────────────────────────────────────────────────────────

@app.get("/api/admin/data")
def admin_data(request: Request):
    _check_admin(request)
    users   = db.get_all_users()
    courses = db.get_all_courses()
    stats   = db.get_email_stats()
    jobs    = db.get_admin_jobs(limit=20)
    return {
        "stats": {
            "total_users":        len(users),
            "active_courses":     sum(1 for c in courses if c["status"] == "active"),
            "processing_courses": sum(1 for c in courses if c["status"] == "processing"),
            "emails_total":       stats["total"],
            "emails_sent":        stats["sent"],
            "emails_today":       stats["sent_today"],
            "emails_pending":     stats["pending"],
            "jobs_pending":       sum(1 for j in jobs if j["status"] == "pending"),
            "jobs_processing":    sum(1 for j in jobs if j["status"] == "processing"),
        },
        "users":   users,
        "courses": courses,
        "jobs":    jobs,
    }


@app.get("/api/admin/emails")
def admin_emails(request: Request, status: str = None, limit: int = 200, offset: int = 0):
    _check_admin(request)
    return {"emails": db.get_all_emails(limit=limit, offset=offset, status_filter=status)}


@app.get("/api/admin/emails/{email_id}")
def admin_get_email(email_id: str, request: Request):
    _check_admin(request)
    row = db.get_email_for_send(email_id)
    if not row:
        raise HTTPException(404, "Email not found")
    return dict(row)


@app.get("/api/admin/system")
def admin_system(request: Request):
    _check_admin(request)
    return db.get_system_stats()


# ── User actions ──────────────────────────────────────────────────────────

@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, request: Request):
    _check_admin(request)
    if not db.delete_user(user_id):
        raise HTTPException(404, "User not found")
    return {"success": True}


# ── Course actions ────────────────────────────────────────────────────────

@app.delete("/api/admin/courses/{course_id}")
def admin_delete_course(course_id: str, request: Request):
    _check_admin(request)
    if not db.delete_course(course_id):
        raise HTTPException(404, "Course not found")
    return {"success": True}


@app.post("/api/admin/courses/{course_id}/cancel")
def admin_cancel_course(course_id: str, request: Request):
    _check_admin(request)
    if not db.cancel_course(course_id):
        raise HTTPException(404, "Course not found or already cancelled")
    return {"success": True}


@app.post("/api/admin/courses/{course_id}/reprocess")
def admin_reprocess_course(course_id: str, request: Request):
    _check_admin(request)
    course = None
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if row:
            course = dict(row)
    if not course:
        raise HTTPException(404, "Course not found")
    db.reset_course_for_reprocess(course_id)
    job_id = db.enqueue_job("process_playlist", {
        "user_id":      course["user_id"],
        "course_id":    course_id,
        "playlist_url": course["playlist_url"],
    })
    return {"success": True, "job_id": job_id}


@app.get("/api/admin/courses/{course_id}/videos")
def admin_course_videos(course_id: str, request: Request):
    _check_admin(request)
    return {"videos": db.get_course_videos_admin(course_id)}


# ── Job actions ───────────────────────────────────────────────────────────

@app.post("/api/admin/jobs/{job_id}/cancel")
def admin_cancel_job(job_id: str, request: Request):
    _check_admin(request)
    if not db.cancel_job(job_id):
        raise HTTPException(404, "Job not found or already finished")
    return {"success": True}


@app.post("/api/admin/jobs/{job_id}/requeue")
def admin_requeue_job(job_id: str, request: Request):
    _check_admin(request)
    if not db.requeue_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"success": True}


# ── Email actions ─────────────────────────────────────────────────────────

@app.post("/api/admin/emails/{email_id}/cancel")
def admin_cancel_email(email_id: str, request: Request):
    _check_admin(request)
    if not db.cancel_email(email_id):
        raise HTTPException(404, "Email not found or already sent/cancelled")
    return {"success": True}


@app.post("/api/admin/emails/{email_id}/resend")
def admin_resend_email(email_id: str, request: Request):
    _check_admin(request)
    row = db.get_email_for_send(email_id)
    if not row:
        raise HTTPException(404, "Email not found")
    if not row.get("html_body"):
        raise HTTPException(400, "Email not yet generated")
    db.reset_email_to_pending(email_id)
    ok = do_send_email(row["user_email"], row["subject"] or "Your MindOS lesson", row["html_body"])
    if ok:
        db.mark_email_sent(email_id)
        return {"success": True}
    raise HTTPException(500, "Send failed — check server logs")


# ── Transcript ────────────────────────────────────────────────────────────

@app.get("/api/admin/videos/{video_id}/transcript")
def admin_video_transcript(video_id: str, request: Request):
    _check_admin(request)
    text = db.get_video_transcript(video_id)
    if text is None:
        raise HTTPException(404, "Transcript not stored — video may have been processed before transcript storage was added")
    return {"video_id": video_id, "transcript": text}


# ── Cron manual trigger ───────────────────────────────────────────────────

@app.post("/api/admin/cron/run")
def admin_run_cron(request: Request):
    _check_admin(request)
    try:
        from scheduler_jobs import send_due_emails, generate_upcoming, sync_to_sheets
        send_due_emails()
        generate_upcoming()
        return {"success": True, "time": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/admin/cron/advance")
def admin_run_advance(request: Request):
    _check_admin(request)
    try:
        advance_processing()
        return {"success": True, "time": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# User-facing: transcript (requires email param that matches course owner)
# ---------------------------------------------------------------------------

@app.get("/api/videos/{video_id}/transcript")
def get_video_transcript(video_id: str, email: str):
    if not email:
        raise HTTPException(400, "Email required")
    # Verify the video belongs to this user's course
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT v.id FROM videos v
               JOIN courses c ON v.course_id = c.id
               JOIN users u ON c.user_id = u.id
               WHERE v.id=? AND u.email=?""",
            (video_id, email)
        ).fetchone()
    if not row:
        raise HTTPException(403, "Access denied")
    text = db.get_video_transcript(video_id)
    if not text:
        raise HTTPException(404, "Transcript not available")
    return {"transcript": text}


# ── Analytics ─────────────────────────────────────────────────────────────

@app.get("/api/admin/analytics")
def admin_analytics(request: Request):
    _check_admin(request)
    return db.get_analytics()


# ── Audit log ─────────────────────────────────────────────────────────────

@app.get("/api/admin/audit")
def admin_audit(request: Request, limit: int = 200, offset: int = 0):
    _check_admin(request)
    return {"entries": db.get_audit_log(limit=limit, offset=offset)}


# ── Feature flags ─────────────────────────────────────────────────────────

@app.get("/api/admin/flags")
def admin_get_flags(request: Request):
    _check_admin(request)
    return db.get_feature_flags()


class FlagBody(BaseModel):
    enabled: bool

@app.put("/api/admin/flags/{key}")
def admin_set_flag(key: str, body: FlagBody, request: Request):
    _check_admin(request)
    db.set_feature_flag(key, body.enabled)
    db.add_audit("flag_set", "flag", key, f"enabled={body.enabled}")
    return {"success": True}


# ── User controls ─────────────────────────────────────────────────────────

class UserSettingsBody(BaseModel):
    timezone:    Optional[str] = None
    frequency:   Optional[str] = None
    tone:        Optional[str] = None
    depth:       Optional[str] = None
    active_days: Optional[List[str]] = None

@app.put("/api/admin/users/{user_id}")
def admin_edit_user(user_id: str, body: UserSettingsBody, request: Request):
    _check_admin(request)
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not db.update_user_settings(user_id, **fields):
        raise HTTPException(404, "User not found")
    db.add_audit("user_edit", "user", user_id, str(fields))
    return {"success": True}

@app.post("/api/admin/users/{user_id}/pause")
def admin_pause_user(user_id: str, request: Request):
    _check_admin(request)
    db.pause_user(user_id)
    db.add_audit("user_pause", "user", user_id)
    return {"success": True}

@app.post("/api/admin/users/{user_id}/resume")
def admin_resume_user(user_id: str, request: Request):
    _check_admin(request)
    db.resume_user(user_id)
    db.add_audit("user_resume", "user", user_id)
    return {"success": True}


# ── Course controls ───────────────────────────────────────────────────────

@app.post("/api/admin/courses/{course_id}/pause")
def admin_pause_course(course_id: str, request: Request):
    _check_admin(request)
    db.pause_course(course_id)
    db.add_audit("course_pause", "course", course_id)
    return {"success": True}

@app.post("/api/admin/courses/{course_id}/resume")
def admin_resume_course(course_id: str, request: Request):
    _check_admin(request)
    db.resume_course(course_id)
    db.add_audit("course_resume", "course", course_id)
    return {"success": True}


# ── Video controls ────────────────────────────────────────────────────────

@app.post("/api/admin/videos/{video_id}/retry")
def admin_retry_video(video_id: str, request: Request):
    _check_admin(request)
    # Find the course and enqueue a targeted job
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT v.*, c.user_id, c.playlist_url FROM videos v
               JOIN courses c ON v.course_id = c.id WHERE v.id=?""",
            (video_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Video not found")
    if not db.retry_video(video_id):
        raise HTTPException(400, "Video is not in failed/skipped state")
    # Re-enqueue the whole course job — worker skips ready videos automatically
    job_id = db.enqueue_job("process_playlist", {
        "user_id": row["user_id"],
        "course_id": row["course_id"],
        "playlist_url": row["playlist_url"],
    })
    db.add_audit("video_retry", "video", video_id)
    return {"success": True, "job_id": job_id}


class TranscriptBody(BaseModel):
    text: str

@app.put("/api/admin/videos/{video_id}/transcript")
def admin_edit_transcript(video_id: str, body: TranscriptBody, request: Request):
    _check_admin(request)
    if not db.update_video_transcript(video_id, body.text):
        raise HTTPException(404, "Video not found")
    db.add_audit("transcript_edit", "video", video_id)
    return {"success": True}


class IngestByYoutubeIdBody(BaseModel):
    youtube_id: str
    text: str
    token: Optional[str] = None

@app.post("/api/admin/ingest-by-youtube-id")
def admin_ingest_by_youtube_id(body: IngestByYoutubeIdBody):
    """Bookmarklet endpoint: accepts youtube_id + transcript text + admin token."""
    if not ADMIN_SECRET:
        raise HTTPException(503, "Admin token is not configured")
    if not body.token or not hmac.compare_digest(body.token, ADMIN_SECRET):
        raise HTTPException(401, "Unauthorized")
    text = body.text.strip()
    if len(text) < 100:
        raise HTTPException(400, "Transcript too short")
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT v.id, v.course_id, c.user_id, c.playlist_url FROM videos v JOIN courses c ON v.course_id = c.id WHERE v.youtube_id=? ORDER BY v.created_at DESC LIMIT 1",
            (body.youtube_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"No video found with youtube_id={body.youtube_id}")
    video_id = row["id"]
    db.update_video_transcript(video_id, text)
    db.retry_video(video_id)
    job_id = db.enqueue_job("process_playlist", {
        "user_id": row["user_id"],
        "course_id": row["course_id"],
        "playlist_url": row["playlist_url"],
    })
    db.add_audit("transcript_injected_bookmarklet", "video", video_id, f"yt={body.youtube_id} chars={len(text)}")
    return {"success": True, "job_id": job_id, "video_id": video_id}


@app.post("/api/admin/videos/{video_id}/inject-transcript")
def admin_inject_transcript(video_id: str, body: TranscriptBody, request: Request):
    """Browser-fetched transcript: save it, reset video to pending, enqueue processing."""
    _check_admin(request)
    text = body.text.strip()
    if len(text) < 100:
        raise HTTPException(400, "Transcript too short")
    if not db.update_video_transcript(video_id, text):
        raise HTTPException(404, "Video not found")
    db.retry_video(video_id)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT v.course_id, c.user_id, c.playlist_url FROM videos v JOIN courses c ON v.course_id = c.id WHERE v.id=?",
            (video_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Video not found")
    job_id = db.enqueue_job("process_playlist", {
        "user_id": row["user_id"],
        "course_id": row["course_id"],
        "playlist_url": row["playlist_url"],
    })
    db.add_audit("transcript_injected", "video", video_id, f"chars={len(text)}")
    return {"success": True, "job_id": job_id}


# ── Email controls ────────────────────────────────────────────────────────

class RescheduleBody(BaseModel):
    scheduled_at: str  # ISO datetime string

@app.put("/api/admin/emails/{email_id}/schedule")
def admin_reschedule_email(email_id: str, body: RescheduleBody, request: Request):
    _check_admin(request)
    if not db.reschedule_email(email_id, body.scheduled_at):
        raise HTTPException(404, "Email not found")
    db.add_audit("email_reschedule", "email", email_id, body.scheduled_at)
    return {"success": True}


class EmailContentBody(BaseModel):
    subject:   str
    html_body: str

@app.put("/api/admin/emails/{email_id}/content")
def admin_edit_email(email_id: str, body: EmailContentBody, request: Request):
    _check_admin(request)
    if not db.edit_email_content(email_id, body.subject, body.html_body):
        raise HTTPException(404, "Email not found")
    db.add_audit("email_edit", "email", email_id)
    return {"success": True}

@app.post("/api/admin/emails/{email_id}/send-now")
def admin_send_email_now(email_id: str, request: Request):
    _check_admin(request)
    row = db.get_email_for_send(email_id)
    if not row:
        raise HTTPException(404, "Email not found")
    if not row.get("html_body"):
        raise HTTPException(400, "Email has no content yet")
    db.reset_email_to_pending(email_id)
    ok = do_send_email(row["user_email"], row["subject"] or "Your MindOS lesson", row["html_body"])
    if ok:
        db.mark_email_sent(email_id)
        db.add_audit("email_send_now", "email", email_id, row["user_email"])
        return {"success": True}
    raise HTTPException(500, "Send failed")


# ── Transcripts list ──────────────────────────────────────────────────────

@app.get("/api/admin/transcripts")
def admin_transcripts(request: Request, limit: int = 200, offset: int = 0):
    _check_admin(request)
    return {"transcripts": db.get_all_transcripts(limit=limit, offset=offset)}


# ── Broadcast ─────────────────────────────────────────────────────────────

class BroadcastBody(BaseModel):
    subject:   str
    html_body: str

@app.post("/api/admin/broadcast")
def admin_broadcast(body: BroadcastBody, request: Request):
    _check_admin(request)
    emails = db.get_all_user_emails_for_broadcast()
    if not emails:
        raise HTTPException(400, "No active users to send to")
    sent, failed = 0, 0
    for email in emails:
        ok = do_send_email(email, body.subject, body.html_body)
        if ok:
            sent += 1
        else:
            failed += 1
    db.add_audit("broadcast", "system", None, f"sent={sent} failed={failed} subject={body.subject[:60]}")
    return {"success": True, "sent": sent, "failed": failed, "total": len(emails)}


# ── DB download ───────────────────────────────────────────────────────────

from fastapi.responses import FileResponse

@app.get("/api/admin/db/download")
def admin_db_download(request: Request):
    _check_admin(request)
    from config import DATABASE_PATH
    import os
    if not os.path.exists(DATABASE_PATH):
        raise HTTPException(404, "Database file not found")
    db.add_audit("db_download", "system", None)
    return FileResponse(
        DATABASE_PATH,
        media_type="application/octet-stream",
        filename="mindos.db",
    )


# ── Live logs ─────────────────────────────────────────────────────────────

import subprocess as _subprocess

@app.get("/api/admin/logs")
def admin_logs(request: Request, lines: int = 100):
    _check_admin(request)
    # Read from the log file if it exists, otherwise return empty
    import os
    log_paths = ["/tmp/mindos.log", "/app/mindos.log"]
    for path in log_paths:
        if os.path.exists(path):
            try:
                result = _subprocess.run(
                    ["tail", f"-{lines}", path],
                    capture_output=True, text=True, timeout=5
                )
                return {"lines": result.stdout.splitlines()}
            except Exception:
                pass
    # Fallback: read from journald/docker logs if available
    return {"lines": [], "note": "No log file found — logs visible in fly logs"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# 1x1 transparent GIF — email open tracking pixel
_PIXEL = bytes([
    0x47,0x49,0x46,0x38,0x39,0x61,0x01,0x00,0x01,0x00,0x80,0x00,0x00,
    0xFF,0xFF,0xFF,0x00,0x00,0x00,0x21,0xF9,0x04,0x00,0x00,0x00,0x00,
    0x00,0x2C,0x00,0x00,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0x02,0x02,
    0x44,0x01,0x00,0x3B
])

@app.get("/track/o/{email_id}")
def track_open(email_id: str):
    """Email open pixel — logs the open silently, returns a transparent GIF."""
    try:
        db.log_email_open(email_id)
    except Exception:
        pass
    return Response(content=_PIXEL, media_type="image/gif", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


@app.get("/api/admin/open-stats")
def admin_open_stats(req: Request):
    _check_admin(req)
    return db.get_open_stats()


@app.get("/api/admin/activity")
def admin_activity(req: Request):
    _check_admin(req)
    return db.get_activity_feed(limit=40)


@app.get("/api/admin/slot-stats")
def admin_slot_stats(req: Request):
    _check_admin(req)
    return db.get_slot_stats()


@app.get("/api/admin/heartbeat")
def admin_heartbeat(req: Request):
    _check_admin(req)
    return db.get_heartbeat(days=14)


@app.get("/api/admin/user-health")
def admin_user_health(req: Request):
    _check_admin(req)
    return db.get_user_health()


@app.get("/api/admin/stale-jobs")
def admin_stale_jobs(req: Request):
    _check_admin(req)
    return db.get_stale_jobs()


@app.get("/api/admin/dow-heatmap")
def admin_dow_heatmap(req: Request):
    _check_admin(req)
    return db.get_dow_heatmap()


@app.get("/api/admin/unit-economics")
def admin_unit_economics(req: Request):
    _check_admin(req)
    return db.get_unit_economics()


@app.get("/api/admin/search")
def admin_search(req: Request, q: str = ""):
    _check_admin(req)
    return db.search_all(q)


@app.post("/api/webhooks/openrouter")
async def openrouter_webhook(request: Request):
    """Receives OpenRouter observability traces and stores them."""
    try:
        payload = await request.json()
        db.save_llm_trace(payload)
    except Exception as e:
        print(f"[webhook] openrouter trace parse error: {e}")
    return {"ok": True}


@app.get("/api/admin/llm-traces")
def admin_llm_traces(req: Request):
    _check_admin(req)
    return db.get_llm_trace_stats()
