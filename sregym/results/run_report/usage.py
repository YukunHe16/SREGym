"""Save the labeller's token usage next to the reports.

Only the usage the provider reports is counted. Totals from before tracking started, and prices, are unknown.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

COUNTERS = ("requests", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "cache_hits")


class UsageLedger:
    """Token usage of the current build and of earlier builds in the same output folder."""

    def __init__(self, path: Path, labellers) -> None:
        """Load the earlier builds from ``path`` and start a new build."""
        self.path, self.labellers, self.lock = path, labellers, threading.Lock()
        self.id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.started_clock = time.monotonic()
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        self.previous = previous
        self.builds = list(previous.get("builds", [])) if previous.get("usage_schema") == 2 else []
        # A snapshot in the older file format may overlap the builds, so it is kept as it is and never added to
        # the totals. An existing answer cache means some usage was never recorded.
        self.legacy = previous.get("legacy_snapshot") if previous.get("usage_schema") == 2 else previous or None
        self.history_missing = (
            previous.get("historical_usage_missing", False)
            if previous.get("usage_schema") == 2
            else bool(self.legacy) or any(path.parent.glob("label_cache*.jsonl"))
        )
        self.since = (
            previous.get("tracked_since", self.started_at) if previous.get("usage_schema") == 2 else self.started_at
        )
        self.status = "running"
        for one in labellers.distinct():
            one.progress = self.save
        self.save()

    def save(self, status: str | None = None) -> None:
        """Write the usage file. Called after each request and when the build ends."""
        with self.lock:
            if status:
                self.status = status
            each = [one.stats() for one in self.labellers.distinct()]
            this = {
                "id": self.id,
                "started_at": self.started_at,
                "updated_at": datetime.now(UTC).isoformat(),
                "status": self.status,
                "by_labeller": each,
            }
            builds = self.builds + [this]
            totals = {}
            for build in builds:
                for entry in build["by_labeller"]:
                    total = totals.setdefault(entry["labeller"], {k: 0 for k in COUNTERS})
                    for k in COUNTERS:
                        value = entry.get(k)
                        if value is None and (entry.get("requests") or 0) > 0:
                            total[k] = None
                        elif total[k] is not None:
                            total[k] += value or 0
            result = {
                **(each[0] if each else {}),
                "seconds": round(time.monotonic() - self.started_clock),
                "usage_schema": 2,
                "tracked_since": self.since,
                "historical_usage_missing": self.history_missing,
                "scope": "observed provider usage since tracked_since; interrupted in-flight requests may be billed without returned usage",
                "legacy_snapshot": self.legacy,
                "builds": builds,
                "current_build": this,
                "by_labeller": each,
                "all_builds": {"scope": "tracked builds only, not historical total cost", "by_labeller": totals},
                "estimated_cost_usd": None,
                "price_source": None,
            }
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, indent=2) + "\n")
            temporary.replace(self.path)
