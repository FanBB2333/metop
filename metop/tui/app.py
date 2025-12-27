"""
Terminal User Interface for metop using Rich.

This provides a real-time dashboard showing GPU, ANE, and system metrics
with colorful progress bars and auto-refreshing display.
"""

import time
import os
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn, TaskID
from rich.style import Style
from rich import box

from ..collectors import GPUCollector, ANECollector, SystemCollector, MemoryCollector
from ..models import GPUSample, ANESample, SystemInfo, MemorySample


def format_bytes(bytes_val: int) -> str:
    """Format bytes to human-readable string."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 ** 2:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 ** 3:
        return f"{bytes_val / (1024 ** 2):.1f} MB"
    else:
        return f"{bytes_val / (1024 ** 3):.2f} GB"


def format_power(power_mw: float) -> str:
    """Format power in milliwatts to appropriate unit."""
    if power_mw < 1000:
        return f"{power_mw:.0f} mW"
    else:
        return f"{power_mw / 1000:.2f} W"


def get_utilization_color(value: float) -> str:
    """Get color based on utilization percentage."""
    if value < 25:
        return "green"
    elif value < 50:
        return "yellow"
    elif value < 75:
        return "orange1"
    else:
        return "red"


def create_bar(value: float, width: int = 30, label: str = "") -> Text:
    """Create a colored progress bar."""
    filled = int(value / 100 * width)
    empty = width - filled
    
    color = get_utilization_color(value)
    
    bar = Text()
    if label:
        bar.append(f"{label:12} ")
    bar.append("[")
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="dim")
    bar.append(f"] {value:5.1f}%")
    
    return bar


class MetopApp:
    """
    Main TUI application for metop.
    
    Displays real-time GPU, ANE, CPU, and memory metrics
    using Rich for colorful terminal output.
    """
    
    def __init__(
        self,
        interval_ms: int = 1000,
        show_ane: bool = True,
        color_scheme: int = 0
    ):
        """
        Initialize the TUI app.
        
        Args:
            interval_ms: Refresh interval in milliseconds
            show_ane: Whether to show ANE metrics (requires sudo)
            color_scheme: Color scheme index (0-8)
        """
        self.interval_ms = interval_ms
        self.show_ane = show_ane and ANECollector.check_sudo()
        self.color_scheme = color_scheme
        
        self.console = Console()
        
        # Initialize collectors
        self.gpu_collector = GPUCollector()
        self.system_collector = SystemCollector()
        self.memory_collector = MemoryCollector()
        
        if self.show_ane:
            self.ane_collector = ANECollector(interval_ms=interval_ms)
        else:
            self.ane_collector = None
        
        # Cached data
        self.system_info: Optional[SystemInfo] = None
        self.last_gpu: Optional[GPUSample] = None
        self.last_ane: Optional[ANESample] = None
        self.last_memory: Optional[MemorySample] = None
        
        # History for averaging/graphing
        self.gpu_history: list[float] = []
        self.ane_history: list[float] = []
        self.max_history = 60
    
    def _collect_samples(self) -> None:
        """Collect all samples from collectors."""
        self.last_gpu = self.gpu_collector.sample()
        self.last_memory = self.memory_collector.sample()
        
        if self.ane_collector:
            self.last_ane = self.ane_collector.sample()
        
        # Update history
        if self.last_gpu:
            self.gpu_history.append(self.last_gpu.device_utilization)
            if len(self.gpu_history) > self.max_history:
                self.gpu_history.pop(0)
        
        if self.last_ane:
            self.ane_history.append(self.last_ane.estimated_utilization)
            if len(self.ane_history) > self.max_history:
                self.ane_history.pop(0)
    
    def _create_header(self) -> Panel:
        """Create header panel with system info."""
        if self.system_info is None:
            self.system_info = self.system_collector.collect()
        
        info = self.system_info
        mem_total = format_bytes(info.memory_total_bytes)
        
        header_text = Text()
        header_text.append(f"{info.chip_name}", style="bold cyan")
        header_text.append(f"  |  CPU: {info.cpu_cores} cores ({info.cpu_p_cores}P + {info.cpu_e_cores}E)", style="dim")
        header_text.append(f"  |  GPU: {info.gpu_cores} cores", style="dim")
        header_text.append(f"  |  ANE: {info.ane_cores} cores", style="dim")
        header_text.append(f"  |  Memory: {mem_total}", style="dim")
        
        return Panel(header_text, title="metop", border_style="blue", box=box.ROUNDED)
    
    def _create_gpu_panel(self) -> Panel:
        """Create GPU metrics panel."""
        content = Text()
        
        if self.last_gpu:
            gpu = self.last_gpu
            
            # Utilization bars
            content.append_text(create_bar(gpu.device_utilization, label="Device"))
            content.append("\n")
            content.append_text(create_bar(gpu.renderer_utilization, label="Renderer"))
            content.append("\n")
            content.append_text(create_bar(gpu.tiler_utilization, label="Tiler"))
            content.append("\n\n")
            
            # Memory stats
            content.append("Memory: ", style="bold")
            content.append(f"{format_bytes(gpu.memory_used_bytes)} / {format_bytes(gpu.memory_allocated_bytes)} allocated")
            
            # Additional stats
            if gpu.tiled_scene_bytes > 0:
                content.append(f"\nTiled Scene: {format_bytes(gpu.tiled_scene_bytes)}")
            if gpu.recovery_count > 0:
                content.append(f"\nRecoveries: {gpu.recovery_count}", style="yellow")
        else:
            content.append("No GPU data available", style="dim")
        
        return Panel(content, title="GPU (Metal)", border_style="green", box=box.ROUNDED)
    
    def _create_ane_panel(self) -> Panel:
        """Create ANE metrics panel."""
        content = Text()
        
        if not self.show_ane:
            content.append("Run with ", style="dim")
            content.append("sudo", style="bold yellow")
            content.append(" to enable ANE monitoring", style="dim")
        elif self.last_ane:
            ane = self.last_ane
            
            # Utilization bar (estimated from power)
            content.append_text(create_bar(ane.estimated_utilization, label="Utilization"))
            content.append("\n\n")
            
            # Power consumption
            content.append("Power: ", style="bold")
            content.append(format_power(ane.power_mw))
            
            if ane.energy_mj > 0:
                content.append(f"  (Energy: {ane.energy_mj:.1f} mJ/sample)")
        else:
            content.append("Waiting for ANE data...", style="dim")
        
        return Panel(content, title="ANE (Neural Engine)", border_style="magenta", box=box.ROUNDED)
    
    def _create_memory_panel(self) -> Panel:
        """Create system memory panel."""
        content = Text()
        
        if self.last_memory:
            mem = self.last_memory
            
            # Memory usage bar
            content.append_text(create_bar(mem.usage_percent, label="RAM"))
            content.append("\n\n")
            
            # Memory details
            content.append("Used: ", style="bold")
            content.append(f"{format_bytes(mem.used_bytes)}")
            content.append("  Available: ", style="bold")
            content.append(f"{format_bytes(mem.available_bytes)}")
            
            # Swap if used
            if mem.swap_used_bytes > 0:
                swap_pct = (mem.swap_used_bytes / mem.swap_total_bytes * 100) if mem.swap_total_bytes > 0 else 0
                content.append(f"\n\nSwap: {format_bytes(mem.swap_used_bytes)} / {format_bytes(mem.swap_total_bytes)} ({swap_pct:.1f}%)")
        else:
            content.append("No memory data available", style="dim")
        
        return Panel(content, title="System Memory", border_style="yellow", box=box.ROUNDED)
    
    def _create_sparkline(self, history: list[float], width: int = 40) -> str:
        """Create a sparkline from history data."""
        if not history:
            return "─" * width
        
        blocks = "▁▂▃▄▅▆▇█"
        
        # Sample or repeat to fill width
        if len(history) < width:
            # Repeat last value to fill
            sampled = history + [history[-1]] * (width - len(history))
        else:
            # Sample evenly
            step = len(history) / width
            sampled = [history[int(i * step)] for i in range(width)]
        
        # Map values to block characters
        result = ""
        for val in sampled:
            idx = int(val / 100 * (len(blocks) - 1))
            idx = max(0, min(len(blocks) - 1, idx))
            result += blocks[idx]
        
        return result
    
    def _create_history_panel(self) -> Panel:
        """Create sparkline history panel."""
        content = Text()
        
        content.append("GPU: ", style="green")
        content.append(self._create_sparkline(self.gpu_history))
        
        if self.show_ane:
            content.append("\nANE: ", style="magenta")
            content.append(self._create_sparkline(self.ane_history))
        
        return Panel(content, title="History (last 60s)", border_style="dim", box=box.ROUNDED)
    
    def _create_layout(self) -> Layout:
        """Create the main layout."""
        layout = Layout()
        
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=4)
        )
        
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right")
        )
        
        layout["left"].split_column(
            Layout(name="gpu"),
            Layout(name="memory")
        )
        
        layout["right"].split_column(
            Layout(name="ane"),
            Layout(name="extra")
        )
        
        return layout
    
    def _render(self) -> Layout:
        """Render the current state."""
        self._collect_samples()
        
        layout = self._create_layout()
        
        layout["header"].update(self._create_header())
        layout["gpu"].update(self._create_gpu_panel())
        layout["memory"].update(self._create_memory_panel())
        layout["ane"].update(self._create_ane_panel())
        layout["extra"].update(self._create_history_panel())
        
        # Footer with help
        footer_text = Text()
        footer_text.append("Press ", style="dim")
        footer_text.append("Ctrl+C", style="bold")
        footer_text.append(" to exit  |  ", style="dim")
        footer_text.append(f"Refresh: {self.interval_ms}ms", style="dim")
        if not self.show_ane:
            footer_text.append("  |  ", style="dim")
            footer_text.append("ANE disabled (needs sudo)", style="yellow")
        
        layout["footer"].update(Panel(footer_text, box=box.ROUNDED))
        
        return layout
    
    def run(self) -> None:
        """Run the TUI application."""
        try:
            with Live(self._render(), console=self.console, refresh_per_second=1000/self.interval_ms) as live:
                while True:
                    live.update(self._render())
                    time.sleep(self.interval_ms / 1000)
        
        except KeyboardInterrupt:
            self.console.print("\n[dim]Goodbye![/dim]")
        
        finally:
            # Cleanup
            if self.ane_collector:
                self.ane_collector.stop_streaming()
