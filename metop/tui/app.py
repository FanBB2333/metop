"""
Terminal User Interface for metop using Rich.

This provides a real-time dashboard showing GPU, ANE, CPU, memory, disk,
power, history, and top GPU processes with switchable display modes.
"""

import select
import sys
import termios
import time
import tty
from typing import Optional, Union

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..collectors import (
    ANECollector,
    CPUCollector,
    DiskCollector,
    GPUCollector,
    MemoryCollector,
    SystemCollector,
)
from ..models import (
    ANESample,
    CPUSample,
    DiskSample,
    GPUSample,
    MemorySample,
    PowerMetricsSample,
    SystemCPUSample,
    SystemInfo,
)


def format_bytes(bytes_val: Union[int, float]) -> str:
    """Format bytes to human-readable string."""
    if bytes_val < 1024:
        return f"{bytes_val:.0f} B"
    if bytes_val < 1024 ** 2:
        return f"{bytes_val / 1024:.1f} KB"
    if bytes_val < 1024 ** 3:
        return f"{bytes_val / (1024 ** 2):.1f} MB"
    return f"{bytes_val / (1024 ** 3):.2f} GB"


def format_rate(bytes_per_sec: float) -> str:
    """Format bytes per second."""
    return f"{format_bytes(bytes_per_sec)}/s"


def format_power(power_mw: float) -> str:
    """Format power in milliwatts to appropriate unit."""
    if power_mw < 1000:
        return f"{power_mw:.0f} mW"
    return f"{power_mw / 1000:.2f} W"


def format_duration_ms(duration_ms: float) -> str:
    """Format milliseconds into a compact duration string."""
    if duration_ms < 1000:
        return f"{duration_ms:.1f} ms"
    return f"{duration_ms / 1000:.2f} s"


def get_utilization_color(value: float) -> str:
    """Get color based on utilization percentage."""
    if value < 25:
        return "green"
    if value < 50:
        return "yellow"
    if value < 75:
        return "orange1"
    return "red"


def create_bar(value: float, width: int = 20, label: str = "") -> Text:
    """Create a colored progress bar."""
    filled = int(value / 100 * width)
    empty = width - filled

    color = get_utilization_color(value)

    bar = Text()
    if label:
        bar.append(f"{label:10} ")
    bar.append("[")
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="dim")
    bar.append(f"] {value:5.1f}%")

    return bar


class InputController:
    """Best-effort terminal input controller for runtime hotkeys."""

    def __init__(self):
        self.enabled = False
        self._fd: Optional[int] = None
        self._saved_attrs = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self

        try:
            self._fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.enabled = True
        except Exception:
            self.enabled = False

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.enabled or self._fd is None or self._saved_attrs is None:
            return

        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
        except Exception:
            pass

    def read_pending(self) -> list[str]:
        """Read all pending single-character inputs."""
        if not self.enabled:
            return []

        chars: list[str] = []
        try:
            while select.select([sys.stdin], [], [], 0)[0]:
                char = sys.stdin.read(1)
                if not char:
                    break
                chars.append(char)
        except Exception:
            return chars

        return chars


