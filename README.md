# metop

A Python-based GPU/ANE monitoring tool for Apple Silicon Macs. Like `nvtop` or `nvidia-smi`, but for Metal and the Neural Engine.

## Features

- **GPU Monitoring** (no sudo required)
  - Device, Renderer, and Tiler utilization percentage
  - GPU memory usage (in-use vs allocated)
  - Real-time sparkline history

- **ANE Monitoring** (requires sudo)
  - Power consumption in mW/W
  - Estimated utilization based on power draw
  
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

# Full monitoring with ANE (requires sudo)
sudo metop

# Custom refresh interval (500ms)
metop -i 500

# Debug mode (single sample, raw output)
metop --debug
```

## Screenshot

```
┌─────────────────────── metop ────────────────────────┐
│ Apple M1 Pro  |  CPU: 10 cores  |  GPU: 16 cores     │
└──────────────────────────────────────────────────────┘
┌─── GPU (Metal) ───┐  ┌─── ANE (Neural Engine) ───┐
│ Device   [████░░] │  │ Utilization [██░░░░░░░░] │
│ Renderer [███░░░] │  │ Power: 2.5 W              │
│ Tiler    [██░░░░] │  └───────────────────────────┘
│ Memory: 1.2 GB    │
└───────────────────┘
```

## How It Works

### GPU Monitoring
Uses `IOKit` via `ioreg` command to query the `AGXAccelerator` driver's `PerformanceStatistics`. This provides:
- `Device Utilization %` - Overall GPU busy percentage
- `Renderer Utilization %` - Shader/compute units
- `Tiler Utilization %` - Geometry processing

### ANE Monitoring
Uses `powermetrics` to get Neural Engine power consumption. Requires `sudo` because `powermetrics` needs root access. ANE utilization is estimated from:
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
