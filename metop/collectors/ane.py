"""
ANE (Apple Neural Engine) metrics collector using powermetrics.

This collector requires sudo privileges to access powermetrics data.
ANE utilization is estimated from power consumption since Apple
doesn't expose direct utilization metrics.
"""

import subprocess
import plistlib
import time
import os
import signal
import threading
from typing import Optional, Dict, Any, Callable
from queue import Queue, Empty

from ..models import ANESample, CPUSample, PowerMetricsSample


class ANECollector:
    """
    Collects ANE metrics from powermetrics.
    
    Requires sudo privileges. The collector can run in two modes:
    1. One-shot: Single sample per call (higher latency)
    2. Streaming: Continuous sampling with callback (lower latency)
    """
    
    # Default max ANE power in mW for utilization estimation
    DEFAULT_MAX_ANE_POWER = 8000.0  # ~8W typical max
    
    def __init__(self, interval_ms: int = 1000, max_ane_power_mw: float = DEFAULT_MAX_ANE_POWER):
        """
        Initialize ANE collector.
        
        Args:
            interval_ms: Sampling interval in milliseconds
            max_ane_power_mw: Maximum ANE power for utilization calculation
        """
        self.interval_ms = interval_ms
        self.max_ane_power_mw = max_ane_power_mw
        self._last_sample: Optional[ANESample] = None
        self._last_cpu_sample: Optional[CPUSample] = None
        self._last_power_sample: Optional[PowerMetricsSample] = None
        self._process: Optional[subprocess.Popen] = None
        self._streaming = False
        self._sample_queue: Queue = Queue()
        self._reader_thread: Optional[threading.Thread] = None
    
    @staticmethod
    def check_sudo() -> bool:
        """Check if running with sudo/root privileges."""
        return os.geteuid() == 0
    
    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Best-effort numeric conversion."""
        if isinstance(value, (int, float)):
            return float(value)
        return None
    
    def _parse_powermetrics_plist(
        self, data: Dict[str, Any]
    ) -> tuple[Optional[ANESample], Optional[CPUSample], Optional[PowerMetricsSample]]:
        """Parse a powermetrics plist sample."""
        timestamp = time.time()
        processor = data.get("processor", {})
        if not isinstance(processor, dict):
            processor = {}
        
        # CPU metrics (clusters)
        clusters = processor.get("clusters", [])
        e_active = 0.0
        p_active = 0.0
        e_freq = 0
        p_freq = 0
        
        if isinstance(clusters, list):
            for cluster in clusters:
                if not isinstance(cluster, dict):
                    continue
                name = str(cluster.get("name", ""))
                idle_ratio = self._safe_float(cluster.get("idle_ratio"))
                freq_hz = self._safe_float(cluster.get("freq_hz"))
                active = (1 - idle_ratio) * 100 if idle_ratio is not None else 0.0
                freq = int(freq_hz / 1e6) if freq_hz is not None else 0
                
                if name.startswith("E"):
                    e_active = max(e_active, active)
                    e_freq = max(e_freq, freq)
                elif name.startswith("P"):
                    p_active = max(p_active, active)
                    p_freq = max(p_freq, freq)
        
        # Power metrics (mW). Prefer explicit *_power if present, fall back to *_energy.
        cpu_energy = self._safe_float(processor.get("cpu_energy"))
        gpu_energy = self._safe_float(processor.get("gpu_energy"))
        ane_energy = self._safe_float(processor.get("ane_energy"))

        cpu_power_mw = self._safe_float(processor.get("cpu_power"))
        if cpu_power_mw is None and cpu_energy is not None and self.interval_ms > 0:
            cpu_power_mw = (cpu_energy / self.interval_ms) * 1000
        cpu_power_mw = cpu_power_mw or 0.0

        gpu_power_mw = self._safe_float(processor.get("gpu_power"))
        if gpu_power_mw is None and gpu_energy is not None and self.interval_ms > 0:
            gpu_power_mw = (gpu_energy / self.interval_ms) * 1000
        gpu_power_mw = gpu_power_mw or 0.0

        ane_power_mw = self._safe_float(processor.get("ane_power"))
        if ane_power_mw is None and ane_energy is not None and self.interval_ms > 0:
            ane_power_mw = (ane_energy / self.interval_ms) * 1000
        ane_power_mw = ane_power_mw or 0.0

        combined_power_mw = self._safe_float(processor.get("combined_power"))
        if combined_power_mw is None:
            combined_power_mw = cpu_power_mw + gpu_power_mw + ane_power_mw
        combined_power_mw = combined_power_mw or 0.0
        
        # GPU power/frequency/residency (if gpu_power sampler present)
        gpu_freq_mhz = 0.0
        gpu_idle_residency = 0.0
        gpu_active_residency = 0.0
        gpu_block = data.get("gpu")
        if isinstance(gpu_block, dict):
            freq_hz = self._safe_float(gpu_block.get("freq_hz"))
            freq_mhz = self._safe_float(gpu_block.get("freq"))
            if freq_hz is not None:
                gpu_freq_mhz = freq_hz / 1e6
            elif freq_mhz is not None:
                gpu_freq_mhz = freq_mhz
            
            idle_ratio = self._safe_float(gpu_block.get("idle_ratio"))
            if idle_ratio is not None:
                gpu_idle_residency = max(0.0, min(100.0, idle_ratio * 100))
                gpu_active_residency = 100.0 - gpu_idle_residency
        
        # ANE frequency/residency (if ane_power sampler present)
        ane_freq_mhz = 0.0
        ane_idle_residency = 0.0
        ane_active_residency = 0.0
        ane_block = data.get("ane")
        if isinstance(ane_block, dict):
            freq_hz = self._safe_float(ane_block.get("freq_hz"))
            if freq_hz is not None:
                ane_freq_mhz = freq_hz / 1e6
            idle_ratio = self._safe_float(ane_block.get("idle_ratio"))
            if idle_ratio is not None:
                ane_idle_residency = max(0.0, min(100.0, idle_ratio * 100))
                ane_active_residency = 100.0 - ane_idle_residency
        elif isinstance(ane_block, list):
            idle_ratios: list[float] = []
            freqs_hz: list[float] = []
            for item in ane_block:
                if not isinstance(item, dict):
                    continue
                idle_ratio = self._safe_float(item.get("idle_ratio"))
                if idle_ratio is not None:
                    idle_ratios.append(idle_ratio)
                freq_hz = self._safe_float(item.get("freq_hz"))
                if freq_hz is not None:
                    freqs_hz.append(freq_hz)
            if freqs_hz:
                ane_freq_mhz = max(freqs_hz) / 1e6
            if idle_ratios:
                idle_ratio_avg = sum(idle_ratios) / len(idle_ratios)
                ane_idle_residency = max(0.0, min(100.0, idle_ratio_avg * 100))
                ane_active_residency = 100.0 - ane_idle_residency
        
        cpu_sample = CPUSample(
            e_cluster_active=e_active,
            p_cluster_active=p_active,
            e_cluster_freq_mhz=e_freq,
            p_cluster_freq_mhz=p_freq,
            cpu_power_mw=cpu_power_mw,
            timestamp=timestamp
        )

        power_sample = PowerMetricsSample(
            cpu_power_mw=cpu_power_mw,
            gpu_power_mw=gpu_power_mw,
            ane_power_mw=ane_power_mw,
            combined_power_mw=combined_power_mw,
            gpu_freq_mhz=gpu_freq_mhz,
            gpu_active_residency=gpu_active_residency,
            gpu_idle_residency=gpu_idle_residency,
            ane_freq_mhz=ane_freq_mhz,
            ane_active_residency=ane_active_residency,
            ane_idle_residency=ane_idle_residency,
            timestamp=timestamp,
        )

        # ANE sample: prefer residency if available, fall back to power estimate.
        has_ane_metrics = (
            "ane_power" in processor
            or "ane_energy" in processor
            or isinstance(ane_block, (dict, list))
        )

        ane_sample: Optional[ANESample] = None
        if has_ane_metrics:
            if isinstance(ane_block, (dict, list)):
                estimated_util = ane_active_residency
            elif self.max_ane_power_mw > 0:
                estimated_util = min(100.0, (ane_power_mw / self.max_ane_power_mw) * 100)
            else:
                estimated_util = 0.0
            
            ane_sample = ANESample(
                power_mw=ane_power_mw,
                energy_mj=ane_energy or 0.0,
                estimated_utilization=estimated_util,
                timestamp=timestamp,
            )

        return ane_sample, cpu_sample, power_sample

    @staticmethod
    def _iter_plists(raw: bytes) -> list[Dict[str, Any]]:
        """Parse NUL-separated plist records from powermetrics output."""
        plists: list[Dict[str, Any]] = []
        for chunk in raw.split(b"\0"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                data = plistlib.loads(chunk)
            except Exception:
                continue
            if isinstance(data, dict):
                plists.append(data)
        return plists
    
    def sample(self) -> Optional[ANESample]:
        """
        Collect a single ANE sample.
        
        Requires sudo privileges. Returns None if not running as root.
        
        Returns:
            ANESample with current ANE metrics, or None if unavailable.
        """
        if not self.check_sudo():
            return None
        
        try:
            result = subprocess.run(
                [
                    "powermetrics",
                    "-i", str(self.interval_ms),
                    "-n", "1",
                    "-s", "cpu_power,gpu_power,ane_power",
                    "-f", "plist"
                ],
                capture_output=True,
                text=False,
                timeout=self.interval_ms / 1000 + 5
            )
            
            if result.returncode != 0:
                return None
            
            plists = self._iter_plists(result.stdout)
            if not plists:
                return None
            
            ane_sample = None
            cpu_sample = None
            power_sample = None
            for data in plists:
                ane_sample, cpu_sample, power_sample = self._parse_powermetrics_plist(data)
            
            self._last_sample = ane_sample
            self._last_cpu_sample = cpu_sample
            self._last_power_sample = power_sample
            
            return ane_sample
        
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None
    
    def start_streaming(self, callback: Optional[Callable[[ANESample, CPUSample], None]] = None) -> bool:
        """
        Start continuous powermetrics sampling.
        
        Args:
            callback: Optional callback function called with each sample.
                     If None, samples are queued and can be retrieved with get_sample().
        
        Returns:
            True if streaming started successfully.
        """
        if not self.check_sudo():
            return False
        
        if self._streaming:
            return True
        
        try:
            self._process = subprocess.Popen(
                [
                    "powermetrics",
                    "-i", str(self.interval_ms),
                    "-n", "-1",  # Infinite samples
                    "-b", "1",
                    "-s", "cpu_power,gpu_power,ane_power",
                    "-f", "plist"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False
            )
            
            self._streaming = True
            
            def reader():
                buffer = b""
                
                while self._streaming and self._process and self._process.poll() is None:
                    try:
                        chunk = self._process.stdout.read(4096)
                        if not chunk:
                            break
                        
                        buffer += chunk

                        while b"\0" in buffer:
                            record, buffer = buffer.split(b"\0", 1)
                            record = record.strip()
                            if not record:
                                continue

                            try:
                                data = plistlib.loads(record)
                            except Exception:
                                continue

                            if not isinstance(data, dict):
                                continue

                            ane_sample, cpu_sample, power_sample = self._parse_powermetrics_plist(data)

                            self._last_sample = ane_sample
                            self._last_cpu_sample = cpu_sample
                            self._last_power_sample = power_sample

                            if callback and ane_sample:
                                callback(ane_sample, cpu_sample)
                            elif ane_sample:
                                self._sample_queue.put((ane_sample, cpu_sample))
                    
                    except Exception:
                        break
            
            self._reader_thread = threading.Thread(target=reader, daemon=True)
            self._reader_thread.start()
            
            return True
        
        except Exception:
            self._streaming = False
            return False
    
    def stop_streaming(self) -> None:
        """Stop continuous sampling."""
        self._streaming = False
        
        if self._process:
            try:
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            
            self._process = None
        
        if self._reader_thread:
            self._reader_thread.join(timeout=1)
            self._reader_thread = None
    
    def get_sample(self, timeout: float = 0.1) -> Optional[tuple[ANESample, CPUSample]]:
        """
        Get a sample from the streaming queue.
        
        Args:
            timeout: How long to wait for a sample in seconds.
        
        Returns:
            Tuple of (ANESample, CPUSample), or None if no sample available.
        """
        try:
            return self._sample_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_last_sample(self) -> Optional[ANESample]:
        """Return the last collected ANE sample."""
        return self._last_sample
    
    def get_last_cpu_sample(self) -> Optional[CPUSample]:
        """Return the last collected CPU sample."""
        return self._last_cpu_sample

    def get_last_power_sample(self) -> Optional[PowerMetricsSample]:
        """Return the last collected power/frequency sample."""
        return self._last_power_sample
    
    def __del__(self):
        """Cleanup on destruction."""
        self.stop_streaming()
