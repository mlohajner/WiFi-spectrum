# WiFi Spectrum – Config

This directory contains configuration files used by the WiFi Spectrum application.

---

## 📁 Files

### `wifi.conf`
Defines WiFi connection parameters used with `nmcli` (NetworkManager CLI).

Example:
```ini
ssid="MyNetwork"
password="mypassword"
interface="wlan0"
autoconnect=true
hidden=false
```

These values are used to generate an `nmcli` connection command for joining WiFi networks.

---

### `theme.conf`
Defines the UI theme mode.

Supported values:
- `auto` – follow system theme
- `light` – light mode
- `dark` – dark mode

Example:
```ini
theme=dark
```

---

### `theme.css`
Full custom CSS for the application UI.

Used at runtime to style the interface without rebuilding the app.

Example:
```css
body {
  background: #121212;
  color: #ffffff;
}
```

---

## ⚙️ Notes
- WiFi connections are managed via `nmcli`
- Theme changes are applied at runtime
- CSS overrides the full UI style

