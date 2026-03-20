"""
llm.py — All prompts + LLM generation.

Architecture:
  - SLOT_JOBS: what each email slot must accomplish (keyed by slot × frequency)
  - DEPTH_CONFIGS: extraction density and structure per depth mode
  - LENS_CONFIGS: the angle applied to extraction (replaces TONE_CONFIGS)
  - build_slot_prompt(): combines all three dynamically — no N×M hardcoding
  - instructor enforces Pydantic schema — JSON breakage solved permanently
"""
import os
import re
import json
import time as _time
from typing import Optional

import requests as _requests
import openai
from pydantic import BaseModel, Field

from config import OPENROUTER_API_KEY, LLM_MODEL, LLM_MODEL_FALLBACKS, LLM_BUDGET_CAP


# ---------------------------------------------------------------------------
# Pydantic output models
# ---------------------------------------------------------------------------

class EmailOutput(BaseModel):
    subject: str = Field(description="Email subject line, max 60 chars, no clickbait")
    body: str    = Field(description="Complete email body, plain text, no markdown symbols")


class ReviewOutput(BaseModel):
    subject: str = Field(description="Review email subject line")
    body: str    = Field(description="Complete review email body, plain text")


class SynthesisOutput(BaseModel):
    subject: str = Field(description="Synthesis email subject line")
    body: str    = Field(description="Complete synthesis email body, plain text")


# ---------------------------------------------------------------------------
# Slot jobs — what each email in a multi-email day is responsible for
# ---------------------------------------------------------------------------

SLOT_JOBS = {
    "morning": {
        "1x": (
            "This is the only email today. Extract everything worth keeping.\n"
            "Open with the most counterintuitive claim in this video — the thing that would make"
            " a smart person say 'wait, really?' Name it directly.\n"
            "Explain what the video actually argues (not what the title promises)."
            " Be specific: who said what, what data, what case.\n"
            "Surface the underlying assumption being challenged — the thing mainstream thinking"
            " gets wrong that this video quietly corrects.\n"
            "One action derived from the video's actual argument. Not generic advice."
            " Something you could only give if you watched this video.\n"
            "End with the real question the video is asking — not the stated one."
        ),
        "2x": (
            "First email of two today.\n"
            "Lead with the most surprising or uncomfortable claim in this video.\n"
            "What does this video argue that most smart people would initially dismiss?"
            " State it plainly.\n"
            "One specific example from the video — name, situation, exact outcome.\n"
            "End with the tension: 'Tonight we look at what this actually costs.'"
        ),
        "3x": (
            "First email of three today.\n"
            "The claim: what is this video actually arguing? State it in one blunt sentence.\n"
            "Why most people won't accept this — the cognitive bias or social incentive"
            " that makes this hard to hear.\n"
            "One named example from the video that makes the claim concrete.\n"
            "End with a question that makes them sit with the discomfort until midday."
        ),
        "5x": (
            "First email of five today.\n"
            "Open with the thing this video says that directly contradicts what most people believe.\n"
            "The core claim in two sentences — plain language, no hedging, no qualifications.\n"
            "End: 'Four more emails today. Each goes one layer deeper. Watch for it.'"
        ),
    },
    "late_morning": {
        "5x": (
            "Second email of five. Do NOT re-explain the concept.\n"
            "The evidence layer: who in the video proves this claim true?\n"
            "Format strictly: name → their context → what they did → specific outcome"
            " (include a number, date, or measurable result).\n"
            "The default behavior most people use instead — and the specific way it fails.\n"
            "End: 'At 2pm: when this goes wrong.'"
        ),
    },
    "midday": {
        "3x": (
            "Second email of three. Do NOT re-explain the morning.\n"
            "The failure case or blind spot: who applied this wrong, or who is this video"
            " implicitly warning against? Pull it from the transcript, don't invent it.\n"
            "The hidden cost: what do you give up — structurally, socially, psychologically"
            " — if you actually live by this idea?\n"
            "One named example with specifics.\n"
            "Bridge to night: 'Tonight: how to actually use this."
            " Until then — [one specific thing to notice before you sleep].'"
        ),
    },
    "afternoon": {
        "5x": (
            "Third email of five.\n"
            "The failure case — someone who ignored or misapplied what this video argues.\n"
            "Format strictly: name → what they believed instead → what happened."
            " No moralizing. No lesson. Just facts.\n"
            "The structural reason this goes wrong — not a personal failing, a system problem.\n"
            "End: 'At 5pm: the framework.'"
        ),
    },
    "evening": {
        "5x": (
            "Fourth email of five.\n"
            "Convert the video's argument into a decision-making tool the reader can actually use:\n"
            "  Trigger: when you encounter [specific situation the video describes]...\n"
            "  Default (what most people do): [the mistake this video is warning against]\n"
            "  The move: [what this video is actually prescribing — be specific]\n"
            "  How you know it's working: [specific signal or outcome from the video]\n"
            "  The trap: [how people half-apply this and still fail]\n"
            "End: 'At 9pm: the full picture.'"
        ),
    },
    "night": {
        "2x": (
            "Second email of two. They've had all day with this.\n"
            "One-line callback to the morning — don't re-explain.\n"
            "The thing the video never says out loud but implies:"
            " what belief do you have to give up to actually act on this?\n"
            "Three bullets: the three extractable insights worth keeping permanently.\n"
            "One journal prompt — specific enough to generate a real answer, not a feeling."
        ),
        "3x": (
            "Third email of three. Close the loop.\n"
            "The arc in two sentences: what the morning introduced, what the midday complicated.\n"
            "The synthesis: what does it actually cost you to believe this — what old story breaks?\n"
            "Three key bullets: the extractable claims.\n"
            "Two journal prompts: one about where you've already seen this in your own life,"
            " one about what you'd do differently starting tomorrow."
        ),
        "5x": (
            "Fifth email of five. Full lock-in.\n"
            "The arc in two sentences — what changed from email 1 to email 5.\n"
            "The master insight: one sentence that contains everything."
            " Write it like it needs to survive 10 years.\n"
            "Five bullets — one per email, the extractable claim from each.\n"
            "Write tonight:\n"
            "  - 'The moment I recognized this in my own life was...'\n"
            "  - 'The belief I have to give up to live by this is...'\n"
            "  - 'Someone I know who already does this is... and what I notice about them is...'\n"
            "'Take 10 minutes. Write until you run out.'"
        ),
    },
}

