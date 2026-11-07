"""Рисует карточку-картинку (v4): меш-градиент, «стекло», кольцо оценки, аватар владельца ника, зерно.
Два вида: обычная карточка ника и карточка-досье (если у ника есть владелец — profile).
Шрифт Inter лежит в fonts/ (InterDisplay-ExtraBold.otf, Inter-SemiBold.otf, Inter-Medium.otf).
Если файлов нет — рисуется запасным DejaVuSans-Bold.ttf, бот не падает."""
import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FONTS = Path(__file__).parent / "fonts"
W, H, SS = 1200, 675, 2  # размер итоговой картинки и коэффициент суперсэмплинга (рисуем в 2 раза крупнее)
FONT_FILES = {"xb": "InterDisplay-ExtraBold.otf", "sb": "Inter-SemiBold.otf", "md": "Inter-Medium.otf"}
FALLBACK = "DejaVuSans-Bold.ttf"

# base — тёмная основа, blobs — цветные пятна меш-градиента, a1/a2 — акценты (светлый и насыщенный)
THEMES = {
    "top": dict(base=(13, 9, 36), blobs=[(110, 70, 255), (255, 190, 70), (60, 120, 255)], a1=(255, 216, 110), a2=(255, 150, 60)),
    "strong": dict(base=(30, 9, 20), blobs=[(255, 110, 50), (225, 40, 115), (255, 185, 80)], a1=(255, 205, 135), a2=(255, 98, 72)),
    "ok": dict(base=(5, 23, 33), blobs=[(20, 190, 170), (30, 110, 225), (120, 255, 210)], a1=(140, 255, 228), a2=(60, 200, 255)),
    "weak": dict(base=(16, 18, 27), blobs=[(110, 120, 152), (70, 80, 112), (150, 160, 192)], a1=(214, 220, 232), a2=(140, 150, 178)),
}
STATUS = {
    "free": ("СВОБОДЕН", (52, 211, 120)),
    "fragment": ("FRAGMENT", (64, 156, 255)),
    "taken": ("ЗАНЯТ", (244, 80, 80)),
}
KINDS = {"user": "ПОЛЬЗОВАТЕЛЬ", "bot": "БОТ", "channel": "КАНАЛ", "group": "ГРУППА", "unknown": "АККАУНТ"}
BLUE, RED, GOLD = (64, 156, 255), (244, 80, 80), (255, 200, 80)


def S(v) -> int:
    return int(round(v * SS))


@lru_cache(maxsize=64)
def _font(kind: str, px: int):
    for name in (FONT_FILES[kind], FALLBACK):
        try:
            return ImageFont.truetype(str(FONTS / name), px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def F(kind: str, size: float):
    return _font(kind, S(size))


def _tier_key(sc: int) -> str:
    return "top" if sc >= 75 else "strong" if sc >= 55 else "ok" if sc >= 35 else "weak"


def _tier(sc: int) -> str:
    return "ТОП" if sc >= 75 else "СИЛЬНЫЙ" if sc >= 55 else "НОРМ" if sc >= 35 else "СЛАБЫЙ"


def fmt_num(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return f"{n:,}".replace(",", " ")


# ---------- фон ----------
def _mesh(th) -> Image.Image:
    """Меш-градиент: рисуем размытые пятна на крошечном холсте и плавно растягиваем."""
    w, h = 96, 54
    base = Image.new("RGBA", (w, h), th["base"] + (255,))
    spots = [(0.10, 0.12, 0.50, 0, 190), (0.92, 0.88, 0.55, 1, 170), (0.78, 0.06, 0.30, 2, 120), (0.46, 0.40, 0.34, 0, 110)]
    for fx, fy, fr, ci, a in spots:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        r = fr * w
        ImageDraw.Draw(layer).ellipse((fx * w - r, fy * h - r * 0.8, fx * w + r, fy * h + r * 0.8), fill=th["blobs"][ci] + (a,))
        base = Image.alpha_composite(base, layer.filter(ImageFilter.GaussianBlur(r * 0.45)))
    # виньетка: углы темнее
    vig = Image.radial_gradient("L").resize((w, h)).point(lambda v: int(v * 0.62))
    base.paste((0, 0, 0), (0, 0), vig)
    return base.convert("RGB").resize((W * SS, H * SS), Image.BICUBIC)


def _lin(size, c1, c2, horizontal=False) -> Image.Image:
    m = Image.linear_gradient("L").resize(size)
    if horizontal:
        m = m.rotate(90)
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), m)


