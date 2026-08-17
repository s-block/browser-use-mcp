from __future__ import annotations

import logging

_LOGGER = logging.getLogger("browser_use_mcp.audit")


def audit_event(
    event: str,
    *,
    outcome: str,
    tenant_namespace: str | None = None,
    reason: str | None = None,
    persistent: bool | None = None,
) -> None:
    """Emit a bounded event without credentials, URLs, selectors, or page content."""
    fields = [f"event={event}", f"outcome={outcome}"]
    if tenant_namespace is not None:
        fields.append(f"tenant={tenant_namespace[:16]}")
    if reason is not None:
        fields.append(f"reason={reason}")
    if persistent is not None:
        fields.append(f"persistent={str(persistent).lower()}")
    _LOGGER.info("security_audit %s", " ".join(fields))
