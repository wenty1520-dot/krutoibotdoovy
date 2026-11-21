"""
Telegram-бот «Швейцарский нож».
Запуск:  set BOT_TOKEN=123:ABC   (Windows)   →   python bot.py

Что умеет:
  фото      → удалить фон, текст с фото (OCR), сжать, в JPG (в т.ч. HEIC)
  голосовое → в текст, в mp3
  видео     → в GIF, сжать, достать звук
  PDF       → в картинки, сжать
  ссылка    → сократить, QR, скачать видео
  текст     → QR-код
  команды   → /cur /unit /ohm /remind /note /notes /clearnotes /size /vsize
"""
import asyncio
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dotenv import load_dotenv
from functools import lru_cache
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:  # поддержка HEIC (фото с iPhone)
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

NOTES_FILE = Path("notes.json")
URL_RE = re.compile(r"https?://\S+")
HEAVY = asyncio.Semaphore(2)  # не больше 2 тяжёлых задач одновременно

ACTIONS = {
    "image": [("🪄 Удалить фон", "bg"), ("📝 Текст с фото", "ocr"),
              ("🗜 Сжать", "imgc"), ("🖼 В JPG", "jpg")],
    "voice": [("📝 В текст", "stt"), ("🎵 В mp3", "mp3")],
    "video": [("🎞 В GIF", "gif"), ("🗜 Сжать", "vidc"), ("🎵 Звук в mp3", "mp3")],
    "pdf": [("🖼 В картинки", "pdf2img"), ("🗜 Сжать", "pdfc")],
    "url": [("🔗 Сократить", "short"), ("▦ QR-код", "qr"), ("⬇️ Скачать видео", "dl")],
    "text": [("▦ QR-код", "qr")],
}

HELP = (
    "Пришли мне файл или ссылку — я предложу, что с ними сделать.\n\n"
    "📷 Фото: удалить фон, распознать текст, сжать, в JPG (HEIC тоже)\n"
    "🎤 Голосовое: в текст, в mp3\n"
    "🎬 Видео: в GIF, сжать, вытащить звук\n"
    "📄 PDF: в картинки, сжать\n"
    "🔗 Ссылка: сократить, QR, скачать видео\n"
    "✍️ Любой текст: QR-код\n\n"
    "Команды:\n"
    "/cur 100 usd eur — конвертер валют\n"
    "/unit 5 km miles — конвертер единиц\n"
    "/ohm U=12 I=2 — закон Ома и мощность (любые две из U, I, R, P)\n"
    "/remind 3h купить хлеб — напоминание (s, m, h, d; можно 1h30m)\n"
    "/note текст, /notes, /clearnotes — заметки\n"
    "/size 300 — целевой размер фото и PDF в КБ (сейчас по умолчанию 500)\n"
    "/vsize 10 — целевой размер видео в МБ (по умолчанию 10)"
)


# ───────────────────────── обработка файлов (синхронная часть) ─────────────────────────

def open_image(path):
    from PIL import Image, ImageOps

    img = Image.open(path)
    return ImageOps.exif_transpose(img)


@lru_cache(maxsize=1)
def _bg_session():
    from rembg import new_session

    return new_session("u2net")


def remove_bg(src, out):
    from rembg import remove

    img = open_image(src).convert("RGBA")
    img.thumbnail((2000, 2000))
    remove(img, session=_bg_session()).save(out, "PNG")


@lru_cache(maxsize=1)
def _ocr_reader():
    import easyocr

    return easyocr.Reader(["ru", "en"], gpu=False)


def ocr(src):
    import numpy as np

    img = open_image(src).convert("RGB")
    lines = _ocr_reader().readtext(np.array(img), detail=0, paragraph=True)
    return "\n".join(lines).strip()


def to_jpg(src, out):
    open_image(src).convert("RGB").save(out, "JPEG", quality=92)


def compress_image(src, out, target_kb):
    img = open_image(src).convert("RGB")
    target = target_kb * 1024
    scale = 1.0
    while True:
        w, h = img.size
        cur = img if scale == 1.0 else img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        for q in (90, 80, 70, 60, 50, 40, 30):
            buf = io.BytesIO()
            cur.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() <= target:
                out.write_bytes(buf.getvalue())
                return
        scale *= 0.8
        if scale < 0.1:
            out.write_bytes(buf.getvalue())
            return


def ffmpeg(*args):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("На ПК не установлен ffmpeg (см. инструкцию по установке).")
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)],
        capture_output=True, text=True,
    )
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr[-300:])


