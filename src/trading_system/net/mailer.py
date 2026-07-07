"""SMTP email sender for ops alerts — stdlib only (smtplib), no SDK.

Defaults target Gmail SMTP (smtp.gmail.com:587, STARTTLS). Gmail needs an *App Password*
(Account → Security → 2-Step Verification → App passwords), not your normal password.
If SMTP user/password/recipient aren't configured, send() no-ops with a warning so a
half-configured deploy degrades quietly instead of crashing the notifier loop.
"""
import smtplib
from email.message import EmailMessage

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)


def send_email(body: str, *, subject: str = "Kite token — action needed",
               timeout: float = 20.0) -> bool:
    """Send a plain-text email. Returns True on success. Never raises — SMTP/network
    errors are logged and return False so a failed alert can't kill the caller's loop."""
    s = get_settings()
    if not (s.smtp_user and s.smtp_password and s.alert_email_to):
        logger.warning("email_not_configured")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.alert_email_from or s.smtp_user
    msg["To"] = s.alert_email_to
    msg.set_content(body)

    # Gmail shows App Passwords grouped as "xxxx xxxx xxxx xxxx" for readability, but
    # SMTP AUTH needs the bare 16 chars — strip whitespace so either paste-form works.
    password = "".join(s.smtp_password.split())
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=timeout) as server:
            server.starttls()
            server.login(s.smtp_user, password)
            server.send_message(msg)
    except Exception as exc:   # smtplib.SMTPException, OSError, ssl errors — all non-fatal
        logger.error("email_send_failed", error=str(exc))
        return False
    logger.info("email_sent", to=s.alert_email_to, subject=subject)
    return True
