"""
worker.py — Background job processor.

Flow:
  1. Claim a job from the SQLite queue
  2. Resolve playlist → insert videos
  3. For each video: get transcript → LLM → schedule emails → spaced rep rows
  4. Every 5 videos: schedule synthesis
  5. Update job progress (frontend polls /api/job/{id})
"""
import json
import time
import threading
from datetime import datetime
from typing import Optional

import uuid

import db
from transcript import get_transcript, publish_transcript, resolve_playlist
from llm import generate_all_slots, render_html
from config import WORKER_CONCURRENCY

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Core processor
# ---------------------------------------------------------------------------

def process_playlist_job(job: dict):
    payload   = json.loads(job["payload"])
    job_id    = job["id"]
    user_id   = payload["user_id"]
    course_id = payload["course_id"]
    url       = payload["playlist_url"]

    user = None
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            user = dict(row)

    if not user:
        db.update_job(job_id, 0, "User not found", "failed")
        return

    frequency   = user["frequency"]
    tone        = user["tone"]
    depth       = user["depth"]
    timezone    = user["timezone"]
    active_days = json.loads(user["active_days"])

    # Get course creation time (used as schedule anchor)
    with db.get_db() as conn:
        course_row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        course_created_at = dict(course_row)["created_at"] if course_row else datetime.utcnow().isoformat()

    db.update_job(job_id, 5, "Resolving playlist...")

    # Videos already inserted at course-creation time; just fetch them
    with db.get_db() as conn:
        video_rows = conn.execute(
            "SELECT * FROM videos WHERE course_id=? ORDER BY position",
            (course_id,)
        ).fetchall()
        videos = [dict(r) for r in video_rows]

    total = len(videos)
    if total == 0:
        db.update_job(job_id, 100, "No videos found", "failed")
        return

    db.update_job(job_id, 10, f"Processing {total} video(s)...")

    # Reset counter to already-done count — prevents double-increment if job was requeued.
    # "ready" and "skipped" are permanent; "failed" will be retried so don't count it yet.
    already_done = sum(1 for v in videos if v["status"] in ("ready", "skipped"))
    db.reset_videos_processed(course_id, already_done)

    # Track delivery datetimes for synthesis scheduling
    batch_delivery_datetimes = []
    batch_video_ids = []
    synthesis_batch_num = 0

    for i, video in enumerate(videos):
        video_id   = video["id"]
        youtube_id = video["youtube_id"]
        title      = video["title"] or f"Video {i+1}"
        vurl       = f"https://www.youtube.com/watch?v={youtube_id}"

        # Skip already-successfully-processed videos on rerun (e.g. after timeout reset)
        # "skipped" = no captions (permanent), "ready" = done. "failed" = transient, retry it.
        if video["status"] in ("ready", "skipped"):
            continue

        progress = int(10 + 85 * i / total)
        db.update_job(job_id, progress, f"[{i+1}/{total}] {title[:50]}...")
        db.update_video_status(video_id, "processing")

        # 1. Transcript
        transcript = get_transcript(vurl)
        if transcript:
            db.save_transcript(video_id, transcript)
        if not transcript:
            db.update_video_status(video_id, "skipped", error_msg="No captions available")
            db.increment_videos_processed(course_id)
            continue

        # 2. Publish transcript
        transcript_url = None
        try:
            transcript_url = publish_transcript(transcript, title)
        except Exception:
            pass

        # 3. LLM — generate all email slots
        slot_outputs = generate_all_slots(
            video_title=title,
            transcript=transcript,
            frequency=frequency,
            depth=depth,
            tone=tone,
        )

        if not slot_outputs:
            db.update_video_status(video_id, "failed", error_msg="LLM generation failed")
            db.increment_videos_processed(course_id)
            continue

        # 4. Build email_data dict for scheduling
        # Pre-generate IDs so the tracking pixel can reference them.
        email_data = {}
        for slot, output in slot_outputs.items():
            email_id = str(uuid.uuid4())
            html = render_html(output.subject, output.body, slot=slot, video_title=title,
                               video_position=i, total_videos=total, email_id=email_id)
            email_data[slot] = {
                "email_id":   email_id,
                "subject":    output.subject,
                "html_body":  html,
                "plain_body": output.body,
            }

        # 5. Schedule emails + reviews
        delivery_day = db.schedule_video_emails(
            video_id=video_id,
            user_id=user_id,
            course_created_at=course_created_at,
            position=i,
            active_days=active_days,
            user_timezone=timezone,
            frequency=frequency,
            email_data=email_data,
        )

        db.update_video_status(video_id, "ready", transcript_url=transcript_url)
        db.increment_videos_processed(course_id)

        # 6. Track for synthesis
        batch_video_ids.append(video_id)
        batch_delivery_datetimes.append(delivery_day)

        # Every 5 videos: schedule a synthesis email
        if len(batch_video_ids) == 5:
            synthesis_batch_num += 1
            # Schedule synthesis 30 min after the last email of the 5th video
            last_delivery = batch_delivery_datetimes[-1]
            db.schedule_synthesis(
                course_id=course_id,
                user_id=user_id,
                video_ids=batch_video_ids.copy(),
                after_datetime=last_delivery.replace(hour=21, minute=0),
                user_timezone=timezone,
            )
            batch_video_ids = []
            batch_delivery_datetimes = []

        # Brief pause between videos to respect API rate limits
        time.sleep(2)

    db.update_course_status(course_id, "active")
    db.update_job(job_id, 100, f"Done — {total} video(s) processed.", "done")
    print(f"[worker] ✅ job {job_id} complete for course {course_id}")


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _worker_loop():
    print("[worker] started")
    while not _stop_event.is_set():
        job = db.claim_next_job()
        if job:
            print(f"[worker] claiming job {job['id']} type={job['type']}")
            try:
                if job["type"] == "process_playlist":
                    process_playlist_job(job)
                else:
                    db.update_job(job["id"], 0, f"Unknown job type: {job['type']}", "failed")
            except Exception as e:
                print(f"[worker] ❌ job {job['id']} crashed: {e}")
                db.update_job(job["id"], 0, str(e), "failed")
        else:
            time.sleep(3)
    print("[worker] stopped")


def start_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()


def stop_worker():
    _stop_event.set()