# ---------------------------------------------------------------------------
# Depth configs — controls extraction density and structure
# ---------------------------------------------------------------------------

DEPTH_CONFIGS = {
    "signal": {
        "length": "80-120 words",
        "rules": (
            "One insight only. The single most extractable, transferable idea from this video.\n"
            "Format strictly: the claim → why it's true → one proof from the video → one action.\n"
            "If a sentence can be cut without losing meaning, cut it.\n"
            "No warmup sentence. No sign-off. Just the signal."
        ),
    },
    "full": {
        "length": "250-350 words",
        "rules": (
            "Complete extraction. The video's argument + why + evidence + implication + action.\n"
            "Use real names, real numbers, real companies from the video — never generic stand-ins.\n"
            "Surface the thing most people skim past: the buried qualifier, the footnoted exception,"
            " the claim the speaker almost walked back.\n"
            "End with one action derived from the video's actual argument — not common sense advice"
            " you could give without watching the video."
        ),
    },
    "blunt": {
        "length": "150-200 words",
        "rules": (
            "Lead with the uncomfortable truth — the specific claim this video makes that"
            " most people will resist or quietly ignore.\n"
            "State it without softening. No 'it's nuanced', no 'of course it depends'.\n"
            "The evidence in one sentence — just enough to make it stick.\n"
            "What this means if you actually take it seriously.\n"
            "No warmup. No sign-off."
        ),
    },
    "autopsy": {
        "length": "500-700 words",
        "rules": (
            "Take the video's argument apart completely. Plain text headers, ALL CAPS, no symbols.\n"
            "Cover in this exact order:\n"
            "  THE ACTUAL CLAIM: what the video argues, stated without euphemism\n"
            "  WHY IT'S COUNTERINTUITIVE: the mainstream belief this directly contradicts\n"
            "  THE EVIDENCE: 2-3 named cases from the video with specifics (numbers, outcomes, dates)\n"
            "  WHAT THEY LEFT OUT: the caveat, edge case, or thing the video glosses over\n"
            "  THE BURIED SIGNAL: something subtle most people won't catch on first watch\n"
            "  HOW TO USE IT: 3-step application with real specifics, not generic steps\n"
            "  THE REAL QUESTION: what this video is actually asking you to examine about yourself"
        ),
    },
    # Legacy key aliases — existing DB records with old depth values still work
    "tldr":     None,
    "mix":      None,
    "nobs":     None,
    "hardcore": None,
}
# Map old keys to new
DEPTH_CONFIGS["tldr"]     = DEPTH_CONFIGS["signal"]
DEPTH_CONFIGS["mix"]      = DEPTH_CONFIGS["full"]
DEPTH_CONFIGS["nobs"]     = DEPTH_CONFIGS["blunt"]
DEPTH_CONFIGS["hardcore"] = DEPTH_CONFIGS["autopsy"]

