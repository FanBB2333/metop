"""
Data models for metop metrics.
"""

from dataclasses import dataclass, field
from typing import Optional, List
import time


@dataclass
class GPUSample:
    """GPU utilization and memory metrics from a single sample."""
    
    device_utilization: float = 0.0  # 0-100%
    renderer_utilization: float = 0.0  # 0-100%
    tiler_utilization: float = 0.0  # 0-100%
    memory_used_bytes: int = 0
    memory_allocated_bytes: int = 0
    timestamp: float = field(default_factory=time.time)
    
    # Additional metrics if available
    recovery_count: int = 0
    split_scene_count: int = 0
    tiled_scene_bytes: int = 0


@dataclass
class ANESample:
    """ANE (Apple Neural Engine) metrics from a single sample."""
    
    power_mw: float = 0.0  # Power consumption in milliwatts
    energy_mj: float = 0.0  # Energy in millijoules (from powermetrics)
    estimated_utilization: float = 0.0  # 0-100% (estimated from power)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CPUSample:
    """CPU utilization metrics."""
    
    e_cluster_active: float = 0.0  # E-cluster utilization 0-100%
    p_cluster_active: float = 0.0  # P-cluster utilization 0-100%
    e_cluster_freq_mhz: int = 0
    p_cluster_freq_mhz: int = 0
    cpu_power_mw: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class PowerMetricsSample:
    """Power/frequency metrics from powermetrics (requires sudo)."""

    cpu_power_mw: float = 0.0
    gpu_power_mw: float = 0.0
    ane_power_mw: float = 0.0
    combined_power_mw: float = 0.0

    gpu_freq_mhz: float = 0.0
    gpu_active_residency: float = 0.0  # 0-100%
    gpu_idle_residency: float = 0.0  # 0-100%

    ane_freq_mhz: float = 0.0
    ane_active_residency: float = 0.0  # 0-100%
    ane_idle_residency: float = 0.0  # 0-100%

    timestamp: float = field(default_factory=time.time)


@dataclass
class MemorySample:
    """System memory metrics."""
    
    total_bytes: int = 0
    used_bytes: int = 0
    available_bytes: int = 0
    swap_total_bytes: int = 0
    swap_used_bytes: int = 0
    timestamp: float = field(default_factory=time.time)
    
    @property
    def usage_percent(self) -> float:
        """Calculate memory usage percentage.
        
        On macOS, this uses (total - available) / total to match
        Activity Monitor's memory pressure calculation.
        """
        if self.total_bytes == 0:
            return 0.0
        # Use (total - available) for accurate usage on macOS
        # This accounts for compressed memory and cached files
        return ((self.total_bytes - self.available_bytes) / self.total_bytes) * 100


@dataclass
class ProcessGPUUsage:
    """Per-process GPU usage information."""
    
    pid: int
    name: str
    gpu_time_ms: float = 0.0  # GPU time in milliseconds


@dataclass
class SystemInfo:
    """Static system information."""
    
    chip_name: str = "Unknown"
    cpu_cores: int = 0
    cpu_e_cores: int = 0
    cpu_p_cores: int = 0
    gpu_cores: int = 0
    memory_total_bytes: int = 0
    ane_cores: int = 16  # Default for M1/M2/M3 series
    
    # Max power estimates (for utilization calculation)
    ane_max_power_mw: float = 8000.0  # ~8W typical max for ANE


@dataclass
class CombinedSample:
    """Combined sample from all collectors."""
    
    gpu: Optional[GPUSample] = None
    ane: Optional[ANESample] = None
    cpu: Optional[CPUSample] = None
    power: Optional[PowerMetricsSample] = None
    memory: Optional[MemorySample] = None
    processes: List[ProcessGPUUsage] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
