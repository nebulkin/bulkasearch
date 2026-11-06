"""Рисует карточку-картинку с ником: @example крупно, оценка, статус, цена."""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"
W, H = 1000, 560

# (верх градиента, низ градиента, акцент)
THEMES = {
    "top": ((96, 60, 255), (20, 12, 64), (255, 214, 90)),
    "strong": ((255, 128, 56), (78, 20, 44), (255, 232, 160)),
    "ok": ((28, 170, 160), (8, 48, 70), (190, 255, 240)),
    "weak": ((112, 122, 142), (28, 32, 44), (214, 218, 228)),
}
STATUS = {
    "free": ("СВОБОДЕН", (52, 199, 106)),
    "fragment": ("FRAGMENT", (58, 150, 255)),
    "taken": ("ЗАНЯТ", (232, 72, 72)),
}


def _font(size: int):
    try:
        return ImageFont.truetype(str(FONT), size)
    except OSError:
        return ImageFont.load_default(size)


def _theme(sc: int):
    return THEMES["top" if sc >= 75 else "strong" if sc >= 55 else "ok" if sc >= 35 else "weak"]


def _tier(sc: int) -> str:
    return "ТОП" if sc >= 75 else "СИЛЬНЫЙ" if sc >= 55 else "НОРМ" if sc >= 35 else "СЛАБЫЙ"


def render_card(u: str, sc: int, st: str, lo: int, hi: int) -> bytes:
    top, bot, accent = _theme(sc)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

    # мягкие круги для глубины
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.ellipse((-160, -200, 380, 340), fill=(255, 255, 255, 22))
    od.ellipse((640, 260, 1180, 800), fill=(255, 255, 255, 16))
    od.rounded_rectangle((40, 36, W - 40, H - 36), radius=36, outline=(255, 255, 255, 60), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), ov)
    d = ImageDraw.Draw(img)

    # статус (пилюля слева сверху)
    label, color = STATUS.get(st, ("НЕ ПРОВЕРЕН", (140, 146, 160)))
    f = _font(28)
    tw = d.textlength(label, font=f)
    d.rounded_rectangle((70, 66, 70 + tw + 56, 120), radius=27, fill=color)
    d.ellipse((88, 86, 102, 100), fill=(255, 255, 255))
    d.text((114, 93), label, font=f, fill=(255, 255, 255), anchor="lm")

    # оценка справа сверху
    d.text((W - 70, 93), f"{sc}/100 · {_tier(sc)}", font=_font(30), fill=accent, anchor="rm")

    # ник по центру, размер подбираем под ширину
    text = f"@{u}"
    size = 170
    while size > 40 and d.textlength(text, font=_font(size)) > W - 140:
        size -= 6
    f = _font(size)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.text((W // 2 + 4, 592 // 2 - 6 + 5), text, font=f, fill=(0, 0, 0, 70), anchor="mm")
    sd.rounded_rectangle((70, 400, W - 70, 418), radius=9, fill=(255, 255, 255, 50))
    sd.text((W // 2, 506), "оценка — эвристика, не котировка", font=_font(20), fill=(255, 255, 255, 120), anchor="mm")
    img = Image.alpha_composite(img, sh)
    d = ImageDraw.Draw(img)
    d.text((W // 2, 592 // 2 - 6), text, font=f, fill=(255, 255, 255), anchor="mm")

    # шкала оценки
    x0, x1, y = 70, W - 70, 400
    d.rounded_rectangle((x0, y, x0 + max(18, (x1 - x0) * sc // 100), y + 18), radius=9, fill=accent)

    d.text((x0, 452), f"≈ {lo}–{hi} TON", font=_font(34), fill=(255, 255, 255), anchor="lm")
    d.text((x1, 452), f"{len(u)} симв.", font=_font(30), fill=accent, anchor="rm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()
