"""
GPU metrics collector using IOKit via ioreg command.

This collector gathers GPU utilization and memory statistics from
Apple Silicon GPUs (AGXAccelerator) without requiring sudo privileges.
It also derives per-process GPU activity from AGX user clients by
tracking deltas of accumulated GPU time between samples.
"""

import plistlib
import re
import subprocess
import time
from typing import Optional, Dict, Any

from ..models import GPUSample, ProcessGPUUsage


class GPUCollector:
    """
    Collects GPU metrics from Apple Silicon using IOKit/ioreg.
    
    This uses the ioreg command to query the AGXAccelerator driver's
    PerformanceStatistics dictionary, which contains real-time GPU metrics.
    """

    PID_PATTERN = re.compile(r"^pid\s+(\d+),\s*(.*)$")
    
    def __init__(self):
        self._last_sample: Optional[GPUSample] = None
        self._last_process_totals: Dict[int, int] = {}
        self._last_process_sample_time: Optional[float] = None

    @staticmethod
    def _safe_float(value: Any) -> float:
        """Best-effort float conversion."""
        if isinstance(value, (int, float)):
            return float(value)
        return 0.0

    @staticmethod
    def _safe_int(value: Any) -> int:
        """Best-effort integer conversion."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return 0

    def _select_gpu_root(self, roots: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Select the AGX root that contains the best top-level GPU statistics."""
        best_root: Optional[Dict[str, Any]] = None
        best_allocated = -1

        for root in roots:
            stats = root.get("PerformanceStatistics")
            if not isinstance(stats, dict):
                continue
            if "Device Utilization %" not in stats:
                continue

            allocated = self._safe_int(stats.get("Alloc system memory"))
            if allocated > best_allocated:
                best_allocated = allocated
                best_root = root

        return best_root

    def _iter_registry_entries(self, entry: Dict[str, Any]):
        """Yield registry entries recursively."""
        yield entry

        children = entry.get("IORegistryEntryChildren")
        if not isinstance(children, list):
            return

        for child in children:
            if isinstance(child, dict):
                yield from self._iter_registry_entries(child)

    def _resolve_process_names(self, fallbacks: Dict[int, str]) -> Dict[int, str]:
        """Resolve fuller process names when psutil can improve truncated registry names."""
        if not fallbacks:
            return {}

        try:
            import psutil
        except ImportError:
            return {}

        resolved: Dict[int, str] = {}
        target_pids = set(fallbacks)

        try:
            for proc in psutil.process_iter(["pid", "name"]):
                info = proc.info
                pid = info.get("pid")
                if pid not in target_pids:
                    continue

                proc_name = info.get("name") or ""
                fallback = fallbacks[pid]
                if proc_name and proc_name.startswith(fallback):
                    resolved[pid] = proc_name
        except Exception:
            return resolved

        return resolved

    def _collect_processes(
        self, roots: list[Dict[str, Any]], timestamp: float
    ) -> list[ProcessGPUUsage]:
        """Aggregate per-process GPU activity from AGX user clients."""
        current_totals: Dict[int, Dict[str, Any]] = {}

        for root in roots:
            for entry in self._iter_registry_entries(root):
                creator = entry.get("IOUserClientCreator")
                if not isinstance(creator, str):
                    continue

                match = self.PID_PATTERN.match(creator)
                if match is None:
                    continue

                pid = int(match.group(1))
                fallback_name = match.group(2) or str(pid)
                app_usage = entry.get("AppUsage")
                queue_count = self._safe_int(entry.get("CommandQueueCount"))

                if not isinstance(app_usage, list):
                    app_usage = []

                total_ns = 0
                apis: set[str] = set()
                for usage in app_usage:
                    if not isinstance(usage, dict):
                        continue
                    total_ns += self._safe_int(usage.get("accumulatedGPUTime"))
                    api = usage.get("API")
                    if isinstance(api, str) and api:
                        apis.add(api)

                if total_ns <= 0 and queue_count <= 0:
                    continue

                aggregate = current_totals.setdefault(
                    pid,
                    {
                        "name": fallback_name,
                        "total_ns": 0,
                        "apis": set(),
                        "queue_count": 0,
                    },
                )
                aggregate["total_ns"] += total_ns
                aggregate["queue_count"] = max(aggregate["queue_count"], queue_count)
                aggregate["apis"].update(apis)

                # Keep the longer registry label when there are multiple user clients.
                if len(fallback_name) > len(aggregate["name"]):
                    aggregate["name"] = fallback_name

        interval_s = 0.0
        if self._last_process_sample_time is not None:
            interval_s = max(0.0, timestamp - self._last_process_sample_time)

        resolved_names = self._resolve_process_names(
            {pid: data["name"] for pid, data in current_totals.items()}
        )

        processes: list[ProcessGPUUsage] = []
        for pid, data in current_totals.items():
            previous_total = self._last_process_totals.get(pid)
            if previous_total is None or interval_s <= 0:
                continue

            delta_ns = data["total_ns"] - previous_total
            if delta_ns <= 0:
                continue

            gpu_time_ms = delta_ns / 1e6
            gpu_percent = (delta_ns / (interval_s * 1e9)) * 100.0
            processes.append(
                ProcessGPUUsage(
                    pid=pid,
                    name=resolved_names.get(pid, data["name"]),
                    gpu_time_ms=gpu_time_ms,
                    gpu_percent=gpu_percent,
                    api=", ".join(sorted(data["apis"])),
                    command_queue_count=data["queue_count"],
                )
            )

        self._last_process_totals = {
            pid: data["total_ns"] for pid, data in current_totals.items()
        }
        self._last_process_sample_time = timestamp
        processes.sort(key=lambda process: process.gpu_time_ms, reverse=True)

        return processes

    
    def sample(self) -> GPUSample:
        """
        Collect a single GPU sample.
        
        Returns:
            GPUSample with current GPU metrics.
        """
        sample = GPUSample(timestamp=time.time())
        
        try:
            # Use ioreg to get GPU stats (AGXAccelerator) - no sudo required.
            # The plist form exposes both top-level performance stats and
            # child AGX user clients for per-process accounting.
            result = subprocess.run(
                ["ioreg", "-a", "-r", "-c", "AGXAccelerator", "-l", "-w", "0"],
                capture_output=True,
                text=False,
                timeout=5
            )
            
            if result.returncode != 0:
                return sample
            
            plist = plistlib.loads(result.stdout)
            if not isinstance(plist, list):
                return sample

            roots = [entry for entry in plist if isinstance(entry, dict)]
            root = self._select_gpu_root(roots)

            if root:
                stats = root.get("PerformanceStatistics", {})

                # Extract utilization percentages
                sample.device_utilization = self._safe_float(stats.get("Device Utilization %"))
                sample.renderer_utilization = self._safe_float(stats.get("Renderer Utilization %"))
                sample.tiler_utilization = self._safe_float(stats.get("Tiler Utilization %"))
                
                # Extract memory stats
                sample.memory_used_bytes = self._safe_int(stats.get("In use system memory"))
                sample.memory_allocated_bytes = self._safe_int(stats.get("Alloc system memory"))
                
                # Additional stats
                sample.recovery_count = self._safe_int(stats.get("recoveryCount"))
                sample.split_scene_count = self._safe_int(stats.get("SplitSceneCount"))
                sample.tiled_scene_bytes = self._safe_int(stats.get("TiledSceneBytes"))

            sample.processes = self._collect_processes(roots, sample.timestamp)
        
        except subprocess.TimeoutExpired:
            pass  # Return empty sample on timeout
        except Exception:
            # Log error but don't crash
            pass
        
        self._last_sample = sample
        return sample
    
    def get_last_sample(self) -> Optional[GPUSample]:
        """Return the last collected sample."""
        return self._last_sample


