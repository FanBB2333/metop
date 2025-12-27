"""
System information collector.

Gathers static system information like chip name, core counts,
and memory configuration using sysctl and system_profiler.
"""

import subprocess
import re
from typing import Optional

from ..models import SystemInfo


class SystemCollector:
    """
    Collects static system information about the Mac.
    
    This information doesn't change during runtime, so it's
    collected once and cached.
    """
    
    def __init__(self):
        self._info: Optional[SystemInfo] = None
    
    def _run_sysctl(self, key: str) -> Optional[str]:
        """Run sysctl and return the value for a key."""
        try:
            result = subprocess.run(
                ["sysctl", "-n", key],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    def _get_gpu_cores(self) -> int:
        """Get GPU core count from system_profiler."""
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                # Look for "Total Number of Cores: N"
                match = re.search(r"Total Number of Cores:\s*(\d+)", result.stdout)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        
        # Default estimates based on chip
        return 0
    
    def _get_ane_cores(self, chip_name: str) -> int:
        """Estimate ANE cores based on chip name."""
        # All M1/M2/M3/M4 chips have 16 ANE cores
        if any(m in chip_name for m in ["M1", "M2", "M3", "M4"]):
            return 16
        return 0
    
    def _get_max_ane_power(self, chip_name: str) -> float:
        """Estimate max ANE power based on chip."""
        # These are rough estimates in mW
        if "Ultra" in chip_name:
            return 16000.0  # Two dies
        elif "Max" in chip_name:
            return 12000.0
        elif "Pro" in chip_name:
            return 10000.0
        else:
            return 8000.0  # Base M1/M2/M3
    
    def collect(self) -> SystemInfo:
        """
        Collect system information.
        
        Results are cached after first collection.
        
        Returns:
            SystemInfo with hardware details.
        """
        if self._info is not None:
            return self._info
        
        info = SystemInfo()
        
        # CPU brand string (e.g., "Apple M1 Pro")
        brand = self._run_sysctl("machdep.cpu.brand_string")
        if brand:
            info.chip_name = brand
        
        # CPU core count
        cores = self._run_sysctl("machdep.cpu.core_count")
        if cores:
            info.cpu_cores = int(cores)
        
        # Memory size
        memsize = self._run_sysctl("hw.memsize")
        if memsize:
            info.memory_total_bytes = int(memsize)
        
        # GPU cores
        gpu_cores = self._get_gpu_cores()
        if gpu_cores > 0:
            info.gpu_cores = gpu_cores
        else:
            # Estimate from chip name
            if "Ultra" in info.chip_name:
                info.gpu_cores = 64 if "M1" in info.chip_name else 76
            elif "Max" in info.chip_name:
                info.gpu_cores = 32 if "M1" in info.chip_name else 38
            elif "Pro" in info.chip_name:
                info.gpu_cores = 16 if "M1" in info.chip_name else 19
            else:
                info.gpu_cores = 8 if "M1" in info.chip_name else 10
        
        # ANE cores
        info.ane_cores = self._get_ane_cores(info.chip_name)
        
        # Max ANE power for utilization calculation
        info.ane_max_power_mw = self._get_max_ane_power(info.chip_name)
        
        # E-core and P-core breakdown (estimate)
        if info.cpu_cores > 0:
            if "Ultra" in info.chip_name:
                info.cpu_e_cores = 4 if "M1" in info.chip_name else 4
                info.cpu_p_cores = info.cpu_cores - info.cpu_e_cores
            elif "Max" in info.chip_name:
                info.cpu_e_cores = 2
                info.cpu_p_cores = info.cpu_cores - info.cpu_e_cores
            elif "Pro" in info.chip_name:
                info.cpu_e_cores = 2
                info.cpu_p_cores = info.cpu_cores - info.cpu_e_cores
            else:
                info.cpu_e_cores = 4
                info.cpu_p_cores = info.cpu_cores - info.cpu_e_cores
        
        self._info = info
        return info
    
    def get_info(self) -> Optional[SystemInfo]:
        """Return cached system info, or collect if not available."""
        if self._info is None:
            return self.collect()
        return self._info
