"""
GPU metrics collector using IOKit via ioreg command.

This collector gathers GPU utilization and memory statistics from
Apple Silicon GPUs (AGXAccelerator) without requiring sudo privileges.
"""

import subprocess
import plistlib
import time
import re
from typing import Optional, Dict, Any

from ..models import GPUSample


class GPUCollector:
    """
    Collects GPU metrics from Apple Silicon using IOKit/ioreg.
    
    This uses the ioreg command to query the AGXAccelerator driver's
    PerformanceStatistics dictionary, which contains real-time GPU metrics.
    """
    
    def __init__(self):
        self._last_sample: Optional[GPUSample] = None
        self._gpu_entry_name: Optional[str] = None
        self._detect_gpu_entry()
    
    def _detect_gpu_entry(self) -> None:
        """Detect the GPU accelerator entry name."""
        try:
            # Find the AGX accelerator class
            result = subprocess.run(
                ["ioreg", "-r", "-c", "AGXAccelerator", "-a"],
                capture_output=True,
                text=False,
                timeout=5
            )
            if result.returncode == 0 and result.stdout:
                data = plistlib.loads(result.stdout)
                if data and len(data) > 0:
                    self._gpu_entry_name = data[0].get("IOClass", "AGXAccelerator")
        except Exception:
            self._gpu_entry_name = None
    
    def _parse_performance_stats(self, output: str) -> Optional[Dict[str, Any]]:
        """
        Parse PerformanceStatistics from ioreg text output.
        
        The ioreg output contains a line like:
        "PerformanceStatistics" = {"Device Utilization %"=100, ...}
        
        Note: There may be multiple PerformanceStatistics entries. We look for
        the one that contains "Device Utilization %" which is the AGX GPU stats.
        """
        # Find all PerformanceStatistics dictionaries
        # Use a pattern that captures the full nested dictionary
        pattern = r'"PerformanceStatistics"\s*=\s*\{([^}]+)\}'
        
        for match in re.finditer(pattern, output):
            stats_str = match.group(1)
            
            # Only parse if this contains GPU utilization data
            if "Device Utilization %" not in stats_str:
                continue
            
            stats: Dict[str, Any] = {}
            
            # Parse key-value pairs like "Key Name"=value or "Key Name"="string"
            # Pattern matches: "key"=value or "key"="string"
            kv_pattern = re.compile(r'"([^"]+)"\s*=\s*(-?\d+(?:\.\d+)?|"[^"]*")')
            
            for kv_match in kv_pattern.finditer(stats_str):
                key = kv_match.group(1)
                value_str = kv_match.group(2)
                
                # Parse value
                if value_str.startswith('"'):
                    stats[key] = value_str.strip('"')
                elif '.' in value_str:
                    stats[key] = float(value_str)
                else:
                    stats[key] = int(value_str)
            
            if stats:
                return stats
        
        return None

    
    def sample(self) -> GPUSample:
        """
        Collect a single GPU sample.
        
        Returns:
            GPUSample with current GPU metrics.
        """
        sample = GPUSample(timestamp=time.time())
        
        try:
            # Use ioreg to get GPU stats - this doesn't require sudo
            # Note: ioreg output may contain non-UTF-8 bytes, so we decode manually
            result = subprocess.run(
                ["ioreg", "-l"],
                capture_output=True,
                text=False,  # Get bytes, decode manually
                timeout=5
            )
            
            if result.returncode != 0:
                return sample
            
            # Decode with error handling for non-UTF-8 bytes
            output = result.stdout.decode('utf-8', errors='replace')
            
            # Find lines with PerformanceStatistics
            stats = self._parse_performance_stats(output)
            
            if stats:
                # Extract utilization percentages
                sample.device_utilization = float(stats.get("Device Utilization %", 0))
                sample.renderer_utilization = float(stats.get("Renderer Utilization %", 0))
                sample.tiler_utilization = float(stats.get("Tiler Utilization %", 0))
                
                # Extract memory stats
                sample.memory_used_bytes = int(stats.get("In use system memory", 0))
                sample.memory_allocated_bytes = int(stats.get("Alloc system memory", 0))
                
                # Additional stats
                sample.recovery_count = int(stats.get("recoveryCount", 0))
                sample.split_scene_count = int(stats.get("SplitSceneCount", 0))
                sample.tiled_scene_bytes = int(stats.get("TiledSceneBytes", 0))
        
        except subprocess.TimeoutExpired:
            pass  # Return empty sample on timeout
        except Exception as e:
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