# ---------------------------------------------------------------------------
# Lens configs — the extraction angle applied to the content
# (replaces TONE_CONFIGS; old tone values mapped for backward compat)
# ---------------------------------------------------------------------------

LENS_CONFIGS = {
    "Straight": (
        "Report what the video actually says. Precise attribution."
        " Let the content do the work. No editorializing."
    ),
    "Red Pill": (
        "Surface the uncomfortable implication — what this video says that most people will"
        " acknowledge intellectually and avoid acting on. Write for the reader who actually"
        " wants to change something, not just feel like they learned something."
    ),
    "Forensic": (
        "Treat the transcript like a primary source under examination. What is the video"
        " really arguing vs what it claims to be arguing? Where is the evidence strongest?"
        " Where does it rely on assertion or social proof rather than data?"
    ),
    "Street": (
        "No credentials, no context, no safety net. Write for someone who has to act on"
        " this today with limited resources and no margin for error. Concrete, direct, zero hedging."
    ),
    "Philosophical": (
        "Trace the idea to its root assumption. What has to be true about the world for"
        " this video's argument to hold? What does believing this require you to give up?"
        " What older idea does this quietly replace?"
    ),
    # Legacy tone values → nearest lens equivalent
    "Casual":   "Report what the video actually says. Precise attribution. Let the content do the work. No editorializing.",
    "Direct":   "No credentials, no context, no safety net. Write for someone who has to act on this today with limited resources and no margin for error. Concrete, direct, zero hedging.",
    "Academic": "Treat the transcript like a primary source under examination. What is the video really arguing vs what it claims to be arguing? Where does it rely on assertion rather than data?",
    "ELI5":     "Report what the video actually says. Use the simplest possible language. Precise attribution. Let the content do the work.",
    "Witty":    "Surface the uncomfortable implication — what this video says that most people will acknowledge intellectually and avoid acting on.",
}

# Keep TONE_CONFIGS as alias so any direct references elsewhere don't break
TONE_CONFIGS = LENS_CONFIGS

# ---------------------------------------------------------------------------
# Instructor client (auto-retries on JSON failures)
# ---------------------------------------------------------------------------

def _get_client() -> openai.OpenAI:
    return openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )


def _parse_json_response(raw: str, model_cls):
    """
    Robustly extract JSON from LLM output.
    Handles: markdown fences, preamble text, trailing garbage.
    Validates against a Pydantic model.
    """
    # Strip markdown fences
    text = re.sub(r"```json\s*", "", raw)
    text = re.sub(r"```\s*", "", text).strip()
    # Find outermost { }
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in response")
    text = text[start:end+1]
    data = json.loads(text)
    return model_cls(**data)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_slot_prompt(
    video_title: str,
    transcript: str,
    frequency: str,
    slot: str,
    slot_index: int,
    total_slots: int,
    depth: str,
    tone: str,
) -> str:
    slot_job   = SLOT_JOBS.get(slot, {}).get(frequency, "Extract the most useful insight from this video.")
    depth_cfg  = DEPTH_CONFIGS.get(depth, DEPTH_CONFIGS["full"])
    lens_rule  = LENS_CONFIGS.get(tone, LENS_CONFIGS["Straight"])

    # Truncate transcript intelligently at sentence boundary near 28k chars
    t = transcript[:28000]
    if len(transcript) > 28000:
        last_period = t.rfind(". ")
        if last_period > 20000:
            t = t[:last_period + 1]

    return f"""You are Capsule — a learning extraction system that turns video transcripts into emails that actually teach.

Your job is to extract from the ACTUAL transcript — not from general knowledge about the topic.
If something isn't in the transcript, don't write it. Be specific to this video.

VIDEO:
Title: {video_title}
Transcript:
{t}

---

THIS EMAIL ({slot_index + 1} of {total_slots} today):

JOB:
{slot_job}

DEPTH — {depth} ({depth_cfg['length']}):
{depth_cfg['rules']}

EXTRACTION LENS — {tone}:
{lens_rule}

NON-NEGOTIABLE RULES:
- Everything must come from the transcript. No generic examples you could have written without it.
- Use real names, companies, numbers from the video. If none exist, say what was actually said.
- No emojis. Ever.
- No decorative separators (no ----, no ****).
- Never open with: "In today's world", "It's important to", "As we navigate", "In this fast-paced", or any generic opener.
- Plain text body — no markdown symbols (* # ` ~) in the output.
- Subject line: written like a human, not a newsletter. Specific, not clever.
- Every sentence adds information. Cut anything that restates what came before.

Return ONLY a JSON object with exactly two keys: "subject" and "body".
No preamble. No markdown fences. No explanation. Just the JSON."""


