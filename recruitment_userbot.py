import asyncio
import json
import logging
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from io import BytesIO
from collections import defaultdict

from dotenv import load_dotenv
from openai import AsyncOpenAI
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from telethon import TelegramClient, events

# --------------------------------------------------------------------
# Setup – load .env
# --------------------------------------------------------------------
load_dotenv()

# Debug: show loaded values (remove after everything works)
print("DEBUG env:")
print("  API_ID:", os.getenv("API_ID"))
print("  API_HASH:", os.getenv("API_HASH")[:6] + "****" if os.getenv("API_HASH") else "None")
print("  PHONE:", os.getenv("PHONE"))
print("  OPENAI_API_KEY:", os.getenv("OPENAI_API_KEY")[:6] + "****" if os.getenv("OPENAI_API_KEY") else "None")

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# SQLite database for conversation state
DB = "candidates.db"

# Media storage folder
MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

# Dossier storage folder
DOSSIER_DIR = "dossiers"
os.makedirs(DOSSIER_DIR, exist_ok=True)

# Telegram client (userbot)
client = TelegramClient("recruitment_session", API_ID, API_HASH)

# --------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------
def init_db():
    """Create the candidates table if it doesn't exist."""
    db_is_new = not os.path.exists(DB)
    
    with sqlite3.connect(DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                user_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'chatting',
                conversation_history TEXT,
                specialization TEXT,
                legal_status TEXT,
                car_and_tools TEXT,
                location TEXT,
                rate TEXT,
                media_links TEXT,
                created_at TEXT
            )
            """
        )
        
        # Safe migration for existing database
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN phone_number TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
    
    # Verify the table exists
    with sqlite3.connect(DB) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='candidates'"
        )
        if cur.fetchone() is None:
            raise RuntimeError("Database initialization failed: 'candidates' table was not created.")
    
    if db_is_new:
        logger.info("Created fresh candidates.db with 'candidates' table.")
    else:
        logger.info("Using existing candidates.db — 'candidates' table verified.")

def get_user(user_id):
    """Return user data as a dict, or None."""
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {
            "user_id": row[0],
            "state": row[1],
            "conversation_history": json.loads(row[2]) if row[2] else [],
            "specialization": row[3],
            "legal_status": row[4],
            "car_and_tools": row[5],
            "location": row[6],
            "rate": row[7],
            "media_links": json.loads(row[8]) if row[8] else [],
            "created_at": row[9],
            "phone_number": row[10] if len(row) > 10 else None,
        }
    return None

def upsert_user(user_id, **kwargs):
    """Insert new user if not exists, else update only the given fields."""
    allowed = {
        "state", "conversation_history", "specialization", 
        "legal_status", "car_and_tools", "location", "rate", 
        "phone_number", "media_links", "created_at"
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return

    with sqlite3.connect(DB) as conn:
        cur = conn.execute("SELECT 1 FROM candidates WHERE user_id = ?", (user_id,))
        exists = cur.fetchone() is not None

        if exists:
            set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
            values = list(updates.values()) + [user_id]
            conn.execute(f"UPDATE candidates SET {set_clause} WHERE user_id = ?", values)
        else:
            columns = ", ".join(updates.keys())
            placeholders = ", ".join("?" for _ in updates)
            values = list(updates.values())
            conn.execute(
                f"INSERT INTO candidates (user_id, {columns}) VALUES (?, {placeholders})",
                [user_id] + values,
            )

# --------------------------------------------------------------------
# LLM helper for dynamic chat
# --------------------------------------------------------------------
SYSTEM_MESSAGE = """Главная задача – не выдать себя за бота. Разговор должен идти как будто ты живой человек.
Твоя роль: Ты — помощник рекрутера по имени Роберт. Ты ищешь строителей (каменщиков, гипсокартонщиков, бетонщиков) для объектов в Бельгии.
Твоя цель: Собрать 6 конкретных фактов о кандидате:
1. Точная специализация.
2. Легальный статус (Паспорт ЕС, Карта А, Польская виза, своя фирма BV/TVA).
3. Наличие своего авто и инструмента.
4. Текущая локация (город).
5. Желаемая ставка в час или за метр.
6. Номер телефона (для связи).
ТВОИ ПРАВИЛА ОБЩЕНИЯ (КРИТИЧЕСКИ ВАЖНО):
• Общайся очень коротко, по-мужски, без лишней вежливости. Используй сленг строителей (прораб, объект, внутрянка, гипс, фактура).
• ЗАДАВАЙ ТОЛЬКО ОДИН ВОПРОС ЗА РАЗ. Никогда не вываливай список вопросов.
• Подстраивайся под язык пользователя (Русский).
• Если человек увиливает от ответа про документы, настаивай. Без легальных документов мы не работаем.
• Как только соберешь все 6 фактов, СРАЗУ ЖЕ вызови функцию сохранения данных (save_candidate_data). 
ВАЖНО ДЛЯ ФУНКЦИИ СОХРАНЕНИЯ: Переведи все ответы на ИДЕАЛЬНЫЙ ПРОФЕССИОНАЛЬНЫЙ ГОЛЛАНДСКИЙ (Dutch) язык. Эти данные будет читать бельгийский работодатель. Оформляй ответы красиво и привлекательно (например, вместо "stenen" пиши "Ervaren metselaar", вместо "да" пиши "Beschikt over eigen vervoer en handgereedschap", и т.д.). Убедись, что термины строительства переведены максимально точно.
Суровый сленг и безграмотность: Люди будут писать: "я покрепи делал", "снельбау ложу", "есть оранж карта". – это нормально."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_candidate_data",
            "description": "Call this IMMEDIATELY after you have collected all 6 facts about the candidate. Do not wait for photos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialization": {
                        "type": "string",
                        "description": "The candidate's exact specialization (e.g. bricklayer, plasterer)."
                    },
                    "legal_status": {
                        "type": "string",
                        "description": "The candidate's legal status/documents (e.g. EU Passport, Polish visa)."
                    },
                    "car_and_tools": {
                        "type": "string",
                        "description": "Does the candidate have a car and tools?"
                    },
                    "location": {
                        "type": "string",
                        "description": "The candidate's current location (city)."
                    },
                    "rate": {
                        "type": "string",
                        "description": "Desired hourly or per-meter rate."
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "The candidate's phone number."
                    }
                },
                "required": ["specialization", "legal_status", "car_and_tools", "location", "rate", "phone_number"]
            }
        }
    }
]

