# Binaries

This directory contains a prebuilt binary of WiFi Spectrum, compiled with [Nuitka](https://nuitka.net/) from `scan.py`.

No installation needed — just download the executable, `chmod +x` it, and run.

These binaries are provided for *Fedora users* (and other distros with matching library versions) who prefer not to set up a Python environment or compile from source manually. They are intended for convenience and quick testing of the tool.

## Requirements

The binary is dynamically linked against:

- `libpython3.14.so.1.0`
- `libm.so.6`
- `libc.so.6`

along with `nmcli` (NetworkManager) and `rfkill` at runtime. If `libpython3.14` isn't present on your system, either install it or build from source instead — see the top-level [README](../README.md).

⚠️ Use at your own discretion and ensure you trust the provided builds before running them.
