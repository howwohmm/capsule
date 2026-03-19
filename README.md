# Capsule

Turn any YouTube playlist into a daily email course with spaced repetition. You give it a playlist, it extracts transcripts, chunks them into lessons, and emails you one a day.

**Live at:** [mindos.fly.dev](https://mindos.fly.dev)

## How it works

1. You submit a YouTube playlist URL and your email
2. The app fetches all video transcripts via `youtube-transcript-api`
3. An LLM (OpenAI/Groq) breaks each transcript into digestible lessons with key takeaways
4. Lessons are stored in SQLite with a spaced repetition schedule
5. APScheduler sends one email per day via the mailer until the course is done
6. You can track progress live via SSE stream or poll the job status endpoint

## Tech stack

- **Backend:** FastAPI + APScheduler
- **Transcript extraction:** `youtube-transcript-api`, `yt-dlp` fallback
- **LLM:** OpenAI + Groq (configurable)
- **Database:** SQLite (via custom `db.py`)
- **Email:** Configured via `mailer.py` (SMTP)
- **Sheets sync:** gspread + Google Auth for admin reporting
- **Deployment:** Fly.io (`fly.toml` included)
- **Frontend:** Single-page HTML/JS (`index.html`, `admin.html`)

## API

```
POST /api/enroll          — submit playlist URL + email, starts job
GET  /api/job/{id}        — poll job progress
GET  /api/job/{id}/stream — SSE stream for live progress
GET  /api/courses         — list enrolled courses
DELETE /api/courses/{id}  — cancel a course
GET  /api/cron            — manual cron trigger (protected)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your keys
uvicorn main:app --reload
```

Required env vars: `OPENAI_API_KEY` or `GROQ_API_KEY`, SMTP config, `CRON_SECRET`, `ADMIN_SECRET`.

## Status

Deployed and running. Used internally for converting long YouTube playlists (ML courses, lectures, tutorials) into structured email sequences.
