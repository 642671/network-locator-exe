"""Generate the Windows PNG and ICO assets for Network Locator."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def vertical_gradient(
    size: tuple[int, int],
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(
            round(start[index] + (end[index] - start[index]) * t)
            for index in range(4)
        )
        draw.line((0, y, width, y), fill=color)
    return image


def draw_globe(icon: Image.Image) -> None:
    size = icon.width
    radius = int(size * 0.262)
    center = (size // 2, int(size * 0.598))
    globe = vertical_gradient(
        (radius * 2, radius * 2),
        (92, 199, 255, 255),
        (11, 79, 201, 255),
    )
    mask = Image.new("L", (radius * 2, radius * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, radius * 2 - 1, radius * 2 - 1), fill=255)
    icon.alpha_composite(globe, (center[0] - radius, center[1] - radius), mask)

    overlay = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    stroke = (255, 255, 255, 150)
    width = max(2, int(size * 0.011))
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        outline=(255, 255, 255, 70),
        width=width,
    )
    for offset in (-0.45, 0.45):
        half_height = int(radius * 0.28)
        cy = center[1] + int(radius * offset)
        draw.ellipse(
            (center[0] - radius, cy - half_height, center[0] + radius, cy + half_height),
            outline=stroke,
            width=width,
        )
    for factor in (0.45, 0.0):
        half_width = int(radius * factor)
        if half_width:
            draw.ellipse(
                (
                    center[0] - half_width,
                    center[1] - radius,
                    center[0] + half_width,
                    center[1] + radius,
                ),
                outline=stroke,
                width=width,
            )
        else:
            draw.line(
                (center[0], center[1] - radius, center[0], center[1] + radius),
                fill=stroke,
                width=width,
            )
    draw.line(
        (center[0] - radius, center[1], center[0] + radius, center[1]),
        fill=stroke,
        width=width,
    )
    icon.alpha_composite(overlay)


def draw_pin(icon: Image.Image) -> None:
    size = icon.width
    cx = size // 2
    head_y = int(size * 0.372)
    head_r = int(size * 0.158)
    tip_y = int(size * 0.748)
    pin = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(pin)
    body = vertical_gradient(
        (head_r * 2, tip_y - (head_y - head_r) + 1),
        (86, 194, 255, 255),
        (10, 91, 220, 255),
    )
    body_mask = Image.new("L", body.size, 0)
    body_draw = ImageDraw.Draw(body_mask)
    body_draw.ellipse((0, 0, head_r * 2 - 1, head_r * 2 - 1), fill=255)
    body_draw.polygon(
        (
            (0, head_r),
            (head_r * 2, head_r),
            (head_r, body.size[1] - 1),
        ),
        fill=255,
    )
    pin.paste(body, (cx - head_r, head_y - head_r), body_mask)

    outline = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    outline_draw = ImageDraw.Draw(outline)
    ring_width = max(3, int(size * 0.017))
    outline_draw.ellipse(
        (cx - head_r, head_y - head_r, cx + head_r, head_y + head_r),
        outline=(255, 255, 255, 235),
        width=ring_width,
    )
    outline_draw.polygon(
        (
            (cx - head_r, head_y),
            (cx + head_r, head_y),
            (cx, tip_y),
        ),
        outline=(255, 255, 255, 235),
        width=ring_width,
    )
    icon.alpha_composite(outline)

    hole_r = int(head_r * 0.42)
    hole = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    ImageDraw.Draw(hole).ellipse(
        (cx - hole_r, head_y - hole_r, cx + hole_r, head_y + hole_r),
        fill=(255, 255, 255, 245),
    )
    icon.alpha_composite(hole)


def generate_icon() -> tuple[Path, Path]:
    size = 1024
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    background = vertical_gradient(
        (size, size),
        (255, 255, 255, 255),
        (214, 238, 255, 255),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(size * 0.219),
        fill=255,
    )
    canvas.paste(background, (0, 0), mask)

    draw_globe(canvas)
    draw_pin(canvas)

    ASSETS.mkdir(parents=True, exist_ok=True)
    png_path = ASSETS / "network-locator.png"
    ico_path = ASSETS / "network-locator.ico"
    canvas.resize((256, 256), Image.Resampling.LANCZOS).save(png_path)
    canvas.save(
        ico_path,
        format="ICO",
        sizes=[
            (16, 16),
            (20, 20),
            (24, 24),
            (32, 32),
            (40, 40),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    return png_path, ico_path


if __name__ == "__main__":
    png, ico = generate_icon()
    print(f"Generated: {png}")
    print(f"Generated: {ico}")