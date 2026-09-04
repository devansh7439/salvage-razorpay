"""Merchant-facing agent controls.

Razorpay's own published position on agents in payments names four autonomy
limits a merchant must always hold, in Agent Studio: Principles, Guardrails,
and Merchant Control (razorpay.com/blog):

    Review-first mode   - agents prepare work but hold for merchant approval
    Escalation defaults - sensitive actions escalate rather than execute
    Immediate kill switch - merchants can disable any agent instantly
    Irreversibility blocks - large actions are never auto-approved

`MerchantPolicy` in `economics` already covers escalation and irreversibility:
they are per-decision limits, evaluated inside the policy engine. The other two
are different in kind - they are *operational* controls that sit outside any
one decision and must be changeable while the system is running, without a
deploy and without editing a config file.

That distinction is the reason this module exists separately. A kill switch
that requires a restart is not a kill switch.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AgentMode(str, Enum):
    """How much authority the agent currently holds."""

    REVIEW_FIRST = "review_first"
    """Decide and record, but execute nothing. Every action waits for a human.

    This is the posture a merchant runs on day one: the full decision trail is
    produced and auditable, so they can see exactly what the agent *would* have
    done before granting it the authority to do it.
    """

    AUTONOMOUS = "autonomous"
    """Execute approved decisions within the merchant's guardrails."""


@dataclass
class AgentControls:
    """Live operational controls. Mutable at runtime, by design.

    Attributes:
        enabled: The kill switch. When False the agent takes no action of any
            kind - no links, no messages, no retries. Ingestion and diagnosis
            continue, so the merchant can still see what is arriving.
        mode: Review-first or autonomous.
        disabled_reason: Free text recorded when the switch is thrown, so the
            audit trail explains a gap in activity rather than leaving one.
        changed_at: When the controls were last altered.
        changed_by: Who altered them.
    """

    enabled: bool = True
    mode: AgentMode = AgentMode.AUTONOMOUS
    disabled_reason: str | None = None
    changed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    changed_by: str = "system"

    @property
    def executes(self) -> bool:
        """Whether the agent may take any real-world action right now."""
        return self.enabled and self.mode is AgentMode.AUTONOMOUS

    @property
    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        return self.mode.value

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode.value,
            "status": self.status,
            "executes": self.executes,
            "disabled_reason": self.disabled_reason,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }


class ControlPlane:
    """Process-wide holder for the current controls.

    Guarded by a lock because the kill switch is most useful precisely when
    something is going wrong - mid-batch, under load, from a different request
    thread than the one doing the work. A control that is only safe to change
    when the system is idle is not much of a control.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._controls = AgentControls()

    def _snapshot(self) -> AgentControls:
        """Copy the current state. Caller must already hold the lock.

        Split out deliberately. The obvious implementation has `set` return
        `self.get()`, but `threading.Lock` is not reentrant, so acquiring it
        again from inside the same critical section deadlocks the calling
        thread permanently - and the first caller to hit it would be whoever
        just tried to throw the kill switch.
        """
        return AgentControls(
            enabled=self._controls.enabled,
            mode=self._controls.mode,
            disabled_reason=self._controls.disabled_reason,
            changed_at=self._controls.changed_at,
            changed_by=self._controls.changed_by,
        )

    def get(self) -> AgentControls:
        with self._lock:
            return self._snapshot()

    def set(
        self,
        *,
        enabled: bool | None = None,
        mode: AgentMode | None = None,
        reason: str | None = None,
        actor: str = "merchant",
    ) -> AgentControls:
        """Update the controls and stamp who changed them, and when."""
        with self._lock:
            if enabled is not None:
                self._controls.enabled = enabled
                self._controls.disabled_reason = (
                    None if enabled else (reason or "Disabled by merchant")
                )
            if mode is not None:
                self._controls.mode = mode
            self._controls.changed_at = datetime.now(timezone.utc).isoformat()
            self._controls.changed_by = actor
            return self._snapshot()

    def kill(self, reason: str = "Kill switch activated", actor: str = "merchant"):
        """Stop all agent action immediately."""
        return self.set(enabled=False, reason=reason, actor=actor)


#: The single process-wide control plane.
controls = ControlPlane()