def _glow(img, box, color, blur, strength=1.0):
    """Мягкое свечение: овал цвета color, размытый и подмешанный в картинку."""
    x0, y0, x1, y1 = [S(v) for v in box]
    pad = S(blur) * 3
    reg = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    m = Image.new("L", (reg[2] - reg[0], reg[3] - reg[1]), 0)
    ImageDraw.Draw(m).ellipse((pad, pad, pad + x1 - x0, pad + y1 - y0), fill=int(255 * strength))
    m = m.filter(ImageFilter.GaussianBlur(S(blur)))
    img.paste(Image.new("RGB", m.size, color), (reg[0], reg[1]), m)


# ---------- текст ----------
def _tw(d, text, font, track=0.0) -> float:
    return d.textlength(text, font=font) + S(track) * max(0, len(text) - 1)


def text(d, x, y, s, font, fill, anchor="l", track=0.0, vy="m"):
    """Текст с разрядкой. anchor: l/m/r по горизонтали; vy: m — по центру строки, s — по базовой линии."""
    x, y = S(x), S(y)
    if not track:
        d.text((x, y), s, font=font, fill=fill, anchor={"l": "l", "m": "m", "r": "r"}[anchor] + vy)
        return
    total = _tw(d, s, font, track)
    cur = x - (total / 2 if anchor == "m" else total if anchor == "r" else 0)
    for ch in s:
        d.text((cur, y), ch, font=font, fill=fill, anchor="l" + vy)
        cur += d.textlength(ch, font=font) + S(track)


def fit(d, s, kind, size, max_w, min_size=24):
    while size > min_size and d.textlength(s, font=F(kind, size)) > S(max_w):
        size -= 2
    return size


def clip(d, s, font, max_w):
    if d.textlength(s, font=font) <= S(max_w):
        return s
    while len(s) > 1 and d.textlength(s + "…", font=font) > S(max_w):
        s = s[:-1]
    return s.rstrip() + "…"


def chip(d, x, y, s, color, align="l", filled=False):
    """Пилюля-метка. Возвращает её ширину (в базовых единицах)."""
    f = F("sb", 17)
    w = _tw(d, s, f, 1.6) / SS + 32
    x0 = x - w / 2 if align == "m" else x
    box = (S(x0), S(y - 20), S(x0 + w), S(y + 20))
    d.rounded_rectangle(box, radius=S(20), fill=color + ((235,) if filled else (34,)), outline=color + (190,), width=S(1.6))
    text(d, x0 + w / 2, y, s, f, (255, 255, 255, 255) if filled else color + (255,), "m", track=1.6)
    return w


# ---------- фигуры ----------
def glass(img, box, radius=30, blur=14):
    """«Стекло»: размытый фон под панелью, лёгкая заливка и светящийся край."""
    x0, y0, x1, y1 = [S(v) for v in box]
    m = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=S(radius), fill=255)
    img.paste(img.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(S(blur))), (x0, y0), m)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((x0, y0, x1, y1), radius=S(radius), fill=(255, 255, 255, 22), outline=(255, 255, 255, 52), width=S(1.6))
    d.line((x0 + S(radius), y0 + S(1.5), x1 - S(radius), y0 + S(1.5)), fill=(255, 255, 255, 90), width=S(1.2))


