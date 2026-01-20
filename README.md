# Life Dashboard

A simple web page that displays an iframe with rotation controls, fullscreen capability, and URL management. Designed for a tablet connected to a vertically-oriented monitor where native rotation isn't possible.

## Quick Start

```bash
# Edit credentials in .env
vim .env

# Start the server
python server.py
```

The server runs at **https://0.0.0.0:9473** (accessible from your network).

## Configuration

Edit `.env` file:

```
TDC_USERNAME=admin
TDC_PASSWORD=your_secure_password
```

## Accessing from Tablet

1. Find your machine's IP: `ip addr` or `hostname -I`
2. On tablet, navigate to: `https://<your-ip>:9473`
3. Accept the self-signed certificate warning
4. Login with your credentials

## Usage

1. Login with your credentials
2. Enter a URL in the input field and click **Load** (or press Enter)
3. Use **Rotate** to cycle through 0°, 90°, 180°, 270° rotations
4. Use **Fullscreen** to enter fullscreen mode
5. Press browser back or ESC to exit fullscreen

## Keyboard Shortcuts

- **R** - Rotate content by 90°
- **F** - Toggle fullscreen mode
- **ESC** - Exit fullscreen

## Features

- HTTPS with auto-generated self-signed certificate
- Session-based authentication with secure cookies
- Credentials loaded from `.env` file (gitignored)
- Rotation persisted in localStorage
- URL persisted in localStorage
- Back gesture exits fullscreen (for tablet touch interfaces)

## Security

- HTTPS with TLS encryption
- Passwords hashed with SHA-256
- HttpOnly, Secure, SameSite=Strict cookies
- 24-hour session expiration
- Sessions stored in memory (cleared on server restart)
- Credentials and certificates excluded from git

## Files

- `index.html` - Main app UI
- `server.py` - HTTPS server with authentication
- `.env` - Credentials (gitignored)
- `cert.pem`, `key.pem` - SSL certificate (auto-generated, gitignored)