def build_review_prompt(
    video_title: str,
    original_body: str,
    review_type: str,
    tone: str,
) -> str:
    tone_rule = LENS_CONFIGS.get(tone, LENS_CONFIGS["Straight"])

    configs = {
        "day3": {
            "opener": f"3 days ago you learned something from: '{video_title}'.",
            "job": (
                "Quick recall test. Three questions based on the actual content — start easy, get harder.\n"
                "Then: 3 bullets — what to keep from this forever.\n"
                "Keep it short. This is a check-in, not a re-teach.\n"
                "Target: 120-150 words."
            ),
        },
        "day7": {
            "opener": f"One week ago: '{video_title}'.",
            "job": (
                "Application check. One honest question: did they actually use this?\n"
                "The thing most people have forgotten by week 2 (from the original content).\n"
                "One nudge — specific, not generic.\n"
                "Target: 120-150 words."
            ),
        },
        "day30": {
            "opener": f"One month ago you learned from: '{video_title}'.",
            "job": (
                "Mastery check. Distill everything to 3 sentences.\n"
                "The real question this video was pointing at (unstated).\n"
                "A specific commitment for the next 30 days — not vague.\n"
                "Target: 150-200 words."
            ),
        },
    }

    cfg = configs.get(review_type, configs["day3"])

    return f"""You are Capsule. This is a spaced repetition review email.

{cfg['opener']}

ORIGINAL CONTENT (for reference):
{original_body[:3000]}

JOB:
{cfg['job']}

TONE: {tone_rule}

Rules:
- Reference the original content specifically, not generically.
- No re-teaching from scratch — they learned this. Probe and reinforce.
- No emojis. No markdown symbols.

Return ONLY JSON with keys "subject" and "body"."""


def build_synthesis_prompt(
    video_titles: list,
    email_snippets: list,
    tone: str,
) -> str:
    tone_rule = LENS_CONFIGS.get(tone, LENS_CONFIGS["Straight"])
    videos_block   = "\n".join(f"{i+1}. {t}" for i, t in enumerate(video_titles))
    snippets_block = "\n\n---\n\n".join(
        f"Video {i+1} ({video_titles[i]}):\n{s[:800]}"
        for i, s in enumerate(email_snippets)
    )

    return f"""You are Capsule. This is a synthesis email — sent after the user completes every 5 videos.

VIDEOS COMPLETED THIS BATCH:
{videos_block}

CONTENT SAMPLES:
{snippets_block}

JOB (250-350 words):
1. THE THREAD: the invisible idea running through all 5 videos that was never stated explicitly.
2. THE EMERGENT INSIGHT: what you only see when you put all 5 together — something none of them say alone.
3. THE UNASKED QUESTION: what all 5 videos imply but none of them ask.
4. THE INTEGRATION ACTION: one specific thing that uses all 5 ideas at once. Named, actionable, not vague.

TONE: {tone_rule}

Rules:
- Don't summarize each video — synthesize across all of them.
- No emojis. No markdown symbols.
- This should feel like a revelation, not a recap.

Return ONLY JSON with keys "subject" and "body"."""


# ---------------------------------------------------------------------------
# Generation functions
# ---------------------------------------------------------------------------

