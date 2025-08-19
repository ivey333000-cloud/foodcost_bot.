# main.py — AI-завод: эксперты (OpenRouter) + HeyGen-видео + выбор модели через /models
# Требования: python-telegram-bot==21.4, requests, python-dotenv

import os
import time
import json
import logging
import tempfile
import subprocess
from pathlib import Path

from collections import deque
from typing import Dict, Any

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ── ENV ───────────────────────────────────────────────────────────────────────
load_dotenv(".env")

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY   = os.getenv("OPENROUTER_API_KEY")
ENV                  = os.getenv("ENV", "local")
APP_TITLE            = os.getenv("APP_TITLE", "AI Orchestrator")
ORIGIN_URL           = os.getenv("ORIGIN_URL", "https://example.com")

# HeyGen
HEYGEN_API_KEY       = os.getenv("HEYGEN_API_KEY")
HEYGEN_AVATAR_ID     = os.getenv("HEYGEN_AVATAR_ID", "Daisy-inskirt-20220818")
HEYGEN_VOICE_ID_EN   = os.getenv("HEYGEN_VOICE_ID_EN", "2d5b0e6cf36f460aa7fc47e3eee4ba54")
HEYGEN_VOICE_ID_RU   = os.getenv("HEYGEN_VOICE_ID_RU")   # добавь, если хочешь /video_ru

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("orchestrator")
BUILD_TAG = "orchestrator v2.0 (experts + HeyGen + /models)"

# ── Эксперты (дефолтные модели) ───────────────────────────────────────────────
EXPERTS: Dict[str, Dict[str, Any]] = {
    "kitchen_technologist": {
        "title": "👨‍🍳 Кухонный технолог",
        "system": "Ты опытный шеф-технолог HoReCa. Дай точные, практичные ответы про ТТК, себестоимость, КБЖУ.",
        "model": "openai/gpt-4o-mini",
        "max_history": 16,
    },
    "copywriter": {
        "title": "✍️ Копирайтер",
        "system": "Ты копирайтер. Пиши структуры и тексты лендингов, офферы, преимущества, CTA.",
        "model": "anthropic/claude-3.5-sonnet",
        "max_history": 14,
    },
    "video_script": {
        "title": "🎬 Сценарист видео",
        "system": "Ты сценарист коротких видео. Дай хуки, структуру, таймкоды, идеи кадров.",
        "model": "openai/gpt-4o-mini",
        "max_history": 14,
    },
    "telegram_python": {
        "title": "🤖 Эксперт по Python-ботам",
        "system": "Ты Python-разработчик. Помогаешь с python-telegram-bot v21+, pandas, openpyxl.",
        "model": "meta-llama/llama-3.1-70b-instruct",
        "max_history": 16,
    },
    "horeca_fin": {
        "title": "📈 Финансовый аналитик HoReCa",
        "system": "Ты считаешь себестоимость, маржу, точки безубыточности. Дай формулы и примеры.",
        "model": "openrouter/cinematika-7b-instruct",
        "max_history": 14,
    },
    "smm_strategy": {
        "title": "📣 SMM-стратег",
        "system": "Ты SMM-стратег. Делаешь контент-план, рубрики, идеи Reels/Shorts, KPI.",
        "model": "google/gemini-flash-1.5",
        "max_history": 12,
    },
}
EXPERT_BTNS = [[InlineKeyboardButton(e["title"], callback_data=f"expert:{key}")]
               for key, e in EXPERTS.items()]

# ── Память (история/выбор эксперта) ───────────────────────────────────────────
USER_CONTEXT: Dict[str, Dict[str, deque]] = {}
ACTIVE_EXPERT_BY_USER: Dict[str, str] = {}

def _ukey(user_id: int) -> str: return str(user_id)

def get_or_init_ctx(user_id: int, expert_key: str) -> deque:
    u = _ukey(user_id)
    USER_CONTEXT.setdefault(u, {})
    if expert_key not in USER_CONTEXT[u]:
        USER_CONTEXT[u][expert_key] = deque([{"role": "system", "content": EXPERTS[expert_key]["system"]}])
    return USER_CONTEXT[u][expert_key]

def trim_history(history: deque, max_items: int) -> None:
    while len(history) > max_items:
        if history and history[0].get("role") == "system":
            if len(history) > 1:
                history.remove(history[1])
            else:
                break
        else:
            history.popleft()

