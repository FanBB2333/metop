"""
Disk metrics collector using APFS-aware capacity reporting on macOS.

Capacity prefers the APFS container when available so the values are closer
to what macOS Settings reports. Throughput still comes from psutil I/O
counters when available.
"""

import plistlib
import shutil
import subprocess
import sys
import time
from typing import Optional

from ..models import DiskSample


class DiskCollector:
    """
    Collect disk usage and throughput metrics.

    On macOS, the root mount is often a read-only APFS system snapshot, so
    `disk_usage("/")` matches `df` but not the storage view shown in System
    Settings. This collector prefers APFS container totals when available.
    """

    def __init__(self, mount_point: str = "/", usage_refresh_interval_s: float = 5.0):
        self.mount_point = mount_point
        self.usage_refresh_interval_s = usage_refresh_interval_s
        self._last_sample: Optional[DiskSample] = None
        self._last_io = None
        self._last_io_time: Optional[float] = None
        self._last_usage_refresh: Optional[float] = None
        self._psutil_available = False
        self._cached_usage = DiskSample(mount_point=mount_point)
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

    def _sample_usage_with_diskutil(self) -> Optional[DiskSample]:
        """Read APFS container capacity from diskutil when available."""
        if sys.platform != "darwin":
            return None

        try:
            result = subprocess.run(
                ["diskutil", "info", "-plist", self.mount_point],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0 or not result.stdout:
                return None

            info = plistlib.loads(result.stdout)
            container_total = int(info.get("APFSContainerSize") or 0)
            container_free = int(info.get("APFSContainerFree") or 0)
            if container_total > 0:
                return DiskSample(
                    mount_point=self.mount_point,
                    usage_source="APFS",
                    total_bytes=container_total,
                    used_bytes=max(0, container_total - container_free),
                    free_bytes=max(0, container_free),
                )
        except Exception:
            return None

        return None

    def _sample_usage_with_filesystem(self) -> DiskSample:
        """Read filesystem capacity using psutil or shutil."""
        sample = DiskSample(mount_point=self.mount_point)

        if self._psutil_available:
            import psutil

            usage = psutil.disk_usage(self.mount_point)
        else:
            usage = shutil.disk_usage(self.mount_point)

        sample.total_bytes = int(usage.total)
        sample.used_bytes = int(usage.used)
        sample.free_bytes = int(usage.free)
        return sample

    def _refresh_usage(self, now: float) -> DiskSample:
        """Refresh capacity metrics on a slower cadence than throughput."""
        if (
            self._last_usage_refresh is not None
            and now - self._last_usage_refresh < self.usage_refresh_interval_s
            and self._cached_usage.total_bytes > 0
        ):
            cached = self._cached_usage
            return DiskSample(
                mount_point=cached.mount_point,
                usage_source=cached.usage_source,
                total_bytes=cached.total_bytes,
                used_bytes=cached.used_bytes,
                free_bytes=cached.free_bytes,
                timestamp=now,
            )

        usage_sample = self._sample_usage_with_diskutil()
        if usage_sample is None:
            try:
                usage_sample = self._sample_usage_with_filesystem()
            except Exception:
                usage_sample = DiskSample(mount_point=self.mount_point)

        usage_sample.timestamp = now
        self._cached_usage = usage_sample
        self._last_usage_refresh = now
        return usage_sample

    def _apply_throughput(self, sample: DiskSample) -> None:
        """Populate disk throughput from psutil counters if available."""
        if not self._psutil_available:
            return

        try:
            import psutil

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
        except Exception:
            pass

    def sample(self) -> DiskSample:
        """Collect a disk sample."""
        now = time.time()
        sample = self._refresh_usage(now)
        self._apply_throughput(sample)
        self._last_sample = sample
        return sample

    def get_last_sample(self) -> Optional[DiskSample]:
        """Return the last collected disk sample."""
        return self._last_sample
