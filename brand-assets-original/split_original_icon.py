from pathlib import Path

from PIL import Image, ImageChops, ImageFilter


SOURCE = Path("/Users/wangxi/Downloads/JPEG图像-4B70-8A29-FE-0.jpeg")
OUT = Path(__file__).resolve().parent


def blue_alpha(image: Image.Image) -> Image.Image:
    """Recover the blue artwork from the near-white JPEG background."""
    rgb = image.convert("RGB")
    r, g, b = rgb.split()

    # Blue chroma strength plus a small luminance term keeps the pale-blue facet.
    chroma = ImageChops.subtract(b, r).point(lambda v: min(255, v * 5))
    darkness = r.point(lambda v: max(0, min(255, (248 - v) * 2)))
    alpha = ImageChops.lighter(chroma, darkness)

    # Blue must still be the dominant channel; this rejects neutral card shadows.
    dominance = ImageChops.subtract(b, g).point(lambda v: min(255, v * 8))
    alpha = ImageChops.multiply(alpha, dominance.point(lambda v: min(255, v + 80)))
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.65))
    return alpha


def apply_alpha(image: Image.Image, alpha: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba


def spatial_mask(size, polygon):
    from PIL import ImageDraw

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(polygon, fill=255)
    return mask


def save_layer(name, source, alpha, crop_box=None):
    layer = apply_alpha(source, alpha)
    if crop_box:
        layer = layer.crop(crop_box)
    layer.save(OUT / name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    image = Image.open(SOURCE).convert("RGB")
    alpha = blue_alpha(image)
    w, h = image.size

    # Artwork bounds with breathing room.
    art_box = (260, 245, 995, 1000)

    # The source's four reusable visual parts.
    right_zone = spatial_mask(
        image.size,
        [(795, 425), (995, 425), (995, 1010), (842, 1010), (760, 760), (790, 680)],
    )
    left_eye_zone = spatial_mask(image.size, [(530, 555), (610, 555), (610, 675), (530, 675)])
    right_eye_zone = spatial_mask(image.size, [(665, 555), (750, 555), (750, 675), (665, 675)])
    # The facet is materially lighter than the saturated main C. Combining
    # position with the red-channel floor prevents nearby dark-blue wedges from
    # leaking into the extracted facet.
    source_r = image.getchannel("R")
    pale_blue = source_r.point(
        lambda value: 0 if value <= 65 else min(255, (value - 65) * 6)
    )
    right_alpha = ImageChops.multiply(
        ImageChops.multiply(alpha, right_zone), pale_blue
    )
    left_eye_alpha = ImageChops.multiply(alpha, left_eye_zone)
    right_eye_alpha = ImageChops.multiply(alpha, right_eye_zone)
    eyes_alpha = ImageChops.lighter(left_eye_alpha, right_eye_alpha)
    main_alpha = ImageChops.subtract(alpha, ImageChops.lighter(right_alpha, eyes_alpha))

    save_layer("caddie-logo-transparent.png", image, alpha, art_box)
    save_layer("component-01-main-c.png", image, main_alpha, art_box)
    save_layer("component-02-right-facet.png", image, right_alpha, art_box)
    save_layer("component-03-left-eye.png", image, left_eye_alpha, art_box)
    save_layer("component-04-right-eye.png", image, right_eye_alpha, art_box)

    # Faithful application icon crop, including the original white card.
    app_crop = image.crop((135, 125, 1115, 1115))
    app_crop = app_crop.resize((1024, 1024), Image.Resampling.LANCZOS).convert("RGBA")
    app_mask = Image.new("L", (1024, 1024), 0)
    from PIL import ImageDraw

    ImageDraw.Draw(app_mask).rounded_rectangle(
        (18, 18, 1005, 1005), radius=210, fill=255
    )
    app_crop.putalpha(app_mask)
    app_crop.save(OUT / "caddie-app-icon-1024.png", quality=98)

    # Large transparent brand artwork for 2K/4K compositions.
    logo = apply_alpha(image, alpha).crop(art_box)
    logo.resize((4096, 4208), Image.Resampling.LANCZOS).save(
        OUT / "caddie-logo-transparent-4k.png"
    )

    # macOS template: single-color silhouette with the central negative space intact.
    template_alpha = alpha.point(lambda value: 255 if value >= 90 else 0)
    template_alpha = template_alpha.filter(ImageFilter.MaxFilter(5)).filter(
        ImageFilter.GaussianBlur(0.45)
    )
    template = Image.new("RGBA", image.size, (0, 0, 0, 0))
    template.putalpha(template_alpha)
    template = template.crop(art_box).resize((512, 512), Image.Resampling.LANCZOS)
    template.save(OUT / "caddie-mac-template-512.png")

    # Checkerboard QA preview so transparency and layer boundaries are visible.
    tiles = []
    for filename in (
        "caddie-logo-transparent.png",
        "component-01-main-c.png",
        "component-02-right-facet.png",
        "component-03-left-eye.png",
        "component-04-right-eye.png",
        "caddie-mac-template-512.png",
    ):
        item = Image.open(OUT / filename).convert("RGBA")
        item.thumbnail((360, 360), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (400, 400), "#E7EBF3")
        from PIL import ImageDraw

        draw = ImageDraw.Draw(tile)
        for yy in range(0, 400, 32):
            for xx in range(0, 400, 32):
                if (xx // 32 + yy // 32) % 2:
                    draw.rectangle((xx, yy, xx + 31, yy + 31), fill="#F8FAFD")
        tile.paste(item, ((400 - item.width) // 2, (400 - item.height) // 2), item)
        tiles.append(tile)

    sheet = Image.new("RGB", (1200, 800), "white")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % 3) * 400, (index // 3) * 400))
    sheet.save(OUT / "caddie-components-preview.png")


if __name__ == "__main__":
    main()
