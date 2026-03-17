"""
Terminal User Interface for metop using Rich.

This provides a real-time dashboard showing GPU, ANE, CPU, memory, disk,
power, history, and top GPU processes with switchable display modes.
"""

import os
import queue
import re
import select
import sys
import termios
import threading
import time
import tty
from typing import Optional, Union

from rich import box
from rich.align import Align
from rich.console import Console, Group
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
    ProcessGPUUsage,
    SystemCPUSample,
    SystemInfo,
)


def format_bytes(bytes_val: float) -> str:
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


def create_bar(value: float, width: int = 24, label: str = "") -> Text:
    """Create a colored progress bar."""
    filled = int(value / 100 * width)
    empty = width - filled

    color = get_utilization_color(value)

    bar = Text()
    if label:
        bar.append(f"{label:7} ")
    bar.append("[")
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="dim")
    bar.append(f"] {value:5.1f}%")

    return bar


class InputController:
    """Best-effort terminal input controller for runtime hotkeys and scrolling."""

    _MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h"
    _MOUSE_DISABLE = "\x1b[?1000l\x1b[?1006l"
    _MOUSE_PATTERN = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def __init__(self):
        self.enabled = False
        self._fd: Optional[int] = None
        self._saved_attrs = None
        self._buffer = ""

    def __enter__(self):
        if not sys.stdin.isatty():
            return self

        try:
            self._fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            sys.stdout.write(self._MOUSE_ENABLE)
            sys.stdout.flush()
            self.enabled = True
        except Exception:
            self.enabled = False

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.enabled:
            try:
                sys.stdout.write(self._MOUSE_DISABLE)
                sys.stdout.flush()
            except Exception:
                pass

        if not self.enabled or self._fd is None or self._saved_attrs is None:
            return

        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
        except Exception:
            pass

    def read_events(self) -> list[str]:
        """Read and parse all pending terminal input events."""
        if not self.enabled or self._fd is None:
            return []

        try:
            while select.select([self._fd], [], [], 0)[0]:
                chunk = os.read(self._fd, 4096).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                self._buffer += chunk
        except Exception:
            return []

        events: list[str] = []
        while self._buffer:
            if self._buffer.startswith(("\x1b[A", "\x1bOA")):
                events.append("up")
                self._buffer = self._buffer[3:]
                continue

            if self._buffer.startswith(("\x1b[B", "\x1bOB")):
                events.append("down")
                self._buffer = self._buffer[3:]
                continue

            if self._buffer.startswith("\x1b[<"):
                match = self._MOUSE_PATTERN.match(self._buffer)
                if match is None:
                    if "M" not in self._buffer and "m" not in self._buffer:
                        break
                    self._buffer = self._buffer[1:]
                    continue

                button_code = int(match.group(1))
                x = int(match.group(2))
                y = int(match.group(3))
                action = match.group(4)
                if button_code == 64:
                    events.append("wheel_up")
                elif button_code == 65:
                    events.append("wheel_down")
                elif action == "M" and button_code in (0, 32):
                    events.append(f"left_click:{x}:{y}")

                self._buffer = self._buffer[len(match.group(0)) :]
                continue

            if self._buffer[0] == "\x1b":
                if len(self._buffer) == 1:
                    break
                self._buffer = self._buffer[1:]
                continue

            events.append(self._buffer[0])
            self._buffer = self._buffer[1:]

        return events


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
        self.interval_ms = interval_ms
        self.show_ane = show_ane and ANECollector.check_sudo()
        self.color_scheme = color_scheme
        self.display_mode = display_mode if display_mode in self.DISPLAY_MODES else "stacked"

        self.console = Console()

        self.gpu_collector = GPUCollector()
        self.system_collector = SystemCollector()
        self.memory_collector = MemoryCollector()
        self.cpu_collector = CPUCollector()
        self.disk_collector = DiskCollector()

        if self.show_ane:
            self.ane_collector = ANECollector(interval_ms=interval_ms)
        else:
            self.ane_collector = None

        self.system_info: Optional[SystemInfo] = None
        self.last_gpu: Optional[GPUSample] = None
        self.last_ane: Optional[ANESample] = None
        self.last_cpu: Optional[CPUSample] = None
        self.last_system_cpu: Optional[SystemCPUSample] = None
        self.last_memory: Optional[MemorySample] = None
        self.last_disk: Optional[DiskSample] = None
        self.last_power: Optional[PowerMetricsSample] = None

        self.gpu_history: list[float] = []
        self.ane_history: list[float] = []
        self.cpu_history: list[float] = []
        self.max_history = 60

        self.selected_process_pid: Optional[int] = None
        self.selected_process_index = 0
        self.process_scroll_offset = 0

        self._input = InputController()
        self._sample_queue: queue.Queue[dict] = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None

    def _collect_sample_batch(self) -> dict:
        """Collect a full sample batch in the background."""
        batch = {
            "gpu": self.gpu_collector.sample(),
            "memory": self.memory_collector.sample(),
            "system_cpu": self.cpu_collector.sample(),
            "disk": self.disk_collector.sample(),
            "ane": None,
            "cpu": None,
            "power": None,
        }

        if self.ane_collector:
            batch["ane"] = self.ane_collector.sample()
            batch["cpu"] = self.ane_collector.get_last_cpu_sample()
            batch["power"] = self.ane_collector.get_last_power_sample()

        return batch

    def _apply_sample_batch(self, batch: dict) -> None:
        """Apply a collected sample batch on the UI thread."""
        self.last_gpu = batch["gpu"]
        self.last_memory = batch["memory"]
        self.last_system_cpu = batch["system_cpu"]
        self.last_disk = batch["disk"]
        self.last_ane = batch["ane"]
        self.last_cpu = batch["cpu"]
        self.last_power = batch["power"]

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

        self._sync_process_selection()

    def _start_sampler(self, sample_interval_s: float) -> None:
        """Start the background sampler thread."""
        if self._sampler_thread and self._sampler_thread.is_alive():
            return

        self._stop_event.clear()

        def worker() -> None:
            next_sample_at = time.monotonic()
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now < next_sample_at:
                    self._stop_event.wait(next_sample_at - now)
                    continue

                batch = self._collect_sample_batch()
                while True:
                    try:
                        self._sample_queue.put_nowait(batch)
                        break
                    except queue.Full:
                        try:
                            self._sample_queue.get_nowait()
                        except queue.Empty:
                            break

                next_sample_at = time.monotonic() + sample_interval_s

        self._sampler_thread = threading.Thread(target=worker, daemon=True)
        self._sampler_thread.start()

    def _stop_sampler(self) -> None:
        """Stop the background sampler thread."""
        self._stop_event.set()
        if self._sampler_thread:
            self._sampler_thread.join(timeout=1)
            self._sampler_thread = None

    def _drain_sample_queue(self) -> bool:
        """Drain queued sample batches and apply the latest one."""
        latest_batch = None
        while True:
            try:
                latest_batch = self._sample_queue.get_nowait()
            except queue.Empty:
                break

        if latest_batch is None:
            return False

        self._apply_sample_batch(latest_batch)
        return True

    def _sync_process_selection(self) -> None:
        """Keep process selection stable across samples."""
        processes = self.last_gpu.processes if self.last_gpu else []
        if not processes:
            self.selected_process_pid = None
            self.selected_process_index = 0
            self.process_scroll_offset = 0
            return

        if self.selected_process_pid is not None:
            for index, process in enumerate(processes):
                if process.pid == self.selected_process_pid:
                    self.selected_process_index = index
                    break
            else:
                self.selected_process_index = min(self.selected_process_index, len(processes) - 1)
        else:
            self.selected_process_index = min(self.selected_process_index, len(processes) - 1)

        self.selected_process_pid = processes[self.selected_process_index].pid

    def _move_process_selection(self, delta: int) -> None:
        """Move the selected process up or down."""
        processes = self.last_gpu.processes if self.last_gpu else []
        if not processes:
            return

        next_index = self.selected_process_index + delta
        self.selected_process_index = max(0, min(len(processes) - 1, next_index))
        self.selected_process_pid = processes[self.selected_process_index].pid

    def _visible_process_limit(self) -> int:
        """Estimate how many process rows fit in the current process panel."""
        _, _, _, panel_height = self._process_panel_region()
        content_height = max(1, panel_height - 2)
        return max(1, content_height - 2)

    def _split_lengths(self, total: int, ratios: list[int]) -> list[int]:
        """Split an integer extent by ratios while preserving the total."""
        if total <= 0:
            return [0] * len(ratios)

        ratio_total = sum(max(0, ratio) for ratio in ratios)
        if ratio_total <= 0:
            return [0] * len(ratios)

        lengths = [total * ratio // ratio_total for ratio in ratios]
        remainder = total - sum(lengths)
        if remainder > 0:
            order = sorted(
                range(len(ratios)),
                key=lambda index: ((total * ratios[index]) % ratio_total, ratios[index]),
                reverse=True,
            )
            for index in order[:remainder]:
                lengths[index] += 1

        return lengths

    def _process_panel_region(self) -> tuple[int, int, int, int]:
        """Return the process panel region as 1-based x/y with width/height."""
        total_width = max(20, self.console.size.width)
        total_height = max(18, self.console.size.height)
        header_height = 3
        footer_height = 4
        body_height = max(1, total_height - header_height - footer_height)

        if self.display_mode == "stacked":
            top_height, middle_height, bottom_height = self._split_lengths(body_height, [4, 2, 7])
            return (1, 1 + header_height + top_height + middle_height, total_width, bottom_height)

        left_width, right_width = self._split_lengths(total_width, [1, 1])
        power_height, disk_height, history_height, process_height = self._split_lengths(
            body_height, [1, 1, 1, 2]
        )
        return (
            1 + left_width,
            1 + header_height + power_height + disk_height + history_height,
            right_width,
            process_height,
        )

    def _visible_process_slice(self, limit: int) -> tuple[list[ProcessGPUUsage], int, int]:
        """Return the visible process window and clamp scroll/selection."""
        processes = self.last_gpu.processes if self.last_gpu else []
        if not processes:
            self.process_scroll_offset = 0
            return [], 0, 0

        max_offset = max(0, len(processes) - limit)
        if self.selected_process_index < self.process_scroll_offset:
            self.process_scroll_offset = self.selected_process_index
        elif self.selected_process_index >= self.process_scroll_offset + limit:
            self.process_scroll_offset = self.selected_process_index - limit + 1

        self.process_scroll_offset = max(0, min(max_offset, self.process_scroll_offset))
        end_index = min(len(processes), self.process_scroll_offset + limit)
        return processes[self.process_scroll_offset:end_index], self.process_scroll_offset, end_index

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

    def _create_accelerator_panel(self) -> Panel:
        """Create combined GPU and ANE usage panel."""
        content = Text(justify="center")

        if self.last_gpu:
            gpu = self.last_gpu
            content.append_text(create_bar(gpu.device_utilization, label="GPU"))
            content.append("\n\n")
            content.append(f"Render/Tiler {gpu.renderer_utilization:4.1f}% / {gpu.tiler_utilization:4.1f}%", style="dim")
            content.append("\n\n")
            content.append(
                f"Mem {format_bytes(gpu.memory_used_bytes)} / "
                f"{format_bytes(gpu.memory_allocated_bytes)}",
                style="green",
            )
            if self.last_power and self.last_power.gpu_freq_mhz > 0:
                content.append("\n\n")
                content.append(
                    f"GPU {self.last_power.gpu_freq_mhz:.0f} MHz", style="dim"
                )
                if self.last_power.gpu_active_residency > 0:
                    content.append(
                        f"  |  active {self.last_power.gpu_active_residency:.1f}%",
                        style="dim",
                    )
        else:
            content.append("No GPU data available", style="dim")

        content.append("\n\n\n")
        if not self.show_ane:
            content.append("ANE: sudo required", style="yellow")
        elif self.last_ane:
            content.append_text(create_bar(self.last_ane.estimated_utilization, label="ANE"))
            if self.last_power and self.last_power.ane_freq_mhz > 0:
                content.append("\n\n")
                content.append(
                    f"ANE {self.last_power.ane_freq_mhz:.0f} MHz", style="dim"
                )
                if self.last_power.ane_active_residency > 0:
                    content.append(
                        f"  |  active {self.last_power.ane_active_residency:.1f}%",
                        style="dim",
                    )
        else:
            content.append("ANE: waiting for data...", style="dim")

        return Panel(
            Align.center(content, vertical="middle"),
            title="GPU / ANE Usage",
            border_style="green",
            box=box.ROUNDED,
        )

    def _create_cpu_panel(self) -> Panel:
        """Create compact CPU panel."""
        content = Text(justify="center")

        if self.last_system_cpu:
            cpu = self.last_system_cpu
            content.append_text(create_bar(cpu.overall_percent, label="CPU"))
            content.append("\n\n")
            content.append(
                f"User/System {cpu.user_percent:.1f}% / {cpu.system_percent:.1f}%",
                style="dim",
            )
            content.append("\n\n")
            content.append(
                f"Load {cpu.load_avg_1m:.2f} / {cpu.load_avg_5m:.2f} / {cpu.load_avg_15m:.2f}"
            )

            if self.last_cpu:
                content.append("\n\n")
                content.append(
                    f"E/P {self.last_cpu.e_cluster_active:.1f}% / {self.last_cpu.p_cluster_active:.1f}%",
                    style="dim",
                )
                if self.last_cpu.e_cluster_freq_mhz > 0 or self.last_cpu.p_cluster_freq_mhz > 0:
                    content.append(
                        f"  |  {self.last_cpu.e_cluster_freq_mhz}/{self.last_cpu.p_cluster_freq_mhz} MHz",
                        style="dim",
                    )
        else:
            content.append("No CPU data available", style="dim")

        return Panel(
            Align.center(content, vertical="middle"),
            title="CPU",
            border_style="cyan",
            box=box.ROUNDED,
        )

    def _create_power_panel(self) -> Panel:
        """Create compact power panel."""
        content = Text(justify="center")

        if self.last_power:
            power = self.last_power
            content.append(f"CPU   {format_power(power.cpu_power_mw)}", style="cyan")
            content.append("\n\n")
            content.append(f"GPU   {format_power(power.gpu_power_mw)}", style="green")
            if self.show_ane:
                content.append("\n\n")
                content.append(f"ANE   {format_power(power.ane_power_mw)}", style="magenta")
            if power.combined_power_mw > 0:
                content.append("\n\n")
                content.append(f"Total {format_power(power.combined_power_mw)}", style="bold")
        elif self.show_ane:
            content.append("Waiting for power data...", style="dim")
        else:
            content.append("Power: sudo required", style="yellow")

        return Panel(
            Align.center(content, vertical="middle"),
            title="Power",
            border_style="cyan",
            box=box.ROUNDED,
        )

    def _create_memory_panel(self) -> Panel:
        """Create compact memory panel."""
        content = Text(justify="center")

        if self.last_memory:
            mem = self.last_memory
            effective_used = mem.total_bytes - mem.available_bytes
            content.append_text(create_bar(mem.usage_percent, width=16, label="RAM"))
            content.append("\n\n")
            content.append(
                f"{format_bytes(effective_used)} / {format_bytes(mem.total_bytes)}",
                style="bold",
            )
            content.append(f"  |  avail {format_bytes(mem.available_bytes)}", style="green")
            if mem.swap_total_bytes > 0:
                content.append("\n\n")
                content.append(
                    f"Swap {format_bytes(mem.swap_used_bytes)} / {format_bytes(mem.swap_total_bytes)}",
                    style="dim",
                )
        else:
            content.append("No memory data available", style="dim")

        return Panel(
            Align.center(content, vertical="middle"),
            title="Memory",
            border_style="yellow",
            box=box.ROUNDED,
        )

    def _create_disk_panel(self) -> Panel:
        """Create compact disk panel."""
        content = Text(justify="center")

        if self.last_disk:
            disk = self.last_disk
            content.append_text(create_bar(disk.usage_percent, width=16, label="Disk"))
            content.append("\n\n")
            if disk.usage_source:
                content.append(f"{disk.usage_source} ", style="dim")
            content.append(
                f"{format_bytes(disk.used_bytes)} / {format_bytes(disk.total_bytes)}",
                style="bold",
            )
            content.append(f"  |  free {format_bytes(disk.free_bytes)}", style="green")
            content.append("\n\n")
            content.append(
                f"R {format_rate(disk.read_bytes_per_sec)}  |  W {format_rate(disk.write_bytes_per_sec)}",
                style="dim",
            )
        else:
            content.append("No disk data available", style="dim")

        return Panel(
            Align.center(content, vertical="middle"),
            title="Disk",
            border_style="blue",
            box=box.ROUNDED,
        )

    def _create_sparkline(self, history: list[float], width: int = 22) -> str:
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
            index = int(val / 100 * (len(blocks) - 1))
            index = max(0, min(len(blocks) - 1, index))
            result += blocks[index]
        return result

    def _history_sparkline_width(self, columns: int = 1) -> int:
        """Estimate a sparkline width that fills the history panel without wrapping."""
        if columns <= 1:
            return max(16, min(36, self.console.size.width // 4))
        return max(8, min(18, self.console.size.width // 8))

    def _create_history_line(
        self,
        label: str,
        history: list[float],
        color: str,
        width: int,
    ) -> Text:
        """Create a compact one-line history entry."""
        current = history[-1] if history else 0.0
        line = Text(no_wrap=True, overflow="crop")
        line.append(f"{label} ", style=f"bold {color}")
        line.append(self._create_sparkline(history, width=width), style=color)
        line.append(f" {current:4.1f}%", style="dim")
        return line

    def _create_history_panel(self) -> Panel:
        """Create compact history panel."""
        top_row = Table.grid(expand=True)
        top_row.add_column(ratio=1)
        top_row.add_column(ratio=1)
        top_width = self._history_sparkline_width(columns=2)
        top_row.add_row(
            self._create_history_line("GPU", self.gpu_history, "green", top_width),
            self._create_history_line("CPU", self.cpu_history, "cyan", top_width),
        )

        content: list[object] = [top_row]
        if self.show_ane:
            content.append(
                self._create_history_line(
                    "ANE",
                    self.ane_history,
                    "magenta",
                    self._history_sparkline_width(columns=1),
                )
            )

        return Panel(Group(*content), title="History", border_style="dim", box=box.ROUNDED)

    def _create_process_table(self, limit: int) -> Union[Table, Text]:
        """Create a selectable, scrollable process table."""
        if len(self.gpu_history) < 2:
            return Text("Collecting per-process GPU deltas...", style="dim")

        if not self.last_gpu or not self.last_gpu.processes:
            return Text(
                f"No GPU-active processes in the last {self.interval_ms} ms",
                style="dim",
            )

        visible_processes, start_index, _ = self._visible_process_slice(limit)

        table = Table(box=None, expand=True, pad_edge=False, show_header=True)
        table.add_column("", width=2, no_wrap=True)
        table.add_column("Process", overflow="ellipsis", ratio=3)
        table.add_column("GPU %", justify="right", width=8, no_wrap=True)
        table.add_column("GPU Time", justify="right", width=10, no_wrap=True)
        table.add_column("CPU %", justify="right", width=8, no_wrap=True)
        table.add_column("RSS", justify="right", width=10, no_wrap=True)
        table.add_column("Thr", justify="right", width=5, no_wrap=True)
        table.add_column("API", width=10, no_wrap=True)
        table.add_column("Q", justify="right", width=3, no_wrap=True)
        table.add_column("State", width=10, no_wrap=True)
        table.add_column("PID", justify="right", width=7, no_wrap=True)

        for row_index, process in enumerate(visible_processes, start=start_index):
            is_selected = row_index == self.selected_process_index
            marker = Text("▶", style="bold cyan") if is_selected else Text(" ")
            gpu_percent = Text(
                f"{process.gpu_percent:5.1f}%",
                style=get_utilization_color(min(process.gpu_percent, 100.0)),
            )
            cpu_percent = Text(f"{process.cpu_percent:5.1f}%", style="cyan")
            row_style = "bold black on bright_cyan" if is_selected else ""

            table.add_row(
                marker,
                process.name,
                gpu_percent,
                format_duration_ms(process.gpu_time_ms),
                cpu_percent,
                format_bytes(process.memory_rss_bytes) if process.memory_rss_bytes > 0 else "-",
                str(process.thread_count) if process.thread_count > 0 else "-",
                process.api or "-",
                str(process.command_queue_count),
                process.status or "-",
                str(process.pid),
                style=row_style,
            )

        return table

    def _create_process_details(self, visible_start: int, visible_end: int) -> Text:
        """Create selected-process detail line."""
        details = Text(no_wrap=True, overflow="ellipsis")
        processes = self.last_gpu.processes if self.last_gpu else []
        if not processes:
            details.append("No selected process", style="dim")
            return details

        selected = processes[self.selected_process_index]
        details.append("Selected: ", style="bold")
        details.append(selected.name, style="bold cyan")
        details.append(f"  |  pid {selected.pid}", style="dim")
        if selected.status:
            details.append(f"  |  {selected.status}", style="dim")
        details.append(
            f"  |  GPU {selected.gpu_percent:.1f}% / {format_duration_ms(selected.gpu_time_ms)}"
        )
        details.append(f"  |  CPU {selected.cpu_percent:.1f}%", style="cyan")
        if selected.memory_rss_bytes > 0:
            details.append(f"  |  RSS {format_bytes(selected.memory_rss_bytes)}", style="dim")
        if selected.thread_count > 0:
            details.append(f"  |  threads {selected.thread_count}", style="dim")
        details.append(f"  |  API {selected.api or '-'}", style="dim")
        details.append(f"  |  queues {selected.command_queue_count}", style="dim")
        details.append(
            f"  |  visible {visible_start + 1}-{visible_end}/{len(processes)}",
            style="dim",
        )
        details.append("  |  ↑/↓ select  |  wheel scroll", style="dim")
        return details

    def _create_process_panel(self) -> Panel:
        """Create top GPU process panel with selection and detail view."""
        limit = self._visible_process_limit()
        table = self._create_process_table(limit)
        visible_processes, start_index, end_index = self._visible_process_slice(limit)
        details = self._create_process_details(start_index, end_index)
        _, _, _, panel_height = self._process_panel_region()
        content_height = max(1, panel_height - 2)
        table_height = 1
        if len(self.gpu_history) >= 2 and self.last_gpu and self.last_gpu.processes:
            table_height = 1 + len(visible_processes)

        spacer_lines = max(0, content_height - table_height - 1)
        spacer = [Text(" ", no_wrap=True) for _ in range(spacer_lines)]
        content = Group(table, *spacer, details)
        return Panel(
            content,
            title="Top GPU Processes",
            border_style="white",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _select_process_from_click(self, x: int, y: int) -> bool:
        """Select the process row at the given terminal coordinates."""
        if len(self.gpu_history) < 2 or not self.last_gpu or not self.last_gpu.processes:
            return False

        panel_x, panel_y, panel_width, panel_height = self._process_panel_region()
        if not (
            panel_x <= x <= panel_x + panel_width - 1
            and panel_y <= y <= panel_y + panel_height - 1
        ):
            return False

        limit = self._visible_process_limit()
        visible_processes, start_index, _ = self._visible_process_slice(limit)
        if not visible_processes:
            return False

        data_start_y = panel_y + 2
        data_end_y = data_start_y + len(visible_processes) - 1
        if not (data_start_y <= y <= data_end_y):
            return False

        clicked_index = start_index + (y - data_start_y)
        processes = self.last_gpu.processes
        if not (0 <= clicked_index < len(processes)):
            return False

        self.selected_process_index = clicked_index
        self.selected_process_pid = processes[clicked_index].pid
        return True

    def _create_stacked_layout(self) -> Layout:
        """Create the default stacked layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="content"),
            Layout(name="footer", size=4),
        )

        layout["content"].split_column(
            Layout(name="top", ratio=4),
            Layout(name="middle", ratio=2),
            Layout(name="bottom", ratio=7),
        )

        layout["top"].split_row(
            Layout(name="accelerators", ratio=4),
            Layout(name="cpu", ratio=3),
            Layout(name="power", ratio=2),
        )

        layout["middle"].split_row(
            Layout(name="memory", ratio=3),
            Layout(name="disk", ratio=3),
            Layout(name="history", ratio=4),
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
            Layout(name="accelerators"),
            Layout(name="cpu"),
            Layout(name="memory"),
        )

        layout["right"].split_column(
            Layout(name="power"),
            Layout(name="disk"),
            Layout(name="history"),
            Layout(name="processes", ratio=2),
        )

        return layout

    def _mode_label(self) -> str:
        """Get the current display mode label."""
        return "Stacked" if self.display_mode == "stacked" else "Classic"

    def _toggle_display_mode(self) -> None:
        """Cycle to the next display mode."""
        current_index = self.DISPLAY_MODES.index(self.display_mode)
        self.display_mode = self.DISPLAY_MODES[(current_index + 1) % len(self.DISPLAY_MODES)]
        self.process_scroll_offset = 0

    def _handle_input(self) -> None:
        """Handle pending keyboard and mouse input."""
        state_changed = False

        for event in self._input.read_events():
            if event.startswith("left_click:"):
                _, x_text, y_text = event.split(":", 2)
                state_changed = self._select_process_from_click(int(x_text), int(y_text)) or state_changed
                continue

            lowered = event.lower()
            if lowered == "m":
                self._toggle_display_mode()
                state_changed = True
            elif event == "1":
                self.display_mode = "stacked"
                state_changed = True
            elif event == "2":
                self.display_mode = "classic"
                state_changed = True
            elif event in ("up", "k"):
                self._move_process_selection(-1)
                state_changed = True
            elif event in ("down", "j"):
                self._move_process_selection(1)
                state_changed = True
            elif event == "wheel_up":
                self._move_process_selection(-1)
                state_changed = True
            elif event == "wheel_down":
                self._move_process_selection(1)
                state_changed = True

        return state_changed

    def _render(self) -> Layout:
        """Render the current state."""
        if self.display_mode == "stacked":
            layout = self._create_stacked_layout()
            layout["header"].update(self._create_header())
            layout["accelerators"].update(self._create_accelerator_panel())
            layout["cpu"].update(self._create_cpu_panel())
            layout["power"].update(self._create_power_panel())
            layout["memory"].update(self._create_memory_panel())
            layout["disk"].update(self._create_disk_panel())
            layout["history"].update(self._create_history_panel())
            layout["bottom"].update(self._create_process_panel())
        else:
            layout = self._create_classic_layout()
            layout["header"].update(self._create_header())
            layout["accelerators"].update(self._create_accelerator_panel())
            layout["cpu"].update(self._create_cpu_panel())
            layout["memory"].update(self._create_memory_panel())
            layout["power"].update(self._create_power_panel())
            layout["disk"].update(self._create_disk_panel())
            layout["history"].update(self._create_history_panel())
            layout["processes"].update(self._create_process_panel())

        footer_text = Text()
        footer_text.append("Ctrl+C", style="bold")
        footer_text.append(" exit  |  ", style="dim")
        footer_text.append("m", style="bold")
        footer_text.append(" switch layout  |  ", style="dim")
        footer_text.append("↑/↓", style="bold")
        footer_text.append(" select process  |  ", style="dim")
        footer_text.append("click", style="bold")
        footer_text.append(" select  |  ", style="dim")
        footer_text.append("wheel", style="bold")
        footer_text.append(" scroll list  |  ", style="dim")
        footer_text.append(f"Layout: {self._mode_label()}", style="cyan")
        footer_text.append(f"  |  Refresh: {self.interval_ms}ms", style="dim")
        if not self._input.enabled:
            footer_text.append("  |  input hotkeys unavailable", style="yellow")

        layout["footer"].update(Panel(footer_text, box=box.ROUNDED))
        return layout

    def run(self) -> None:
        """Run the TUI application."""
        sample_interval_s = max(0.05, self.interval_ms / 1000)
        idle_refresh_interval_s = 0.25

        try:
            with self._input:
                self._apply_sample_batch(self._collect_sample_batch())
                self._start_sampler(sample_interval_s)
                next_idle_refresh_at = time.monotonic() + idle_refresh_interval_s

                with Live(
                    self._render(),
                    console=self.console,
                    auto_refresh=False,
                ) as live:
                    while True:
                        input_changed = self._handle_input()
                        sampled = self._drain_sample_queue()
                        now = time.monotonic()
                        idle_refresh = now >= next_idle_refresh_at

                        if input_changed or sampled or idle_refresh:
                            live.update(self._render(), refresh=True)
                            next_idle_refresh_at = now + idle_refresh_interval_s

                        sleep_s = 0.005 if input_changed else 0.01
                        time.sleep(sleep_s)

        except KeyboardInterrupt:
            self.console.print("\n[dim]Goodbye![/dim]")

        finally:
            self._stop_sampler()
            if self.ane_collector:
                self.ane_collector.stop_streaming()
