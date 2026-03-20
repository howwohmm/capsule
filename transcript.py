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
import subprocess
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Tier 0: Invidious public API (free proxy — bypasses YouTube IP blocks)
# ---------------------------------------------------------------------------

_INVIDIOUS_INSTANCES = [
    "https://invidious.io",
    "https://iv.ggtyler.dev",
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
]

def _get_via_invidious(video_id: str) -> Optional[str]:
    """Try public Invidious instances to fetch captions. Free, bypasses IP blocks."""
    for base in _INVIDIOUS_INSTANCES:
        try:
            # Step 1: get caption list
            r = requests.get(
                f"{base}/api/v1/captions/{video_id}",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                continue
            data = r.json()
            captions = data.get("captions", [])
            if not captions:
                continue

            # Prefer English
            track = next((c for c in captions if c.get("language_code", "").startswith("en")), captions[0])
            cap_url = track.get("url", "")
            if not cap_url:
                continue

            # Step 2: fetch the actual caption content (proxied through Invidious)
            if cap_url.startswith("/"):
                cap_url = base + cap_url
            cr = requests.get(cap_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if cr.status_code != 200:
                continue

            # Parse VTT or XML
            raw = cr.text
            if "<transcript>" in raw or "<text " in raw:
                # YouTube XML format
                texts = re.findall(r'<text[^>]*>(.*?)</text>', raw, re.DOTALL)
                text = " ".join(re.sub(r'<[^>]+>', '', t).strip() for t in texts)
            else:
                text = _parse_vtt(raw)

            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 100:
                print(f"   [invidious] ✅ {base}")
                return text

        except Exception as e:
            print(f"   [invidious] {base} failed: {type(e).__name__}: {str(e)[:80]}")
            continue

    return None


# ---------------------------------------------------------------------------
# Tier 1: youtube-transcript-api
# ---------------------------------------------------------------------------

def _fetch_transcript_with_api(video_id: str, api) -> Optional[str]:
    """Inner helper: fetch + clean transcript text using a pre-built api instance."""
    from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound
    try:
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_manually_created_transcript(['en', 'en-US', 'en-GB'])
        except Exception:
            try:
                transcript = transcript_list.find_generated_transcript(['en', 'en-US', 'en-GB'])
            except Exception:
                available = list(transcript_list)
                if not available:
                    return None
                transcript = available[0].translate('en')

        data = transcript.fetch()
        text = " ".join(
            entry.text if hasattr(entry, "text") else entry.get("text", "")
            for entry in data
        )
        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 100 else None
    except (TranscriptsDisabled, NoTranscriptFound):
        return None


def _get_via_api(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig
        from config import YOUTUBE_PROXY, WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS
    except ImportError:
        return None

    # Attempt order:
    # 1. WebshareProxyConfig (rotating residential) — best, bypasses all IP bans
    #    Get from: dashboard.webshare.io → Proxy Settings → Proxy Username/Password
    #    Must be "Residential" plan, NOT "Proxy Server" or "Static Residential"
    # 2. GenericProxyConfig with YOUTUBE_PROXY URL (e.g. Tor socks5 — limited bypass)
    # 3. No proxy — works for popular/non-protected channels from DO's IP
    #
    # NOTE: Cookie auth is officially broken in youtube-transcript-api as of 2025/2026.
    # The only reliable bypass for protected channels is Webshare Residential.

    attempts = []

    try:
        if WEBSHARE_PROXY_USER and WEBSHARE_PROXY_PASS:
            attempts.append(("webshare-residential", YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(
                    proxy_username=WEBSHARE_PROXY_USER,
                    proxy_password=WEBSHARE_PROXY_PASS,
                )
            )))
    except Exception:
        pass

    try:
        if YOUTUBE_PROXY:
            attempts.append(("generic-proxy", YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=YOUTUBE_PROXY,
                    https_url=YOUTUBE_PROXY,
                )
            )))
    except Exception:
        pass

    attempts.append(("no-proxy", YouTubeTranscriptApi()))

    for label, api in attempts:
        try:
            result = _fetch_transcript_with_api(video_id, api)
            if result:
                print(f"   [transcript-api] ✅ {label}")
                return result
        except Exception as e:
            print(f"   [transcript-api] {label} failed: {type(e).__name__}: {str(e)[:120]}")
            continue

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


def _run_ytdlp_subtitles(video_id: str, tmpdir: str, proxy: Optional[str]) -> Optional[str]:
    """Run yt-dlp subtitle extraction with or without proxy. Returns text or None."""
    import yt_dlp
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
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
    if proxy:
        opts["proxy"] = proxy
    from config import YOUTUBE_COOKIES_FILE
    if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([canonical_url])

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


