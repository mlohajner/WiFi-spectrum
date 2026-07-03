# .config

These are configuration files used by the WiFi Spectrum application.  
Files are auto-generated and stored in ~/.config/wifi-spectrum (ofcourse)

---

# 📁 Files

### `nmcli_extra.conf`
Defines extra WiFi connection parameters used with `nmcli` (NetworkManager CLI).  
These values are used to generate an `nmcli` connection command for joining WiFi networks.

Example:
```
802-11-wireless.powersave 2
ipv6.method disabled
```

In this example: disable WiFi powersaving and disable IPv6

---

### `theme.conf`
Defines the UI theme mode.  
(single word/flag)

Supported values:
- `auto` - follow system theme
- `light` - light mode
- `dark` - dark mode

---

### `theme.css`
Full custom CSS for the application UI.  
Used at runtime to style the interface without rebuilding the app.
