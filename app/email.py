"""Sending the bot's emails through Brevo.

Both digests post the same shape of payload to the same endpoint, so the request
is built here once. The caller hands in the pooled session it already has, which
keeps Brevo and Groq on one connection pool instead of a TLS handshake per call.
"""

from __future__ import annotations

import os

import requests

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SEND_TIMEOUT_SECONDS = 30


def send_html_email(
    subject: str,
    html_body: str,
    session: requests.Session,
    recipient_email: str | None = None,
    sender_email: str | None = None,
    cc_emails: list[str] | None = None,
    log=None,
) -> bool:
    """Post one HTML email through Brevo.

    Returns whether it went out. Callers that queue work per delivery use this to
    decide if the work is done, so an outage keeps the items queued for the next
    attempt rather than marking them sent.
    """
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = sender_email or os.getenv("SENDER_EMAIL")
    recipient_email = recipient_email or os.getenv("RECIPIENT_EMAIL")
    cc_emails = cc_emails or []

    if not api_key or not sender_email or not recipient_email:
        log("Email not configured. Skipping notification.")
        return False

    payload: dict[str, object] = {
        "sender": {"email": sender_email},
        "to": [{"email": recipient_email}],
        "subject": subject,
        "htmlContent": html_body,
    }

    if cc_emails:
        payload["cc"] = [{"email": addr} for addr in cc_emails]

    try:
        response = session.post(
            BREVO_URL,
            headers={"accept": "application/json", "api-key": api_key},
            json=payload,
            timeout=SEND_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"Failed to send email: {exc}")
        return False

    cc_note = f" (cc: {', '.join(cc_emails)})" if cc_emails else ""
    log(f"Email sent successfully to {recipient_email}{cc_note}")
    return True
