"""
System CPU metrics collector using psutil.

Collects overall CPU utilization and load averages. This is separate from
powermetrics cluster samples, which are optionally shown when available.
"""

import os
import time
from typing import Optional

from ..models import SystemCPUSample


class CPUCollector:
    """
    Collects system-wide CPU activity metrics.

    Uses psutil for CPU percentages and os.getloadavg for load averages.
    """

    def __init__(self):
        self._last_sample: Optional[SystemCPUSample] = None
        self._psutil_available = False
        self._init_psutil()

    def _init_psutil(self) -> None:
        """Check if psutil is available and prime cpu_percent state."""
        try:
            import psutil

            psutil.cpu_percent(interval=None)
            self._psutil_available = True
        except ImportError:
            self._psutil_available = False

    def sample(self) -> SystemCPUSample:
        """Collect a CPU sample."""
        sample = SystemCPUSample(timestamp=time.time())

        try:
            if self._psutil_available:
                import psutil

                cpu_times = psutil.cpu_times_percent(interval=None)
                sample.overall_percent = float(psutil.cpu_percent(interval=None))
                sample.user_percent = float(getattr(cpu_times, "user", 0.0))
                sample.system_percent = float(getattr(cpu_times, "system", 0.0))
        except Exception:
            pass

        try:
            load_avg = os.getloadavg()
            sample.load_avg_1m = float(load_avg[0])
            sample.load_avg_5m = float(load_avg[1])
            sample.load_avg_15m = float(load_avg[2])
        except (AttributeError, OSError):
            pass

        self._last_sample = sample
        return sample

    def get_last_sample(self) -> Optional[SystemCPUSample]:
        """Return the last collected CPU sample."""
        return self._last_sample
