# MindOS v2

## project name + description
- mindos-v2: Rebuilt YouTube-to-email course platform — FastAPI backend, SQLite, deployed at mindos.fly.dev

## who it's for
- Self-learners aged 18–35 who save YouTube playlists but never finish them

## current status
- in progress — deployed at mindos.fly.dev

## what was actually built
- FastAPI backend with SSE streaming for live progress updates during transcript processing
- SQLite database with tables: Users, Courses, Queue, Content, Sessions
- APScheduler with three jobs: send_due_emails (every 5 min), generate_upcoming (every 1 hr), sync_to_sheets (every 1 hr)
- Worker pool for background transcript processing
- Spaced repetition: auto-generates review emails on days 3, 7, and 30 after a video
- Cross-video synthesis email every 5 videos
- Admin HTML dashboard (55KB) at /admin
- Docker + Fly.io deployment (fly.toml present)
- Transcript pipeline: youtube-transcript-api → yt-dlp fallback
- Endpoints: POST /api/enroll, GET /api/job/{id}, GET /api/job/{id}/stream (SSE), GET /api/courses, DELETE /api/courses/{id}, GET /api/cron
- Frontend: single-page index.html + admin.html

## why it was built
- v1 failed due to Streamlit timeouts, Google Sheets payload limits, no async, and JSON fragility from cheap models; v2 rebuilt everything on a proper async stack

## blockers or reasons shelved
- n/a — not shelved; actively deployed and in progress

## wins or progress moments
- SSE streaming gives users live progress feedback during enrollment (no more timeout UX)
- Spaced repetition and synthesis emails are a step beyond v1 — adds actual pedagogical value
- Docker + Fly.io deployment makes it a real hosted product

## pain points
- OpenRouter (Gemini Flash / Qwen) JSON fragility still a recurring issue — planned migration to Claude API with Pydantic/instructor
- Keeping APScheduler jobs in sync with the worker pool across restarts

## where claude api / ai was used or planned
- Currently: OpenRouter (Gemini Flash / Qwen) for transcript → HTML email generation
- Planned: migrate to Claude API for better JSON reliability using instructor + Pydantic

## what would've helped
- Claude API access earlier — would have avoided the JSON fragility issues from cheap models
- Persistent storage on Fly.io (SQLite doesn't survive deploys without a volume)

## metrics or traction
- none yet — deployed but not publicly launched