class MetopApp:
    """
    Main TUI application for metop.

    Displays real-time GPU, ANE, CPU, memory, disk, and process metrics using
    Rich for colorful terminal output.
    """

    DISPLAY_MODES = ("stacked", "classic")

    def __init__(
        self,
        interval_ms: int = 1000,
        show_ane: bool = True,
        color_scheme: int = 0,
        display_mode: str = "stacked",
    ):
        """
        Initialize the TUI app.

        Args:
            interval_ms: Refresh interval in milliseconds
            show_ane: Whether to show ANE metrics (requires sudo)
            color_scheme: Color scheme index (0-8)
            display_mode: Initial display mode
        """
        self.interval_ms = interval_ms
        self.show_ane = show_ane and ANECollector.check_sudo()
        self.color_scheme = color_scheme
        self.display_mode = display_mode if display_mode in self.DISPLAY_MODES else "stacked"

        self.console = Console()

        # Initialize collectors
        self.gpu_collector = GPUCollector()
        self.system_collector = SystemCollector()
        self.memory_collector = MemoryCollector()
        self.cpu_collector = CPUCollector()
        self.disk_collector = DiskCollector()

        if self.show_ane:
            self.ane_collector = ANECollector(interval_ms=interval_ms)
        else:
            self.ane_collector = None

        # Cached data
        self.system_info: Optional[SystemInfo] = None
        self.last_gpu: Optional[GPUSample] = None
        self.last_ane: Optional[ANESample] = None
        self.last_cpu: Optional[CPUSample] = None
        self.last_system_cpu: Optional[SystemCPUSample] = None
        self.last_memory: Optional[MemorySample] = None
        self.last_disk: Optional[DiskSample] = None
        self.last_power: Optional[PowerMetricsSample] = None

        # History for graphing
        self.gpu_history: list[float] = []
        self.ane_history: list[float] = []
        self.cpu_history: list[float] = []
        self.max_history = 60

        self._input = InputController()

    def _collect_samples(self) -> None:
        """Collect all samples from collectors."""
        self.last_gpu = self.gpu_collector.sample()
        self.last_memory = self.memory_collector.sample()
        self.last_system_cpu = self.cpu_collector.sample()
        self.last_disk = self.disk_collector.sample()

        if self.ane_collector:
            self.last_ane = self.ane_collector.sample()
            self.last_cpu = self.ane_collector.get_last_cpu_sample()
            self.last_power = self.ane_collector.get_last_power_sample()
        else:
            self.last_ane = None
            self.last_cpu = None
            self.last_power = None

        if self.last_gpu:
            self.gpu_history.append(self.last_gpu.device_utilization)
            if len(self.gpu_history) > self.max_history:
                self.gpu_history.pop(0)

        if self.last_ane:
            self.ane_history.append(self.last_ane.estimated_utilization)
            if len(self.ane_history) > self.max_history:
                self.ane_history.pop(0)

        if self.last_system_cpu:
            self.cpu_history.append(self.last_system_cpu.overall_percent)
            if len(self.cpu_history) > self.max_history:
                self.cpu_history.pop(0)

    def _create_header(self) -> Panel:
        """Create header panel with system info."""
        if self.system_info is None:
            self.system_info = self.system_collector.collect()
            if self.ane_collector:
                self.ane_collector.max_ane_power_mw = self.system_info.ane_max_power_mw

        info = self.system_info
        mem_total = format_bytes(info.memory_total_bytes)

        header_text = Text()
        header_text.append(f"{info.chip_name}", style="bold cyan")
        header_text.append(
            f"  |  CPU: {info.cpu_cores} cores ({info.cpu_p_cores}P + {info.cpu_e_cores}E)",
            style="dim",
        )
        header_text.append(f"  |  GPU: {info.gpu_cores} cores", style="dim")
        header_text.append(f"  |  ANE: {info.ane_cores} cores", style="dim")
        header_text.append(f"  |  Memory: {mem_total}", style="dim")

        return Panel(header_text, title="metop", border_style="blue", box=box.ROUNDED)

    def _create_gpu_panel(self) -> Panel:
        """Create GPU utilization panel."""
        content = Text()

        if self.last_gpu:
            gpu = self.last_gpu
            content.append_text(create_bar(gpu.device_utilization, width=18, label="Device"))
            content.append("\n")
            content.append_text(create_bar(gpu.renderer_utilization, width=18, label="Renderer"))
            content.append("\n")
            content.append_text(create_bar(gpu.tiler_utilization, width=18, label="Tiler"))
            content.append("\n\n")
            content.append("Memory: ", style="bold")
            content.append(
                f"{format_bytes(gpu.memory_used_bytes)} / "
                f"{format_bytes(gpu.memory_allocated_bytes)} allocated"
            )
            if gpu.tiled_scene_bytes > 0:
                content.append(f"\nScene: {format_bytes(gpu.tiled_scene_bytes)}", style="dim")
            if gpu.recovery_count > 0:
                content.append(f"\nRecoveries: {gpu.recovery_count}", style="yellow")
        else:
            content.append("No GPU data available", style="dim")

        return Panel(content, title="GPU Usage", border_style="green", box=box.ROUNDED)

    def _create_ane_panel(self) -> Panel:
        """Create ANE utilization panel."""
        content = Text()

        if not self.show_ane:
            content.append("Run with ", style="dim")
            content.append("sudo", style="bold yellow")
            content.append(" to enable ANE + power metrics", style="dim")
        elif self.last_ane:
            ane = self.last_ane
            content.append_text(create_bar(ane.estimated_utilization, width=18, label="ANE"))
            if self.last_power and self.last_power.ane_freq_mhz > 0:
                content.append("\n\nFreq: ", style="bold")
                content.append(f"{self.last_power.ane_freq_mhz:.0f} MHz")
                if self.last_power.ane_active_residency > 0:
                    content.append(
                        f"  |  Active {self.last_power.ane_active_residency:.1f}%",
                        style="dim",
                    )
            if ane.energy_mj > 0:
                content.append(f"\nEnergy: {ane.energy_mj:.1f} mJ/sample", style="dim")
        else:
            content.append("Waiting for ANE data...", style="dim")

        return Panel(content, title="ANE Usage", border_style="magenta", box=box.ROUNDED)

    def _create_power_panel(self) -> Panel:
        """Create component power panel."""
        content = Text()

        if self.last_power:
            power = self.last_power
            rows = [
                ("CPU", power.cpu_power_mw, "cyan"),
                ("GPU", power.gpu_power_mw, "green"),
            ]
            if self.show_ane:
                rows.append(("ANE", power.ane_power_mw, "magenta"))
            if power.combined_power_mw > 0:
                rows.append(("Total", power.combined_power_mw, "bold"))

            for index, (label, value, style) in enumerate(rows):
                if index:
                    content.append("\n")
                content.append(f"{label:5}", style=style)
                content.append(" ")
                content.append(format_power(value), style=style)

            if power.gpu_freq_mhz > 0:
                content.append("\n\nGPU Freq: ", style="bold")
                content.append(f"{power.gpu_freq_mhz:.0f} MHz")
                if power.gpu_active_residency > 0:
                    content.append(f"  |  Active {power.gpu_active_residency:.1f}%", style="dim")

            if self.show_ane and power.ane_freq_mhz > 0:
                content.append("\nANE Freq: ", style="bold")
                content.append(f"{power.ane_freq_mhz:.0f} MHz")
                if power.ane_active_residency > 0:
                    content.append(f"  |  Active {power.ane_active_residency:.1f}%", style="dim")
        elif self.show_ane:
            content.append("Waiting for powermetrics power data...", style="dim")
        else:
            content.append("Run with sudo to enable component power metrics", style="dim")

        return Panel(content, title="Power", border_style="cyan", box=box.ROUNDED)

    def _create_cpu_panel(self) -> Panel:
        """Create CPU panel."""
        content = Text()

        if self.last_system_cpu:
            cpu = self.last_system_cpu
            content.append_text(create_bar(cpu.overall_percent, width=16, label="CPU"))
            content.append("\n\n")
            content.append(f"User {cpu.user_percent:.1f}%", style="cyan")
            content.append("  |  ", style="dim")
            content.append(f"System {cpu.system_percent:.1f}%", style="yellow")
            content.append("\nLoad: ", style="bold")
            content.append(
                f"{cpu.load_avg_1m:.2f} / {cpu.load_avg_5m:.2f} / {cpu.load_avg_15m:.2f}"
            )

            if self.last_cpu:
                content.append("\n\n")
                content.append_text(create_bar(self.last_cpu.e_cluster_active, width=12, label="E Cluster"))
                content.append("\n")
                content.append_text(create_bar(self.last_cpu.p_cluster_active, width=12, label="P Cluster"))
                if self.last_cpu.e_cluster_freq_mhz > 0 or self.last_cpu.p_cluster_freq_mhz > 0:
                    content.append("\n\n")
                    content.append(
                        f"E {self.last_cpu.e_cluster_freq_mhz} MHz", style="green"
                    )
                    content.append("  |  ", style="dim")
                    content.append(
                        f"P {self.last_cpu.p_cluster_freq_mhz} MHz", style="orange1"
                    )
        else:
            content.append("No CPU data available", style="dim")

        return Panel(content, title="CPU", border_style="cyan", box=box.ROUNDED)

    def _create_memory_panel(self) -> Panel:
        """Create system memory panel."""
        content = Text()

        if self.last_memory:
            mem = self.last_memory
            effective_used = mem.total_bytes - mem.available_bytes
            content.append_text(create_bar(mem.usage_percent, width=16, label="RAM"))
            content.append("\n\n")
            content.append(f"{format_bytes(effective_used)}", style="bold")
            content.append(f" / {format_bytes(mem.total_bytes)}")
            content.append("\nAvailable: ", style="bold")
            content.append(f"{format_bytes(mem.available_bytes)}", style="green")
            if mem.swap_total_bytes > 0:
                swap_pct = (mem.swap_used_bytes / mem.swap_total_bytes) * 100
                content.append("\nSwap: ", style="bold")
                content.append(
                    f"{format_bytes(mem.swap_used_bytes)} / {format_bytes(mem.swap_total_bytes)}"
                )
                content.append(f" ({swap_pct:.1f}%)", style="dim")
        else:
            content.append("No memory data available", style="dim")

        return Panel(content, title="Memory", border_style="yellow", box=box.ROUNDED)

    def _create_disk_panel(self) -> Panel:
        """Create disk panel."""
        content = Text()

        if self.last_disk:
            disk = self.last_disk
            content.append_text(create_bar(disk.usage_percent, width=16, label="Disk"))
            content.append("\n\n")
            content.append(f"{format_bytes(disk.used_bytes)}", style="bold")
            content.append(f" / {format_bytes(disk.total_bytes)}")
            content.append("\nFree: ", style="bold")
            content.append(f"{format_bytes(disk.free_bytes)}", style="green")
            content.append("\nRead: ", style="bold")
            content.append(format_rate(disk.read_bytes_per_sec))
            content.append("\nWrite: ", style="bold")
            content.append(format_rate(disk.write_bytes_per_sec))
        else:
            content.append("No disk data available", style="dim")

        return Panel(content, title="Disk", border_style="blue", box=box.ROUNDED)

    def _create_sparkline(self, history: list[float], width: int = 28) -> str:
        """Create a sparkline from history data."""
        if not history:
            return "─" * width

        blocks = "▁▂▃▄▅▆▇█"

        if len(history) < width:
            sampled = history + [history[-1]] * (width - len(history))
        else:
            step = len(history) / width
            sampled = [history[int(i * step)] for i in range(width)]

        result = ""
        for val in sampled:
            idx = int(val / 100 * (len(blocks) - 1))
            idx = max(0, min(len(blocks) - 1, idx))
            result += blocks[idx]

        return result

    def _create_history_panel(self) -> Panel:
        """Create history panel."""
        content = Text()
        content.append("GPU: ", style="green")
        content.append(self._create_sparkline(self.gpu_history))
        content.append("\nCPU: ", style="cyan")
        content.append(self._create_sparkline(self.cpu_history))
        if self.show_ane:
            content.append("\nANE: ", style="magenta")
            content.append(self._create_sparkline(self.ane_history))

        return Panel(content, title="History", border_style="dim", box=box.ROUNDED)

    def _create_process_table(self, limit: int = 8) -> Union[Table, Text]:
        """Create a top-process view from the latest GPU sample."""
        if len(self.gpu_history) < 2:
            return Text("Collecting per-process GPU deltas...", style="dim")

        if not self.last_gpu or not self.last_gpu.processes:
            return Text(
                f"No GPU-active processes in the last {self.interval_ms} ms",
                style="dim",
            )

        table = Table(box=None, expand=True, pad_edge=False, show_header=True)
        table.add_column("Process", overflow="ellipsis")
        table.add_column("API", width=10, no_wrap=True)
        table.add_column("Queues", justify="right", width=6, no_wrap=True)
        table.add_column("GPU %", justify="right", width=8, no_wrap=True)
        table.add_column("GPU Time", justify="right", width=10, no_wrap=True)
        table.add_column("PID", justify="right", width=7, no_wrap=True)

        for process in self.last_gpu.processes[:limit]:
            percent = Text(
                f"{process.gpu_percent:5.1f}%",
                style=get_utilization_color(min(process.gpu_percent, 100.0)),
            )
            table.add_row(
                process.name,
                process.api or "-",
                str(process.command_queue_count),
                percent,
                format_duration_ms(process.gpu_time_ms),
                str(process.pid),
            )

        return table

    def _create_process_panel(self) -> Panel:
        """Create top GPU processes panel."""
        return Panel(
            self._create_process_table(limit=10),
            title="Top GPU Processes",
            border_style="white",
            box=box.ROUNDED,
        )

    def _create_stacked_layout(self) -> Layout:
        """Create the default stacked layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="content"),
            Layout(name="footer", size=4),
        )

        layout["content"].split_column(
            Layout(name="top", ratio=3),
            Layout(name="middle", ratio=3),
            Layout(name="bottom", ratio=2),
        )

        layout["top"].split_row(
            Layout(name="gpu", ratio=3),
            Layout(name="ane", ratio=3),
            Layout(name="power", ratio=2),
        )

        layout["middle"].split_row(
            Layout(name="cpu", ratio=2),
            Layout(name="memory", ratio=2),
            Layout(name="disk", ratio=2),
            Layout(name="history", ratio=2),
        )

        return layout

    def _create_classic_layout(self) -> Layout:
        """Create the classic dense layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=4),
        )

        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )

        layout["left"].split_column(
            Layout(name="gpu"),
            Layout(name="cpu"),
            Layout(name="memory"),
        )

        layout["right"].split_column(
            Layout(name="ane"),
            Layout(name="power"),
            Layout(name="disk"),
            Layout(name="history"),
            Layout(name="processes"),
        )

        return layout

    def _apply_common_updates(self, layout: Layout) -> None:
        """Render shared panels into a prepared layout."""
        layout["header"].update(self._create_header())
        layout["gpu"].update(self._create_gpu_panel())
        layout["ane"].update(self._create_ane_panel())
        layout["power"].update(self._create_power_panel())
        layout["cpu"].update(self._create_cpu_panel())
        layout["memory"].update(self._create_memory_panel())
        layout["disk"].update(self._create_disk_panel())
        layout["history"].update(self._create_history_panel())

    def _mode_label(self) -> str:
        """Get the current display mode label."""
        return "Stacked" if self.display_mode == "stacked" else "Classic"

    def _toggle_display_mode(self) -> None:
        """Cycle to the next display mode."""
        current_index = self.DISPLAY_MODES.index(self.display_mode)
        next_index = (current_index + 1) % len(self.DISPLAY_MODES)
        self.display_mode = self.DISPLAY_MODES[next_index]

    def _handle_input(self) -> None:
        """Handle pending keyboard input."""
        for char in self._input.read_pending():
            lowered = char.lower()
            if lowered == "m":
                self._toggle_display_mode()
            elif char == "1":
                self.display_mode = "stacked"
            elif char == "2":
                self.display_mode = "classic"

    def _render(self) -> Layout:
        """Render the current state."""
        self._collect_samples()

        if self.display_mode == "stacked":
            layout = self._create_stacked_layout()
            self._apply_common_updates(layout)
            layout["bottom"].update(self._create_process_panel())
        else:
            layout = self._create_classic_layout()
            self._apply_common_updates(layout)
            layout["processes"].update(self._create_process_panel())

        footer_text = Text()
        footer_text.append("Ctrl+C", style="bold")
        footer_text.append(" exit  |  ", style="dim")
        footer_text.append("m", style="bold")
        footer_text.append(" switch layout  |  ", style="dim")
        footer_text.append("1", style="bold")
        footer_text.append(" stacked  ", style="dim")
        footer_text.append("2", style="bold")
        footer_text.append(" classic  |  ", style="dim")
        footer_text.append(f"Layout: {self._mode_label()}", style="cyan")
        footer_text.append(f"  |  Refresh: {self.interval_ms}ms", style="dim")
        if not self._input.enabled:
            footer_text.append("  |  layout hotkeys unavailable", style="yellow")

        layout["footer"].update(Panel(footer_text, box=box.ROUNDED))

        return layout

    def run(self) -> None:
        """Run the TUI application."""
        refresh_per_second = max(1.0, 1000 / self.interval_ms)

        try:
            with self._input:
                with Live(self._render(), console=self.console, refresh_per_second=refresh_per_second) as live:
                    while True:
                        self._handle_input()
                        live.update(self._render())
                        time.sleep(self.interval_ms / 1000)

        except KeyboardInterrupt:
            self.console.print("\n[dim]Goodbye![/dim]")

        finally:
            if self.ane_collector:
                self.ane_collector.stop_streaming()
