"""Administrative runner health and single-operator takeover lock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RunnerControl:
    state: str = "OFFLINE"
    last_heartbeat: datetime | None = None
    _lock_owner: str | None = None

    def health(self) -> dict[str, str | None]:
        return {
            "state": self.state,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
        }

    def lock(self, session_id: str) -> bool:
        if self._lock_owner not in (None, session_id):
            return False
        self._lock_owner = session_id
        return True

    def release(self, session_id: str) -> bool:
        if self._lock_owner != session_id:
            return False
        self._lock_owner = None
        return True

    def lock_state(self, session_id: str) -> dict[str, bool]:
        return {"locked": self._lock_owner == session_id}