def ring(img, cx, cy, r, sc, a1, a2, width=13):
    """Кольцо оценки с градиентом и скруглёнными концами."""
    box = (S(cx - r - width), S(cy - r - width), S(cx + r + width), S(cy + r + width))
    size = (box[2] - box[0], box[3] - box[1])
    c = (size[0] // 2, size[1] // 2)
    rr = S(r)
    track = Image.new("L", size, 0)
    ImageDraw.Draw(track).ellipse((c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr), outline=255, width=S(width))
    img.paste((255, 255, 255), (box[0], box[1]), track.point(lambda v: int(v * 0.16)))
    if sc <= 0:
        return
    sc = max(sc, 3)
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    end = -90 + 360 * sc / 100
    md.arc((c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr), -90, end, fill=255, width=S(width))
    for ang in (-90, end):  # круглые концы
        px, py = c[0] + rr * math.cos(math.radians(ang)), c[1] + rr * math.sin(math.radians(ang))
        md.ellipse((px - S(width) / 2, py - S(width) / 2, px + S(width) / 2, py + S(width) / 2), fill=255)
    # свечение под дугой
    img.paste(Image.new("RGB", size, a2), (box[0], box[1]), mask.filter(ImageFilter.GaussianBlur(S(9))).point(lambda v: int(v * 0.5)))
    img.paste(_lin(size, a1, a2), (box[0], box[1]), mask)


def avatar(img, cx, cy, d_px, photo, name, a1, a2):
    """Круглый аватар владельца ника с градиентной обводкой; если фото нет — буква на градиенте."""
    r = d_px / 2
    _glow(img, (cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6), a2, 26, 0.55)
    ring_w = 6
    size = S(d_px + ring_w * 2 + 6)
    box = (S(cx) - size // 2, S(cy) - size // 2)
    outer = Image.new("L", (size, size), 0)
    ImageDraw.Draw(outer).ellipse((0, 0, size - 1, size - 1), fill=255)
    img.paste(_lin((size, size), a1, a2), box, outer)
    inner_d = S(d_px)
    pic = None
    if photo:
        try:
            pic = Image.open(io.BytesIO(photo)).convert("RGB")
            side = min(pic.size)
            pic = pic.crop(((pic.width - side) // 2, (pic.height - side) // 2, (pic.width + side) // 2, (pic.height + side) // 2))
            pic = pic.resize((inner_d, inner_d), Image.LANCZOS)
        except Exception:
            pic = None
    if pic is None:
        pic = _lin((inner_d, inner_d), (28, 30, 46), (52, 56, 84))
        pd = ImageDraw.Draw(pic)
        letter = (name or "@").strip()[:1].upper() or "@"
        pd.text((inner_d // 2, inner_d // 2), letter, font=F("xb", d_px * 0.5), fill=(255, 255, 255), anchor="mm")
    m = Image.new("L", (inner_d, inner_d), 0)
    ImageDraw.Draw(m).ellipse((0, 0, inner_d - 1, inner_d - 1), fill=255)
    # тёмный зазор между обводкой и фото
    gap = inner_d + S(6)
    gm = Image.new("L", (gap, gap), 0)
    ImageDraw.Draw(gm).ellipse((0, 0, gap - 1, gap - 1), fill=255)
    img.paste((14, 14, 22), (S(cx) - gap // 2, S(cy) - gap // 2), gm)
    img.paste(pic, (S(cx) - inner_d // 2, S(cy) - inner_d // 2), m)


def verified_badge(d, cx, cy, r=13):
    d.ellipse((S(cx - r), S(cy - r), S(cx + r), S(cy + r)), fill=BLUE + (255,))
    pts = [(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.5, cy - r * 0.34)]
    d.line([(S(x), S(y)) for x, y in pts], fill=(255, 255, 255, 255), width=S(2.6), joint="curve")


def gradient_text(img, x, y, s, font, c1, c2, anchor="ls"):
    """Текст с вертикальным градиентом (белый → оттенок темы). Возвращает ширину."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text((S(x), S(y)), s, font=font, fill=255, anchor=anchor)
    bb = mask.getbbox()
    if not bb:
        return
    pad = S(8)
    bb = (max(0, bb[0] - pad), max(0, bb[1] - pad), min(img.width, bb[2] + pad), min(img.height, bb[3] + pad))
    m = mask.crop(bb)
    img.paste(_lin(m.size, c1, c2), (bb[0], bb[1]), m)


def _glow_text(img, x, y, s, font, color, blur=22, alpha=0.5, anchor="ls"):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text((S(x), S(y)), s, font=font, fill=255, anchor=anchor)
    bb = mask.getbbox()
    if not bb:
        return
    pad = S(blur) * 3
    bb = (max(0, bb[0] - pad), max(0, bb[1] - pad), min(img.width, bb[2] + pad), min(img.height, bb[3] + pad))
    m = mask.crop(bb).filter(ImageFilter.GaussianBlur(S(blur))).point(lambda v: int(v * alpha))
    img.paste(Image.new("RGB", m.size, color), (bb[0], bb[1]), m)


def _mix(c, t, k):
    return tuple(round(c[i] + (t[i] - c[i]) * k) for i in range(3))


# ---------- главная функция ----------
def render_card(u: str, sc: int, st: str, lo: int, hi: int, profile: dict | None = None, tags: list | None = None) -> bytes:
    """profile — данные владельца ника (из lookup.profile): kind, name, verified, scam, fake, premium, members, photo.
    tags — короткие метки под ником («слово», «палиндром»…). Возвращает JPEG."""
    th = THEMES[_tier_key(sc)]
    a1, a2 = th["a1"], th["a2"]
    img = _mesh(th)
    d = ImageDraw.Draw(img, "RGBA")

    # тонкая сетка из точек
    for gy in range(36, H, 38):
        for gx in range(36, W, 38):
            d.ellipse((S(gx) - 1, S(gy) - 1, S(gx) + 2, S(gy) + 2), fill=(255, 255, 255, 13))

    # ---- верхняя полоса: логотип + статус ----
    lg = _lin((S(46), S(46)), a1, a2)
    lm = Image.new("L", lg.size, 0)
    ImageDraw.Draw(lm).rounded_rectangle((0, 0, lg.width - 1, lg.height - 1), radius=S(14), fill=255)
    img.paste(lg, (S(64), S(46)), lm)
    d = ImageDraw.Draw(img, "RGBA")
    text(d, 87, 69, "B", F("xb", 28), (20, 14, 30, 255), "m")
    text(d, 124, 69, "BULKASEARCH", F("sb", 20), (255, 255, 255, 235), "l", track=3.2)

    label, scol = STATUS.get(st, ("НЕ ПРОВЕРЕН", (146, 152, 168)))
    pf = F("sb", 21)
    pw = _tw(d, label, pf, 2.0) / SS + 70
    px1, px0 = 1136, 1136 - pw
    _glow(img, (px0, 48, px1, 92), scol, 16, 0.35)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((S(px0), S(46), S(px1), S(92)), radius=S(23), fill=scol + (40,), outline=scol + (200,), width=S(1.8))
    for rr, al in ((11, 50), (7, 110), (4.6, 255)):
        d.ellipse((S(px0 + 28 - rr), S(69 - rr), S(px0 + 28 + rr), S(69 + rr)), fill=scol + (al,))
    text(d, px0 + 48, 69, label, pf, (255, 255, 255, 255), "l", track=2.0)

    # ---- центральный блок ----
    if profile:
        _center_profile(img, u, profile, a1, a2)
    else:
        _center_plain(img, u, sc, a1, a2, tags)
    d = ImageDraw.Draw(img, "RGBA")

    # ---- нижняя стеклянная панель ----
    glass(img, (64, 452, 1136, 612), radius=34)
    d = ImageDraw.Draw(img, "RGBA")
    for x in (440, 800):
        d.line((S(x), S(480), S(x), S(584)), fill=(255, 255, 255, 36), width=S(1.4))

    ring(img, 158, 532, 52, sc, a1, a2, width=11)
    d = ImageDraw.Draw(img, "RGBA")
    text(d, 158, 527, str(sc), F("xb", 36), (255, 255, 255, 255), "m")
    text(d, 158, 552, "/100", F("md", 13), (255, 255, 255, 140), "m")
    text(d, 236, 506, "ОЦЕНКА", F("sb", 15), (255, 255, 255, 140), "l", track=2.4)
    text(d, 236, 541, _tier(sc), F("xb", 34), a1 + (255,), "l")
    text(d, 236, 573, "ник по шкале 0–100", F("md", 16), (255, 255, 255, 130), "l")

    text(d, 480, 506, "ОРИЕНТИР ЦЕНЫ", F("sb", 15), (255, 255, 255, 140), "l", track=2.4)
    val = f"≈ {lo}–{hi}"
    vs = fit(d, val, "xb", 46, 230, 26)
    text(d, 480, 548, val, F("xb", vs), (255, 255, 255, 255), "l")
    text(d, 480 + d.textlength(val, font=F("xb", vs)) / SS + 12, 551, "TON", F("xb", 22), a1 + (255,), "l")
    text(d, 480, 580, "эвристика, не котировка", F("md", 15), (255, 255, 255, 115), "l")

    lab, big, small = "ДЛИНА", str(len(u)), "симв."
    if profile and profile.get("members") is not None:
        k = profile.get("kind")
        lab = {"channel": "ПОДПИСЧИКОВ", "group": "УЧАСТНИКОВ", "bot": "ПОЛЬЗОВАТЕЛЕЙ / МЕС"}.get(k, "ДЛИНА")
        if lab != "ДЛИНА":
            big, small = fmt_num(profile["members"]), ""
    elif profile and profile.get("kind") == "user":
        lab, big, small = "АККАУНТ", "Premium" if profile.get("premium") else "Обычный", ""
    text(d, 840, 506, lab, F("sb", 15), (255, 255, 255, 140), "l", track=2.4)
    bs = fit(d, big, "xb", 46, 270, 26)
    text(d, 840, 548, big, F("xb", bs), (255, 255, 255, 255), "l")
    if small:
        text(d, 840 + d.textlength(big, font=F("xb", bs)) / SS + 10, 551, small, F("sb", 22), a1 + (255,), "l")
    if profile and profile.get("members") is None and profile.get("kind") in ("channel", "group"):
        text(d, 840, 580, "число скрыто", F("md", 15), (255, 255, 255, 115), "l")

    foot = "BULKASEARCH  ·  публичные данные Telegram" if profile else "BULKASEARCH  ·  свежие юзернеймы — горячими"
    text(d, 600, 640, foot, F("md", 14), (255, 255, 255, 95), "m", track=1.2)

    out = img.resize((W, H), Image.LANCZOS)
    out = Image.blend(out, Image.effect_noise((W, H), 56).convert("RGB"), 0.028)  # лёгкое зерно против бандинга
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=93, subsampling=0, optimize=True)
    return buf.getvalue()


def _nick(img, u, cx_or_x, base_y, max_w, max_size, a1, center=True):
    """Рисует «@ник» крупно с градиентом и свечением. Возвращает (левый край, правый край)."""
    d = ImageDraw.Draw(img, "RGBA")
    size = fit(d, u, "xb", max_size, max_w - 70, 40)
    fn, fa = F("xb", size), F("xb", size * 0.6)
    wa, wn = d.textlength("@", font=fa) / SS, d.textlength(u, font=fn) / SS
    gap = size * 0.06
    x0 = cx_or_x - (wa + gap + wn) / 2 if center else cx_or_x
    _glow_text(img, x0 + wa + gap, base_y, u, fn, a1, 24, 0.42)
    d = ImageDraw.Draw(img, "RGBA")
    text(d, x0, base_y, "@", fa, a1 + (210,), "l", vy="s")
    gradient_text(img, x0 + wa + gap, base_y, u, fn, (255, 255, 255), _mix((255, 255, 255), a1, 0.35))
    return x0, x0 + wa + gap + wn


def _center_plain(img, u, sc, a1, a2, tags):
    _glow(img, (200, 150, 1000, 400), a2, 70, 0.22)
    d = ImageDraw.Draw(img, "RGBA")
    _nick(img, u, 600, 300, 1030, 200, a1)
    d = ImageDraw.Draw(img, "RGBA")
    chips = [f"{len(u)} СИМВ."] + [t.upper() for t in (tags or [])][:3]
    f = F("sb", 17)
    widths = [_tw(d, c, f, 1.6) / SS + 32 for c in chips]
    total = sum(widths) + 12 * (len(chips) - 1)
    x = 600 - total / 2
    for c, w in zip(chips, widths):
        chip(d, x, 372, c, a1)
        x += w + 12


def _center_profile(img, u, p, a1, a2):
    d = ImageDraw.Draw(img, "RGBA")
    avatar(img, 176, 262, 176, p.get("photo"), p.get("name"), a1, a2)
    d = ImageDraw.Draw(img, "RGBA")
    x0 = 300
    _nick(img, u, x0, 236, 1136 - x0 + 70, 104, a1, center=False)
    d = ImageDraw.Draw(img, "RGBA")
    name = p.get("name") or "Без имени"
    nf = F("sb", 34)
    badge = 40 if p.get("verified") else 0
    name = clip(d, name, nf, 1136 - x0 - badge)
    text(d, x0, 296, name, nf, (255, 255, 255, 240), "l")
    if p.get("verified"):
        verified_badge(d, x0 + d.textlength(name, font=nf) / SS + 24, 297)
    x, y = x0, 354
    x += chip(d, x, y, KINDS.get(p.get("kind"), "АККАУНТ"), a1) + 10
    if p.get("verified"):
        x += chip(d, x, y, "ПРОВЕРЕН", BLUE) + 10
    if p.get("premium"):
        x += chip(d, x, y, "PREMIUM", GOLD) + 10
    if p.get("scam"):
        x += chip(d, x, y, "SCAM", RED, filled=True) + 10
    if p.get("fake"):
        x += chip(d, x, y, "FAKE", RED, filled=True) + 10
    if p.get("restricted"):
        x += chip(d, x, y, "ОГРАНИЧЕН", (255, 150, 60)) + 10
