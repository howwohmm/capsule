"""
transcript.py — Two-tier YouTube transcript fetching.

Tier 1: youtube-transcript-api  (fast, no subprocess, works from most cloud IPs)
Tier 2: yt-dlp                  (fallback, handles restricted/auto-captioned videos)

Returns plain text string or None.
"""
import os
import re
import json
import glob
import tempfile
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Tier 1: youtube-transcript-api
# ---------------------------------------------------------------------------

def _get_via_api(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
        try:
            api = YouTubeTranscriptApi()
            transcript_list = api.list(video_id)
            # Prefer manual captions, fall back to auto-generated
            try:
                transcript = transcript_list.find_manually_created_transcript(['en', 'en-US', 'en-GB'])
            except Exception:
                try:
                    transcript = transcript_list.find_generated_transcript(['en', 'en-US', 'en-GB'])
                except Exception:
                    # Try any language with translation to English
                    available = list(transcript_list)
                    if not available:
                        return None
                    transcript = available[0].translate('en')

            data = transcript.fetch()
            # v1.x returns FetchedTranscriptSnippet objects (not dicts)
            text = " ".join(
                entry.text if hasattr(entry, "text") else entry.get("text", "")
                for entry in data
            )
            text = re.sub(r'\[.*?\]', '', text)   # strip [Music], [Applause] etc.
            text = re.sub(r'\s+', ' ', text).strip()
            return text if len(text) > 100 else None

        except (TranscriptsDisabled, NoTranscriptFound):
            return None
    except ImportError:
        return None
    except Exception as e:
        print(f"   [transcript-api] failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Tier 2: yt-dlp
# ---------------------------------------------------------------------------

def _parse_vtt(vtt_content: str) -> str:
    lines = vtt_content.splitlines()
    seen = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if not seen or line != seen[-1]:
            seen.append(line)
    return " ".join(seen)


def _parse_json3(raw: str) -> str:
    try:
        data = json.loads(raw)
        parts = []
        for event in data.get("events", []):
            for seg in event.get("segs", []):
                t = seg.get("utf8", "").strip()
                if t and t != "\n":
                    parts.append(t)
        return " ".join(parts)
    except Exception:
        return ""


def _get_via_ytdlp(video_id: str) -> Optional[str]:
    try:
        import yt_dlp
    except ImportError:
        return None

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en", "en-US", "en-GB"],
            "subtitlesformat": "json3/vtt/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "ignoreerrors": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([canonical_url])
        except Exception as e:
            print(f"   [yt-dlp] download error: {e}")
            return None

        sub_files = (
            glob.glob(os.path.join(tmpdir, "*.json3")) +
            glob.glob(os.path.join(tmpdir, "*.vtt"))
        )
        if not sub_files:
            return None

        sub_file = sorted(sub_files, key=lambda f: (0 if f.endswith(".json3") else 1))[0]
        with open(sub_file, "r", encoding="utf-8") as f:
            raw = f.read()

        text = _parse_json3(raw) if sub_file.endswith(".json3") else _parse_vtt(raw)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 100 else None


# ---------------------------------------------------------------------------
# Tier 3: Groq Whisper (audio transcription — no captions at all)
# ---------------------------------------------------------------------------

def _get_via_groq(video_id: str) -> Optional[str]:
    """Download raw audio via yt-dlp (no ffmpeg needed for m4a/webm) then
    transcribe with Groq whisper-large-v3. Free tier: 28,800 seconds/day."""
    from config import GROQ_API_KEY
    if not GROQ_API_KEY:
        print(f"   [groq] no GROQ_API_KEY set, skipping")
        return None

    try:
        import yt_dlp
        from groq import Groq
    except ImportError as e:
        print(f"   [groq] missing dependency: {e}")
        return None

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = None

        # Download best audio that doesn't need ffmpeg post-processing
        # m4a and webm are native YouTube streams — no merging required
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "postprocessors": [],   # no ffmpeg post-processing
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([canonical_url])
        except Exception as e:
            print(f"   [groq] yt-dlp audio download failed: {e}")
            return None

        # Find the downloaded audio file
        audio_files = (
            glob.glob(os.path.join(tmpdir, "*.m4a")) +
            glob.glob(os.path.join(tmpdir, "*.webm")) +
            glob.glob(os.path.join(tmpdir, "*.ogg"))
        )
        if not audio_files:
            print(f"   [groq] no audio file found for {video_id}")
            return None

        audio_path = audio_files[0]
        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        print(f"   [groq] audio downloaded: {os.path.basename(audio_path)} ({file_size_mb:.1f} MB)")

        # Groq's limit is 25 MB per file
        if file_size_mb > 24:
            print(f"   [groq] file too large ({file_size_mb:.1f} MB), skipping")
            return None

        try:
            client = Groq(api_key=GROQ_API_KEY)
            with open(audio_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), f),
                    model="whisper-large-v3",
                    response_format="text",
                    language="en",
                )
            text = result if isinstance(result, str) else getattr(result, "text", str(result))
            text = re.sub(r'\s+', ' ', text).strip()
            return text if len(text) > 100 else None
        except Exception as e:
            print(f"   [groq] whisper API failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def get_transcript(video_url: str) -> Optional[str]:
    video_id = extract_video_id(video_url)
    if not video_id:
        print(f"   [transcript] could not extract video ID from: {video_url}")
        return None

    print(f"   [transcript] trying youtube-transcript-api for {video_id}...")
    result = _get_via_api(video_id)
    if result:
        print(f"   [transcript] ✅ api tier — {len(result):,} chars")
        return result

    print(f"   [transcript] falling back to yt-dlp for {video_id}...")
    result = _get_via_ytdlp(video_id)
    if result:
        print(f"   [transcript] ✅ yt-dlp tier — {len(result):,} chars")
        return result

    print(f"   [transcript] falling back to Groq Whisper for {video_id}...")
    result = _get_via_groq(video_id)
    if result:
        print(f"   [transcript] ✅ groq tier — {len(result):,} chars")
        return result

    print(f"   [transcript] ❌ all tiers failed for {video_id}")
    return None


def resolve_playlist(url: str) -> tuple[str, list[dict]]:
    """
    Returns (playlist_title, [{"id": yt_id, "title": title}, ...])
    Works for single videos too.
    """
    try:
        import yt_dlp
        opts = {"extract_flat": True, "quiet": True, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return "", []

        if "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            title = info.get("title", "Course")
            videos = [{"id": e.get("id", ""), "title": e.get("title", "Video")} for e in entries if e.get("id")]
        else:
            title = info.get("title", "Video")
            videos = [{"id": info.get("id", ""), "title": title}]

        return title, videos

    except Exception as e:
        print(f"   [resolve_playlist] error: {e}")
        return "", []


def publish_transcript(text: str, title: str = "") -> Optional[str]:
    """Post raw transcript to paste.rs, fallback to dpaste.org."""
    from config import PASTE_RS_URL, DPASTE_URL
    content = f"{title}\n\n{text}" if title else text

    try:
        resp = requests.post(PASTE_RS_URL, data=content.encode("utf-8"),
                             headers={"Content-Type": "text/plain"}, timeout=15)
        if resp.status_code in (200, 201):
            return resp.text.strip()
    except Exception:
        pass

    try:
        resp = requests.post(DPASTE_URL, data={"content": content, "expiry_days": 365}, timeout=15)
        if resp.status_code == 201:
            return resp.json().get("url", "")
    except Exception:
        pass

    return None
