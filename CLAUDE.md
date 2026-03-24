# Capsule (mindos-v2)

## What
YouTube playlist → daily email course converter. Rebranded from MindOS to Capsule.
LIVE at `http://157.245.103.89` (DigitalOcean, $12/mo, 2GB RAM, NYC).
Stack: FastAPI + SQLite (WAL) + APScheduler + Resend + OpenRouter + Docker.

## Status
LIVE. 2 users, 34 videos, 26 emails sent. Migrated from Fly.io → DO.

## Files
- `main.py` — FastAPI app, all routes, admin auth, APScheduler startup
- `worker.py` — Job queue processor (JIT: 1 video on enroll, rest queued nightly)
- `transcript.py` — 3-tier: youtube-transcript-api → yt-dlp → Groq Whisper + ffmpeg chunking
- `llm.py` — OpenRouter LLM, generates email slots, rate-limit backoff on 429
- `db.py` — SQLite ORM (WAL), scheduling logic, spaced repetition
- `scheduler_jobs.py` — APScheduler: send emails (5min), advance_processing (2am IST), generate reviews (1hr)
- `mailer.py` — Resend API email delivery
- `config.py` — All env vars incl. YOUTUBE_PROXY, LLM_BUDGET_CAP

## Quick Commands
```bash
# Local dev
uvicorn main:app --reload

# Deploy (IMPORTANT: /app is baked into Docker image, NOT volume-mounted)
# Step 1: SCP to host /app (for backup/git tracking)
scp <files> root@157.245.103.89:/app/
# Step 2: docker cp into the running container (this is what actually updates code)
ssh root@157.245.103.89 'docker cp /app/<file> app_capsule_1:/app/<file>'
# Step 3: restart
ssh root@157.245.103.89 'docker restart app_capsule_1'

# One-liner deploy helper (replace file list as needed):
# ssh root@157.245.103.89 'docker cp /app/main.py app_capsule_1:/app/main.py && docker cp /app/db.py app_capsule_1:/app/db.py && docker restart app_capsule_1'

# Rebuild Docker (e.g. after Dockerfile change)
ssh root@157.245.103.89 'docker stop app_capsule_1 && docker rm app_capsule_1 && cd /app && docker-compose up --build -d'

# Logs
ssh root@157.245.103.89 'docker logs app_capsule_1 --tail 50'

# SSH in — <ssh key auth>
ssh root@157.245.103.89
```

## Transcript Pipeline (3 tiers)
1. `youtube-transcript-api` — fast captions, uses YOUTUBE_PROXY if set
2. `yt-dlp` — auto-generated captions fallback, proxy-aware
3. `Groq Whisper` — audio transcription; ffmpeg chunks files >24MB automatically

## Key Env Vars (in `/app/.env` on droplet)
- `OPENROUTER_API_KEY` — LLM. Put $10 credit for reliability (free = 50 req/day)
- `GROQ_API_KEY` — Whisper transcription (28,800s/day free)
- `RESEND_API_KEY` — email delivery
- `YOUTUBE_PROXY` — WebShare proxy <see .env on droplet>
- `LLM_BUDGET_CAP` — hard stop in USD (default 1.0)
- `ADMIN_USER` / `ADMIN_PASSWORD` — admin dashboard login

## Admin
- URL: `http://157.245.103.89/admin`
- Login: <see .env on droplet>

## Architecture Notes
- **JIT processing**: enrollment processes 1 video only. `advance_processing` runs at 2am IST daily to queue next video per course.
- **Default timezone**: Asia/Kolkata (IST) everywhere
- **LLM backoff**: 429 reads X-RateLimit-Reset header, sleeps until reset
- **Job lock**: `_job_lock` in worker.py prevents concurrent playlist processing
- **Docker**: container `app_capsule_1`, image has ffmpeg baked in

## Troubleshooting
| Issue | Fix |
|---|---|
| LLM rate limit | Add $10 credit to OpenRouter |
| Transcript failing | Check YOUTUBE_PROXY is set; check Groq quota |
| Large audio skipped | ffmpeg is in container — should chunk automatically |
| Emails not sending | Check Resend key + FROM_EMAIL in .env |
| Container crashed | docker logs app_capsule_1 then restart |
| DB corrupted | Download via /api/admin/db/download before touching |