class GPUCollectorFast:
    """
    Faster GPU collector using pyobjc for direct IOKit access.
    
    This is an optional alternative that reduces subprocess overhead
    by directly accessing IOKit through Python bindings.
    
    Requires: pyobjc-framework-Cocoa
    """
    
    def __init__(self):
        self._available = False
        self._service = None
        self._last_sample: Optional[GPUSample] = None
        self._init_iokit()
    
    def _init_iokit(self) -> None:
        """Initialize IOKit bindings if available."""
        try:
            import objc
            from Foundation import NSBundle
            
            # Load IOKit framework
            IOKit = NSBundle.bundleWithIdentifier_('com.apple.framework.IOKit')
            if IOKit is None:
                return
            
            # Load required functions
            functions = [
                ("IOServiceMatching", b"@*"),
                ("IOServiceGetMatchingService", b"II@"),
                ("IORegistryEntryCreateCFProperties", b"IIo^@II"),
                ("IOObjectRelease", b"II"),
            ]
            
            objc.loadBundleFunctions(IOKit, globals(), functions)
            self._available = True
            
        except ImportError:
            pass  # pyobjc not installed
        except Exception:
            pass  # Failed to load IOKit
    
    @property
    def available(self) -> bool:
        """Check if fast collector is available."""
        return self._available
    
    def sample(self) -> GPUSample:
        """
        Collect GPU sample using IOKit.
        
        Falls back to GPUCollector if IOKit is not available.
        """
        if not self._available:
            # Fall back to subprocess-based collector
            return GPUCollector().sample()
        
        sample = GPUSample(timestamp=time.time())
        
        try:
            # Get matching service for AGXAccelerator
            matching = IOServiceMatching("AGXAccelerator")
            service = IOServiceGetMatchingService(0, matching)
            
            if service:
                # Get properties
                props = None
                result = IORegistryEntryCreateCFProperties(service, props, None, 0)
                
                if result == 0 and props:
                    perf_stats = props.get("PerformanceStatistics", {})
                    
                    sample.device_utilization = float(perf_stats.get("Device Utilization %", 0))
                    sample.renderer_utilization = float(perf_stats.get("Renderer Utilization %", 0))
                    sample.tiler_utilization = float(perf_stats.get("Tiler Utilization %", 0))
                    sample.memory_used_bytes = int(perf_stats.get("In use system memory", 0))
                    sample.memory_allocated_bytes = int(perf_stats.get("Alloc system memory", 0))
                
                IOObjectRelease(service)
        
        except Exception:
            pass
        
        self._last_sample = sample
        return sample
    
    def get_last_sample(self) -> Optional[GPUSample]:
        """Return the last collected sample."""
        return self._last_sample
