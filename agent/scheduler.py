"""Scheduling: daily caps, buffer pacing with jitter, backoff, ramp."""

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from agent.db import AgentDB

logger = logging.getLogger("agent.scheduler")


class Scheduler:
    """Enforces all timing, cap, and safety rules."""

    def __init__(self, db: AgentDB, config: Dict[str, Any]):
        self.db = db
        self.cfg = config
        self.start_time = datetime.utcnow()

        self.daily_cap = config.get("daily_cap_initial", 60)
        self.ramp_levels = config.get("daily_cap_ramp_levels", [80, 100])
        self.ramp_after_days = config.get("ramp_after_stable_days", 3)

        self.buffer_sec = config.get("buffer_seconds", 300)
        self.jitter_range = config.get("buffer_jitter_seconds", [10, 45])

        self.max_consec_fail = config.get("max_consecutive_failures", 10)
        self.cooldown_min = config.get("cooldown_minutes_on_spike", 60)
        self.run_hours = config.get("run_duration_hours", 24)

        self._ramp_index = 0
        self._cooldown_until: Optional[datetime] = None
        self._reduced_cap = False

    def should_continue(self) -> bool:
        elapsed = (datetime.utcnow() - self.start_time).total_seconds()
        if elapsed >= self.run_hours * 3600:
            logger.info("Run duration reached (%dh). Stopping.", self.run_hours)
            return False
        return True

    def can_apply_now(self) -> bool:
        if self._cooldown_until and datetime.utcnow() < self._cooldown_until:
            rem = (self._cooldown_until - datetime.utcnow()).seconds
            logger.info("Cooldown – %ds remaining", rem)
            return False
        applied = self.db.get_daily_applied()
        cap = self._current_cap()
        if applied >= cap:
            logger.info("Daily cap reached: %d/%d", applied, cap)
            return False
        return True

    def wait_buffer(self):
        """Wait between applications with randomised jitter."""
        lo, hi = self.jitter_range
        jitter = random.randint(lo, hi)
        total = self.buffer_sec + jitter
        logger.info(
            "⏱  Buffer: %ds (base %d + jitter %d). Next at %s",
            total, self.buffer_sec, jitter,
            (datetime.utcnow() + timedelta(seconds=total)).strftime("%H:%M:%S"),
        )
        # Sleep in smaller chunks so Ctrl+C is responsive
        end = time.time() + total
        while time.time() < end:
            remaining = end - time.time()
            time.sleep(min(remaining, 10))

    def report_success(self):
        self._check_ramp()

    def report_failure(self):
        consec = self.db.recent_consecutive_failures()
        if consec >= self.max_consec_fail:
            self._activate_cooldown()

    def report_block_signal(self):
        logger.warning("Block signal – reducing cap + cooldown")
        self._activate_cooldown()
        self._reduced_cap = True

    def _current_cap(self) -> int:
        if self._reduced_cap:
            return max(20, self.daily_cap // 2)
        return self.daily_cap

    def _check_ramp(self):
        if self._ramp_index >= len(self.ramp_levels):
            return
        # Ramp progressively: level 0 after N days, level 1 after 2N days, etc.
        run_days = self.db.consecutive_run_days()
        required_days = self.ramp_after_days * (self._ramp_index + 1)
        if run_days >= required_days:
            old_cap = self.daily_cap
            self.daily_cap = self.ramp_levels[self._ramp_index]
            self._ramp_index += 1
            self._reduced_cap = False
            logger.info("📈 Cap ramped %d → %d (run days: %d, needed: %d)",
                        old_cap, self.daily_cap, run_days, required_days)

    def _activate_cooldown(self):
        self._cooldown_until = (
            datetime.utcnow() + timedelta(minutes=self.cooldown_min)
        )
        logger.warning("🛑 Cooldown until %s",
                        self._cooldown_until.strftime("%H:%M:%S"))

    def time_remaining(self) -> float:
        elapsed = (datetime.utcnow() - self.start_time).total_seconds()
        return max(0, self.run_hours * 3600 - elapsed)

    def status_summary(self) -> str:
        applied = self.db.get_daily_applied()
        cap = self._current_cap()
        rem = self.time_remaining() / 3600
        q = self.db.queue_size()
        return (
            f"Applied: {applied}/{cap} | Queue: {q} | "
            f"Time: {rem:.1f}h | Cap: {cap}"
        )