def _check_budget() -> None:
    """Raise RuntimeError if OpenRouter spend has hit LLM_BUDGET_CAP."""
    if not OPENROUTER_API_KEY:
        return
    try:
        resp = _requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=8,
        )
        if not resp.ok:
            print(f"   [llm] budget check failed (HTTP {resp.status_code}) — allowing call")
            return
        data = resp.json().get("data", {})
        usage = float(data.get("usage", 0) or 0)
        print(f"   [llm] OpenRouter usage: ${usage:.4f} / ${LLM_BUDGET_CAP:.2f} cap")
        if usage >= LLM_BUDGET_CAP:
            try:
                from mailer import send_alert
                send_alert(
                    f"LLM budget cap hit: ${usage:.4f}",
                    f"OpenRouter spend ${usage:.4f} has hit the ${LLM_BUDGET_CAP:.2f} cap.\n\n"
                    f"LLM generation is paused. To resume:\n"
                    f"  1. Add credit to OpenRouter\n"
                    f"  2. Raise LLM_BUDGET_CAP in /app/.env on the server\n"
                    f"  3. docker restart app_capsule_1\n\n"
                    f"Admin: http://157.245.103.89/admin"
                )
            except Exception:
                pass
            raise RuntimeError(
                f"LLM budget cap reached: ${usage:.4f} spent >= ${LLM_BUDGET_CAP:.2f} limit. "
                f"Raise LLM_BUDGET_CAP env var to continue."
            )
    except RuntimeError:
        raise
    except Exception as e:
        print(f"   [llm] budget check error: {e} — allowing call")


def _rate_limit_wait(e: openai.RateLimitError) -> float:
    """Extract how long to wait from a 429 response. Returns seconds."""
    try:
        headers = e.response.headers
        # X-RateLimit-Reset: epoch milliseconds (OpenRouter)
        reset_ms = headers.get("X-RateLimit-Reset")
        if reset_ms:
            wait = int(reset_ms) / 1000 - _time.time()
            return max(2.0, min(wait + 1, 300))
        # Retry-After: seconds (standard)
        retry_after = headers.get("Retry-After")
        if retry_after:
            return float(retry_after) + 1
    except Exception:
        pass
    return 65.0  # default: 65s (RPM window is 60s)


def _call_llm(prompt: str, max_tokens: int = 2000, retries: int = 3) -> Optional[str]:
    """Raw LLM call with model-tier fallback.

    On rate limit: immediately tries next model in tier (no sleeping).
    Only retries the same model on transient errors (non-429).
    """
    client = _get_client()
    model_tier = [LLM_MODEL] + LLM_MODEL_FALLBACKS

    for model in model_tier:
        for attempt in range(1, retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are Capsule, a learning email writer. "
                                "Return ONLY a valid JSON object. No preamble, no markdown fences, no explanation. "
                                "Just the raw JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=0.7,
                )
                content = resp.choices[0].message.content
                if content and content.strip():
                    if model != LLM_MODEL:
                        print(f"   [llm] used fallback model: {model}")
                    return content.strip()
            except openai.RateLimitError:
                print(f"   [llm] {model} rate limited — trying next tier")
                break  # don't retry this model, move to next
            except Exception as e:
                print(f"   [llm] {model} attempt {attempt} failed: {e}")
    return None


def generate_email_slot(
    video_title: str,
    transcript: str,
    frequency: str,
    slot: str,
    slot_index: int,
    total_slots: int,
    depth: str,
    tone: str,
    max_retries: int = 3,
) -> Optional[EmailOutput]:
    prompt = build_slot_prompt(
        video_title, transcript, frequency, slot,
        slot_index, total_slots, depth, tone
    )
    raw = _call_llm(prompt, max_tokens=2000, retries=max_retries)
    if not raw:
        print(f"   [llm] slot '{slot}' — no content returned")
        return None
    try:
        return _parse_json_response(raw, EmailOutput)
    except Exception as e:
        print(f"   [llm] slot '{slot}' — JSON parse failed: {e}\nRaw: {raw[:200]}")
        return None