def media_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def video_to_gif(src, out):
    ffmpeg("-i", src, "-t", 15, "-filter_complex",
           "fps=12,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse", out)


def compress_video(src, out, target_mb):
    dur = max(media_duration(src), 1.0)
    total_kbps = target_mb * 8 * 1024 * 0.95 / dur
    v = int(total_kbps - 96)
    if v < 50:
        raise RuntimeError("Видео слишком длинное для такого размера. Увеличь /vsize.")
    ffmpeg("-i", src, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-b:v", f"{v}k", "-maxrate", f"{v}k", "-bufsize", f"{2 * v}k",
           "-vf", "scale='min(1280,iw)':-2", "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", out)


@lru_cache(maxsize=1)
def _whisper():
    from faster_whisper import WhisperModel

    return WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")


def transcribe(src):
    segments, _info = _whisper().transcribe(str(src), vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def pdf_to_images(src, tmp, limit=10):
    import fitz

    doc = fitz.open(src)
    paths = []
    for i, page in enumerate(doc):
        if i >= limit:
            break
        p = tmp / f"page_{i + 1:02d}.png"
        page.get_pixmap(dpi=150).save(p)
        paths.append(p)
    doc.close()
    return paths


def compress_pdf(src, out, target_kb):
    """Страницы перерисовываются в JPEG — текст в итоговом PDF не выделяется."""
    import fitz

    target = target_kb * 1024
    doc = fitz.open(src)
    for dpi, q in ((130, 70), (110, 55), (90, 45), (72, 35), (60, 28)):
        new = fitz.open()
        for page in doc:
            data = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=q)
            np_ = new.new_page(width=page.rect.width, height=page.rect.height)
            np_.insert_image(np_.rect, stream=data)
        new.save(out, deflate=True)
        new.close()
        if out.stat().st_size <= target:
            break
    doc.close()


def make_qr(text, out):
    import qrcode

    qrcode.make(text).save(out)


def shorten(url):
    api = "https://is.gd/create.php?format=simple&url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = r.read().decode().strip()
    if not res.startswith("http"):
        raise RuntimeError("Не удалось сократить ссылку: " + res[:100])
    return res


