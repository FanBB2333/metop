"""
Disk metrics collector using psutil.

Collects root filesystem usage plus aggregate disk read/write throughput.
"""

import shutil
import time
from typing import Optional

from ..models import DiskSample


class DiskCollector:
    """
    Collects disk usage and throughput metrics.

    Usage is reported for the root filesystem. Throughput is derived from
    successive snapshots of psutil disk I/O counters.
    """

    def __init__(self, mount_point: str = "/"):
        self.mount_point = mount_point
        self._last_sample: Optional[DiskSample] = None
        self._last_io = None
        self._last_io_time: Optional[float] = None
        self._psutil_available = False
        self._init_psutil()

    def _init_psutil(self) -> None:
        """Check if psutil is available."""
        try:
            import psutil

            self._last_io = psutil.disk_io_counters()
            self._last_io_time = time.time()
            self._psutil_available = True
        except ImportError:
            self._psutil_available = False

    def sample(self) -> DiskSample:
        """Collect a disk sample."""
        sample = DiskSample(mount_point=self.mount_point, timestamp=time.time())

        try:
            if self._psutil_available:
                import psutil

                usage = psutil.disk_usage(self.mount_point)
                sample.total_bytes = int(usage.total)
                sample.used_bytes = int(usage.used)
                sample.free_bytes = int(usage.free)

                io = psutil.disk_io_counters()
                now = sample.timestamp
                if io is not None and self._last_io is not None and self._last_io_time is not None:
                    interval_s = max(0.0, now - self._last_io_time)
                    if interval_s > 0:
                        sample.read_bytes_per_sec = (
                            max(0, io.read_bytes - self._last_io.read_bytes) / interval_s
                        )
                        sample.write_bytes_per_sec = (
                            max(0, io.write_bytes - self._last_io.write_bytes) / interval_s
                        )

                self._last_io = io
                self._last_io_time = now
            else:
                usage = shutil.disk_usage(self.mount_point)
                sample.total_bytes = int(usage.total)
                sample.used_bytes = int(usage.used)
                sample.free_bytes = int(usage.free)

        except Exception:
            pass

        self._last_sample = sample
        return sample

    def get_last_sample(self) -> Optional[DiskSample]:
        """Return the last collected disk sample."""
        return self._last_sample
