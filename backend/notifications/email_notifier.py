"""
Gmail email notifier using Python's built-in smtplib.

Setup
-----
1. Enable 2-Step Verification on your Google account.
2. Go to https://myaccount.google.com/apppasswords
3. Create an App Password (select "Mail" + "Other").
4. Add to .env:
       GMAIL_USER=you@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (16-char app password)
       NOTIFY_EMAIL_TO=you@gmail.com             (recipient — defaults to GMAIL_USER)
       NOTIFY_EMAIL_ENABLED=true
"""

import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from loguru import logger


def _send_sync(
    gmail_user: str,
    app_password: str,
    to_address: str,
    subject: str,
    body_html: str,
    body_text: str,
) -> bool:
    """Synchronous send via Gmail SMTP TLS. Run inside asyncio.to_thread()."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AI Bot Trader <{gmail_user}>"
    msg["To"] = to_address

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(gmail_user, app_password)
            server.sendmail(gmail_user, to_address, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "[EmailNotifier] Gmail authentication failed. "
            "Make sure GMAIL_APP_PASSWORD is the 16-char App Password, not your Gmail password."
        )
        return False
    except Exception as exc:
        logger.error(f"[EmailNotifier] Send failed: {exc}")
        return False


def _make_html(title: str, message: str, level: str, metadata: Optional[dict]) -> tuple[str, str]:
    """Return (html_body, plain_body) for a notification email."""
    level_colors = {
        "success": "#22c55e",
        "warning": "#f59e0b",
        "error":   "#ef4444",
        "info":    "#3b82f6",
    }
    color = level_colors.get(level, "#3b82f6")

    # Build metadata table rows
    meta_rows = ""
    if metadata:
        for k, v in metadata.items():
            if v is not None:
                meta_rows += f"<tr><td style='color:#9ca3af;padding:2px 8px'>{k}</td><td style='color:#e5e7eb;padding:2px 8px'>{v}</td></tr>"
    meta_block = f"""
        <table style='margin-top:12px;border-collapse:collapse;width:100%'>
          {meta_rows}
        </table>
    """ if meta_rows else ""

    html = f"""<!DOCTYPE html>
<html><body style='background:#111827;margin:0;padding:20px;font-family:system-ui,sans-serif'>
<div style='max-width:480px;margin:0 auto;background:#1f2937;border-radius:12px;overflow:hidden'>
  <div style='background:{color};padding:14px 20px'>
    <span style='color:#fff;font-weight:bold;font-size:15px'>{title}</span>
  </div>
  <div style='padding:20px'>
    <p style='color:#e5e7eb;margin:0 0 12px'>{message}</p>
    {meta_block}
    <p style='color:#6b7280;font-size:11px;margin-top:20px'>AI Bot Trader · Automated notification</p>
  </div>
</div>
</body></html>"""

    plain = f"{title}\n\n{message}"
    if metadata:
        for k, v in metadata.items():
            if v is not None:
                plain += f"\n{k}: {v}"
    plain += "\n\n-- AI Bot Trader"
    return html, plain


class EmailNotifier:
    """Async Gmail notifier. Uses app-password auth — no OAuth required."""

    def __init__(self, gmail_user: str, app_password: str, to_address: str):
        self.gmail_user = gmail_user
        self.app_password = app_password
        self.to_address = to_address
        self._enabled = bool(gmail_user and app_password)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(
        self,
        title: str,
        message: str,
        level: str = "info",
        metadata: Optional[dict] = None,
    ) -> bool:
        """Send a notification email. Returns True on success."""
        if not self._enabled:
            return False
        html, plain = _make_html(title, message, level, metadata)
        subject = f"[{level.upper()}] {title}"
        try:
            ok = await asyncio.to_thread(
                _send_sync,
                self.gmail_user,
                self.app_password,
                self.to_address,
                subject,
                html,
                plain,
            )
            if ok:
                logger.info(f"[EmailNotifier] Sent '{title}' to {self.to_address}")
            return ok
        except Exception as exc:
            logger.error(f"[EmailNotifier] Unexpected error: {exc}")
            return False
