"""
Memory metrics collector using psutil.

Collects system memory and swap usage statistics.
"""

import time
from typing import Optional

from ..models import MemorySample


class MemoryCollector:
    """
    Collects system memory usage metrics.
    
    Uses psutil for cross-platform memory statistics.
    Falls back to vm_stat if psutil is not available.
    """
    
    def __init__(self):
        self._last_sample: Optional[MemorySample] = None
        self._psutil_available = False
        self._init_psutil()
    
    def _init_psutil(self) -> None:
        """Check if psutil is available."""
        try:
            import psutil
            self._psutil_available = True
        except ImportError:
            self._psutil_available = False
    
    def _sample_with_psutil(self) -> MemorySample:
        """Collect memory sample using psutil."""
        import psutil
        
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        return MemorySample(
            total_bytes=mem.total,
            used_bytes=mem.used,
            available_bytes=mem.available,
            swap_total_bytes=swap.total,
            swap_used_bytes=swap.used,
            timestamp=time.time()
        )
    
    def _sample_with_sysctl(self) -> MemorySample:
        """Collect memory sample using sysctl (fallback)."""
        import subprocess
        
        sample = MemorySample(timestamp=time.time())
        
        try:
            # Get total memory
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                sample.total_bytes = int(result.stdout.strip())
            
            # Get memory pressure and usage from vm_stat
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse vm_stat output
                import re
                
                page_size = 4096  # Default page size
                
                # Look for page size
                ps_match = re.search(r"page size of (\d+) bytes", result.stdout)
                if ps_match:
                    page_size = int(ps_match.group(1))
                
                # Parse page counts
                pages = {}
                for line in result.stdout.split('\n'):
                    match = re.match(r"(.+?):\s+(\d+)", line)
                    if match:
                        key = match.group(1).strip().lower()
                        pages[key] = int(match.group(2))
                
                # Calculate used and available memory
                wired = pages.get("pages wired down", 0) * page_size
                active = pages.get("pages active", 0) * page_size
                inactive = pages.get("pages inactive", 0) * page_size
                speculative = pages.get("pages speculative", 0) * page_size
                free = pages.get("pages free", 0) * page_size
                
                sample.used_bytes = wired + active
                sample.available_bytes = free + inactive + speculative
        
        except Exception:
            pass
        
        return sample
    
    def sample(self) -> MemorySample:
        """
        Collect a memory usage sample.
        
        Returns:
            MemorySample with current memory statistics.
        """
        if self._psutil_available:
            sample = self._sample_with_psutil()
        else:
            sample = self._sample_with_sysctl()
        
        self._last_sample = sample
        return sample
    
    def get_last_sample(self) -> Optional[MemorySample]:
        """Return the last collected sample."""
        return self._last_sample
