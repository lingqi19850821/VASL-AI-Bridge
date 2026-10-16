# Application icons

- `app-icon-32.png` is the selected pixel-aligned small icon. Its original PNG
  bytes are preserved as the 32px frame in `app.ico`.
- `app-icon-large.png` preserves the selected earlier high-resolution design.
- `app.ico` contains 16, 24, 32, and 48px frames from the small icon, and
  64, 128, and 256px frames from the large design. Larger frames preserve the
  source aspect ratio on a transparent square canvas.

Rebuild the ICO with the existing build environment:

```powershell
& .\build\windows-env\Scripts\python.exe -B packaging/prepare_icon.py
```

The same ICO is used for the main executable, Qt application windows, and Setup.
