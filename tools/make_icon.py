#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 BiliDown 应用图标（开发期工具，不参与打包）。

在 1024x1024 上绘制再降采样，保证 16x16 也清晰：
青色渐变圆角方底 + 白色下载箭头 + 底部托盘。
"""
from pathlib import Path

from PIL import Image, ImageDraw

SS = 1024                      # 超采样尺寸
OUT = Path(__file__).resolve().parent.parent / "app.ico"
TOP = (0x00, 0xC6, 0xF4)       # #00c6f4
BOTTOM = (0x00, 0x6E, 0xB8)    # #006eb8


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    return mask


def gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)],
               fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)))
    return img


def build() -> Image.Image:
    mask = rounded_mask(SS, int(SS * 0.22))
    base = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    base.paste(gradient(SS), (0, 0), mask)

    # 内部高光，让图标不至于太平
    gloss = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse(
        (-SS * 0.35, -SS * 0.75, SS * 1.35, SS * 0.42),
        fill=(255, 255, 255, 26))
    base = Image.alpha_composite(base, Image.composite(
        gloss, Image.new("RGBA", (SS, SS), (0, 0, 0, 0)), mask))

    layer = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    white = (255, 255, 255, 255)

    # 竖杆
    d.rounded_rectangle((SS * 0.435, SS * 0.20, SS * 0.565, SS * 0.53),
                        radius=SS * 0.045, fill=white)
    # 箭头
    d.polygon([(SS * 0.285, SS * 0.455), (SS * 0.715, SS * 0.455),
               (SS * 0.5, SS * 0.735)], fill=white)
    # 托盘
    d.rounded_rectangle((SS * 0.225, SS * 0.775, SS * 0.775, SS * 0.865),
                        radius=SS * 0.045, fill=white)

    out = Image.alpha_composite(base, layer)
    return out.resize((256, 256), Image.LANCZOS)


def main() -> None:
    img = build()
    img.save(OUT, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    png = OUT.with_suffix(".png")
    img.save(png)
    print(f"icon written: {OUT} ({OUT.stat().st_size} bytes)")
    print(f"preview     : {png}")


if __name__ == "__main__":
    main()
