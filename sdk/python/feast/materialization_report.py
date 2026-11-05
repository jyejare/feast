"""Optional, observed outcomes for synchronous CLI materialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import uuid4


@dataclass
class MaterializationReport:
    project: str
    incremental: bool
    end_date: datetime
    version: Optional[str] = None
    start_date: Optional[datetime] = None
    views: list[dict[str, Any]] = field(default_factory=list)
    on_event: Optional[Callable[[dict[str, Any]], None]] = None
    started: bool = False
    operation_id: str = field(default_factory=lambda: uuid4().hex)

    def emit(self, event: str, **fields: Any) -> None:
        if self.on_event is not None:
            self.on_event(
                {
                    "event": event,
                    "operation_id": self.operation_id,
                    "project": self.project,
                    **fields,
                }
            )

    def begin(self) -> None:
        self.started = True
        self.emit(
            "started", incremental=self.incremental, end_ts=self.end_date.isoformat()
        )

    def completed(self, name: str, start_date: datetime) -> None:
        item = {
            "name": name,
            "status": "completed",
            "start_ts": start_date.isoformat(),
            "end_ts": self.end_date.isoformat(),
        }
        self.views.append(item)
        self.emit("view_completed", **item)

    def result(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "project": self.project,
            "incremental": self.incremental,
            "requested_start_ts": self.start_date.isoformat()
            if self.start_date
            else None,
            "end_ts": self.end_date.isoformat(),
            "version": self.version,
            "completed_views": sorted(self.views, key=lambda item: item["name"]),
        }
