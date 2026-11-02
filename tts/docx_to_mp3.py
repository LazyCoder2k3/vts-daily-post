"""Đọc file Word (.docx) thành MP3 bằng giọng Neural tiếng Việt của Microsoft Edge TTS.

Cách dùng:
    python tts/docx_to_mp3.py INPUT.docx OUTPUT.mp3 [--voice vi-VN-NamMinhNeural]

Mỗi đoạn văn được đọc riêng rồi ghép lại với khoảng lặng: dài hơn sau tiêu đề,
ngắn hơn giữa các đoạn, để giọng đọc có nhịp nghỉ tự nhiên như người kể chuyện.
Cần cài: pip install edge-tts python-docx ; và có ffmpeg trong PATH.
"""

import argparse
import asyncio
import os
import re
import ssl
import subprocess
import sys
import tempfile

import docx
import edge_tts
import edge_tts.communicate

# Môi trường có proxy riêng thì trỏ edge-tts tới CA bundle của proxy (vẫn giữ kiểm tra TLS).
_CA = os.environ.get("EDGE_TTS_CA_BUNDLE") or (
    "/root/.ccr/ca-bundle.crt" if os.path.exists("/root/.ccr/ca-bundle.crt") else None
)
if _CA:
    edge_tts.communicate._SSL_CTX = ssl.create_default_context(cafile=_CA)

# Giọng mặc định: nam trầm hơn một chút, đọc chậm vừa phải cho văn chính luận, lịch sử.
VOICE_PRESETS = {
    "vi-VN-NamMinhNeural": {"rate": "-8%", "pitch": "-2Hz"},
    "vi-VN-HoaiMyNeural": {"rate": "-8%", "pitch": "+0Hz"},
}

# Sửa lỗi chính tả trong văn bản gốc để máy đọc đúng âm.
CORRECTIONS = {
    "từ sáng đến tói": "từ sáng đến tối",
    "mãnh da tay": "mảnh da tay",
    "đàm đìa": "đầm đìa",
    "mẫu chuyện": "mẩu chuyện",
    # Đọc tên riêng theo kiểu Việt, tránh ngắt câu sau chữ viết tắt.
    "A. Kan": "A Kan",
    "Paris": "Pa-ri",
}

PAUSE_AFTER_HEADING = 1.5
PAUSE_BETWEEN_PARAGRAPHS = 1.3
PAUSE_BEFORE_SECTION = 1.9


def clean(text: str) -> str:
    for wrong, right in CORRECTIONS.items():
        text = text.replace(wrong, right)
    # Giọng đọc phát ra một tiếng nhỏ ở dấu ngoặc kép, nên bỏ dấu này khi đọc:
    # ngoặc đóng giữa câu thành dấu phẩy, còn lại xoá đi (dấu hai chấm đã tạo nhịp nghỉ).
    text = re.sub(r"(?<=\w)[”\"]\s+(?=\w)", ", ", text)
    text = re.sub(r"[“”\"]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_heading(text: str) -> bool:
    return len(text.split()) <= 15 and not re.search(r"[.!?…:;)]$", text)


def build_segments(path: str):
    """Trả về danh sách (câu cần đọc, số giây im lặng sau câu đó)."""
    paragraphs = [clean(p.text) for p in docx.Document(path).paragraphs]
    paragraphs = [p for p in paragraphs if p]
    segments = []
    for i, text in enumerate(paragraphs):
        heading = is_heading(text)
        if heading:
            if text.isupper():
                text = text.capitalize().replace(" bác", " Bác")
            text += "."
        # Dòng trích nguồn trong ngoặc: bỏ ngoặc, đọc như một câu dẫn.
        if text.startswith("(") and text.endswith(")."):
            text = text[1:-2].strip() + "."
            text = re.sub(r"^Trích [Cc]uốn sách ", "Trích cuốn sách: ", text)
        # Ngoặc đơn giữa câu: đọc như một ý chêm, ngắt bằng dấu phẩy.
        text = re.sub(r"\s*\(([^)]*)\)\s*", r", \1, ", text)
        next_is_heading = i + 1 < len(paragraphs) and is_heading(paragraphs[i + 1])
        if heading:
            pause = PAUSE_AFTER_HEADING
        elif next_is_heading:
            pause = PAUSE_BEFORE_SECTION
        else:
            pause = PAUSE_BETWEEN_PARAGRAPHS
        segments.append((text, pause))
    return segments


async def synth(text: str, voice: str, out: str, rate: str, pitch: str) -> None:
    for attempt in range(6):
        try:
            await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(out)
            if os.path.getsize(out) > 0:
                return
        except Exception as exc:  # mạng chập chờn: thử lại
            if attempt == 5:
                raise
            print(f"  thử lại ({exc})", file=sys.stderr)
        await asyncio.sleep(2 ** attempt)


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--voice", default="vi-VN-NamMinhNeural")
    ap.add_argument("--rate")
    ap.add_argument("--pitch")
    args = ap.parse_args()

    preset = VOICE_PRESETS.get(args.voice, {"rate": "-8%", "pitch": "+0Hz"})
    rate = args.rate or preset["rate"]
    pitch = args.pitch or preset["pitch"]

    segments = build_segments(args.input)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for n, (text, pause) in enumerate(segments):
            print(f"[{n + 1}/{len(segments)}] {text[:70]}")
            mp3 = os.path.join(tmp, f"{n:03d}.mp3")
            await synth(text, args.voice, mp3, rate, pitch)
            wav = os.path.join(tmp, f"{n:03d}.wav")
            # Cắt khoảng lặng thừa ở hai đầu, rồi thêm khoảng nghỉ đúng ý ở cuối đoạn.
            trim = "silenceremove=start_periods=1:start_threshold=-50dB"
            run(["ffmpeg", "-y", "-i", mp3, "-af",
                 f"{trim},areverse,{trim},areverse,apad=pad_dur={pause}",
                 "-ar", "24000", "-ac", "1", wav])
            parts.append(wav)

        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w") as f:
            f.write("".join(f"file '{p}'\n" for p in parts))
        # Nghỉ 0,5 giây ở đầu, chuẩn hoá âm lượng để phát qua loa cho đều.
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing,
             "-af", "adelay=500,loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.3",
             "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k",
             "-id3v2_version", "3", "-metadata", "title=Trường học của Bác",
             "-metadata", "artist=" + args.voice, args.output])
    print("Xong:", args.output)


if __name__ == "__main__":
    asyncio.run(main())
