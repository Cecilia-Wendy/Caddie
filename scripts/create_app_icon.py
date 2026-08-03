"""Create Caddie application icons from the approved brand artwork."""
from pathlib import Path
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "icon"
ICONSET = BUILD / "Caddie.iconset"
OUTPUT = ROOT / "assets" / "Caddie.png"
WINDOWS_OUTPUT = ROOT / "assets" / "Caddie.ico"
SOURCE = ROOT / "brand-assets-original" / "caddie-app-icon-1024.png"
TRANSPARENT_SOURCE = ROOT / "brand-assets-original" / "caddie-logo-transparent.png"
STATIC_APP_ICON = ROOT / "static" / "caddie-app-icon.png"
STATIC_MASCOT = ROOT / "static" / "caddie-mascot.png"


def render(size: int) -> Image.Image:
    if not SOURCE.exists():
        raise FileNotFoundError(f"Approved icon source is missing: {SOURCE}")
    image = Image.open(SOURCE).convert("RGBA").resize(
        (size, size), Image.Resampling.LANCZOS
    )

    # macOS does not apply the app-icon silhouette for arbitrary ICNS artwork.
    # Keep the outer corners transparent so Finder/Launchpad render a real
    # rounded-square icon instead of an opaque white square.
    scale = max(4, size)
    mask = Image.new("L", (scale, scale), 0)
    draw = ImageDraw.Draw(mask)
    inset = round(scale * 0.018)
    radius = round(scale * 0.205)
    draw.rounded_rectangle(
        (inset, inset, scale - inset - 1, scale - inset - 1),
        radius=radius,
        fill=255,
    )
    if scale != size:
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    image.putalpha(mask)
    return image


def main():
    ICONSET.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    for point in (16, 32, 128, 256, 512):
        render(point).save(ICONSET / f"icon_{point}x{point}.png")
        render(point * 2).save(ICONSET / f"icon_{point}x{point}@2x.png")
    # PyInstaller converts the 1024px PNG to ICNS during the macOS bundle
    # build.  Keeping this source image also avoids iconutil differences across
    # macOS/Xcode versions.
    render(1024).save(OUTPUT)
    render(1024).save(STATIC_APP_ICON)
    mascot = Image.open(TRANSPARENT_SOURCE).convert("RGBA")
    clean_alpha = mascot.getchannel("A").point(
        lambda value: 0 if value <= 52 else min(255, round((value - 52) * 255 / 203))
    )
    mascot.putalpha(clean_alpha)
    mascot.thumbnail((896, 896), Image.Resampling.LANCZOS)
    mascot_canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    mascot_canvas.alpha_composite(
        mascot, ((1024 - mascot.width) // 2, (1024 - mascot.height) // 2)
    )
    mascot_canvas.save(STATIC_MASCOT)
    render(256).save(
        WINDOWS_OUTPUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(OUTPUT)
    print(WINDOWS_OUTPUT)
    print(STATIC_APP_ICON)
    print(STATIC_MASCOT)


if __name__ == "__main__":
    main()
