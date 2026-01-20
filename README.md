# Tablet Display Controller

A simple web page that displays an iframe with rotation controls, fullscreen capability, and URL management. Designed for a tablet connected to a vertically-oriented monitor where native rotation isn't possible.

## Usage

1. Open `index.html` in a browser
2. Enter a URL in the input field and click **Load** (or press Enter)
3. Use **Rotate** to cycle through 0°, 90°, 180°, 270° rotations
4. Use **Fullscreen** to enter fullscreen mode
5. Press browser back or ESC to exit fullscreen

## Keyboard Shortcuts

- **R** - Rotate content by 90°
- **F** - Toggle fullscreen mode
- **ESC** - Exit fullscreen

## Features

- Rotation persisted in localStorage
- URL persisted in localStorage
- Back gesture exits fullscreen (for tablet touch interfaces)
- Responsive sizing adjusts to container and rotation

## Default Content

The default URL is Google Calendar in agenda mode. Note that some sites block iframe embedding (X-Frame-Options). If a site doesn't load, try a different URL or use the site's embed URL format.

## Deployment

Simply serve `index.html` via any static file server, or open directly in a browser.
