"""
scheduler_jobs.py — APScheduler jobs.

Jobs:
  every 5 min  → send_due_emails()         send pending emails/reviews/synthesis
  every 1 hour → generate_upcoming()       pre-generate review + synthesis content
  every 1 hour → sync_to_sheets()          push stats to Google Sheets
"""
import json
from datetime import datetime, timezone

import db
from mailer import send_email
from llm import generate_review, generate_synthesis, render_html


# ---------------------------------------------------------------------------
# 1. Send due emails (every 5 min)
# ---------------------------------------------------------------------------

def send_due_emails():
    import pytz as _pytz
    now_str = datetime.now(_pytz.timezone("Asia/Kolkata")).strftime("%H:%M IST")

    # Reschedule any emails that are >48h stale before sending
    db.reschedule_stale_emails()

    # Regular course emails
    due = db.get_due_emails(limit=50)
    if due:
        print(f"[scheduler] sending {len(due)} email(s) at {now_str}")
    for item in due:
        ok = send_email(item["user_email"], item["subject"], item["html_body"])
        if ok:
            db.mark_email_sent(item["id"])
        else:
            print(f"[scheduler] ⚠ failed to send email {item['id']}")

    # Spaced repetition reviews (only those with html_body already generated)
    due_reviews = db.get_due_reviews(limit=50)
    for item in due_reviews:
        ok = send_email(item["user_email"], item["subject"], item["html_body"])
        if ok:
            db.mark_review_sent(item["id"])

    # Synthesis emails
    due_synth = db.get_due_synthesis(limit=20)
    for item in due_synth:
        ok = send_email(item["user_email"], item["subject"], item["html_body"])
        if ok:
            db.mark_synthesis_sent(item["id"])


# ---------------------------------------------------------------------------
# 2. Pre-generate review + synthesis content (every 1 hour)
# ---------------------------------------------------------------------------

def generate_upcoming():
    """
    Reviews and synthesis are scheduled as placeholder rows when videos are processed.
    This job fills in the html_body before they go out.
    """
    # Reviews
    pending_reviews = db.get_pending_reviews_for_generation(limit=20)
    for review in pending_reviews:
        original_body = db.get_first_email_body_for_video(review["video_id"])
        if not original_body:
            continue
        out = generate_review(
            video_title=review["video_title"],
            original_body=original_body,
            review_type=review["review_type"],
            tone=review.get("tone", "Casual"),
        )
        if out:
            html = render_html(out.subject, out.body, slot=review["review_type"])
            db.update_review_content(review["id"], out.subject, html)
            print(f"[scheduler] ✅ review generated: {review['review_type']} for {review['video_title'][:40]}")

    # Synthesis
    pending_synth = db.get_pending_synthesis_for_generation(limit=10)
    for synth in pending_synth:
        video_ids = json.loads(synth["video_ids"])
        videos = db.get_videos_by_ids(video_ids)
        titles = [v["title"] or "Video" for v in videos]

        # Gather morning email bodies as context
        snippets = []
        for v in videos:
            body = db.get_first_email_body_for_video(v["id"])
            snippets.append(body or "")

        if not any(snippets):
            continue

        out = generate_synthesis(
            video_titles=titles,
            email_snippets=snippets,
            tone=synth.get("tone", "Casual"),
        )
        if out:
            html = render_html(out.subject, out.body, slot="synthesis")
            db.update_synthesis_content(synth["id"], out.subject, html)
            print(f"[scheduler] ✅ synthesis generated for course {synth['course_id']}")


# ---------------------------------------------------------------------------
# 3. Advance JIT processing (daily at 2am IST = 20:30 UTC)
# ---------------------------------------------------------------------------

def advance_processing():
    """
    Just-in-time video processing: for each active course, ensure the next
    video is processed 1 day before it's due. Keeps resource usage flat
    regardless of playlist size — only processes what's actually needed.
    """
    now = datetime.utcnow()

    with db.get_db() as conn:
        courses = conn.execute(
            "SELECT * FROM courses WHERE status='active'"
        ).fetchall()

    for course_row in courses:
        course = dict(course_row)
        course_id = course["id"]

        with db.get_db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE course_id=?", (course_id,)
            ).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE course_id=? AND status IN ('ready','skipped')",
                (course_id,)
            ).fetchone()[0]

        if done >= total:
            continue  # course complete

        # Calculate how many videos should be ready by now + 1 day buffer
        # (1 video per active day at minimum, regardless of email frequency)
        try:
            created_at = datetime.fromisoformat(course["created_at"].replace("Z", ""))
        except Exception:
            continue
        days_elapsed = max(0, (now - created_at).days)
        target = min(days_elapsed + 2, total)  # +2 = today + 1 day buffer

        if done >= target:
            continue  # already ahead of schedule

        videos_needed = target - done
        print(f"[advance] course {course_id}: {done}/{total} done, processing {videos_needed} more")

        db.enqueue_job("process_playlist", {
            "user_id":      course["user_id"],
            "course_id":    course_id,
            "playlist_url": course["playlist_url"],
            "max_videos":   videos_needed,
        })


# ---------------------------------------------------------------------------
# 4. Sync to Google Sheets (every 1 hour)
# ---------------------------------------------------------------------------

def sync_to_sheets():
    from config import GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # -- Users tab --
        users = db.get_all_users()
        _sync_tab(
            sh, "Users",
            headers=["ID", "Email", "Timezone", "Frequency", "Tone", "Depth", "Active Days", "Joined"],
            rows=[
                [u["id"], u["email"], u["timezone"], u["frequency"],
                 u["tone"], u["depth"], u["active_days"], u["created_at"]]
                for u in users
            ]
        )

        # -- Courses tab --
        courses = db.get_all_courses()
        _sync_tab(
            sh, "Courses",
            headers=["ID", "User Email", "Playlist", "Status", "Progress", "Total", "Created"],
            rows=[
                [c["id"], c["email"], c["playlist_title"] or c["playlist_url"],
                 c["status"],
                 f"{c['videos_processed']}/{c['total_videos']}",
                 c["total_videos"], c["created_at"]]
                for c in courses
            ]
        )

        # -- Stats tab --
        stats = db.get_email_stats()
        _sync_tab(
            sh, "Stats",
            headers=["Metric", "Value", "Updated"],
            rows=[
                ["Total Users",       len(users),             datetime.utcnow().isoformat()],
                ["Active Courses",    sum(1 for c in courses if c["status"] == "active"), ""],
                ["Emails Total",      stats["total"],          ""],
                ["Emails Sent",       stats["sent"],           ""],
                ["Emails Sent Today", stats["sent_today"],     ""],
                ["Emails Pending",    stats["pending"],        ""],
            ]
        )

        import pytz as _pytz2
        print(f"[sheets] ✅ synced at {datetime.now(_pytz2.timezone('Asia/Kolkata')).strftime('%H:%M IST')}")

    except Exception as e:
        print(f"[sheets] ❌ sync failed: {e}")


def _sync_tab(sh, tab_name: str, headers: list, rows: list):
    try:
        try:
            ws = sh.worksheet(tab_name)
        except Exception:
            ws = sh.add_worksheet(title=tab_name, rows=500, cols=len(headers))
        ws.clear()
        ws.append_row(headers)
        if rows:
            ws.append_rows([[str(c) for c in row] for row in rows])
    except Exception as e:
        print(f"[sheets] ⚠ tab '{tab_name}' sync error: {e}")
