# Capsule

Turn any YouTube playlist into a daily email course with spaced repetition. Submit a playlist URL and your email — get one lesson a day until the course is done.

**Live at:** [mindos.fly.dev](https://mindos.fly.dev)

## How it works

1. Submit a YouTube playlist URL + email
2. App fetches all video transcripts (3-tier pipeline)
3. LLM (OpenRouter) breaks each transcript into lessons with key takeaways
4. Lessons stored in SQLite with a spaced repetition schedule
5. APScheduler emails one lesson per day via Resend until the course is complete
6. Track progress live via SSE stream or poll the job status endpoint

## Tech stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + APScheduler |
| Transcript | `youtube-transcript-api` → `yt-dlp` → Groq Whisper (3-tier fallback) |
| LLM | OpenRouter (configurable model) |
| Database | SQLite (WAL mode) |
| Email | Resend |
| Admin reporting | gspread + Google Auth |
| Monitoring | Sentry |
| Deployment | DigitalOcean (Docker) |
| Frontend | Single-page HTML/JS (`index.html`, `admin.html`) |

## API

```
POST /api/enroll                    — submit playlist URL + email, starts job
GET  /api/job/{id}                  — poll job progress
GET  /api/job/{id}/stream           — SSE stream for live progress
GET  /api/courses                   — list enrolled courses (by email)
DELETE /api/courses/{id}            — cancel a course
GET  /api/courses/{id}/schedule     — full lesson schedule
GET  /api/videos/{id}/emails        — emails generated for a video
POST /api/emails/{id}/send-now      — manually trigger a specific email
POST /api/courses/{id}/send-first   — trigger first email for a course
GET  /api/cron                      — manual cron trigger (CRON_SECRET required)
```

## Admin

`/admin` — password-protected dashboard for monitoring enrollments, viewing job status, and manually triggering emails.

## Transcript pipeline

1. `youtube-transcript-api` — fast, uses `YOUTUBE_PROXY` if set
2. `yt-dlp` — auto-generated captions fallback
3. `Groq Whisper` — audio transcription; ffmpeg auto-chunks files >24MB

## Processing architecture

- **JIT**: enrollment processes 1 video immediately
- `advance_processing` runs at 2am IST nightly to queue the next video per course
- `send_emails` job runs every 5 minutes
- `generate_reviews` runs hourly

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Required env vars:

```
OPENROUTER_API_KEY
GROQ_API_KEY
RESEND_API_KEY
CRON_SECRET
ADMIN_USER
ADMIN_PASSWORD
YOUTUBE_PROXY       # optional, WebShare format
LLM_BUDGET_CAP      # optional, hard USD cap (default 1.0)
SENTRY_DSN          # optional
```
