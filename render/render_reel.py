#!/usr/bin/env python3
"""台本JSONから、縦型（1080x1920）の文字・モーショングラフィックス型リール動画を作る。

外部API・ストック素材を使わないため、台本と映像のずれが起きず、費用もかからない。
使い方:  python render_reel.py script.json 出力フォルダ
出力:    reel.mp4 / cover.png / frames/*.png（確認用の静止画）/ qa.json / caption.txt
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
# Instagramの画面UI（上部ラベル、下部のキャプション・右側のボタン）に隠れない範囲
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 80, 1000, 230, 1450
SAFE_RIGHT_LOW = 930          # 画面下半分（右側にいいね等のボタンがある）の右端
LOW_ZONE_Y = 1100
NO_LINE_START = "、。，．・：；？！ー」』）】〉》…％"

DEFAULT_THEME = {
    "bg_top": "#0f1b2d", "bg_bottom": "#1d3557", "text": "#ffffff", "sub": "#c9d6ea",
    "accent": "#21c87a", "mark": "#ffd166", "warn": "#ff6b6b", "card": "#ffffff",
    "card_text": "#1b2433", "grid": "#d5dbe5", "header": "#eef1f6",
}

# ---------------------------------------------------------------- fonts
_FONT_CACHE: dict = {}


def _font_candidates(weight: str) -> list[tuple[str, int]]:
    linux = [(f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc", 0),
             (f"/usr/share/fonts/noto-cjk/NotoSansCJK-{weight}.ttc", 0)]
    win_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    bold = weight in ("Bold", "Black")
    windows = [(str(win_dir / ("YuGothB.ttc" if bold else "YuGothM.ttc")), 0),
               (str(win_dir / ("meiryob.ttc" if bold else "meiryo.ttc")), 0)]
    return linux + windows


def font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key not in _FONT_CACHE:
        for path, index in _font_candidates(weight):
            if Path(path).is_file():
                _FONT_CACHE[key] = ImageFont.truetype(path, size, index=index)
                break
        else:
            raise RuntimeError("日本語フォントが見つかりません（Noto Sans CJK / 游ゴシック / メイリオ）")
    return _FONT_CACHE[key]


# ---------------------------------------------------------------- helpers
def rgb(color: str, alpha: int | None = None):
    color = color.lstrip("#")
    value = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    return value + (alpha,) if alpha is not None else value


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease_out(x: float) -> float:
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_back(x: float) -> float:
    x = clamp(x)
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def progress(t: float, start: float, length: float = 0.35) -> float:
    return clamp((t - start) / length)


def parse_marked(text: str) -> list[tuple[str, bool]]:
    """*強調* を (文字, 強調するか) の列に変換する。"""
    chars, emph = [], False
    for ch in text:
        if ch == "*":
            emph = not emph
            continue
        chars.append((ch, emph))
    return chars


def wrap(chars: list[tuple[str, bool]], fnt, max_w: float) -> list[list[tuple[str, bool]]]:
    lines, cur = [], []
    for item in chars:
        ch = item[0]
        if ch == "\n":
            lines.append(cur)
            cur = []
            continue
        width = fnt.getlength("".join(c for c, _ in cur) + ch)
        if width <= max_w or not cur:
            cur.append(item)
        elif ch in NO_LINE_START:          # 句読点で行を始めない
            carry = cur[-1]
            lines.append(cur[:-1])
            cur = [carry, item]
        else:
            lines.append(cur)
            cur = [item]
    lines.append(cur)
    return [ln for ln in lines if ln] or [[]]


class Ctx:
    """1本分の描画状態（警告・効果音の記録を含む）。"""

    def __init__(self, theme: dict):
        self.theme = {**DEFAULT_THEME, **(theme or {})}
        self.warnings: list[str] = []
        self.sfx: list[tuple[float, str]] = []
        self._fit_cache: dict = {}

    def fit(self, text: str, max_w: int, max_h: int, size: int, min_size: int,
            weight: str = "Bold", spacing: float = 1.25, max_lines: int = 4, label: str = ""):
        key = (text, max_w, max_h, size, min_size, weight, spacing, max_lines)
        if key in self._fit_cache:
            return self._fit_cache[key]
        chars = parse_marked(text)
        s = size
        while True:
            fnt = font(s, weight)
            lines = wrap(chars, fnt, max_w)
            height = len(lines) * s * spacing
            explicit = text.count("\n") + 1
            no_autowrap = "\n" not in text or len(lines) == explicit
            if (height <= max_h and len(lines) <= max_lines and no_autowrap) or s <= min_size:
                break
            s -= 2
        if height > max_h or len(lines) > max_lines:
            self.warnings.append(f"文字が枠に収まりません: {label or text[:20]}（{len(lines)}行）")
        elif "\n" in text and len(lines) != text.count("\n") + 1:
            self.warnings.append(f"指定した改行位置以外でも折り返しています: {label or text[:20]}")
        result = (fnt, lines, s)
        self._fit_cache[key] = result
        return result


def draw_lines(img: Image.Image, ctx: Ctx, fnt, lines, size: int, cx: float, top: float,
               color, emph_color, spacing: float = 1.25, align: str = "center",
               left: float | None = None, reveal: float = 1.0, stagger: float = 0.0,
               t: float = 0.0, start: float = 0.0, pop: bool = False, marker: str | None = None):
    """行ごとに描画。pop=Trueなら行ごとに弾むように出す。"""
    for i, line in enumerate(lines):
        p = 1.0 if not (pop or stagger) else progress(t, start + i * stagger, 0.32)
        if p <= 0:
            continue
        text = "".join(c for c, _ in line)
        lw = fnt.getlength(text)
        lh = int(size * spacing)
        layer = Image.new("RGBA", (int(lw) + 40, lh + 20), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        x = 20
        for ch, emph in line:
            if emph and marker:
                cw = fnt.getlength(ch)
                d.rectangle([x - 2, lh * 0.55, x + cw + 2, lh * 0.95], fill=rgb(marker, 150))
            x += fnt.getlength(ch)
        x = 20
        for ch, emph in line:
            d.text((x + 3, 13), ch, font=fnt, fill=(0, 0, 0, 90))
            d.text((x, 10), ch, font=fnt, fill=emph_color if emph else color)
            x += fnt.getlength(ch)
        if pop:
            scale = 0.6 + 0.4 * ease_back(p)
            layer = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))),
                                 Image.LANCZOS)
        if p < 1:
            alpha = layer.split()[3].point(lambda a: int(a * clamp(p * 1.6)))
            layer.putalpha(alpha)
        y = top + i * lh + (lh - layer.height) / 2 + (0 if pop else (1 - ease_out(p)) * 30)
        if align == "center":
            x0 = cx - layer.width / 2
        else:
            x0 = (left or SAFE_LEFT) - 20
        img.paste(layer, (int(x0), int(y)), layer)
    return top + len(lines) * size * spacing


def text_box(fnt, lines, cx: float, top: float, end: float):
    """中央揃えテキストの実際の外枠。"""
    w = max((fnt.getlength("".join(c for c, _ in ln)) for ln in lines), default=0)
    return [cx - w / 2, top, cx + w / 2, end]


def rounded(d: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def check_box(ctx: Ctx, box, label: str):
    x0, y0, x1, y1 = box
    right_limit = SAFE_RIGHT_LOW if y1 > LOW_ZONE_Y else SAFE_RIGHT
    if x0 < SAFE_LEFT - 10 or x1 > right_limit + 10 or y0 < SAFE_TOP - 10 or y1 > SAFE_BOTTOM + 10:
        msg = f"表示が安全領域の外にはみ出ています: {label}"
        if not any(w.startswith(msg) for w in ctx.warnings):
            ctx.warnings.append(f"{msg} {tuple(int(v) for v in box)}")


# ---------------------------------------------------------------- background
_BG_CACHE: dict = {}


def background(ctx: Ctx, t: float) -> Image.Image:
    key = (ctx.theme["bg_top"], ctx.theme["bg_bottom"])
    if key not in _BG_CACHE:
        top, bottom = np.array(rgb(key[0]), float), np.array(rgb(key[1]), float)
        ramp = np.linspace(0, 1, H)[:, None, None]
        arr = (top * (1 - ramp) + bottom * ramp).astype(np.uint8)
        arr = np.repeat(arr, W, axis=1)
        _BG_CACHE[key] = Image.fromarray(arr, "RGB")
    img = _BG_CACHE[key].copy()
    d = ImageDraw.Draw(img, "RGBA")
    # ゆっくり動く装飾の円（単調さを避ける）
    for i, (bx, by, r) in enumerate([(880, 380, 260), (160, 1560, 320), (980, 1350, 180)]):
        ox = math.sin(t * 0.6 + i * 2.1) * 30
        oy = math.cos(t * 0.5 + i * 1.3) * 30
        d.ellipse([bx + ox - r, by + oy - r, bx + ox + r, by + oy + r], fill=rgb(ctx.theme["accent"], 18))
    return img


def series_badge(img, ctx: Ctx, text: str, t: float):
    if not text:
        return
    p = ease_out(progress(t, 0.0, 0.4))
    fnt = font(40, "Bold")
    w = fnt.getlength(text) + 56
    d = ImageDraw.Draw(img, "RGBA")
    x0, y0 = SAFE_LEFT, SAFE_TOP + 10 - (1 - p) * 30
    rounded(d, [x0, y0, x0 + w, y0 + 70], 35, fill=rgb(ctx.theme["accent"], int(255 * p)))
    d.text((x0 + 28, y0 + 9), text, font=fnt, fill=(255, 255, 255, int(255 * p)))


def progress_bar(img, ctx: Ctx, t: float, total: float):
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, W, 10], fill=(255, 255, 255, 40))
    d.rectangle([0, 0, W * clamp(t / total), 10], fill=rgb(ctx.theme["accent"], 230))


# ---------------------------------------------------------------- scenes
def scene_hook(img, ctx: Ctx, sc: dict, t: float):
    th = ctx.theme
    prev = sc.get("_preview")
    if prev is not None:
        # 完成したシート画面を背景に薄く出し、「文字だけの動画」にしない
        if "_preview_img" not in sc:
            tmp_ctx = Ctx(ctx.theme)
            layer = background(tmp_ctx, 0).copy()
            scene_sheet(layer, tmp_ctx, {**prev, "title": None, "note": None, "_start": 0}, 99.0)
            sc["_preview_img"] = layer.crop((0, SAFE_TOP + 60, W, SAFE_TOP + 60 + 1100))
        zoom = 1.0 + 0.04 * clamp(t / max(sc["dur"], 0.1))
        pv = sc["_preview_img"]
        pv = pv.resize((int(pv.width * zoom), int(pv.height * zoom)), Image.BILINEAR)
        img.paste(pv, (int((W - pv.width) / 2), int(SAFE_TOP + 300 - (pv.height - 1100) / 2)))
        d0 = ImageDraw.Draw(img, "RGBA")
        d0.rectangle([0, 0, W, H], fill=rgb(th["bg_top"], 200))
    fnt, lines, size = ctx.fit(sc["text"], 860, 620, sc.get("size", 112), 64, "Black", 1.22, 4, "冒頭")
    block_h = len(lines) * size * 1.22
    top = 820 - block_h / 2
    sub_h = 0
    if sc.get("sub"):
        _f, _l, _s = ctx.fit(sc["sub"], 820, 220, 56, 40, "Bold", 1.3, 3, "冒頭サブ")
        sub_h = 50 + len(_l) * _s * 1.3
    if prev is not None:
        # 文字の後ろに暗い帯を敷き、背景の表と重ならないようにする
        pb = ease_out(progress(t, 0.0, 0.25))
        d1 = ImageDraw.Draw(img, "RGBA")
        rounded(d1, [SAFE_LEFT - 20, top - 50, SAFE_RIGHT + 20 - 80, top + block_h + sub_h + 40], 36,
                fill=rgb(th["bg_top"], int(235 * pb)))
    end = draw_lines(img, ctx, fnt, lines, size, W / 2, top, rgb(th["text"]), rgb(th["mark"]),
                     1.22, t=t, start=0.1, pop=True, stagger=0.14)
    if t > 1.2:
        check_box(ctx, text_box(fnt, lines, W / 2, top, end), "冒頭テキスト")
    for i in range(len(lines)):
        ctx.sfx.append((sc["_start"] + 0.1 + i * 0.14, "pop"))
    if sc.get("sub"):
        f2, l2, s2 = ctx.fit(sc["sub"], 820, 220, 56, 40, "Bold", 1.3, 3, "冒頭サブ")
        draw_lines(img, ctx, f2, l2, s2, W / 2, end + 50, rgb(th["sub"]), rgb(th["mark"]), 1.3,
                   t=t, start=0.25 + len(lines) * 0.14, stagger=0.1)


def scene_points(img, ctx: Ctx, sc: dict, t: float):
    """番号付きの要点を順番に出す。"""
    th = ctx.theme
    y = SAFE_TOP + 130
    if sc.get("title"):
        f, l, s = ctx.fit(sc["title"], 840, 240, 72, 48, "Black", 1.25, 3, "要点タイトル")
        y = draw_lines(img, ctx, f, l, s, W / 2, y, rgb(th["text"]), rgb(th["mark"]), 1.25, t=t, start=0.05,
                       stagger=0.1) + 40
    items = sc["items"]
    gap = sc.get("item_gap", 0.9)
    d = ImageDraw.Draw(img, "RGBA")
    for i, item in enumerate(items):
        st = 0.5 + i * gap
        p = ease_out(progress(t, st, 0.35))
        if p <= 0:
            y += 190
            continue
        f, l, s = ctx.fit(item, 640, 150, 52, 38, "Bold", 1.3, 3, f"要点{i + 1}")
        card_h = max(150, len(l) * s * 1.3 + 50)
        x0 = SAFE_LEFT + (1 - p) * 120
        box = [x0, y, x0 + 850, y + card_h]
        rounded(d, box, 28, fill=rgb(th["card"], int(240 * p)))
        d.ellipse([x0 + 28, y + card_h / 2 - 44, x0 + 116, y + card_h / 2 + 44], fill=rgb(th["accent"], int(255 * p)))
        nf = font(54, "Black")
        num = str(i + 1)
        d.text((x0 + 72 - nf.getlength(num) / 2, y + card_h / 2 - 40), num, font=nf, fill=(255, 255, 255, int(255 * p)))
        draw_lines(img, ctx, f, l, s, 0, y + 25, rgb(th["card_text"]), rgb(th["accent"]), 1.3,
                   align="left", left=x0 + 150)
        if p >= 1:
            check_box(ctx, box, f"要点カード{i + 1}")
        if abs(t - st) < 1 / FPS:
            ctx.sfx.append((sc["_start"] + st, "pop"))
        y += card_h + 30


def _cell_ref(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z])(\d+)", ref)
    if not m:
        raise ValueError(f"セル番地が不正です: {ref}")
    return ord(m.group(1)) - 65, int(m.group(2)) - 1


def scene_sheet(img, ctx: Ctx, sc: dict, t: float):
    """スプレッドシート画面を描き、数式の入力→結果の表示を見せる。"""
    th = ctx.theme
    d = ImageDraw.Draw(img, "RGBA")
    # 上の説明
    y = SAFE_TOP + 110
    if sc.get("title"):
        f, l, s = ctx.fit(sc["title"], 860, 200, 70, 46, "Black", 1.22, 2, "シート見出し")
        y = draw_lines(img, ctx, f, l, s, W / 2, y, rgb(th["text"]), rgb(th["mark"]), 1.22, t=t, start=0.0,
                       stagger=0.08) + 30
    rows = sc["rows"]
    tc, tr = _cell_ref(sc["target"])
    spill_refs = [_cell_ref(ref) for ref, _ in sc.get("spill", [])]
    n_rows = max([len(rows), tr + 1] + [r + 1 for _, r in spill_refs])
    n_cols = max([max(len(r) for r in rows), tc + 1] + [c + 1 for c, _ in spill_refs])
    widths = sc.get("widths") or [1] * n_cols
    x_left, x_right = SAFE_LEFT + 14, SAFE_RIGHT_LOW - 14
    rn_w = 64
    total_w = x_right - x_left - rn_w
    col_w = [total_w * w / sum(widths) for w in widths]
    row_h = sc.get("row_h", 70)
    # 数式バーの文字サイズと改行（長い数式は2行に）
    formula = sc.get("formula", "")
    avail = (x_right - x_left) - 110
    fx_font = font(40, "Bold")
    while formula and fx_font.getlength(formula) > avail and fx_font.size > 30:
        fx_font = font(fx_font.size - 2, "Bold")
    fx_lines = [formula]
    if formula and fx_font.getlength(formula) > avail:
        commas = [i for i, ch in enumerate(formula) if ch == ","]
        cut = min(commas, key=lambda i: abs(i - len(formula) / 2)) + 1 if commas else len(formula) // 2
        fx_lines = [formula[:cut], formula[cut:]]
        while max(fx_font.getlength(x) for x in fx_lines) > avail and fx_font.size > 24:
            fx_font = font(fx_font.size - 2, "Bold")
        if max(fx_font.getlength(x) for x in fx_lines) > avail:
            ctx.warnings.append(f"数式が数式バーに収まりません: {formula[:20]}")
    fx_lh = int(fx_font.size * 1.3)
    fb_h = 92 + (fx_lh if len(fx_lines) > 1 else 0)
    panel_top = y
    grid_top = panel_top + fb_h + 16
    panel_bottom = grid_top + row_h * (n_rows + 1) + 14
    pp = ease_out(progress(t, 0.15, 0.4))
    off = (1 - pp) * 80
    panel = [x_left - 14, panel_top - 14 + off, x_right + 14, panel_bottom + off]
    rounded(d, panel, 24, fill=rgb(th["card"], int(250 * pp)))
    if pp >= 1:
        check_box(ctx, panel, "シート")
    if pp < 0.05:
        return
    a = int(255 * pp)
    cell_f = font(sc.get("cell_size", 38), "Medium")
    head_f = font(30, "Bold")
    # 数式バー
    typing_start, typing_len = sc.get("type_at", 0.9), sc.get("type_len", 1.4)
    formula = sc.get("formula", "")
    keys = sc.get("keys")
    n_typed = int(len(formula) * clamp((t - typing_start) / typing_len)) if formula else 0
    entered_at = typing_start + (typing_len if formula else 0) + sc.get("enter_delay", 0.35)
    if keys:
        entered_at = typing_start + len(keys) * 0.35 + 0.3
    entered = t >= entered_at
    rounded(d, [x_left, panel_top + off, x_right, panel_top + fb_h + off], 14, fill=rgb(th["header"], a))
    d.text((x_left + 18, panel_top + 18 + off), "fx", font=font(40, "Black"), fill=rgb(th["accent"], a))
    if keys:
        n_typed = 0
    shown = formula[:n_typed] if not entered else formula
    pos, caret = 0, (x_left + 90, panel_top + 46 + off)
    for li, part in enumerate(fx_lines):
        seg = shown[pos:pos + len(part)]
        pos += len(part)
        ly = panel_top + 46 + li * fx_lh + off
        if seg:
            d.text((x_left + 90, ly - fx_font.size * 0.62), seg, font=fx_font, fill=rgb(th["card_text"], a))
            caret = (x_left + 90 + fx_font.getlength(seg), ly)
    if formula and not entered and t >= typing_start and int(t * 3) % 2 == 0:
        cx, cy = caret
        d.rectangle([cx + 4, cy - fx_font.size * 0.6, cx + 8, cy + fx_font.size * 0.6], fill=rgb(th["card_text"], a))
    if formula and t >= typing_start and n_typed > 0 and not entered:
        # タイピング音（数文字ごと）
        if n_typed % 3 == 0 and int(len(formula) * clamp((t - 1 / FPS - typing_start) / typing_len)) != n_typed:
            ctx.sfx.append((sc["_start"] + t, "tick"))
    # 列見出し
    gx = [x_left + rn_w]
    for w in col_w:
        gx.append(gx[-1] + w)
    gy = [grid_top + off + row_h * i for i in range(n_rows + 2)]
    d.rectangle([x_left, gy[0], x_right, gy[1]], fill=rgb(th["header"], a))
    d.rectangle([x_left, gy[1], x_left + rn_w, gy[-1]], fill=rgb(th["header"], a))
    for c in range(n_cols):
        label = chr(65 + c)
        d.text(((gx[c] + gx[c + 1]) / 2 - head_f.getlength(label) / 2, gy[0] + row_h / 2 - 20), label,
               font=head_f, fill=(110, 120, 135, a))
    for r in range(n_rows):
        label = str(r + 1)
        d.text((x_left + rn_w / 2 - head_f.getlength(label) / 2, gy[r + 1] + row_h / 2 - 20), label,
               font=head_f, fill=(110, 120, 135, a))
    # 範囲のハイライト
    hl = sc.get("highlight")
    if hl and t >= sc.get("highlight_at", typing_start + 0.4):
        c0, r0 = _cell_ref(hl.split(":")[0])
        c1, r1 = _cell_ref(hl.split(":")[1])
        box = [gx[c0], gy[r0 + 1], gx[c1 + 1], gy[r1 + 2]]
        d.rectangle(box, fill=rgb(th["mark"], 90))
        dash = int(t * 20) % 2
        d.rectangle(box, outline=rgb(th["accent"], 255), width=5 if dash else 4)
    # セルの中身
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            if val in (None, ""):
                continue
            text = str(val)
            is_header = r == 0 and sc.get("header_row", True)
            fnt = font(sc.get("cell_size", 38), "Bold") if is_header else cell_f
            while fnt.getlength(text) > col_w[c] - 16 and fnt.size > 22:
                fnt = font(fnt.size - 2, "Bold" if is_header else "Medium")
            if fnt.getlength(text) > col_w[c] - 16:
                ctx.warnings.append(f"セルの文字がはみ出ます: {chr(65 + c)}{r + 1}")
            numeric = bool(re.fullmatch(r"[-¥$0-9,.%/]+", text))
            tx = gx[c + 1] - 12 - fnt.getlength(text) if numeric and not is_header else gx[c] + 12
            d.text((tx, gy[r + 1] + row_h / 2 - fnt.size * 0.62), text, font=fnt,
                   fill=rgb(th["card_text"], a))
    # 罫線
    for x in gx:
        d.line([x, gy[0], x, gy[-1]], fill=rgb(th["grid"], a), width=2)
    for yy in gy:
        d.line([x_left, yy, x_right, yy], fill=rgb(th["grid"], a), width=2)
    d.line([x_left, gy[0], x_left, gy[-1]], fill=rgb(th["grid"], a), width=2)
    # 対象セル
    tbox = [gx[tc], gy[tr + 1], gx[tc + 1], gy[tr + 2]]
    if t >= typing_start - 0.3:
        if entered:
            pe = ease_back(progress(t, entered_at, 0.35))
            d.rectangle(tbox, fill=rgb(th["accent"], int(60 * (1 - progress(t, entered_at + 0.5, 0.8)))))
            res = str(sc.get("result", ""))
            rf = font(int(sc.get("cell_size", 38) * (0.7 + 0.3 * pe)) + 2, "Black")
            while rf.getlength(res) > col_w[tc] - 16 and rf.size > 22:
                rf = font(rf.size - 2, "Black")
            numeric = bool(re.fullmatch(r"[-¥$0-9,.%/]+", res))
            rx = tbox[2] - 12 - rf.getlength(res) if numeric else tbox[0] + 12
            d.text((rx, tbox[1] + row_h / 2 - rf.size * 0.62), res, font=rf, fill=rgb(th["accent"], 255))
            if abs(t - entered_at) < 1 / FPS:
                ctx.sfx.append((sc["_start"] + entered_at, "ding"))
        elif formula and n_typed:
            sf = font(28, "Medium")
            vis = formula[:n_typed]
            while vis and sf.getlength(vis) > col_w[tc] - 20:  # 入力中の数式はセルの枠内に収める
                vis = vis[1:]
            d.text((tbox[0] + 8, tbox[1] + row_h / 2 - 18), vis,
                   font=sf, fill=rgb(th["card_text"], 255))
        d.rectangle(tbox, outline=rgb(th["accent"], 255), width=6)
    # スピル・フィルダウンで複数セルに結果が出る場合
    for k, (ref, val) in enumerate(sc.get("spill", [])):
        st = entered_at + 0.25 + k * sc.get("spill_gap", 0.18)
        ps = ease_out(progress(t, st, 0.25))
        if ps <= 0:
            continue
        c, r = _cell_ref(ref)
        box = [gx[c], gy[r + 1], gx[c + 1], gy[r + 2]]
        d.rectangle(box, fill=rgb(th["accent"], int(40 * ps)))
        text = str(val)
        sf = font(sc.get("cell_size", 38), "Bold")
        while sf.getlength(text) > col_w[c] - 16 and sf.size > 22:
            sf = font(sf.size - 2, "Bold")
        numeric = bool(re.fullmatch(r"[-¥$0-9,.%/]+", text))
        sx = box[2] - 12 - sf.getlength(text) if numeric else box[0] + 12
        d.text((sx, box[1] + row_h / 2 - sf.size * 0.62 + (1 - ps) * 12), text, font=sf,
               fill=rgb(th["accent"], int(255 * ps)))
        if abs(t - st) < 1 / FPS:
            ctx.sfx.append((sc["_start"] + st, "tick"))
    # キー操作（ショートカット紹介用）
    key_bottom = gy[-1] + 14
    if keys:
        kf = font(46, "Black")
        widths_k = [max(130, kf.getlength(k) + 60) for k in keys]
        plus = 50
        total = sum(widths_k) + plus * (len(keys) - 1)
        kx = W / 2 - total / 2
        ky = gy[-1] + 44
        for i, k in enumerate(keys):
            ks = typing_start + i * 0.35
            pk = ease_back(progress(t, ks, 0.3))
            if pk > 0:
                pressed = ks <= t < ks + 0.18
                dy = 8 if pressed else 0
                rounded(d, [kx, ky + 10, kx + widths_k[i], ky + 120], 20, fill=(0, 0, 0, 90))
                rounded(d, [kx, ky + dy, kx + widths_k[i], ky + 110 + dy], 20,
                        fill=rgb(th["card"], int(255 * clamp(pk))), outline=rgb(th["accent"], 255), width=4)
                d.text((kx + widths_k[i] / 2 - kf.getlength(k) / 2, ky + 22 + dy), k, font=kf,
                       fill=rgb(th["card_text"], 255))
                if abs(t - ks) < 1 / FPS:
                    ctx.sfx.append((sc["_start"] + ks, "key"))
            kx += widths_k[i]
            if i < len(keys) - 1:
                d.text((kx + plus / 2 - 14, ky + 28), "+", font=font(50, "Black"), fill=rgb(th["text"], 255))
                kx += plus
        key_bottom = ky + 130
        check_box(ctx, [W / 2 - total / 2, ky, W / 2 + total / 2, ky + 120], "キー表示")
    # 下の補足
    if sc.get("note"):
        note_at = sc.get("note_at", entered_at + 0.4)
        f, l, s = ctx.fit(sc["note"], 820, 200, 54, 40, "Bold", 1.28, 3, "シート補足")
        top = key_bottom + 30
        end = draw_lines(img, ctx, f, l, s, W / 2, top, rgb(th["text"]), rgb(th["mark"]), 1.3, t=t,
                         start=note_at, stagger=0.1, marker=None)
        if t >= note_at + 0.5:
            check_box(ctx, text_box(f, l, W / 2, top, end), "シート補足")


def scene_compare(img, ctx: Ctx, sc: dict, t: float):
    """Before → After を縦に並べる。"""
    th = ctx.theme
    d = ImageDraw.Draw(img, "RGBA")
    y = SAFE_TOP + 120
    if sc.get("title"):
        f, l, s = ctx.fit(sc["title"], 860, 200, 70, 46, "Black", 1.22, 2, "比較見出し")
        y = draw_lines(img, ctx, f, l, s, W / 2, y, rgb(th["text"]), rgb(th["mark"]), 1.22, t=t, stagger=0.08) + 40
    for i, (key, color, label) in enumerate([("before", th["warn"], sc.get("before_label", "いつものやり方")),
                                             ("after", th["accent"], sc.get("after_label", "この技なら"))]):
        st = 0.4 + i * 1.1
        p = ease_out(progress(t, st, 0.4))
        if p <= 0:
            continue
        f, l, s = ctx.fit(sc[key], 700, 200, 60, 42, "Black", 1.25, 3, label)
        h = len(l) * s * 1.25 + 130
        box = [SAFE_LEFT + 20, y + (1 - p) * 60, SAFE_LEFT + 830, y + h + (1 - p) * 60]
        rounded(d, box, 30, fill=rgb(th["card"], int(245 * p)))
        rounded(d, [box[0], box[1], box[0] + 18, box[3]], 9, fill=rgb(color, int(255 * p)))
        d.text((box[0] + 50, box[1] + 20), label, font=font(38, "Bold"), fill=rgb(color, int(255 * p)))
        draw_lines(img, ctx, f, l, s, 0, box[1] + 80, rgb(th["card_text"]), rgb(color), 1.25,
                   align="left", left=box[0] + 50)
        if key == "before" and t > 0.4 + 1.1 + 0.3:
            ps = ease_out(progress(t, 1.8, 0.3))
            for li, ln in enumerate(l):
                yy = box[1] + 80 + li * s * 1.25 + s * 0.72
                lw = min(700, f.getlength("".join(c for c, _ in ln)))
                d.line([box[0] + 46, yy, box[0] + 46 + lw * ps, yy], fill=rgb(th["warn"], 220), width=6)
        if p >= 1:
            check_box(ctx, box, label)
        if abs(t - st) < 1 / FPS:
            ctx.sfx.append((sc["_start"] + st, "pop" if key == "before" else "ding"))
        y += h + 70
        if i == 0:
            pa = ease_out(progress(t, 1.3, 0.3))
            if pa > 0:
                ax = W / 2
                d.polygon([(ax - 40, y - 60), (ax + 40, y - 60), (ax, y - 10)], fill=rgb(th["mark"], int(255 * pa)))


def scene_cta(img, ctx: Ctx, sc: dict, t: float):
    th = ctx.theme
    d = ImageDraw.Draw(img, "RGBA")
    # しおりアイコン（保存ボタン）
    p = ease_back(progress(t, 0.1, 0.45))
    if p > 0:
        cx, cy, s = W / 2, 560, 120 * p
        pts = [(cx - s * 0.6, cy - s), (cx + s * 0.6, cy - s), (cx + s * 0.6, cy + s), (cx, cy + s * 0.55),
               (cx - s * 0.6, cy + s)]
        d.polygon(pts, fill=rgb(th["mark"], 255))
        ctx.sfx.append((sc["_start"] + 0.1, "pop")) if abs(t - 0.1) < 1 / FPS else None
    f, l, s = ctx.fit(sc["text"], 860, 400, 96, 60, "Black", 1.22, 3, "締め")
    end = draw_lines(img, ctx, f, l, s, W / 2, 760, rgb(th["text"]), rgb(th["mark"]), 1.22, t=t, start=0.4,
                     pop=True, stagger=0.12)
    check_box(ctx, text_box(f, l, W / 2, 760, end), "締めテキスト")
    if sc.get("sub"):
        f2, l2, s2 = ctx.fit(sc["sub"], 800, 300, 50, 36, "Bold", 1.35, 4, "締めサブ")
        end2 = draw_lines(img, ctx, f2, l2, s2, W / 2, end + 50, rgb(th["sub"]), rgb(th["mark"]), 1.35, t=t,
                          start=1.0, stagger=0.1)
        check_box(ctx, text_box(f2, l2, W / 2, end + 50, end2), "締めサブ")


def scene_quiz(img, ctx: Ctx, sc: dict, t: float):
    """テンポの速いクイズ1問：大きな問題 → 減っていく時間バー → 答えがはじける。

    {"type": "quiz", "dur": 2.6, "q": "蝸牛", "a": "かたつむり", "no": 1, "of": 5,
     "reveal_at": 1.6, "label": "読める？", "note": "出典：デジタル大辞泉"}
    """
    th = ctx.theme
    d = ImageDraw.Draw(img, "RGBA")
    dur = float(sc["dur"])
    reveal = float(sc.get("reveal_at", dur * 0.6))
    # 問題番号（例：3 / 5）
    if sc.get("no"):
        fb = font(54, "Black")
        label = f"{sc['no']}" + (f" / {sc['of']}" if sc.get("of") else "")
        w = fb.getlength(label) + 70
        rounded(d, [W / 2 - w / 2, 300, W / 2 + w / 2, 392], 46, fill=rgb(th["accent"], 255))
        d.text((W / 2 - fb.getlength(label) / 2, 314), label, font=fb, fill=rgb("#ffffff"))
    if sc.get("label"):
        f, l, sz = ctx.fit(sc["label"], 820, 120, 60, 40, "Bold", 1.2, 1, "クイズの見出し")
        draw_lines(img, ctx, f, l, sz, W / 2, 430, rgb(th["sub"]), rgb(th["mark"]), 1.2)
    # 問題のカード
    pin = ease_back(progress(t, 0.0, 0.22))
    card = [SAFE_LEFT + 20, 560, SAFE_RIGHT - 20, 1010]
    cx = (card[0] + card[2]) / 2
    if pin > 0:
        grow = 0.86 + 0.14 * pin
        hw, hh = (card[2] - card[0]) / 2 * grow, (card[3] - card[1]) / 2 * grow
        cy = (card[1] + card[3]) / 2
        rounded(d, [cx - hw, cy - hh, cx + hw, cy + hh], 40, fill=rgb(th["card"], int(255 * min(1, pin))))
        q = str(sc["q"])
        size = sc.get("size", 260 if len(q) <= 2 else 210 if len(q) == 3 else 170 if len(q) == 4 else 130)
        fq = font(int(size * grow), "Black")
        while fq.getlength(q) > hw * 2 - 60 and fq.size > 60:
            fq = font(fq.size - 6, "Black")
        bb = d.textbbox((0, 0), q, font=fq)
        d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]), q, font=fq,
               fill=rgb(th["card_text"], 255))
        if abs(t - 0.02) < 1 / FPS:
            ctx.sfx.append((sc["_start"] + 0.02, "pop"))
    # 時間バー（答えが出るまで減っていく）
    if t < reveal:
        left = 1 - clamp(t / reveal)
        bar = [card[0], 1050, card[2], 1076]
        rounded(d, bar, 13, fill=rgb(th["sub"], 70))
        rounded(d, [bar[0], bar[1], bar[0] + (bar[2] - bar[0]) * left, bar[3]], 13,
                fill=rgb(th["warn"] if left < 0.35 else th["mark"], 255))
        for k in range(1, int(reveal / 0.5) + 1):              # 0.5秒ごとにカチッ
            if abs(t - k * 0.5) < 1 / FPS and k * 0.5 < reveal:
                ctx.sfx.append((sc["_start"] + k * 0.5, "tick"))
    else:
        pa = ease_back(progress(t, reveal, 0.25))
        if abs(t - reveal) < 1 / FPS:
            ctx.sfx.append((sc["_start"] + reveal, "ding"))
        fa = font(int(120 * (0.6 + 0.4 * pa)), "Black")
        a = str(sc["a"])
        while fa.getlength(a) > SAFE_RIGHT_LOW - SAFE_LEFT - 40 and fa.size > 50:
            fa = font(fa.size - 4, "Black")
        bb = d.textbbox((0, 0), a, font=fa)
        w, hgt = bb[2] - bb[0], bb[3] - bb[1]
        box = [W / 2 - w / 2 - 44, 1100, W / 2 + w / 2 + 44, 1100 + hgt + 64]
        rounded(d, box, 30, fill=rgb(th["accent"], int(255 * clamp(pa))))
        d.text((W / 2 - w / 2 - bb[0], 1100 + 32 - bb[1]), a, font=fa, fill=rgb("#ffffff", int(255 * clamp(pa))))
        if sc.get("note") and t > reveal + 0.15:
            fn = font(40, "Bold")
            n = str(sc["note"])
            d.text((W / 2 - fn.getlength(n) / 2, 1330), n, font=fn, fill=rgb(th["sub"], 230))
        if t > reveal + 0.3:
            check_box(ctx, box, "クイズの答え")


SCENES = {"hook": scene_hook, "points": scene_points, "sheet": scene_sheet, "compare": scene_compare,
          "cta": scene_cta, "quiz": scene_quiz}


# ---------------------------------------------------------------- audio
# BGM：外部の曲・素材は使わず、ここで一から合成する（著作権の問題がない自作曲）
_NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_MINOR = {9, 4, 2}                                                    # vi・iii・ii は短三和音
# 曲の雰囲気（台本の "bgm_style" で選ぶ。省略時は BGM_STYLE_DEFAULT）
BGM_STYLES = {
    # 落ち着いたローファイ（最初の版）
    "lofi":   {"bpm": (84, 88, 92, 96), "progs": [[0, 7, 9, 5], [9, 5, 0, 7], [0, 9, 5, 7], [5, 7, 4, 9]],
               "lead": "epiano", "arp": "eighth", "arp_amp": 0.022, "drums": "soft", "oct": 0, "bass": "half"},
    # A 明るいポップ：ピアノ＋4つ打ち＋手拍子
    "pop":    {"bpm": (118, 120, 122, 124), "progs": [[0, 7, 9, 5], [0, 5, 7, 5], [5, 0, 7, 9]],
               "lead": "piano", "arp": "eighth", "arp_amp": 0.035, "drums": "four", "oct": 12, "bass": "eighth"},
    # B シティポップ風：おしゃれな7th・はねるベース
    "city":   {"bpm": (108, 110, 112, 114), "progs": [[5, 4, 2, 0], [5, 7, 4, 9], [2, 7, 0, 9]],
               "lead": "epiano", "arp": "sync", "arp_amp": 0.03, "drums": "funk", "oct": 0, "bass": "funk"},
    # C ウクレレ風：軽い弦のカッティング＋シェイカー
    "uke":    {"bpm": (124, 126, 128, 130), "progs": [[0, 5, 7, 0], [0, 7, 5, 7], [0, 9, 5, 7]],
               "lead": "pluck", "arp": "strum", "arp_amp": 0.045, "drums": "shaker", "oct": 12, "bass": "half"},
    # D ピコピコ（チップチューン風）：元気な電子音
    "chip":   {"bpm": (126, 128, 130, 132), "progs": [[0, 7, 9, 5], [0, 5, 0, 7], [9, 5, 0, 7]],
               "lead": "square", "arp": "sixteenth", "arp_amp": 0.03, "drums": "four", "oct": 12, "bass": "eighth"},
}
BGM_STYLES["wa"] = {"bpm": (84, 88, 92), "progs": [[0]], "lead": "koto"}   # 和風：琴・尺八・拍子木
BGM_STYLE_DEFAULT = "city"   # 2026-11-04 社長がB案（シティポップ風）に決定


def synth_bgm(total: float, seed: str = "", sr: int = 48000, style: str | None = None) -> np.ndarray:
    """BGMを合成する。台本の id から調・コード進行・テンポを決めるので、リールごとに少しずつ違う曲になる。"""
    if (style or BGM_STYLE_DEFAULT) == "wa":
        return _synth_wa(total, seed, sr)
    st = BGM_STYLES.get(style or BGM_STYLE_DEFAULT, BGM_STYLES["lofi"])
    h = sum(ord(c) * (i + 1) for i, c in enumerate(seed or "reel"))
    key = _NOTE["CDFGA"[h % 5]]
    prog = st["progs"][(h // 5) % len(st["progs"])]
    bpm = st["bpm"][(h // 20) % len(st["bpm"])]
    beat = 60.0 / bpm
    n = int(total * sr) + sr
    out = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(h)

    def hz(midi):
        return 440.0 * 2 ** ((midi - 69) / 12)

    def note(f, dur, amp, decay, harm=(1.0, 0.35, 0.12), detune=0.0, square=False):
        m = int(dur * sr)
        tt = np.arange(m) / sr
        if square:
            w = np.sign(np.sin(2 * np.pi * f * tt)) * 0.6
        else:
            w = sum(a * np.sin(2 * np.pi * f * (k + 1) * (1 + detune) * tt) for k, a in enumerate(harm))
        env = np.minimum(1, tt / 0.008) * np.exp(-tt * decay)
        env *= np.minimum(1, (dur - tt) / 0.03)    # 終わりを少しずつ消す（プツッという音を防ぐ）
        return (w * env * amp).astype(np.float32)

    def add(at, sig):
        i = int(at * sr)
        if 0 <= i < n:
            j = min(n, i + len(sig))
            out[i:j] += sig[: j - i]

    def noise(dur, amp, decay, smooth=1):
        m = int(dur * sr)
        x = rng.uniform(-1, 1, m + smooth)
        x = np.diff(x, n=1)[:m] if smooth == 1 else x[:m]
        return (x * np.exp(-np.arange(m) / sr * decay) * amp).astype(np.float32)

    m = int(0.22 * sr)
    tt = np.arange(m) / sr
    kick = (np.sin(2 * np.pi * np.cumsum(np.linspace(120, 45, m)) / sr) * np.exp(-tt * 18) * 0.55).astype(np.float32)
    hat = noise(0.04, 0.05, 90)
    clap = (noise(0.12, 0.09, 30, smooth=0) * 0.8).astype(np.float32)
    shaker = noise(0.06, 0.035, 60)

    lead = st["lead"]
    harm = {"epiano": (1.0, 0.35, 0.12), "piano": (1.0, 0.5, 0.25, 0.1), "pluck": (1.0, 0.6, 0.3, 0.15)}.get(lead, (1.0,))
    bar, t = 0, 0.0
    while t < total + beat * 4:
        deg = prog[bar % 4]
        root = 48 + key + deg
        minor = deg in _MINOR
        third = 3 if minor else 4
        top = root + 12 + st["oct"]
        chord = [top, top + third, top + 7, top + (10 if minor else 11)]
        # 和音
        if st["arp"] == "strum":                    # ウクレレ風のカッティング（ジャ・ジャ・ジャジャ）
            for at in (0, 1, 1.5, 2.5, 3):
                for k, mn in enumerate(chord[:3] + [top + 12]):
                    add(t + at * beat + k * 0.008, note(hz(mn), beat * 0.45, st["arp_amp"], 7.0, harm=harm))
        else:
            hits = (0,) if lead in ("epiano",) else (0, 2) if lead == "piano" else (0, 1.5, 3) if lead == "square" else (0,)
            for at in hits:
                for k, mn in enumerate(chord):
                    add(t + at * beat + k * 0.01, note(hz(mn), beat * (4 if len(hits) == 1 else 2), 0.045, 1.2 if lead != "square" else 4,
                                                       harm=harm, detune=0.0015 * (k - 1.5), square=(lead == "square" and k == 0)))
        # ベース
        if st["bass"] == "half":
            add(t, note(hz(root - 12), beat * 2, 0.13, 1.4, harm=(1.0, 0.2)))
            add(t + beat * 2.5, note(hz(root - 5), beat * 1.5, 0.09, 1.8, harm=(1.0, 0.2)))
        elif st["bass"] == "eighth":
            for e in range(8):
                add(t + e * beat / 2, note(hz(root - 12 + (12 if e % 2 else 0)), beat * 0.45, 0.1, 5, harm=(1.0, 0.3)))
        else:                                        # funk：はねるベース
            for at, off in ((0, 0), (0.75, 12), (1.5, 7), (2.5, 0), (3.25, 10), (3.5, 12)):
                add(t + at * beat, note(hz(root - 12 + off), beat * 0.4, 0.12, 6, harm=(1.0, 0.35)))
        # アルペジオ
        arp = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 12]
        if st["arp"] in ("eighth", "sixteenth"):
            steps = 8 if st["arp"] == "eighth" else 16
            for e in range(steps):
                add(t + e * beat * 4 / steps + 0.01, note(hz(arp[e % 4]), beat * 0.5, st["arp_amp"], 8.0,
                                                          harm=(1.0, 0.5, 0.25), square=(lead == "square")))
        elif st["arp"] == "sync":
            for at, k in ((0.5, 0), (1.25, 2), (2, 1), (2.75, 3), (3.5, 2)):
                add(t + at * beat, note(hz(arp[k]), beat * 0.4, st["arp_amp"], 7.0, harm=(1.0, 0.4)))
        # リズム
        for b in range(4):
            d = st["drums"]
            if d == "soft":
                if b in (0, 2):
                    add(t + b * beat, kick)
                add(t + b * beat + beat / 2, hat)
            elif d == "four":
                add(t + b * beat, kick)
                add(t + b * beat + beat / 2, hat * 1.4)
                if b in (1, 3):
                    add(t + b * beat, clap)
            elif d == "funk":
                if b in (0, 2):
                    add(t + b * beat, kick)
                if b == 2:
                    add(t + b * beat + beat * 0.75, kick * 0.7)
                if b in (1, 3):
                    add(t + b * beat, clap)
                for q in range(4):
                    add(t + b * beat + q * beat / 4, hat * (1.0 if q % 2 else 0.5))
            elif d == "shaker":
                if b in (0, 2):
                    add(t + b * beat, kick * 0.8)
                for q in range(4):
                    add(t + b * beat + q * beat / 4, shaker * (1.0 if q % 2 else 0.6))
        t += beat * 4
        bar += 1

    out = out[: int(total * sr)]
    out = np.convolve(out, np.ones(4, dtype=np.float32) / 4, mode="same").astype(np.float32)
    fade_in, fade_out = int(0.4 * sr), int(1.6 * sr)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.32).astype(np.float32)   # 効果音より控えめ


def _synth_wa(total: float, seed: str = "", sr: int = 48000) -> np.ndarray:
    """和風のBGM：琴の旋律（都節の音階）＋尺八の長い音＋低い琴の支え＋拍子木。"""
    h = sum(ord(c) * (i + 1) for i, c in enumerate(seed or "wa"))
    rng = np.random.default_rng(h)
    bpm = BGM_STYLES["wa"]["bpm"][h % 3]
    beat = 60.0 / bpm
    key = 62 + (0, 2, -3)[(h // 3) % 3]                   # D・E・B あたり
    scale = [0, 1, 5, 7, 8]                               # 都節音階（ミ・ファ・ラ・シ・ド）
    n = int(total * sr) + sr
    out = np.zeros(n, dtype=np.float32)

    def add(at, sig):
        i = int(at * sr)
        if 0 <= i < n:
            j = min(n, i + len(sig))
            out[i:j] += sig[: j - i]

    def hz(m):
        return 440.0 * 2 ** ((m - 69) / 12)

    def koto(f, dur, amp):
        m = int(dur * sr)
        tt = np.arange(m) / sr
        bend = 1 + 0.006 * np.exp(-tt * 25)               # はじいた直後に少し高い
        ph = 2 * np.pi * np.cumsum(f * bend) / sr
        w = np.sin(ph) + 0.55 * np.sin(2 * ph) * np.exp(-tt * 6) + 0.3 * np.sin(3 * ph) * np.exp(-tt * 9) \
            + 0.15 * np.sin(5 * ph) * np.exp(-tt * 14)
        env = np.minimum(1, tt / 0.003) * np.exp(-tt * (3.2 if dur < 3 else 0.9))
        env *= np.minimum(1, (dur - tt) / 0.03)
        return (w * env * amp).astype(np.float32)

    def shaku(f, dur, amp):
        m = int(dur * sr)
        tt = np.arange(m) / sr
        vib = 1 + 0.004 * np.sin(2 * np.pi * 5.2 * tt) * np.minimum(1, tt / 0.6)
        ph = 2 * np.pi * np.cumsum(f * vib) / sr
        breath = np.convolve(rng.uniform(-1, 1, m), np.ones(30) / 30, mode="same")
        w = np.sin(ph) + 0.12 * np.sin(2 * ph) + 0.25 * breath
        env = np.minimum(1, tt / 0.25) * np.minimum(1, (dur - tt) / 0.5)
        return (w * env * amp).astype(np.float32)

    m = int(0.05 * sr)
    tt = np.arange(m) / sr
    clack = (np.sin(2 * np.pi * 1900 * tt) * np.exp(-tt * 140) * 0.25).astype(np.float32)   # 拍子木
    m = int(0.5 * sr)
    tt = np.arange(m) / sr
    taiko = (np.sin(2 * np.pi * np.cumsum(np.linspace(95, 55, m)) / sr) * np.exp(-tt * 7) * 0.35).astype(np.float32)

    step = beat / 2
    t, k, bar = 0.0, 0, 0
    deg = 2
    while t < total + beat * 4:
        # 2小節ごとに、低い琴の和音（根音＋5度）と太鼓
        if k % 16 == 0:
            add(t, koto(hz(key - 12), beat * 8.5, 0.10))
            add(t + 0.02, koto(hz(key - 5), beat * 8.5, 0.06))
            add(t, taiko)
            if bar % 2 == 0:                               # 尺八の長い音
                add(t + beat, shaku(hz(key + scale[[2, 3, 4][bar // 2 % 3]]), beat * 5, 0.035))
            bar += 1
        # 琴の旋律：音階の上を近い音へ動く。ときどき休む
        if rng.random() > (0.28 if k % 2 else 0.12):
            deg = int(np.clip(deg + rng.choice([-2, -1, -1, 1, 1, 2]), 0, 9))
            mn = key + scale[deg % 5] + 12 * (deg // 5)
            add(t, koto(hz(mn), beat * 1.6, 0.055))
            if k % 8 == 6 and rng.random() < 0.5:          # 装飾の二音
                add(t + step / 2, koto(hz(mn + 12), beat, 0.03))
        if k % 8 == 4:
            add(t, clack)
        t += step
        k += 1

    out = out[: int(total * sr)]
    fade_in, fade_out = int(0.3 * sr), int(1.6 * sr)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.32).astype(np.float32)


# 読み上げ音声：pyopenjtalk（オフラインの日本語音声合成。台本の scenes[].say に書いた文を読む）
def synth_voice(lines: list[tuple[float, str, float]], total: float, sr: int = 48000,
                speed: float = 1.15, warn=None) -> np.ndarray | None:
    """lines = [(開始秒, 文, その場面の終わりの秒)]。声のトラック（48kHz）を返す。使えなければ None。"""
    if not lines:
        return None
    try:
        import pyopenjtalk
    except Exception:
        if warn is not None:
            warn.append("読み上げ（say）がありますが、pyopenjtalk が入っていないため声なしで作りました")
        return None
    out = np.zeros(int(total * sr) + sr, dtype=np.float32)
    for at, text, end in lines:
        x, vsr = pyopenjtalk.tts(text, speed=speed)
        x = x.astype(np.float32) / 32768.0
        if vsr != sr:
            idx = np.arange(0, len(x), vsr / sr)
            x = np.interp(idx, np.arange(len(x)), x).astype(np.float32)
        if warn is not None and at + len(x) / sr > end + 0.15:
            warn.append(f"読み上げが場面の長さを超えます（{text[:12]}… {len(x) / sr:.1f}秒）")
        i = int(at * sr)
        j = min(len(out), i + len(x))
        out[i:j] += x[: j - i]
    out = out[: int(total * sr)]
    peak = float(np.max(np.abs(out))) or 1.0
    return out / peak * 0.85


def synth_sfx(events: list[tuple[float, str]], total: float, path: Path, sr: int = 48000,
              bgm: np.ndarray | None = None, voice: np.ndarray | None = None):
    """著作権の問題がない、自作の効果音（＋自作BGM）の音声トラックを作る。"""
    audio = np.zeros(int(total * sr) + sr, dtype=np.float32)

    def tone(f0, f1, dur, amp, noise=0.0):
        n = int(dur * sr)
        tt = np.arange(n) / sr
        freq = np.linspace(f0, f1, n)
        wave_ = np.sin(2 * np.pi * np.cumsum(freq) / sr)
        if noise:
            wave_ = wave_ * (1 - noise) + np.random.default_rng(1).uniform(-1, 1, n) * noise
        env = np.exp(-tt * (8 / dur))
        return (wave_ * env * amp).astype(np.float32)

    sounds = {"pop": tone(880, 520, 0.09, 0.22), "ding": tone(1320, 1320, 0.35, 0.16) + np.pad(
        tone(1760, 1760, 0.3, 0.08), (0, int(0.05 * sr))),
        "tick": tone(2400, 1800, 0.025, 0.06, noise=0.6), "key": tone(300, 200, 0.06, 0.2, noise=0.4)}
    seen = set()
    for at, kind in events:
        key = (round(at, 2), kind)
        if key in seen:
            continue
        seen.add(key)
        s = sounds[kind]
        i = int(at * sr)
        if 0 <= i < len(audio):
            j = min(len(audio), i + len(s))
            audio[i:j] += s[: j - i]
    audio = audio[: int(total * sr)]
    if bgm is not None:
        bgm = bgm[: len(audio)].copy()
        if voice is not None:                      # 声の間はBGMを小さくする（ダッキング）
            env = np.abs(voice[: len(bgm)])
            env = np.convolve(env, np.ones(int(0.25 * sr)) / (0.25 * sr), mode="same")
            bgm[: len(env)] *= 1 - 0.55 * np.clip(env / (env.max() or 1) * 3, 0, 1)
        audio[: len(bgm)] += bgm
    if voice is not None:
        audio[: len(voice)] += voice[: len(audio)] * 0.9
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    if rms > 1e-6:                                # スマホで聞きやすい音量にそろえる（目安 -16 LUFS 前後）
        audio = audio * (0.17 / rms)
    audio = np.tanh(audio * 1.1) * 0.9            # 割れないように、なめらかに抑える
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


ACCOUNT = "@aiaiai4772"
CAPTION_FOOTER = ("保存しておくと、次に使うときすぐ見返せます。\n"
                  "ほかに知りたい事務ワザがあれば、コメントで教えてください。\n"
                  f"▶ ひとり事務の時短ワザを毎週投稿中 {ACCOUNT}")


def build_caption(spec: dict) -> str:
    """投稿文の完成形（本文＋共通の締め文＋ハッシュタグ5個まで）。自動投稿でも同じものを使う。"""
    post = spec.get("post", {})
    tags = spec.get("hashtags", [])[:5]
    caption = spec.get("caption", "").strip()
    voiced = spec.get("voice", True) and any(sc.get("say") or sc.get("say_answer") for sc in spec.get("scenes", []))
    if voiced and VOICE_NOTE not in caption:
        caption += "\n\n" + VOICE_NOTE             # 合成音声を使ったことを明記する
    return caption + "\n\n" + post.get("footer", CAPTION_FOOTER) + ("\n\n" + " ".join(tags) if tags else "")


VOICE_NOTE = "※ナレーションは合成音声です。"


def write_post_info(spec: dict, out_dir: Path) -> Path:
    """投稿に必要な情報（表紙の文字・投稿文・ハッシュタグ・代替テキストなど）を1ファイルにまとめる。"""
    post = spec.get("post", {})
    tags = spec.get("hashtags", [])[:5]          # Instagramは1投稿5個まで
    full = build_caption(spec)
    lines = [
        f"# 投稿情報：{spec.get('series', spec.get('id', ''))}",
        "",
        "## 1. 表紙（カバー）",
        f"- 表紙画像：同じフォルダの cover.png（または動画の{post.get('cover_at', '冒頭')}を選ぶ）",
        f"- 表紙に入れる文字（動画の途中を表紙にする場合だけ）：{post.get('cover_title', '').replace(chr(10), ' / ')}　※cover.pngは冒頭の文字入りなので、そのまま使えば文字の追加は不要",
        "",
        "## 2. 投稿文（キャプション）※下の枠をそのままコピー",
        "```",
        full,
        "```",
        f"（{len(full)}字。Instagramの上限は2,200字）",
        "",
        "## 3. ハッシュタグ（5個まで）",
        " ".join(tags),
        "",
        "## 4. 詳細設定",
        f"- 代替テキスト：{post.get('alt_text', '')}",
        f"- 音源：{post.get('music_hint', 'アプリ内の音源から、歌なしの落ち着いた曲を選び、音量を小さめに')}",
        f"- 投稿時間の目安：{post.get('post_time', '平日 12時台 または 20〜21時台（データ分析班の結果で見直す）')}",
        "- 「フィードにもシェア」：オン",
        "- 場所・タグ付け：不要",
        "",
        "## 5. 投稿後にすること",
        "- リール実績DBのこの動画の行に、投稿日とリールURLを入れる",
        "- 3日後、インサイト画面のスクリーンショットを同じ行の「スクショ」に貼る",
    ]
    if post.get("notes"):
        lines += ["", "## 注意", *[f"- {n}" for n in post["notes"]]]
    path = out_dir / "投稿情報.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- main
def render(script_path: Path, out_dir: Path) -> dict:
    spec = json.loads(script_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    ctx = Ctx(spec.get("theme"))
    scenes = spec["scenes"]
    start = 0.0
    for sc in scenes:
        sc["_start"] = start
        start += float(sc["dur"])
    total = start
    first_sheet = next((sc for sc in scenes if sc["type"] == "sheet"), None)
    for sc in scenes:
        if sc["type"] == "hook" and first_sheet is not None and sc.get("preview", True):
            sc["_preview"] = first_sheet
    if not 5 <= total <= 90:
        ctx.warnings.append(f"動画の長さが想定外です: {total:.1f}秒")
    # 読む速さのチェック（1秒あたりの文字数）
    reading = []
    for i, sc in enumerate(scenes):
        text = "".join(str(sc.get(k, "")) for k in ("text", "sub", "title", "note", "before", "after", "q", "a", "label"))
        text += "".join(sc.get("items", []))
        n = len(re.sub(r"[\s*\n]", "", text))
        cps = n / float(sc["dur"])
        reading.append({"scene": i + 1, "type": sc["type"], "chars": n, "seconds": sc["dur"],
                        "chars_per_sec": round(cps, 1)})
        limit = 11.0 if (sc.get("say") and spec.get("voice", True)) else 7.5   # 読み上げがある場面は耳でも追える
        if cps > limit:
            ctx.warnings.append(f"場面{i + 1}は文字が多く、読み切れない可能性（{cps:.1f}字/秒）")
    video = out_dir / "reel.mp4"
    silent = out_dir / "_sfx.wav"
    n_frames = int(round(total * FPS))
    snapshot_frames = {}
    for i, sc in enumerate(scenes):
        for frac in (0.35, 0.95):
            snapshot_frames[int((sc["_start"] + sc["dur"] * frac) * FPS)] = f"scene{i + 1:02d}_{int(frac * 100)}"
    cover_frame = int(min(total, scenes[0]["_start"] + min(1.6, scenes[0]["dur"] * 0.9)) * FPS)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", str(out_dir / "_video.mp4")], stdin=subprocess.PIPE)
    for fi in range(n_frames):
        t = fi / FPS
        idx = next(i for i, sc in enumerate(scenes) if t < sc["_start"] + sc["dur"] or i == len(scenes) - 1)
        sc = scenes[idx]
        local = t - sc["_start"]
        img = background(ctx, t)
        SCENES[sc["type"]](img, ctx, sc, local)
        series_badge(img, ctx, spec.get("series", ""), t)
        # 場面の切り替え（短いフェード）
        fade = clamp(local / 0.18) if idx > 0 else 1.0
        if fade < 1:
            img = Image.blend(background(ctx, t), img, fade)
        progress_bar(img, ctx, t, total)
        frame = img.convert("RGB")
        if fi in snapshot_frames:
            frame.save(frames_dir / f"{snapshot_frames[fi]}.png")
        if fi == cover_frame:
            frame.save(out_dir / "cover.png")
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError("動画のエンコードに失敗しました")
    bgm = synth_bgm(total, spec.get("id", ""), style=spec.get("bgm_style")) if spec.get("bgm", True) else None
    lines = []
    if spec.get("voice", True):
        for sc in scenes:
            if sc.get("say"):
                lines.append((sc["_start"] + float(sc.get("say_at", 0.05)), sc["say"], sc["_start"] + float(sc["dur"])))
            if sc.get("say_answer"):                # クイズ：答えが出た瞬間に読む
                at = sc["_start"] + float(sc.get("reveal_at", float(sc["dur"]) * 0.6)) + 0.02
                lines.append((at, sc["say_answer"], sc["_start"] + float(sc["dur"])))
    voice = synth_voice(lines, total, speed=float(spec.get("voice_speed", 1.15)), warn=ctx.warnings)
    synth_sfx(ctx.sfx, total, silent, bgm=bgm, voice=voice)
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out_dir / "_video.mp4"), "-i", str(silent),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                        "-ar", "48000", "-shortest", "-movflags", "+faststart", str(video)],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("音声の結合に失敗しました: " + r.stderr[-800:])
    (out_dir / "_video.mp4").unlink()
    silent.unlink()
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
         "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout)
    vs = next(s for s in probe["streams"] if s["codec_type"] == "video")
    aus = next((s for s in probe["streams"] if s["codec_type"] == "audio"), {})
    if (vs.get("width"), vs.get("height")) != (W, H):
        ctx.warnings.append("解像度が1080x1920ではありません")
    caption = spec.get("caption", "").strip()
    tags = " ".join(spec.get("hashtags", []))
    (out_dir / "caption.txt").write_text(caption + ("\n\n" + tags if tags else "") + "\n", encoding="utf-8")
    write_post_info(spec, out_dir)
    if "post" not in spec:
        ctx.warnings.append("台本に post（表紙の文字・代替テキストなど）がありません")
    qa = {"passed_format_checks": not ctx.warnings,
          "duration_seconds": round(float(probe["format"]["duration"]), 2),
          "resolution": f"{vs['width']}x{vs['height']}", "video_codec": vs.get("codec_name"),
          "audio_codec": aus.get("codec_name"), "scenes": len(scenes), "reading_speed": reading,
          "warnings": sorted(set(ctx.warnings)),
          "note": "形式・レイアウトの自動チェックのみ。内容の正しさは別途レビューが必要。"}
    (out_dir / "qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[3] == "--post-only":
        _spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        Path(sys.argv[2]).mkdir(parents=True, exist_ok=True)
        print(write_post_info(_spec, Path(sys.argv[2])))
        sys.exit(0)
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    result = render(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
