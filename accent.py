"""The app's accent: a green to purple gradient, the logo's two colors, wherever the stock blue was.

Tk draws text, lines and fills in one color, so the gradient is faked where it can't be real:
text a letter at a time, lines in short pieces, outlines and badges as images.
"""
import os

GREEN, PURPLE = (30, 190, 100), (140, 45, 235)
SOLID = "#7a3fd6"  # for the few places that can only be one color (a selected row's background)


def rgb(t):
    t = min(1.0, max(0.0, t))
    return tuple(int(GREEN[i] + (PURPLE[i] - GREEN[i]) * t) for i in range(3))


def hex_at(t):
    return "#%02x%02x%02x" % rgb(t)


def text(canvas, x, y, string, font, anchor="nw", tags=()):
    """string in the gradient, a character at a time. anchor is the whole string's, as for create_text"""
    total = max(1, font.measure(string))
    if anchor.endswith("w"):
        left = x
    elif anchor.endswith("e"):
        left = x - total
    else:
        left = x - total / 2
    vertical = anchor[0] if anchor[0] in "ns" and anchor != "center" else ""
    ids = []
    for i, ch in enumerate(string):
        if ch == " ":
            continue
        start = font.measure(string[:i])
        mid = start + font.measure(ch) / 2
        ids.append(canvas.create_text(left + start, y, text=ch, font=font, anchor=vertical + "w", tags=tags,
                                      fill=hex_at(mid / total)))
    return ids


def hline(canvas, x0, x1, y, width=3, tags=()):
    n = max(1, int((x1 - x0) // 4))
    for i in range(n):
        a, b = x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n + 1
        canvas.create_line(a, y, b, y, fill=hex_at((i + 0.5) / n), width=width, tags=tags)


def gradient_image(w, h):
    """a w x h RGBA image, green on the left to purple on the right"""
    from PIL import Image
    row = Image.new("RGBA", (w, 1))
    for x in range(w):
        row.putpixel((x, 0), rgb(x / max(1, w - 1)) + (255,))
    return row.resize((w, h))


_images = {}


def outline_pill(master, w, h, radius, width=1.5):
    """an outlined rounded rectangle, the outline in the gradient (transparent inside); cached"""
    key = ("pill", w, h, radius, width)
    if key not in _images:
        from PIL import Image, ImageDraw, ImageTk
        big = 4
        mask = Image.new("L", (w * big, h * big), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, w * big - 1, h * big - 1), radius=radius * big, fill=255)
        inset = int(width * big)
        draw.rounded_rectangle((inset, inset, w * big - 1 - inset, h * big - 1 - inset),
                               radius=max(0, radius * big - inset), fill=0)
        image = gradient_image(w * big, h * big)
        image.putalpha(mask)
        _images[key] = ImageTk.PhotoImage(image.resize((w, h), Image.LANCZOS), master=master)
    return _images[key]


def filled_box(master, w, h, radius):
    key = ("box", w, h, radius)
    if key not in _images:
        from PIL import Image, ImageDraw, ImageTk
        big = 4
        mask = Image.new("L", (w * big, h * big), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * big - 1, h * big - 1), radius=radius * big, fill=255)
        image = gradient_image(w * big, h * big)
        image.putalpha(mask)
        _images[key] = ImageTk.PhotoImage(image.resize((w, h), Image.LANCZOS), master=master)
    return _images[key]


def tip_image(master, string, size_pt=9, pad=(8, 3)):
    """a small label: white bold text on a rounded gradient, for the drag tooltip"""
    key = ("tip", string)
    if key not in _images:
        from PIL import Image, ImageDraw, ImageFont, ImageTk
        scaling = float(master.tk.call("tk", "scaling"))
        px = max(8, round(size_pt * scaling))
        try:
            font = ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf"), px)
        except OSError:
            font = ImageFont.load_default()
        left, top, right, bottom = font.getbbox(string)
        w, h = right - left + 2 * pad[0], (bottom - top) + 2 * pad[1] + 4
        image = gradient_image(w, h)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=4, fill=255)
        image.putalpha(mask)
        ImageDraw.Draw(image).text((pad[0] - left, pad[1] + 2 - top), string, font=font, fill=(255, 255, 255, 255))
        _images[key] = ImageTk.PhotoImage(image, master=master)
    return _images[key]