def generate_all_slots(
    video_title: str,
    transcript: str,
    frequency: str,
    depth: str,
    tone: str,
) -> dict:
    """
    Generate all email slots for a video based on frequency.
    Returns {slot_name: EmailOutput} for successfully generated slots.
    """
    _check_budget()   # hard stop if spend >= LLM_BUDGET_CAP
    from db import SLOTS_BY_FREQUENCY
    slots = SLOTS_BY_FREQUENCY.get(frequency, ["morning"])
    total = len(slots)
    results = {}

    for i, slot in enumerate(slots):
        print(f"   [llm] generating slot {i+1}/{total}: {slot}")
        out = generate_email_slot(
            video_title=video_title,
            transcript=transcript,
            frequency=frequency,
            slot=slot,
            slot_index=i,
            total_slots=total,
            depth=depth,
            tone=tone,
        )
        if out:
            results[slot] = out
        else:
            print(f"   [llm] ⚠ slot '{slot}' skipped after retries")

    return results


def generate_review(
    video_title: str,
    original_body: str,
    review_type: str,
    tone: str,
) -> Optional[ReviewOutput]:
    prompt = build_review_prompt(video_title, original_body, review_type, tone)
    raw = _call_llm(prompt, max_tokens=800)
    if not raw:
        return None
    try:
        return _parse_json_response(raw, ReviewOutput)
    except Exception as e:
        print(f"   [llm] review '{review_type}' parse failed: {e}")
        return None


