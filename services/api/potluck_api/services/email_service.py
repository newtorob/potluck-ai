"""Transactional email via the Resend SDK.

Task: 6

Responsibilities:
- send_magic_link(to_email, magic_link_url) -> None
- send_welcome(to_email, display_name) -> None
- send_membership_confirmation(to_email, plan) -> None

Uses RESEND_API_KEY and FROM_EMAIL from settings. When no real key is
configured (local dev), email is not sent — the payload is logged instead so a
developer can copy the magic link out of the console.
"""
from __future__ import annotations

import asyncio

import structlog

from potluck_api.config import get_settings

logger = structlog.get_logger(__name__)


def _is_real_key(key: str) -> bool:
    """True only for a usable Resend key.

    The dev docker-compose sets ``RESEND_API_KEY=re_dev_placeholder`` (non-empty),
    so an emptiness check alone would try to send through Resend with a bogus
    key. Treat any placeholder value as "not configured".
    """
    key = key.strip()
    return bool(key) and "placeholder" not in key.lower()


class EmailService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return _is_real_key(self.settings.resend_api_key)

    async def send_magic_link(self, to_email: str, magic_link_url: str) -> None:
        subject = "Your Potluck sign-in link"
        html = (
            "<p>Click below to sign in to Potluck:</p>"
            f'<p><a href="{magic_link_url}">Sign in to Potluck</a></p>'
            f"<p>This link expires in {self.settings.magic_link_expiry_minutes} "
            "minutes. If you did not request it, you can ignore this email.</p>"
        )
        await self._send(to_email, subject, html, magic_link_url=magic_link_url)

    async def send_welcome(self, to_email: str, display_name: str | None) -> None:
        name = display_name or "there"
        subject = "Welcome to Potluck"
        html = f"<p>Hi {name}, welcome to Potluck.</p>"
        await self._send(to_email, subject, html)

    async def send_membership_confirmation(self, to_email: str, plan: str) -> None:
        subject = "Your Potluck membership is active"
        html = (
            f"<p>Your {plan} membership is now active. "
            "Thank you for joining Potluck.</p>"
        )
        await self._send(to_email, subject, html)

    async def _send(
        self,
        to_email: str,
        subject: str,
        html: str,
        *,
        magic_link_url: str | None = None,
    ) -> None:
        if not self.enabled:
            # Dev transport: log instead of sending so the link is copy-pasteable.
            logger.warning(
                "email_not_sent_dev_mode",
                to=to_email,
                subject=subject,
                magic_link_url=magic_link_url,
            )
            return

        import resend  # lazy import — the dep is only needed when sending

        resend.api_key = self.settings.resend_api_key

        def _do_send() -> None:
            resend.Emails.send(
                {
                    "from": self.settings.from_email,
                    "to": [to_email],
                    "subject": subject,
                    "html": html,
                }
            )

        # The Resend SDK is synchronous; run it off the event loop.
        await asyncio.to_thread(_do_send)
        logger.info("email_sent", to=to_email, subject=subject)
