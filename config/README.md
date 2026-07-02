# WiFi Spectrum – Config

This directory contains configuration files used by the WiFi Spectrum application.
these are auto-generated and stored in ~/.config/wifi-spectrum (ofcourse)

---

# 📁 Files

### `nmcli_extra.conf`
Defines WiFi connection parameters used with `nmcli` (NetworkManager CLI).

Example:
```
802-11-wireless.powersave 2
ipv6.method disabled
```

These values are used to generate an `nmcli` connection command for joining WiFi networks.

---

### `theme.conf`
Defines the UI theme mode.

Supported values:
- `auto` – follow system theme
- `light` – light mode
- `dark` – dark mode

---

### `theme.css`
Full custom CSS for the application UI.
Used at runtime to style the interface without rebuilding the app.
