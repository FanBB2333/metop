"""
CLI entry point for metop.

Usage:
    python -m metop [options]
    metop [options]  (if installed via pip)
"""

import argparse
import sys
import os


def check_sudo_hint() -> None:
    """Print hint about sudo for powermetrics-based metrics."""
    if os.geteuid() != 0:
        print(
            "\033[33mNote: Run with 'sudo metop' to enable ANE + power metrics (powermetrics).\033[0m"
        )
        print()


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="metop",
        description="macOS GPU/ANE Monitor for Apple Silicon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    metop                   Start monitoring with default settings
    sudo metop              Start with ANE monitoring enabled
    metop -i 500            Sample every 500ms (faster updates)
    metop --no-ane          Disable ANE monitoring even with sudo

For bug reports and contributions:
    https://github.com/FanBB2333/metop
        """
    )
    
    parser.add_argument(
        "-i", "--interval",
        type=int,
        default=1000,
        metavar="MS",
        help="Refresh interval in milliseconds (default: 1000)"
    )
    
    parser.add_argument(
        "--no-ane",
        action="store_true",
        help="Disable ANE monitoring even when running with sudo"
    )
    
    parser.add_argument(
        "--color",
        type=int,
        default=0,
        choices=range(9),
        metavar="N",
        help="Color scheme (0-8, default: 0)"
    )
    
    parser.add_argument(
        "-v", "--version",
        action="version",
        version="%(prog)s 0.1.0"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output"
    )
    
    args = parser.parse_args()
    
    # Check for Rich dependency
    try:
        import rich
    except ImportError:
        print("Error: 'rich' package is required. Install with: pip install rich")
        return 1
    
    # Show sudo hint
    check_sudo_hint()
    
    # Debug mode: print raw data instead of TUI
    if args.debug:
        from .collectors import GPUCollector, ANECollector, SystemCollector, MemoryCollector
        
        print("=== System Info ===")
        sys_collector = SystemCollector()
        info = sys_collector.collect()
        print(f"Chip: {info.chip_name}")
        print(f"CPU Cores: {info.cpu_cores} ({info.cpu_p_cores}P + {info.cpu_e_cores}E)")
        print(f"GPU Cores: {info.gpu_cores}")
        print(f"Memory: {info.memory_total_bytes / (1024**3):.1f} GB")
        print()
        
        print("=== GPU Sample ===")
        gpu_collector = GPUCollector()
        gpu = gpu_collector.sample()
        print(f"Device Utilization: {gpu.device_utilization}%")
        print(f"Renderer Utilization: {gpu.renderer_utilization}%")
        print(f"Tiler Utilization: {gpu.tiler_utilization}%")
        print(f"Memory Used: {gpu.memory_used_bytes / (1024**2):.1f} MB")
        print(f"Memory Allocated: {gpu.memory_allocated_bytes / (1024**2):.1f} MB")
        print()
        
        print("=== Memory Sample ===")
        mem_collector = MemoryCollector()
        mem = mem_collector.sample()
        print(f"Used: {mem.used_bytes / (1024**3):.2f} GB")
        print(f"Available: {mem.available_bytes / (1024**3):.2f} GB")
        print(f"Usage: {mem.usage_percent:.1f}%")
        print()
        
        if ANECollector.check_sudo():
            print("=== ANE Sample (requires waiting for interval) ===")
            ane_collector = ANECollector(interval_ms=args.interval)
            ane = ane_collector.sample()
            if ane:
                print(f"Power: {ane.power_mw:.1f} mW")
                print(f"Estimated Utilization: {ane.estimated_utilization:.1f}%")
            else:
                print("No ANE data available")
        else:
            print("=== ANE ===")
            print("Run with sudo for ANE monitoring")
        
        return 0
    
    # Start TUI
    try:
        from .tui import MetopApp
        
        app = MetopApp(
            interval_ms=args.interval,
            show_ane=not args.no_ane,
            color_scheme=args.color
        )
        app.run()
        return 0
    
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
