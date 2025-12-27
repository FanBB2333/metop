# metop

A Python-based GPU/ANE monitoring tool for Apple Silicon Macs. Like `nvtop` or `nvidia-smi`, but for Metal and the Neural Engine.

## Features

- **GPU Monitoring** (no sudo required)
  - Device, Renderer, and Tiler utilization percentage
  - GPU memory usage (in-use vs allocated)
  - Real-time sparkline history

- **ANE + Power Metrics** (requires sudo / `powermetrics`)
  - CPU/GPU/ANE/Total power (mW/W)
  - GPU/ANE frequency and active/idle residency (when available)
  - ANE utilization estimated from residency (fallback: power-based)
  
- **System Info**
  - Chip detection (M1/M2/M3/M4 series)
  - CPU/GPU/ANE core counts
  - Memory usage and swap

## Installation

```bash
# Install from source
pip install -e .

# Or with optional fast IOKit bindings
pip install -e ".[fast]"
```

## Usage

```bash
# Basic monitoring (GPU only)
metop

# Enable ANE + power metrics (requires sudo)
sudo metop

# Custom refresh interval (500ms)
metop -i 500

# Disable ANE/powermetrics even with sudo
metop --no-ane

# Debug mode (single sample, raw output)
metop --debug
```

## Screenshot

![metop main screen](resources/sample1.png)

## How It Works

### GPU Monitoring
Uses `IOKit` via `ioreg` command to query the `AGXAccelerator` driver's `PerformanceStatistics`. This provides:
- `Device Utilization %` - Overall GPU busy percentage
- `Renderer Utilization %` - Shader/compute units
- `Tiler Utilization %` - Geometry processing

### ANE + Power Metrics
Uses `powermetrics` (`-f plist`) to collect CPU/GPU/ANE power and residency/frequency data. Requires `sudo` because `powermetrics` needs root access.

ANE utilization is estimated from:
- preferred: ANE active residency reported by `powermetrics`
- fallback: power-based estimate

```
utilization = (current_power / max_power) * 100%
```

## Requirements

- macOS Monterey (12.0) or later
- Apple Silicon Mac (M1/M2/M3/M4 series)
- Python 3.9+
- `rich` (terminal UI)
- `psutil` (memory stats)

## License

MIT License
