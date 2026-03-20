"""
mailer.py — Email delivery.
Primary: Resend API (RESEND_API_KEY).
Fallback: Gmail SMTP (GMAIL_USER + GMAIL_APP_PASSWORD).
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from config import RESEND_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, FROM_EMAIL, ALERT_EMAIL


def send_email(to: str, subject: str, html_body: str) -> bool:
    if RESEND_API_KEY:
        return _send_resend(to, subject, html_body)
    return _send_gmail(to, subject, html_body)


def _send_resend(to: str, subject: str, html_body: str) -> bool:
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": [to],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"   [mailer] ❌ Resend error {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"   [mailer] ❌ Resend exception: {e}")
        return False


def _send_gmail(to: str, subject: str, html_body: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("   [mailer] ❌ No mail credentials configured (set RESEND_API_KEY or GMAIL_*)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to, msg.as_string())
        return True
    except Exception as e:
        print(f"   [mailer] ❌ Gmail failed to send to {to}: {e}")
        return False


def send_alert(subject: str, body: str) -> bool:
    """Send a plain-text ops alert to ALERT_EMAIL. No-ops if ALERT_EMAIL is not set."""
    if not ALERT_EMAIL:
        print(f"   [alert] ALERT_EMAIL not set — skipping: {subject}")
        return False
    html = f"""<!DOCTYPE html><html><body style="font-family:monospace;font-size:14px;
color:#111;background:#fff;padding:32px;max-width:600px;">
<p style="font-weight:bold;font-size:16px;margin:0 0 16px;">CAPSULE ALERT</p>
<p style="white-space:pre-wrap;margin:0;">{body}</p>
<p style="margin:24px 0 0;color:#888;font-size:12px;">capsule.ohm.quest — automated alert</p>
</body></html>"""
    ok = send_email(ALERT_EMAIL, f"[Capsule] {subject}", html)
    if ok:
        print(f"   [alert] sent: {subject}")
    return ok


def send_welcome_email(to: str, course_title: str, total_videos: int,
                       frequency: str, timezone: str) -> bool:
    freq_labels = {
        "1x": "one email per day",
        "2x": "morning &amp; evening",
        "3x": "morning, midday &amp; night",
        "5x": "five times a day — full arc",
    }
    freq_label = freq_labels.get(frequency, frequency)
    short_title = (course_title[:60] + "…") if len(course_title) > 60 else course_title

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>You're enrolled</title></head>
<body style="margin:0;padding:0;background-color:#FAFAF8;">
<table width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#FAFAF8">
  <tr><td align="center" style="padding:48px 20px 64px;">
    <table width="600" border="0" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;">

      <tr>
        <td style="padding-bottom:24px;border-bottom:2px solid #E8E2DC;">
          <table width="100%" border="0" cellspacing="0" cellpadding="0">
            <tr>
              <td style="font-family:Helvetica,Arial,sans-serif;font-size:11px;font-weight:bold;
                  letter-spacing:3px;text-transform:uppercase;color:#0D0D0D;">CAPSULE</td>
              <td align="right" style="font-family:Helvetica,Arial,sans-serif;font-size:11px;
                  color:#A09890;text-transform:uppercase;letter-spacing:1.5px;">Welcome</td>
            </tr>
          </table>
        </td>
      </tr>

      <tr><td height="36"></td></tr>

      <tr>
        <td style="padding-bottom:8px;">
          <p style="font-family:Helvetica,Arial,sans-serif;font-size:11px;color:#A09080;
              text-transform:uppercase;letter-spacing:1.5px;margin:0;padding:0;">
            {total_videos} video{'s' if total_videos != 1 else ''} · {freq_label}</p>
        </td>
      </tr>

      <tr>
        <td style="padding-bottom:34px;">
          <h1 style="font-family:Georgia,'Times New Roman',serif;font-size:30px;
              line-height:1.2;letter-spacing:-0.5px;color:#0D0D0D;
              margin:0;padding:0;font-weight:normal;">You're in. Your first lesson arrives tomorrow morning.</h1>
        </td>
      </tr>

      <tr>
        <td style="padding-bottom:18px;">
          <p style="font-family:Georgia,serif;font-size:16px;line-height:1.85;color:#2A2520;margin:0;padding:0;">
            You've enrolled in <strong>{short_title}</strong>. We're processing your {total_videos} video{'s' if total_videos != 1 else ''} now —
            generating {freq_label} for each one.
          </p>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:18px;">
          <p style="font-family:Georgia,serif;font-size:16px;line-height:1.85;color:#2A2520;margin:0;padding:0;">
            Your first email lands tomorrow at your local morning time ({timezone}).
            Each lesson is tailored, focused, and short enough to read with your coffee.
          </p>
        </td>
      </tr>
      <tr>
        <td style="padding-bottom:18px;">
          <p style="font-family:Georgia,serif;font-size:16px;line-height:1.85;color:#2A2520;margin:0;padding:0;">
            Spaced repetition reviews come automatically — 3 days, 7 days, and 30 days after each lesson.
            You don't have to remember to study. We handle the timing.
          </p>
        </td>
      </tr>

      <tr><td height="28"></td></tr>

      <tr>
        <td>
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
  </td></tr>
</table>
</body></html>"""

    subject = f"You're enrolled — \"{short_title}\""
    return send_email(to, subject, html)