# ── OpenRouter /models выбор пользователем ───────────────────────────────────
OPENROUTER_MODELS_CACHE = {"ts": 0, "items": []}  # кэш списка моделей
USER_MODEL_OVERRIDE: Dict[str, str] = {}          # user_id -> выбранная модель
OPENROUTER_CACHE_TTL = 3600                       # сек

def fetch_openrouter_models() -> list:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("Нет OPENROUTER_API_KEY")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Accept": "application/json",
        "HTTP-Referer": ORIGIN_URL,
        "X-Title": APP_TITLE,
    }
    r = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=60)
    r.raise_for_status()
    items = r.json().get("data", [])
    models = []
    for m in items:
        mid = m.get("id")
        if not mid:
            continue
        label = (m.get("name") or mid)
        if len(label) > 40:
            label = label[:37] + "..."
        models.append({"id": mid, "label": label})
    models.sort(key=lambda x: (not (x["id"].startswith("openai/") or x["id"].startswith("anthropic/")), x["id"]))
    return models

def get_openrouter_models_cached() -> list:
    now = time.time()
    if OPENROUTER_MODELS_CACHE["items"] and now - OPENROUTER_MODELS_CACHE["ts"] < OPENROUTER_CACHE_TTL:
        return OPENROUTER_MODELS_CACHE["items"]
    items = fetch_openrouter_models()
    OPENROUTER_MODELS_CACHE.update({"ts": now, "items": items})
    return items

def paginate(lst: list, page: int, per_page: int = 8):
    total = max(1, (len(lst) + per_page - 1) // per_page)
    page = max(1, min(page, total))
    start = (page - 1) * per_page
    return lst[start:start + per_page], page, total

def kb_models_page(models: list, page: int = 1) -> InlineKeyboardMarkup:
    page_items, page, total = paginate(models, page, per_page=8)
    rows = [[InlineKeyboardButton(m["label"], callback_data=f"orm:pick:{m['id']}")] for m in page_items]
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("«", callback_data=f"orm:page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total}", callback_data="orm:none"))
    if page < total: nav.append(InlineKeyboardButton("»", callback_data=f"orm:page:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="orm:refresh"),
                 InlineKeyboardButton("❌ Сбросить",  callback_data="orm:reset")])
    return InlineKeyboardMarkup(rows)

# ── OpenRouter chat completion ────────────────────────────────────────────────
def call_openrouter(model: str, messages: list) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": ORIGIN_URL,
        "X-Title": APP_TITLE,
    }
    payload = {"model": model, "messages": messages, "max_tokens": 800, "temperature": 0.5}
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers=headers, data=json.dumps(payload), timeout=90)
    r.raise_for_status()
    data = r.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"text": content}

# ── HeyGen API ────────────────────────────────────────────────────────────────
BASE_V2 = "https://api.heygen.com/v2"
BASE_V1 = "https://api.heygen.com/v1"

def _headers_heygen():
    if not HEYGEN_API_KEY:
        raise RuntimeError("HEYGEN_API_KEY не задан в .env")
    return {"X-Api-Key": HEYGEN_API_KEY, "Content-Type": "application/json", "Accept": "application/json"}

def heygen_generate_video_sync(text: str, voice_id: str, language: str) -> str:
    payload = {
        "video_inputs": [
            {
                "character": {"type": "avatar", "avatar_id": HEYGEN_AVATAR_ID, "avatar_style": "normal"},
                "voice": {"type": "text", "voice_id": voice_id, "language": language, "input_text": text}
            }
        ],
        "dimension": {"width": 720, "height": 1280}  # вертикаль 9:16
    }
    r = requests.post(f"{BASE_V2}/video/generate", headers=_headers_heygen(),
                      data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    d = r.json().get("data") or {}
    vid = d.get("video_id")
    if not vid:
        raise RuntimeError(f"Не получил video_id: {r.text}")
    return vid

def heygen_wait_result_sync(video_id: str, timeout_sec: int = 600, poll_every: int = 5) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < timeout_sec:
        r = requests.get(f"{BASE_V1}/video_status.get", headers=_headers_heygen(),
                         params={"video_id": video_id}, timeout=60)
        r.raise_for_status()
        d = r.json().get("data") or {}
        status = d.get("status")
        if status == "completed":
            return {"status": "completed", "video_url": d.get("video_url") or d.get("url"), "raw": d}
        if status == "failed":
            raise RuntimeError(f"Рендер упал: {d}")
        time.sleep(poll_every)
    raise TimeoutError("Ожидание рендера превысило 10 минут.")

# ── Команды/хендлеры ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("Привет! Я твой ИИ-завод.\n\n"
           "• Выбери эксперта и просто пиши текст — я отвечу.\n"
           "• Видео-аватар: /video (EN), /video_ru (RU)\n"
           "• Выбор любой модели OpenRouter: /models\n")
    await update.message.reply_text(txt + "\nВыбери эксперта:", reply_markup=InlineKeyboardMarkup(EXPERT_BTNS))

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BUILD_TAG)

