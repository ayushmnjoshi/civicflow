"""Event service — emits application lifecycle events to the n8n webhook.

Design rules
------------
- Does NOTHING unless both N8N_WEBHOOK_URL and N8N_WEBHOOK_TOKEN are set.
- Fires in a daemon thread so the caller never waits.
- Swallows every exception (logs id + error class only).
- Never raises.
- Must be called AFTER the database commit.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)


def emit_application_event(
    event_type: str,
    application_id: int,
    new_status: str,
    citizen_id: int,
) -> None:
    """Emit an application lifecycle event to the n8n webhook.

    Fires in a background daemon thread.  Returns immediately.
    Does nothing when N8N_WEBHOOK_URL or N8N_WEBHOOK_TOKEN is unset.
    """
    url = settings.n8n_webhook_url
    token = settings.n8n_webhook_token

    if not url or not token:
        return

    payload = {
        "event_type": event_type,
        "application_id": application_id,
        "new_status": new_status,
        "citizen_id": citizen_id,
    }

    def _post() -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2):
                pass
            logger.info(
                "event_emitted application_id=%d event_type=%s",
                application_id,
                event_type,
            )
        except Exception as exc:
            logger.warning(
                "event_emit_failed application_id=%d error_class=%s",
                application_id,
                type(exc).__name__,
            )

    t = threading.Thread(target=_post, daemon=True)
    t.start()