async def generate_chat_response(user_id, text, user_history):
    user_history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_MESSAGE}] + user_history
    
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            temperature=0.8,
            max_tokens=250,
        )
        
        msg = response.choices[0].message
        
        if msg.tool_calls:
            tool_call = msg.tool_calls[0]
            if tool_call.function.name == "save_candidate_data":
                args = json.loads(tool_call.function.arguments)
                upsert_user(
                    user_id,
                    specialization=args.get("specialization"),
                    legal_status=args.get("legal_status"),
                    car_and_tools=args.get("car_and_tools"),
                    location=args.get("location"),
                    rate=args.get("rate"),
                    phone_number=args.get("phone_number"),
                    state="ask_media"
                )
                
                # Append tool execution
                assistant_msg_dict = msg.model_dump()
                assistant_msg_dict.pop("function_call", None)
                if assistant_msg_dict.get("content") is None:
                    assistant_msg_dict["content"] = ""
                user_history.append(assistant_msg_dict)

                user_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": "save_candidate_data",
                    "content": "Data saved successfully."
                })
                
                # We do not call the LLM again. Hand over to code for media collection.
                # Re-read user from DB to get fresh media_links (may include photos sent during chatting)
                fresh_user = get_user(user_id)
                photo_count = len(fresh_user["media_links"]) if fresh_user else 0
                if photo_count > 0:
                    final_msg = f"Отлично. Данные принял. Вижу у тебя уже есть {photo_count} фото. Можешь докинуть ещё или напиши 'готово'."
                else:
                    final_msg = "Отлично. Теперь скинь 3-4 фотографии своих работ (или видео)."
                user_history.append({"role": "assistant", "content": final_msg})
                upsert_user(user_id, conversation_history=json.dumps(user_history))
                return final_msg, True
        
        reply_text = msg.content.strip() if msg.content else "..."
        user_history.append({"role": "assistant", "content": reply_text})
        upsert_user(user_id, conversation_history=json.dumps(user_history))
        return reply_text, False
        
    except Exception as e:
        logger.error(f"LLM error: {e}", exc_info=True)
        return "Секунду, я перезвоню. Что-то связь оборвалась.", False

# --------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------
def is_valid_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False

