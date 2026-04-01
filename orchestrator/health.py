"""Health Monitor — checks agent health and auto-restarts dead agents.

Runs as a background thread, checking every 10 seconds:
1. Is the process still alive?
2. Is it registered with the chat server?
3. Is it responding to triggers?

Auto-restarts agents that fail, with exponential backoff.
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, pool, check_interval: int = 10):
        self.pool = pool
        self.check_interval = check_interval
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Health monitor started (interval: %ds)", self.check_interval)

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self.pool.restart_dead()
            except Exception as e:
                logger.error("Health check error: %s", e)
            time.sleep(self.check_interval)
