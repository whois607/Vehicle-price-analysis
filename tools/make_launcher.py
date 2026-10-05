"""One-time launcher setup for VPA (Windows).

    Double-click "Setup Import App.bat"   (or: python tools\\make_launcher.py)

Does two things, then never needs running again unless Python or this
folder moves:
  1. Draws the VPA icon - the app's own brand mark (white rising line on the
     blue tile in the nav bar) - as assets/vpa.ico for Windows, assets/vpa.svg
     for the browser tab, and assets/vpa-192.png / vpa-512.png for a phone
     home screen.
  2. Creates a "VPA" shortcut on the Desktop, in the Start menu and in this
     folder, pointing at pythonw.exe -m vpa import-ui (pythonw = no console
     window), with that icon and the right working directory.

Double-clicking the shortcut starts the server quietly and opens the page in
the default browser; if it's already running it just opens the page again.
Shut it down with the in-app power button. If it cannot start, a message box
says why (details in output/vpa-app.log).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO = ASSETS / "vpa.ico"
SVG = ASSETS / "vpa.svg"
NAME = "VPA"
OLD_LNK = ROOT / "Vehicle Import.lnk"      # the shortcut this one replaces

# The brand mark, exactly as the nav bar draws it (vpa/ui.py ICONS["trend"]):
# a rising line and its arrowhead on a 24-unit grid, centred at (12, 12).
TREND_LINE = [(3, 17), (9, 11), (13, 15), (21, 7)]
TREND_HEAD = [(15, 7), (21, 7), (21, 13)]
TOP, BOTTOM = (0x3a, 0x6f, 0xd8), (0x1f, 0x3f, 0x8f)   # the logo's gradient

SVG_TEXT = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#{''.join(f'{c:02x}' for c in TOP)}"/>
<stop offset="1" stop-color="#{''.join(f'{c:02x}' for c in BOTTOM)}"/>
</linearGradient></defs>
<rect width="64" height="64" rx="15" fill="url(#g)"/>
<path d="M3 17l6-6 4 4 8-8M15 7h6v6" transform="translate(8 8) scale(2)" fill="none"
 stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def _tile(px: int, *, inset: float, radius: float, glyph: float, stroke: float,
          full_bleed: bool = False):
    """One square icon image, drawn 4x oversize and scaled down so edges are
    smooth. `inset`/`radius` are fractions of the side; `glyph` is how much of
    the side the 24-unit grid spans; `stroke` is in grid units."""
    from PIL import Image, ImageDraw

    ss = 4
    s = px * ss
    # diagonal gradient, top-left to bottom-right, like the CSS logo
    grad = Image.new("RGBA", (256, 256))       # drawn small, then stretched
    gp = grad.load()
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            gp[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
    grad = grad.resize((s, s), Image.BILINEAR)
    mask = Image.new("L", (s, s), 0)
    m = ImageDraw.Draw(mask)
    if full_bleed:
        m.rectangle([0, 0, s, s], fill=255)
    else:
        i = round(s * inset)
        m.rounded_rectangle([i, i, s - 1 - i, s - 1 - i], radius=round(s * radius), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    unit = s * glyph / 24
    off = (s - 24 * unit) / 2
    w = max(1, round(stroke * unit))
    white = (255, 255, 255, 255)

    def pt(p):
        return (off + p[0] * unit, off + p[1] * unit)
    for seg in (TREND_LINE, TREND_HEAD):
        pts = [pt(p) for p in seg]
        d.line(pts, fill=white, width=w, joint="curve")
        for x, y in pts:                          # round caps and joins
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=white)
    return img.resize((px, px), Image.LANCZOS)


def draw_icon() -> None:
    ASSETS.mkdir(exist_ok=True)
    # Windows: a rounded tile with a small margin, like other app icons. The
    # small sizes get a bolder line and a bigger glyph so they stay legible.
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [_tile(n, inset=0.04 if n >= 32 else 0.0, radius=0.22,
                    glyph=0.72 if n >= 48 else 0.86,
                    stroke=2.3 if n >= 48 else 3.0) for n in sizes]
    frames[-1].save(ICO, sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    # phone home screen: full-bleed square - Android applies its own mask
    for n in (192, 512):
        _tile(n, inset=0, radius=0, glyph=0.6, stroke=2.3, full_bleed=True) \
            .save(ASSETS / f"vpa-{n}.png")
    SVG.write_text(SVG_TEXT, encoding="utf-8", newline="\n")
    print(f"icon written: {ICO}  (+ vpa.svg, vpa-192.png, vpa-512.png)")


def _special_folder(name: str) -> Path:
    """The real Desktop / Start menu folder (OneDrive often moves the
    Desktop, so it is asked for, not assumed)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def make_shortcut(where: Path) -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)   # console fallback, still works
        print("note: pythonw.exe not found next to python.exe - the shortcut "
              "will show a console window.")
    lnk = where / f"{NAME}.lnk"
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{lnk}')
$s.TargetPath = '{pythonw}'
$s.Arguments = '-m vpa import-ui'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = '{ICO},0'
$s.Description = 'VPA - vehicle price analysis'
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
    print(f"shortcut written: {lnk}")
    return lnk


def main() -> int:
    draw_icon()
    for where in (ROOT, _special_folder("Desktop"), _special_folder("Programs")):
        make_shortcut(where)
    if OLD_LNK.exists():                 # replaced by "VPA" - same target
        OLD_LNK.unlink()
        print(f"removed the old shortcut: {OLD_LNK.name}")
    print()
    print("Done. Everyday use: double-click 'VPA' on the Desktop, or type VPA")
    print("in the Start menu. Pin it: right-click -> Pin to taskbar / Pin to Start.")
    print("Close the app with its power button.")
    print("If you move this folder or reinstall Python, run this setup again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