# --------------------------------------------------------------------
# Dossier PDF generator
# --------------------------------------------------------------------
def create_dossier(user):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    story = []
    styles = getSampleStyleSheet()

    # Register Cyrillic font to fix black squares
    try:
        font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
        bold_font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans-Bold.ttf")
        
        pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold_font_path))
        pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold")
        
        styles["Title"].fontName = "DejaVuSans"
        styles["Normal"].fontName = "DejaVuSans"
    except Exception as e:
        logger.error(f"Failed to register Cyrillic font: {e}")

    story.append(Paragraph(f"Kandidaat", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>Specialisatie:</b> {user.get('specialization', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Documenten:</b> {user.get('legal_status', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Auto/Gereedschap:</b> {user.get('car_and_tools', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Locatie:</b> {user.get('location', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Tarief:</b> {user.get('rate', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Telefoon:</b> {user.get('phone_number', 'N/A')}", styles["Normal"]))

    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>Foto's van werk:</b>", styles["Normal"]))
    story.append(Spacer(1, 6))

    for path in user["media_links"]:
        full_path = os.path.join(MEDIA_DIR, path)
        if os.path.exists(full_path) and path.lower().endswith((".jpg", ".jpeg", ".png")):
            try:
                img_reader = ImageReader(full_path)
                iw, ih = img_reader.getSize()
                aspect = ih / float(iw)
                width = 4 * inch
                height = width * aspect
                
                # If height is too tall for a page, cap it and recalculate width
                if height > 5 * inch:
                    height = 5 * inch
                    width = height / aspect

                img = Image(full_path, width=width, height=height)
                story.append(img)
                story.append(Spacer(1, 6))
            except Exception as e:
                story.append(Paragraph(f"(Error loading image: {path})", styles["Normal"]))
                logger.error(f"Image error {path}: {e}")
        else:
            story.append(Paragraph(f"(File: {path})", styles["Normal"]))

    doc.build(story)
    buffer.seek(0)
    return buffer

# --------------------------------------------------------------------
# Concurrency control
# --------------------------------------------------------------------
user_locks = defaultdict(asyncio.Lock)
# Track how many times a user tried "готово" without photos
empty_done_attempts = defaultdict(int)

async def generate_and_send_pdf(user_id, client):
    """Generate dossier PDF and send to recruiter. No delay — caller decides timing."""
    async with user_locks[user_id]:
        user = get_user(user_id)
        if not user:
            return

        await client.send_message(
            user_id,
            "Принял. Роберт сейчас посмотрит твой профиль и наберет тебя, если есть объект под твои запросы."
        )

        try:
            pdf_buffer = create_dossier(user)
            dossier_filename = os.path.join(DOSSIER_DIR, f"dossier_{user_id}.pdf")
            with open(dossier_filename, "wb") as f:
                f.write(pdf_buffer.getvalue())

            # Tell Telethon what the file is called instead of "unnamed"
            pdf_buffer.name = f"dossier_{user_id}.pdf"

            # Send the PDF to Saved Messages ("me") so the candidate doesn't see it
            await client.send_file(
                "me",
                pdf_buffer,
                caption=f"Новый кандидат (ID {user_id}). Профиль готов.",
            )
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")

# --------------------------------------------------------------------
# Core message handler
# --------------------------------------------------------------------
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handle_message(event):
    user_id = event.chat_id
    
    async with user_locks[user_id]:
        print(f"DEBUG: message received from {user_id}: {event.text}") 
        try:
            text = event.text or ""
            user = get_user(user_id)

            # If brand new user, create entry
            if user is None:
                upsert_user(
                    user_id,
                    state="chatting",
                    conversation_history=json.dumps([]),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                user = get_user(user_id)

            # Process according to state
            state = user["state"]

            if state == "chatting":
                # Check if user sent a photo or video
                llm_text = text
                if event.photo:
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))
                    llm_text = (text + " [Пользователь прислал фото]").strip()
                elif event.video or (event.document and "video" in getattr(event.document, "mime_type", "")):
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))
                    llm_text = (text + " [Пользователь прислал видео]").strip()

                reply_msg, tool_called = await generate_chat_response(user_id, llm_text, user["conversation_history"])
                await event.respond(reply_msg)
                # Note: if tool_called, state is now ask_media, waiting for user response
                
            elif state == "ask_media":
                if event.photo or event.video or (event.document and "video" in getattr(event.document, "mime_type", "")):
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))
                    count = len(user["media_links"])
                    await event.respond(f"Принял. Всего фото: {count}. Можешь добавить ещё или напиши 'готово'.")
                else:
                    text_lower = text.strip().lower()
                    if text_lower in ("все", "всё", "готово", "done", "ok", "ок"):
                        if not user["media_links"]:
                            empty_done_attempts[user_id] += 1
                            if empty_done_attempts[user_id] >= 3:
                                await event.respond("Ок, отправлю профиль без фото. Если что, рекрутер запросит их сам.")
                                upsert_user(user_id, state="done")
                                await generate_and_send_pdf(user_id, client)
                            else:
                                remaining = 3 - empty_done_attempts[user_id]
                                await event.respond(f"Без фото профиль неполный. Пришли хотя бы 2-3 фотографии. Осталось попыток: {remaining}.")
                        else:
                            empty_done_attempts.pop(user_id, None)  # reset counter on success
                            upsert_user(user_id, state="done")
                            await generate_and_send_pdf(user_id, client)
                    else:
                        await event.respond("Жду фотографии твоих работ. Как скинешь все, напиши 'готово'.")

            elif state == "done":
                # If they send more media after done, just append and regenerate silently
                if event.photo or event.video or (event.document and "video" in getattr(event.document, "mime_type", "")):
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))
                else:
                    # Forward their question to the recruiter's Saved Messages
                    await client.send_message(
                        "me", 
                        f"Кандидат (ID {user_id}) задает вопрос:\n\n{text}"
                    )
                    await event.respond("Приняла, профиль уже готов, на связи. Свяжусь позже.")

            else:
                logger.warning(f"Unknown state {state} for user {user_id}, resetting.")
                upsert_user(user_id, state="chatting")
                await event.respond("Давай начнем сначала. Какая у тебя специализация?")

        except Exception as e:
            logger.error(f"Handler error: {e}", exc_info=True)
            await event.respond("Сорри, ошибка. Команда свяжется с тобой напрямую.")

# --------------------------------------------------------------------
# Main – with explicit login handling
# --------------------------------------------------------------------
async def main():
    init_db()
    await client.start(phone=PHONE)
    logger.info("Userbot is now running...")
    try:
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cancel any remaining background tasks (like PDF generators) quietly
        pending = asyncio.all_tasks(loop=loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()