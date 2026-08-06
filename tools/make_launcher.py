"""One-time launcher setup for the import UI (Windows).

    Double-click "Setup Import App.bat"   (or: python tools\\make_launcher.py)

Does two things, then never needs running again unless Python or this
folder moves:
  1. Draws assets/vehicle-import.ico (blue tile, white car, gold trend arrow).
  2. Creates "Vehicle Import.lnk" in the project folder pointing at
     pythonw.exe -m vpa import-ui (pythonw = no console window), with the
     icon, a description, and the right working directory.

Double-clicking the shortcut starts the server quietly and opens the page;
if it's already running it just opens the page again. Shut it down with the
in-app "Shut down" button.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO = ASSETS / "vehicle-import.ico"
LNK = ROOT / "Vehicle Import.lnk"


def draw_icon() -> None:
    from PIL import Image, ImageDraw

    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # blue rounded tile
    d.rounded_rectangle([8, 8, s - 8, s - 8], radius=44, fill=(48, 96, 160, 255))

    # white car silhouette (body, cabin, wheels)
    d.rounded_rectangle([40, 138, 216, 186], radius=18, fill=(255, 255, 255, 255))
    d.polygon([(78, 142), (100, 104), (168, 104), (190, 142)],
              fill=(255, 255, 255, 255))
    d.rounded_rectangle([106, 110, 160, 140], radius=6, fill=(48, 96, 160, 255))
    for cx in (86, 178):
        d.ellipse([cx - 22, 168, cx + 22, 212], fill=(24, 40, 64, 255))
        d.ellipse([cx - 10, 180, cx + 10, 200], fill=(230, 235, 242, 255))

    # gold rising trend line with arrowhead
    pts = [(46, 92), (96, 72), (136, 84), (196, 44)]
    d.line(pts, fill=(240, 190, 60, 255), width=14, joint="curve")
    d.polygon([(214, 32), (206, 66), (180, 44)], fill=(240, 190, 60, 255))

    ASSETS.mkdir(exist_ok=True)
    img.save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                         (128, 128), (256, 256)])
    print(f"icon written: {ICO}")


def make_shortcut() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)   # console fallback, still works
        print("note: pythonw.exe not found next to python.exe - the shortcut "
              "will show a console window.")
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{LNK}')
$s.TargetPath = '{pythonw}'
$s.Arguments = '-m vpa import-ui'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = '{ICO},0'
$s.Description = 'Vehicle price import - preview listings before committing'
$s.Save()
"""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(ps)
        script = f.name
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", script], check=True)
    finally:
        Path(script).unlink(missing_ok=True)
    print(f"shortcut written: {LNK}")


def main() -> int:
    draw_icon()
    make_shortcut()
    print()
    print("Done. Everyday use: double-click 'Vehicle Import' in this folder.")
    print("Pin it: right-click the shortcut -> Pin to Start (or drag a copy")
    print("to the Desktop). Close the app with its 'Shut down' button.")
    print("If you move this folder or reinstall Python, run this setup again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
