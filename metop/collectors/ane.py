"""
ANE (Apple Neural Engine) metrics collector using powermetrics.

This collector requires sudo privileges to access powermetrics data.
ANE utilization is estimated from power consumption since Apple
doesn't expose direct utilization metrics.
"""

import subprocess
import json
import time
import os
import signal
import threading
from typing import Optional, Dict, Any, Callable
from queue import Queue, Empty

from ..models import ANESample, CPUSample


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
        self._process: Optional[subprocess.Popen] = None
        self._streaming = False
        self._sample_queue: Queue = Queue()
        self._reader_thread: Optional[threading.Thread] = None
    
    @staticmethod
    def check_sudo() -> bool:
        """Check if running with sudo/root privileges."""
        return os.geteuid() == 0
    
    def _parse_powermetrics_json(self, data: Dict[str, Any]) -> tuple[Optional[ANESample], Optional[CPUSample]]:
        """Parse powermetrics JSON output."""
        ane_sample = None
        cpu_sample = None
        timestamp = time.time()
        
        # Parse processor metrics
        processor = data.get("processor", {})
        
        # ANE energy (in mJ per sampling interval)
        ane_energy = processor.get("ane_energy", 0)
        if ane_energy > 0:
            # Convert energy to power: P = E / t
            # interval_ms is the sampling period
            ane_power_mw = (ane_energy / self.interval_ms) * 1000
            estimated_util = min(100.0, (ane_power_mw / self.max_ane_power_mw) * 100)
            
            ane_sample = ANESample(
                power_mw=ane_power_mw,
                energy_mj=ane_energy,
                estimated_utilization=estimated_util,
                timestamp=timestamp
            )
        
        # CPU metrics
        clusters = processor.get("clusters", [])
        e_active = 0.0
        p_active = 0.0
        e_freq = 0
        p_freq = 0
        
        for cluster in clusters:
            name = cluster.get("name", "")
            active = (1 - cluster.get("idle_ratio", 1)) * 100
            freq = int(cluster.get("freq_hz", 0) / 1e6)
            
            if name.startswith("E"):
                e_active = max(e_active, active)
                e_freq = max(e_freq, freq)
            elif name.startswith("P"):
                p_active = max(p_active, active)
                p_freq = max(p_freq, freq)
        
        cpu_power = processor.get("cpu_energy", 0)
        if self.interval_ms > 0:
            cpu_power_mw = (cpu_power / self.interval_ms) * 1000
        else:
            cpu_power_mw = 0
        
        cpu_sample = CPUSample(
            e_cluster_active=e_active,
            p_cluster_active=p_active,
            e_cluster_freq_mhz=e_freq,
            p_cluster_freq_mhz=p_freq,
            cpu_power_mw=cpu_power_mw,
            timestamp=timestamp
        )
        
        return ane_sample, cpu_sample
    
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
                    "--samplers", "cpu_power",
                    "-f", "json"
                ],
                capture_output=True,
                text=True,
                timeout=self.interval_ms / 1000 + 5
            )
            
            if result.returncode != 0:
                return None
            
            data = json.loads(result.stdout)
            ane_sample, cpu_sample = self._parse_powermetrics_json(data)
            
            self._last_sample = ane_sample
            self._last_cpu_sample = cpu_sample
            
            return ane_sample
        
        except subprocess.TimeoutExpired:
            return None
        except json.JSONDecodeError:
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
                    "--samplers", "cpu_power",
                    "-f", "json"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            self._streaming = True
            
            def reader():
                buffer = ""
                brace_count = 0
                
                while self._streaming and self._process and self._process.poll() is None:
                    try:
                        char = self._process.stdout.read(1)
                        if not char:
                            break
                        
                        buffer += char
                        
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            
                            if brace_count == 0 and buffer.strip():
                                try:
                                    data = json.loads(buffer)
                                    ane_sample, cpu_sample = self._parse_powermetrics_json(data)
                                    
                                    self._last_sample = ane_sample
                                    self._last_cpu_sample = cpu_sample
                                    
                                    if callback and ane_sample:
                                        callback(ane_sample, cpu_sample)
                                    elif ane_sample:
                                        self._sample_queue.put((ane_sample, cpu_sample))
                                
                                except json.JSONDecodeError:
                                    pass
                                
                                buffer = ""
                    
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
    
    def __del__(self):
        """Cleanup on destruction."""
        self.stop_streaming()
