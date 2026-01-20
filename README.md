# Tablet Display Controller

A simple web page that displays an iframe with rotation controls, fullscreen capability, and URL management. Designed for a tablet connected to a vertically-oriented monitor where native rotation isn't possible.

## Quick Start

```bash
# Start the server with authentication
python server.py

# Open in browser
# http://127.0.0.1:9473
```

Default credentials: `admin` / `changeme`

## Custom Credentials

Set environment variables before starting:

```bash
export TDC_USERNAME="myuser"
export TDC_PASSWORD="mysecurepassword"
python server.py
```

## Usage

1. Navigate to `http://127.0.0.1:9473`
2. Login with your credentials
3. Enter a URL in the input field and click **Load** (or press Enter)
4. Use **Rotate** to cycle through 0°, 90°, 180°, 270° rotations
5. Use **Fullscreen** to enter fullscreen mode
6. Press browser back or ESC to exit fullscreen

## Keyboard Shortcuts

- **R** - Rotate content by 90°
- **F** - Toggle fullscreen mode
- **ESC** - Exit fullscreen

## Features

- Session-based authentication with secure cookies
- Rotation persisted in localStorage
- URL persisted in localStorage
- Back gesture exits fullscreen (for tablet touch interfaces)
- Responsive sizing adjusts to container and rotation

## Security

- Passwords hashed with SHA-256
- HttpOnly session cookies (not accessible via JavaScript)
- SameSite=Strict cookies (CSRF protection)
- 24-hour session expiration
- Sessions stored in memory (cleared on server restart)

## Default Content

The default URL is Google Calendar in agenda mode. Note that some sites block iframe embedding (X-Frame-Options). If a site doesn't load, try a different URL or use the site's embed URL format.

## Without Authentication

To use without the server, open `index.html` directly in a browser.