def download_video(url, tmp):
    import yt_dlp

    opts = {
        "outtmpl": str(tmp / "dl_%(id)s.%(ext)s"),
        "format": "b[height<=720][ext=mp4]/b[height<=720]/b",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": 49 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = list(tmp.glob("dl_*"))
    if not files:
        raise RuntimeError("Не удалось скачать (возможно, видео больше 50 МБ или закрыто).")
    return files[0]


_rates = {"t": 0.0, "d": {}}


def convert_currency(amount, a, b):
    if time.time() - _rates["t"] > 3600:
        req = urllib.request.Request("https://open.er-api.com/v6/latest/USD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            _rates["d"] = json.load(r)["rates"]
        _rates["t"] = time.time()
    rates = _rates["d"]
    a, b = a.upper(), b.upper()
    if a not in rates or b not in rates:
        raise ValueError("Неизвестная валюта. Пример: /cur 100 usd rub")
    return amount / rates[a] * rates[b]


def ohm_solve(v):
    U, I, R, P = (v.get(k) for k in "UIRP")
    if U is not None and I is not None:
        R, P = U / I, U * I
    elif U is not None and R is not None:
        I, P = U / R, U * U / R
    elif I is not None and R is not None:
        U, P = I * R, I * I * R
    elif U is not None and P is not None:
        I, R = P / U, U * U / P
    elif I is not None and P is not None:
        U, R = P / I, P / (I * I)
    elif R is not None and P is not None:
        U, I = math.sqrt(P * R), math.sqrt(P / R)
    else:
        raise ValueError("Нужны две величины, например: /ohm U=12 R=4")
    return U, I, R, P


def process(action, src, text, tmp, st):
    """Возвращает список результатов: (тип, значение)."""
    if action == "bg":
        o = tmp / "no_background.png"
        remove_bg(src, o)
        return [("doc", o)]
    if action == "ocr":
        return [("text", ocr(src) or "Текст не найден.")]
    if action == "imgc":
        o = tmp / "compressed.jpg"
        compress_image(src, o, st["kb"])
        return [("doc", o), ("text", f"Размер: {o.stat().st_size // 1024} КБ")]
    if action == "jpg":
        o = tmp / "photo.jpg"
        to_jpg(src, o)
        return [("doc", o)]
    if action == "stt":
        return [("text", transcribe(src) or "Речь не распознана.")]
    if action == "mp3":
        o = tmp / "audio.mp3"
        ffmpeg("-i", src, "-vn", "-codec:a", "libmp3lame", "-q:a", 4, o)
        return [("audio", o)]
    if action == "gif":
        o = tmp / "clip.gif"
        video_to_gif(src, o)
        return [("anim", o)]
    if action == "vidc":
        o = tmp / "compressed.mp4"
        compress_video(src, o, st["vmb"])
        return [("video", o)]
    if action == "pdf2img":
        return [("doc", p) for p in pdf_to_images(src, tmp)]
    if action == "pdfc":
        o = tmp / "compressed.pdf"
        compress_pdf(src, o, st["kb"])
        return [("doc", o), ("text", f"Размер: {o.stat().st_size // 1024} КБ")]
    if action == "qr":
        o = tmp / "qr.png"
        make_qr(text, o)
        return [("doc", o)]
    if action == "short":
        return [("text", shorten(text))]
    if action == "dl":
        return [("video", download_video(text, tmp))]
    raise ValueError("Неизвестное действие")


# ───────────────────────── Telegram ─────────────────────────

async def send_results(bot, chat_id, results):
    for kind, val in results:
        if kind == "text":
            for i in range(0, len(val), 4000):
                await bot.send_message(chat_id, val[i:i + 4000])
            continue
        with open(val, "rb") as fh:
            if kind == "doc":
                await bot.send_document(chat_id, fh, filename=Path(val).name)
            elif kind == "audio":
                await bot.send_audio(chat_id, fh)
            elif kind == "video":
                await bot.send_video(chat_id, fh, supports_streaming=True)
            elif kind == "anim":
                await bot.send_animation(chat_id, fh)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    p = None
    if m.photo:
        p = {"kind": "image", "file_id": m.photo[-1].file_id, "ext": ".jpg"}
    elif m.animation:
        p = {"kind": "video", "file_id": m.animation.file_id, "ext": ".mp4"}
    elif m.video:
        p = {"kind": "video", "file_id": m.video.file_id, "ext": ".mp4"}
    elif m.video_note:
        p = {"kind": "video", "file_id": m.video_note.file_id, "ext": ".mp4"}
    elif m.voice:
        p = {"kind": "voice", "file_id": m.voice.file_id, "ext": ".ogg"}
    elif m.audio:
        ext = Path(m.audio.file_name or "").suffix or ".mp3"
        p = {"kind": "voice", "file_id": m.audio.file_id, "ext": ext}
    elif m.document:
        d = m.document
        ext = Path(d.file_name or "").suffix.lower()
        mime = d.mime_type or ""
        if mime.startswith("image/") or ext in (".heic", ".heif"):
            kind = "image"
        elif ext == ".pdf" or mime == "application/pdf":
            kind = "pdf"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "voice"
        else:
            await m.reply_text("Такой формат я не поддерживаю.")
            return
        p = {"kind": kind, "file_id": d.file_id, "ext": ext or ".bin"}
    elif m.text:
        found = URL_RE.search(m.text)
        if found:
            p = {"kind": "url", "text": found.group(0)}
        else:
            p = {"kind": "text", "text": m.text}
    if not p:
        return

    store = context.chat_data
    store[m.message_id] = p
    while len(store) > 20:  # не копим старое
        store.pop(next(iter(store)))

    buttons = [InlineKeyboardButton(label, callback_data=f"{act}|{m.message_id}")
               for label, act in ACTIONS[p["kind"]]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await m.reply_text("Что сделать?", reply_markup=InlineKeyboardMarkup(rows),
                       reply_to_message_id=m.message_id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, mid = q.data.split("|")
    p = context.chat_data.get(int(mid))
    if not p:
        await q.edit_message_text("Файл устарел — пришли его заново.")
        return
    await q.edit_message_text("⏳ Работаю…")
    tmp = Path(tempfile.mkdtemp(prefix="tgbot_"))
    st = {"kb": context.user_data.get("kb", 500), "vmb": context.user_data.get("vmb", 10)}
    try:
        async with HEAVY:
            src = None
            if p.get("file_id"):
                f = await context.bot.get_file(p["file_id"])
                src = tmp / ("input" + p["ext"])
                await f.download_to_drive(src)
            results = await asyncio.to_thread(process, action, src, p.get("text"), tmp, st)
        await send_results(context.bot, q.message.chat_id, results)
        await q.edit_message_text("✅ Готово")
    except BadRequest as e:
        if "too big" in str(e).lower():
            await q.edit_message_text("❌ Файл больше 20 МБ — Telegram не отдаёт такие файлы ботам.")
        else:
            log.exception("telegram error")
            await q.edit_message_text(f"❌ Ошибка Telegram: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("processing error")
        await q.edit_message_text(f"❌ {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────── команды ─────────────────────────

async def cmd_cur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount, a, b = context.args
        res = await asyncio.to_thread(convert_currency, float(amount.replace(",", ".")), a, b)
        await update.message.reply_text(f"{amount} {a.upper()} = {res:,.2f} {b.upper()}")
    except ValueError as e:
        await update.message.reply_text(str(e) if "валют" in str(e) else "Пример: /cur 100 usd rub")
    except Exception:  # noqa: BLE001
        await update.message.reply_text("Пример: /cur 100 usd rub")


async def cmd_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import pint

        amount, a, b = context.args
        ureg = pint.UnitRegistry()
        res = (float(amount.replace(",", ".")) * ureg(a)).to(b)
        await update.message.reply_text(f"{amount} {a} = {res.magnitude:.6g} {b}")
    except Exception:  # noqa: BLE001
        await update.message.reply_text("Пример: /unit 5 km miles   или   /unit 100 celsius fahrenheit")


async def cmd_ohm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vals = {k.upper(): float(v.replace(",", "."))
            for k, v in re.findall(r"([UIRPuirp])\s*=\s*([\d.,]+)", " ".join(context.args))}
    try:
        U, I, R, P = ohm_solve(vals)
        await update.message.reply_text(
            f"U = {U:.4g} В\nI = {I:.4g} А\nR = {R:.4g} Ом\nP = {P:.4g} Вт")
    except (ValueError, ZeroDivisionError) as e:
        await update.message.reply_text(str(e) or "Пример: /ohm U=12 I=2")


async def _remind_cb(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(context.job.chat_id, "⏰ " + context.job.data)


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not re.fullmatch(r"(\d+[smhd])+", context.args[0].lower()):
        await update.message.reply_text("Пример: /remind 3h купить хлеб  (s, m, h, d; можно 1h30m)")
        return
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    secs = sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([smhd])", context.args[0].lower()))
    text = " ".join(context.args[1:])
    context.job_queue.run_once(_remind_cb, secs, chat_id=update.effective_chat.id, data=text)
    await update.message.reply_text(f"Ок, напомню через {context.args[0]}.")


def _load_notes():
    return json.loads(NOTES_FILE.read_text("utf-8")) if NOTES_FILE.exists() else {}


def _save_notes(d):
    NOTES_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /note купить кабель")
        return
    d = _load_notes()
    d.setdefault(str(update.effective_user.id), []).append(" ".join(context.args))
    _save_notes(d)
    await update.message.reply_text("Записал.")


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = _load_notes().get(str(update.effective_user.id), [])
    await update.message.reply_text(
        "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1)) or "Заметок нет.")


async def cmd_clearnotes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _load_notes()
    d.pop(str(update.effective_user.id), None)
    _save_notes(d)
    await update.message.reply_text("Заметки удалены.")


async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kb = int(context.args[0])
        assert 20 <= kb <= 20000
        context.user_data["kb"] = kb
        await update.message.reply_text(f"Целевой размер фото/PDF: {kb} КБ.")
    except Exception:  # noqa: BLE001
        await update.message.reply_text("Пример: /size 300  (в КБ, от 20 до 20000)")


async def cmd_vsize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mb = float(context.args[0])
        assert 1 <= mb <= 48
        context.user_data["vmb"] = mb
        await update.message.reply_text(f"Целевой размер видео: {mb} МБ.")
    except Exception:  # noqa: BLE001
        await update.message.reply_text("Пример: /vsize 10  (в МБ, от 1 до 48)")


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Задай переменную окружения BOT_TOKEN (токен от @BotFather).")
    app = (Application.builder().token(token).concurrent_updates(True)
           .read_timeout(60).write_timeout(120).connect_timeout(30).build())
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("cur", cmd_cur),
                     ("unit", cmd_unit), ("ohm", cmd_ohm), ("remind", cmd_remind),
                     ("note", cmd_note), ("notes", cmd_notes), ("clearnotes", cmd_clearnotes),
                     ("size", cmd_size), ("vsize", cmd_vsize)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    media = (filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO
             | filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION)
    app.add_handler(MessageHandler(media | (filters.TEXT & ~filters.COMMAND), on_message))
    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