async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ukey = _ukey(update.effective_user.id)
    exp_key = ACTIVE_EXPERT_BY_USER.get(ukey)
    if not exp_key:
        await update.message.reply_text("Эксперт не выбран. Нажми /start и выбери эксперта.")
        return
    current = USER_MODEL_OVERRIDE.get(ukey) or EXPERTS[exp_key]["model"]
    await update.message.reply_text(f"🔧 Активный эксперт: {EXPERTS[exp_key]['title']}\n🧠 Модель: `{current}`",
                                    parse_mode="Markdown")

async def video_cfg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        f"🧑‍🎤 avatar_id: `{HEYGEN_AVATAR_ID}`",
        f"🔊 voice_en: `{HEYGEN_VOICE_ID_EN}`",
        f"🔊 voice_ru: `{HEYGEN_VOICE_ID_RU or '(not set)'}`",
    ]
    await update.message.reply_text("Текущие параметры HeyGen:\n" + "\n".join(lines), parse_mode="Markdown")

async def expert_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    expert_key = q.data.split(":", 1)[1]
    u = _ukey(q.from_user.id)
    ACTIVE_EXPERT_BY_USER[u] = expert_key
    get_or_init_ctx(q.from_user.id, expert_key)
    await q.edit_message_text(
        f"✅ Эксперт выбран: {EXPERTS[expert_key]['title']}\n\n"
        "Теперь *просто напиши сообщение* — отвечу от лица выбранного эксперта.\n"
        "_Или используй команду:_ /ask <вопрос>", parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обычное текстовое сообщение → ответ модели (учёт выбранного эксперта и /models-override)."""
    user_id = update.effective_user.id
    u = _ukey(user_id)
    expert_key = ACTIVE_EXPERT_BY_USER.get(u)
    if not expert_key:
        await update.message.reply_text("Сначала выбери эксперта командой /start")
        return

    msg_text = (update.message.text or "").strip()
    if not msg_text:
        return

    hist = get_or_init_ctx(user_id, expert_key)
    hist.append({"role": "user", "content": msg_text})
    trim_history(hist, EXPERTS[expert_key]["max_history"])

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        if not OPENROUTER_API_KEY:
            await update.message.reply_text("Нет OPENROUTER_API_KEY в .env — добавь ключ OpenRouter и перезапусти бота.")
            return

        model = USER_MODEL_OVERRIDE.get(u) or EXPERTS[expert_key]["model"]
        logger.info("Using model %s for expert %s (user %s)", model, expert_key, u)

        result = call_openrouter(model, list(hist))
        ans = (result.get("text") or "").strip() or "(пустой ответ)"

        hist.append({"role": "assistant", "content": ans})
        trim_history(hist, EXPERTS[expert_key]["max_history"])
        await update.message.reply_text(ans)
    except requests.HTTPError as e:
        body = e.response.text[:600] if e.response is not None else ""
        logger.exception("HTTP ошибка OpenRouter")
        await update.message.reply_text(f"Ошибка OpenRouter HTTP {getattr(e.response, 'status_code', '?')}.\n{body}")
    except requests.Timeout:
        logger.exception("Timeout OpenRouter")
        await update.message.reply_text("⏳ Таймаут запроса к модели. Попробуй ещё раз или короче запрос.")
    except Exception as e:
        logger.exception("Сбой при вызове модели")
        await update.message.reply_text(f"Ошибка: {e}")

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос в одной строке: /ask текст"""
    u = _ukey(update.effective_user.id)
    exp = ACTIVE_EXPERT_BY_USER.get(u)
    if not exp:
        await update.message.reply_text("Сначала выбери эксперта командой /start")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /ask <ваш вопрос>")
        return
    update.message.text = text
    await handle_text(update, context)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Voice/Audio → ffmpeg (ogg→wav) → OpenRouter Whisper (STT) → handle_text.
    Делает полноценную диагностику, если OpenRouter вернул не-JSON.
    """
    if not OPENROUTER_API_KEY:
        await update.message.reply_text("Нет OPENROUTER_API_KEY в .env — добавь ключ OpenRouter и перезапусти бота.")
        return

    v = update.message.voice or update.message.audio
    if not v:
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    tgfile = await context.bot.get_file(v.file_id)

    try:
        import tempfile, subprocess
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            in_path  = Path(tmpdir) / "input.ogg"
            wav_path = Path(tmpdir) / "input.wav"

            await tgfile.download_to_drive(str(in_path))

            # ffmpeg: ogg/opus → wav 16k mono
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(in_path), "-ar", "16000", "-ac", "1", str(wav_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            # --- ВАЖНО: корректные заголовки для OpenRouter ---
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Accept": "application/json",
                "HTTP-Referer": ORIGIN_URL,
                "X-Title": APP_TITLE,
            }

            # Отправляем в Whisper через OpenRouter
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/audio/transcriptions",
                    headers=headers,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={
                        "model": "openai/whisper-large-v3",
                        # опционально:
                        "translate": "false",
                        "temperature": "0",
                        # "language": "ru",  # можно принудительно указать язык, если надо
                    },
                    timeout=180
                )

            status = resp.status_code
            ctype = resp.headers.get("Content-Type", "")
            body_preview = resp.text[:800] if resp.text else ""

            # Если ошибка HTTP — покажем тело ответа
            if status >= 400:
                await update.message.reply_text(
                    f"Ошибка распознавания (HTTP {status}).\n"
                    f"Ответ сервера:\n{body_preview}"
                )
                return

            # Пытаемся разобрать JSON; если не JSON — тоже сообщим
            try:
                data = resp.json()
            except Exception:
                await update.message.reply_text(
                    "Сервис распознавания вернул не-JSON.\n"
                    f"Content-Type: {ctype}\n"
                    f"Первые 300 символов ответа:\n{body_preview[:300]}"
                )
                return

            text = (data or {}).get("text", "").strip()
            if not text:
                # Иногда OpenRouter кладёт результат по другому ключу
                text = (data.get("data", {}) or {}).get("text", "").strip()

            if not text:
                await update.message.reply_text(
                    "Не удалось распознать речь. Тело ответа:\n" + body_preview[:500]
                )
                return

            # Готово — прокидываем как обычный текст
            update.message.text = text
            await handle_text(update, context)

    except subprocess.CalledProcessError:
        await update.message.reply_text("Не удалось запустить ffmpeg. Убедись, что он установлен: `brew install ffmpeg`.")
    except requests.Timeout:
        await update.message.reply_text("Таймаут запроса к распознаванию. Попробуй ещё раз.")
    except Exception as e:
        logger.exception("voice/STT error")
        await update.message.reply_text(f"Ошибка распознавания: {e}")




# ── /models: выбор модели OpenRouter ─────────────────────────────────────────
async def models_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        models = get_openrouter_models_cached()
        u = _ukey(update.effective_user.id)
        current = USER_MODEL_OVERRIDE.get(u)
        head = "🧠 Выбор модели OpenRouter\n"
        head += f"Сейчас активна: `{current}`\n\n" if current else "Сейчас активна модель эксперта по умолчанию.\n\n"
        await update.message.reply_text(head + "Выбери модель:",
                                        reply_markup=kb_models_page(models, page=1),
                                        parse_mode="Markdown")
    except Exception as e:
        logger.exception("models_cmd error")
        await update.message.reply_text(f"Не удалось получить список моделей: {e}")

async def models_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "orm:none":
        return
    if data == "orm:refresh":
        OPENROUTER_MODELS_CACHE["items"] = []
        models = get_openrouter_models_cached()
        await q.edit_message_reply_markup(reply_markup=kb_models_page(models, page=1))
        return
    if data == "orm:reset":
        USER_MODEL_OVERRIDE.pop(_ukey(q.from_user.id), None)
        models = get_openrouter_models_cached()
        await q.edit_message_text("✅ Сброс: используем модель эксперта по умолчанию.",
                                  reply_markup=kb_models_page(models, page=1))
        return
    if data.startswith("orm:page:"):
        page = int(data.split(":")[-1])
        models = get_openrouter_models_cached()
        await q.edit_message_reply_markup(reply_markup=kb_models_page(models, page=page))
        return
    if data.startswith("orm:pick:"):
        model_id = data.split(":", 2)[2]
        USER_MODEL_OVERRIDE[_ukey(q.from_user.id)] = model_id
        models = get_openrouter_models_cached()
        await q.edit_message_text(f"✅ Модель установлена:\n`{model_id}`\n\nПиши сообщение — отвечу этой моделью.",
                                  reply_markup=kb_models_page(models, page=1),
                                  parse_mode="Markdown")
        return

# ── HeyGen команды ───────────────────────────────────────────────────────────
async def video_en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not HEYGEN_API_KEY:
        await update.message.reply_text("В .env нет HEYGEN_API_KEY — добавь ключ и перезапусти бота.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Как пользоваться:\n/video Hello! Testing avatar video for Instagram Reels.")
        return
    await update.message.reply_text("🎬 Запускаю рендер в HeyGen (EN)…")
    try:
        vid = await context.application.run_in_executor(None, heygen_generate_video_sync, text, HEYGEN_VOICE_ID_EN, "en-US")
        await update.message.reply_text(f"ID задачи: `{vid}`\nЖду готовности…", parse_mode="Markdown")
        res = await context.application.run_in_executor(None, heygen_wait_result_sync, vid)
        url = res.get("video_url")
        await update.message.reply_text(f"✅ Готово! {url}" if url else "Готово, но ссылка не найдена.")
    except requests.HTTPError as e:
        await update.message.reply_text(f"Ошибка HeyGen HTTP {getattr(e.response,'status_code','?')}.\n{e.response.text[:800] if e.response else ''}")
    except Exception as e:
        logger.exception("HeyGen error")
        await update.message.reply_text(f"Ошибка: {e}")

async def video_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not HEYGEN_API_KEY:
        await update.message.reply_text("В .env нет HEYGEN_API_KEY — добавь ключ и перезапусти бота.")
        return
    if not HEYGEN_VOICE_ID_RU:
        await update.message.reply_text("Добавь в .env: HEYGEN_VOICE_ID_RU=<твой_ru_voice_id> и перезапусти бота.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Как пользоваться:\n/video_ru Привет! Тест русского ролика.")
        return
    await update.message.reply_text("🎬 Запускаю рендер в HeyGen (RU)…")
    try:
        vid = await context.application.run_in_executor(None, heygen_generate_video_sync, text, HEYGEN_VOICE_ID_RU, "ru-RU")
        await update.message.reply_text(f"ID задачи: `{vid}`\nЖду готовности…", parse_mode="Markdown")
        res = await context.application.run_in_executor(None, heygen_wait_result_sync, vid)
        url = res.get("video_url")
        await update.message.reply_text(f"✅ Готово! {url}" if url else "Готово, но ссылка не найдена.")
    except requests.HTTPError as e:
        await update.message.reply_text(f"Ошибка HeyGen HTTP {getattr(e.response,'status_code','?')}.\n{e.response.text[:800] if e.response else ''}")
    except Exception as e:
        logger.exception("HeyGen error")
        await update.message.reply_text(f"Ошибка: {e}")

# ── Старт приложения ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    me = await app.bot.get_me()
    logger.info("🚀 Bot starting… %s", BUILD_TAG)
    logger.info("✅ Connected as: %s (@%s)", me.first_name, me.username)
    try:
        t = TELEGRAM_TOKEN
        res = requests.get(f"https://api.telegram.org/bot{t}/deleteWebhook", timeout=10).text
        logger.info("deleteWebhook: %s", res)
    except Exception:
        logger.warning("Не удалось удалить webhook (не критично)")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # сервисные
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("video_cfg", video_cfg_cmd))

    # эксперты
    app.add_handler(CallbackQueryHandler(expert_pick_cb, pattern=r"^expert:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("ask", ask_cmd))

    # выбор модели OpenRouter
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CallbackQueryHandler(models_cb, pattern=r"^orm:"))

    # HeyGen видео
    app.add_handler(CommandHandler("video",    video_en_cmd))
    app.add_handler(CommandHandler("video_ru", video_ru_cmd))

    logger.info("Bot started in %s mode", ENV)
    app.run_polling()

if __name__ == "__main__":
    main()
