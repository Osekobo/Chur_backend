"""Password reset / notification delivery.

If SMTP is configured the reset link is emailed. In development the link is
additionally returned in the API response and written to the log so the flow
can be exercised without a mail server.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def build_reset_url(token: str) -> str:
    return f"{settings.FRONTEND_URL}/reset-password?token={token}"


def send_password_reset_email(recipient: str, full_name: str, token: str) -> str | None:
    """Send the reset email. Returns the reset URL when it should be surfaced."""
    reset_url = build_reset_url(token)

    if not settings.SMTP_HOST:
        logger.info("SMTP is not configured. Password reset link for %s: %s", recipient, reset_url)
        return reset_url if settings.RETURN_RESET_LINK_IN_RESPONSE else None

    message = EmailMessage()
    message["Subject"] = "Reset your Church Finance System password"
    message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = recipient
    message.set_content(
        f"Hello {full_name or recipient},\n\n"
        "Use the link below to choose a new password for the Church Finance System. "
        f"The link expires in {settings.PASSWORD_RESET_EXPIRE_MINUTES} minutes.\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can safely ignore this email.\n"
    )

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(message)
        logger.info("Password reset email sent to %s", recipient)
    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", recipient, exc)

    return reset_url if settings.RETURN_RESET_LINK_IN_RESPONSE else None