def generate_synthesis(
    video_titles: list,
    email_snippets: list,
    tone: str,
) -> Optional[SynthesisOutput]:
    prompt = build_synthesis_prompt(video_titles, email_snippets, tone)
    raw = _call_llm(prompt, max_tokens=1500)
    if not raw:
        return None
    try:
        return _parse_json_response(raw, SynthesisOutput)
    except Exception as e:
        print(f"   [llm] synthesis parse failed: {e}")
        return None


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def render_html(subject: str, body: str, slot: str = "morning", video_title: str = "",
                video_position: int = -1, total_videos: int = 0,
                email_id: str = "") -> str:
    """Convert plain text email body to beautiful, production-grade HTML email."""
    import html as _html

    SLOT_LABELS = {
        "morning":      "Morning",
        "late_morning": "Mid-Morning",
        "midday":       "Midday",
        "afternoon":    "Afternoon",
        "evening":      "Evening",
        "night":        "Tonight",
        "day3":         "3-Day Review",
        "day7":         "7-Day Review",
        "day30":        "30-Day Review",
        "synthesis":    "Weekly Synthesis",
    }
    label = SLOT_LABELS.get(slot, "Lesson")

    def esc(s: str) -> str:
        return _html.escape(str(s) if s else "")

    # ── Body rows ──────────────────────────────────────────────────────────
    lines = body.strip().split("\n")
    body_rows = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        # ALL CAPS short line = section header (hardcore depth mode)
        if s == s.upper() and len(s) < 60 and len(s) > 2 and s.replace(" ", "").isalpha():
            body_rows.append(
                f'<tr><td style="padding:28px 0 10px 0;">'
                f'<p style="font-family:Helvetica,Arial,sans-serif;font-size:10px;font-weight:bold;'
                f'text-transform:uppercase;letter-spacing:2.5px;color:#A09890;margin:0;padding:0;">'
                f'{esc(s)}</p></td></tr>'
            )
        # Bullet lines
        elif s.startswith("- ") or s.startswith("• "):
            text = esc(s[2:])
            body_rows.append(
                f'<tr><td style="padding:4px 0;">'
                f'<table border="0" cellspacing="0" cellpadding="0"><tr>'
                f'<td style="color:#C8C0B8;font-family:Georgia,serif;font-size:16px;'
                f'padding-right:12px;vertical-align:top;line-height:1.85;">—</td>'
                f'<td style="font-family:Georgia,serif;font-size:16px;line-height:1.85;'
                f'color:#2A2520;">{text}</td>'
                f'</tr></table></td></tr>'
            )
        # Numbered list
        elif len(s) > 2 and s[0].isdigit() and s[1] in ".)":
            body_rows.append(
                f'<tr><td style="padding:4px 0;">'
                f'<p style="font-family:Georgia,serif;font-size:16px;line-height:1.85;'
                f'color:#2A2520;margin:0;padding:0;">{esc(s)}</p></td></tr>'
            )
        else:
            body_rows.append(
                f'<tr><td style="padding:0 0 18px 0;">'
                f'<p style="font-family:Georgia,serif;font-size:16px;line-height:1.85;'
                f'color:#2A2520;margin:0;padding:0;">{esc(s)}</p></td></tr>'
            )
    body_html = "\n".join(body_rows)

    # ── Progress bar ───────────────────────────────────────────────────────
    progress_row = ""
    if total_videos > 0 and video_position >= 0:
        pos = video_position + 1
        pct = min(100, round(pos / total_videos * 100))
        progress_row = f"""
  <tr><td style="padding:8px 0 0 0;">
    <table width="100%" border="0" cellspacing="0" cellpadding="0">
      <tr>
        <td style="font-family:Helvetica,Arial,sans-serif;font-size:11px;color:#C0B8B0;
            text-transform:uppercase;letter-spacing:1px;">Video {pos} of {total_videos}</td>
        <td align="right" style="font-family:Helvetica,Arial,sans-serif;font-size:11px;
            color:#C0B8B0;">{pct}%</td>
      </tr>
      <tr><td colspan="2" height="10"></td></tr>
      <tr>
        <td colspan="2" bgcolor="#E8E2DC" height="2"
            style="background:#E8E2DC;border-radius:2px;line-height:2px;font-size:2px;">
          <div style="background:#0D0D0D;height:2px;width:{pct}%;font-size:0px;
              line-height:0px;">&nbsp;</div>
        </td>
      </tr>
    </table>
  </td></tr>"""

    # ── Video subtitle ─────────────────────────────────────────────────────
    subtitle_row = ""
    if video_title:
        subtitle_row = (
            f'<tr><td style="padding-bottom:12px;">'
            f'<p style="font-family:Helvetica,Arial,sans-serif;font-size:11px;color:#A09080;'
            f'text-transform:uppercase;letter-spacing:1.5px;margin:0;padding:0;">'
            f'{esc(video_title[:80])}</p></td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#FAFAF8;">
<table width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#FAFAF8"
       style="background-color:#FAFAF8;">
  <tr>
    <td align="center" style="padding:48px 20px 64px;">
      <table width="600" border="0" cellspacing="0" cellpadding="0"
             style="max-width:600px;width:100%;">

        <!-- ── HEADER ──────────────────────────────────────────────── -->
        <tr>
          <td style="padding-bottom:24px;border-bottom:2px solid #E8E2DC;">
            <table width="100%" border="0" cellspacing="0" cellpadding="0">
              <tr>
                <td style="font-family:Helvetica,Arial,sans-serif;font-size:11px;font-weight:bold;
                    letter-spacing:3px;text-transform:uppercase;color:#0D0D0D;">CAPSULE</td>
                <td align="right" style="font-family:Helvetica,Arial,sans-serif;font-size:11px;
                    color:#A09890;text-transform:uppercase;letter-spacing:1.5px;">{label}</td>
              </tr>
            </table>
          </td>
        </tr>

        <tr><td height="36"></td></tr>

        <!-- ── VIDEO SUBTITLE ──────────────────────────────────────── -->
        {subtitle_row}

        <!-- ── SUBJECT HEADLINE ────────────────────────────────────── -->
        <tr>
          <td style="padding-bottom:34px;">
            <h1 style="font-family:Georgia,'Times New Roman',serif;font-size:30px;
                line-height:1.2;letter-spacing:-0.5px;color:#0D0D0D;
                margin:0;padding:0;font-weight:normal;">{esc(subject)}</h1>
          </td>
        </tr>

        <!-- ── BODY ────────────────────────────────────────────────── -->
        {body_html}

        <tr><td height="28"></td></tr>

        <!-- ── PROGRESS BAR ────────────────────────────────────────── -->
        {progress_row}

        <!-- ── FOOTER ──────────────────────────────────────────────── -->
        <tr>
          <td style="padding-top:40px;">
            <table width="100%" border="0" cellspacing="0" cellpadding="0">
              <tr>
                <td style="border-top:1px solid #E8E2DC;padding-top:22px;">
                  <p style="font-family:Helvetica,Arial,sans-serif;font-size:11px;
                      color:#C8C0B8;margin:0;padding:0;line-height:1.6;">
                    Capsule — knowledge that sticks.
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
{f'<img src="https://capsule.ohm.quest/track/o/{email_id}" width="1" height="1" alt="" style="display:none">' if email_id else ''}
</body>
</html>"""
