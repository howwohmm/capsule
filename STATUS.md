# MindOS v2

**status:** in progress — deployed at mindos.fly.dev

**what:** Turn any YouTube playlist into a structured daily email course. Paste URL, pick frequency (1–5x/day) and tone — app extracts transcripts, runs through LLM, generates HTML email lessons, delivers on schedule.

**for:** Self-learners (18–35) who save playlists but never finish them

**built:**
- FastAPI backend with SSE streaming (live progress)
- SQLite DB: Users, Courses, Queue, Content, Sessions
- APScheduler: send_due_emails (5min), generate_upcoming (1hr), sync_to_sheets (1hr)
- Worker pool for background transcript processing
- Spaced repetition: auto review emails on days 3, 7, 30
- Cross-video synthesis email every 5 videos
- Admin HTML dashboard (55KB)
- Docker + Fly.io deployment
- Transcript: youtube-transcript-api → yt-dlp fallback

**tech:** FastAPI, SQLite, APScheduler, Docker, Fly.io, OpenRouter (OpenAI SDK)

**ai:** OpenRouter (Gemini Flash / Qwen) for transcript → email generation
→ next: switch to Claude API for better JSON reliability (instructor/Pydantic)

**why rebuilt from v1:** Streamlit timeouts, Google Sheets payload limits, no async, JSON fragility from cheap models
