"""Email worker for processing the outbox queue via Resend API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update

from app.config import settings
from app.schemas.auth import AuthEmailOutbox
from app.utils.db_async import SessionLocal

logger = logging.getLogger(__name__)


async def _send_via_resend(
    *,
    to: str,
    subject: str,
    body: str,
) -> bool:
    """Send an email via the Resend API.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain text email body.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    if not settings.resend_api_key:
        logger.warning("Resend API key not configured, skipping email to %s", to)
        return False

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.email_from_address,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
                timeout=30.0,
            )
            if response.status_code == 200:
                logger.info("Email sent successfully to %s", to)
                return True
            else:
                logger.error(
                    "Failed to send email to %s: %s %s",
                    to,
                    response.status_code,
                    response.text,
                )
                return False
        except httpx.RequestError as exc:
            logger.error("HTTP error sending email to %s: %s", to, exc)
            return False


async def send_pending_emails(
    *,
    batch_size: int = 10,
) -> int:
    """Process pending emails from the outbox.

    Creates its own database session, safe to call from BackgroundTasks.

    Args:
        batch_size: Maximum number of emails to process in this batch.

    Returns:
        Number of emails successfully sent.
    """
    sent_count = 0
    now = datetime.now(UTC).replace(tzinfo=None)

    async with SessionLocal() as db:
        async with db.begin():
            # Fetch pending emails
            result = await db.execute(
                select(AuthEmailOutbox)
                .where(AuthEmailOutbox.sent_at.is_(None))  # type: ignore[union-attr]
                .order_by(AuthEmailOutbox.created_at)  # type: ignore[arg-type]
                .limit(batch_size)
            )
            emails = list(result.scalars().all())

        for email in emails:
            if email.id is None:
                continue

            success = await _send_via_resend(
                to=email.to_email,
                subject=email.subject,
                body=email.body,
            )

            if success:
                async with db.begin():
                    await db.execute(
                        update(AuthEmailOutbox)
                        .where(AuthEmailOutbox.id == email.id)  # type: ignore[arg-type]
                        .values(sent_at=now, provider="resend")
                    )
                sent_count += 1

    return sent_count
