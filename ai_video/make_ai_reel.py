"""AI動画リール（@satisfying.ai.flow 用）の試作：Google Veo で映像を作り、文字とAI表記を重ねる。

使い方：
  python ai_video/make_ai_reel.py 台本.json 出力フォルダ            # Veo で映像を作る（有料。GEMINI_API_KEY が必要）
  python ai_video/make_ai_reel.py 台本.json 出力フォルダ --video x.mp4  # 手元の映像に文字だけ重ねる（無料）

料金の目安（Gemini API・2026年10月時点）：Veo 3.1 Fast 720p は 1秒 0.10ドル → 8秒で約0.8ドル。
出力：reel.mp4（1080×1920）・cover.jpg・投稿情報.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
AI_NOTE = "※AIで生成した映像です。実在の人物・お店・賞金とは関係ありません。"
FONT_PATHS = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"]
PRICE_PER_SEC = {"veo-3.1-lite-generate-preview": 0.05, "veo-3.1-fast-generate-preview": 0.10,
                 "veo-3.1-generate-preview": 0.40}


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    raise FileNotFoundError("日本語フォント（Noto Sans CJK）が見つかりません")


# ---------- 1. Veo で映像を作る ----------
def generate_video(spec: dict, out: Path) -> Path:
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY が未設定です（GitHubのSecretsに入れてください）")
    model = spec.get("model", "veo-3.1-fast-generate-preview")
    dur = int(spec.get("duration", 8))
    print(f"Veo で映像を作ります：{model}／{dur}秒（目安 {PRICE_PER_SEC.get(model, 0) * dur:.2f}ドル）", flush=True)
    client = genai.Client(api_key=key)
    op = client.models.generate_videos(
        model=model,
        prompt=spec["prompt"],
        config=types.GenerateVideosConfig(
            aspect_ratio="9:16",
            resolution=spec.get("resolution", "720p"),
            duration_seconds=dur,
            negative_prompt=spec.get("negative_prompt", "text, subtitles, captions, watermark, logo"),
            number_of_videos=1,
        ),
    )
    waited = 0
    while not op.done:
        if waited > 900:
            sys.exit("Veo の映像づくりが15分で終わりませんでした")
        time.sleep(10)
        waited += 10
        op = client.operations.get(op)
    if getattr(op, "error", None):
        sys.exit(f"Veo がエラーを返しました：{op.error}")
    vids = (op.response.generated_videos if op.response else None) or []
    if not vids:
        sys.exit(f"映像が返ってきませんでした（安全のため弾かれた可能性）：{op.response}")
    client.files.download(file=vids[0].video)
    raw = out / "raw.mp4"
    vids[0].video.save(str(raw))
    print(f"映像を受け取りました（{waited}秒待ち）", flush=True)
    return raw


# ---------- 2. 文字を重ねる ----------
def _draw_rich(d: ImageDraw.ImageDraw, text: str, y: int, size: int, color, accent, stroke=10) -> int:
    """1行ずつ中央ぞろえで描く。*囲み* はアクセント色。戻り値：次の行のy。"""
    f = font(size)
    for line in text.split("\n"):
        parts = [(p, i % 2 == 1) for i, p in enumerate(line.split("*")) if p]
        width = sum(d.textlength(p, font=f) for p, _ in parts)
        x = (W - width) / 2
        for p, acc in parts:
            d.text((x, y), p, font=f, fill=accent if acc else color, stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += d.textlength(p, font=f)
        y += int(size * 1.22)
    return y


def overlay_main(spec: dict, path: Path) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 230
    if spec.get("badge"):
        f = font(44)
        tw = d.textlength(spec["badge"], font=f)
        d.rounded_rectangle([(W - tw) / 2 - 28, y, (W + tw) / 2 + 28, y + 76], 38, fill=(230, 40, 40))
        d.text(((W - tw) / 2, y + 8), spec["badge"], font=f, fill="white")
        y += 110
    y = _draw_rich(d, spec["hook"], y, int(spec.get("hook_size", 104)), "white", (255, 214, 0))
    if spec.get("sub"):
        _draw_rich(d, spec["sub"], y + 10, 54, "white", (255, 214, 0), stroke=7)
    f = font(28)                                               # AI表記（下のUIにかからない高さ）
    tw = d.textlength("AI生成映像", font=f)
    d.rounded_rectangle([W - tw - 80, 1500, W - 40, 1552], 12, fill=(0, 0, 0, 150))
    d.text((W - tw - 60, 1506), "AI生成映像", font=f, fill="white")
    img.save(path)


def overlay_end(spec: dict, path: Path) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _draw_rich(d, spec["end_text"], 860, 96, "white", (255, 214, 0))
    img.save(path)


def probe(path: Path) -> tuple[float, bool]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    return float(j["format"]["duration"]), any(s.get("codec_type") == "audio" for s in j.get("streams", []))


def compose(spec: dict, raw: Path, out: Path) -> dict:
    dur, has_audio = probe(raw)
    overlay_main(spec, out / "ov_main.png")
    inputs = ["-i", str(raw), "-i", str(out / "ov_main.png")]
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[bg];"
          f"[bg][1:v]overlay=0:0[v1]")
    last = "v1"
    if spec.get("end_text"):
        overlay_end(spec, out / "ov_end.png")
        inputs += ["-i", str(out / "ov_end.png")]
        t0 = max(dur - float(spec.get("end_secs", 1.6)), 0)
        fc += f";[v1][2:v]overlay=0:0:enable='gte(t,{t0:.2f})'[v2]"
        last = "v2"
    audio = ["-map", "0:a", "-c:a", "aac", "-b:a", "160k"] if has_audio else \
        ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs]
    if has_audio:
        cmd += ["-filter_complex", fc, "-map", f"[{last}]", *audio]
    else:
        cmd += audio + ["-filter_complex", fc, "-map", f"[{last}]", "-map", f"{len(inputs) // 2}:a",
                        "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out / "reel.mp4")]
    subprocess.run(cmd, check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.0, dur / 3):.2f}", "-i",
                    str(out / "reel.mp4"), "-frames:v", "1", "-q:v", "3", str(out / "cover.jpg")], check=True)
    return {"duration_seconds": round(dur, 2), "has_audio": has_audio}


def caption(spec: dict) -> str:
    tags = spec.get("hashtags", [])[:5]                         # ハッシュタグは5個まで
    text = spec.get("caption", "").strip()
    return f"{text}\n\n{AI_NOTE}\n\n{' '.join(tags)}".strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("out")
    ap.add_argument("--video", help="Veo を使わず、この映像に文字を重ねる")
    a = ap.parse_args()
    spec = json.loads(Path(a.script).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = Path(a.video) if a.video else generate_video(spec, out)
    info = compose(spec, raw, out)
    cap = caption(spec)
    (out / "投稿情報.md").write_text(f"# {spec.get('id', '')}\n\n## 投稿文\n\n{cap}\n\n## 映像の指示（Veo）\n\n"
                                     f"{spec['prompt']}\n", encoding="utf-8")
    info["caption_chars"] = len(cap)
    (out / "qa.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