def _get_via_ytdlp(video_id: str) -> Optional[str]:
    try:
        import yt_dlp
    except ImportError:
        return None

    from config import YOUTUBE_PROXY

    # Try with proxy first, then without
    proxies_to_try = []
    if YOUTUBE_PROXY:
        proxies_to_try.append(YOUTUBE_PROXY)
    proxies_to_try.append(None)  # no-proxy fallback

    for proxy in proxies_to_try:
        label = f"proxy={proxy[:30]}..." if proxy else "no-proxy"
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                result = _run_ytdlp_subtitles(video_id, tmpdir, proxy)
                if result:
                    print(f"   [yt-dlp] ✅ {label}")
                    return result
                else:
                    print(f"   [yt-dlp] {label}: no subtitles found")
            except Exception as e:
                print(f"   [yt-dlp] {label} error: {type(e).__name__}: {str(e)[:120]}")
                continue

    return None


# ---------------------------------------------------------------------------
# Audio chunking helper (for files > 25 MB)
# ---------------------------------------------------------------------------

def _split_audio_ffmpeg(audio_path: str, chunk_mb: float = 22.0) -> list:
    """Split audio into <chunk_mb MB chunks using ffmpeg. Returns list of chunk paths."""
    tmpdir = os.path.dirname(audio_path)
    ext = os.path.splitext(audio_path)[1] or ".m4a"
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(probe.stdout)
        duration = 0.0
        for stream in info.get("streams", []):
            d = stream.get("duration")
            if d:
                duration = float(d)
                break
        if not duration:
            return []

        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        chunk_duration = duration * (chunk_mb / file_size_mb)

        chunks = []
        start = 0.0
        idx = 0
        while start < duration - 1:
            chunk_path = os.path.join(tmpdir, f"chunk_{idx:03d}{ext}")
            subprocess.run(
                ["ffmpeg", "-v", "quiet", "-i", audio_path,
                 "-ss", str(start), "-t", str(chunk_duration), "-c", "copy", chunk_path],
                capture_output=True, timeout=120, check=True,
            )
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                chunks.append(chunk_path)
            start += chunk_duration
            idx += 1
        return chunks
    except FileNotFoundError:
        print("   [groq] ffmpeg not found — cannot chunk large file")
        return []
    except Exception as e:
        print(f"   [groq] ffmpeg split failed: {e}")
        return []


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

        from config import YOUTUBE_PROXY

        # Download best audio — try proxy first, then no proxy
        proxies_to_try = ([YOUTUBE_PROXY] if YOUTUBE_PROXY else []) + [None]
        audio_files = []
        for proxy in proxies_to_try:
            label = "proxy" if proxy else "no-proxy"
            base_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "postprocessors": [],
            }
            if proxy:
                base_opts["proxy"] = proxy
            from config import YOUTUBE_COOKIES_FILE
            if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
                base_opts["cookiefile"] = YOUTUBE_COOKIES_FILE
            try:
                with yt_dlp.YoutubeDL(base_opts) as ydl:
                    ydl.download([canonical_url])
                audio_files = (
                    glob.glob(os.path.join(tmpdir, "*.m4a")) +
                    glob.glob(os.path.join(tmpdir, "*.webm")) +
                    glob.glob(os.path.join(tmpdir, "*.ogg"))
                )
                if audio_files:
                    print(f"   [groq] audio downloaded via {label}")
                    break
            except Exception as e:
                print(f"   [groq] yt-dlp {label} failed: {type(e).__name__}: {str(e)[:120]}")
                continue

        if not audio_files:
            print(f"   [groq] no audio file found for {video_id}")
            return None

        audio_path = audio_files[0]
        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        print(f"   [groq] audio downloaded: {os.path.basename(audio_path)} ({file_size_mb:.1f} MB)")

        client = Groq(api_key=GROQ_API_KEY)

        # Groq's limit is 25 MB per file — split large files with ffmpeg
        if file_size_mb > 24:
            print(f"   [groq] file too large ({file_size_mb:.1f} MB), splitting with ffmpeg...")
            chunks = _split_audio_ffmpeg(audio_path)
            if not chunks:
                print(f"   [groq] cannot split, skipping")
                return None
            parts = []
            for chunk_path in chunks:
                try:
                    with open(chunk_path, "rb") as f:
                        result = client.audio.transcriptions.create(
                            file=(os.path.basename(chunk_path), f),
                            model="whisper-large-v3",
                            response_format="text",
                            language="en",
                        )
                    t = result if isinstance(result, str) else getattr(result, "text", str(result))
                    if t and t.strip():
                        parts.append(t.strip())
                except Exception as e:
                    print(f"   [groq] chunk {os.path.basename(chunk_path)} failed: {e}")
            if not parts:
                return None
            text = " ".join(parts)
            text = re.sub(r'\s+', ' ', text).strip()
            return text if len(text) > 100 else None

        try:
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

    print(f"   [transcript] trying Invidious for {video_id}...")
    result = _get_via_invidious(video_id)
    if result:
        print(f"   [transcript] ✅ invidious tier — {len(result):,} chars")
        return result

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
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f"transcript: all 3 tiers failed for {video_id}",
            level="warning",
            extras={"video_id": video_id, "video_url": video_url},
        )
    except Exception:
        pass
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
