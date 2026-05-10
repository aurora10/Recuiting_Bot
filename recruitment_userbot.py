import asyncio
import json
import logging
import os
import random
import smtplib
import sqlite3
import sys
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from io import BytesIO
from collections import defaultdict

from dotenv import load_dotenv
import openai
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
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")

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
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN languages TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN team_size TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN availability TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN candidate_name TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN media_phase_started TEXT")
        except sqlite3.OperationalError:
            pass
    
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
        try:
            conv_history = json.loads(row[2]) if row[2] else []
        except (json.JSONDecodeError, TypeError):
            conv_history = []
            logger.warning(f"Corrupt conversation_history for user {user_id}, resetting.")

        try:
            media = json.loads(row[8]) if row[8] else []
        except (json.JSONDecodeError, TypeError):
            media = []
            logger.warning(f"Corrupt media_links for user {user_id}, resetting.")

        return {
            "user_id": row[0],
            "state": row[1],
            "conversation_history": conv_history,
            "specialization": row[3],
            "legal_status": row[4],
            "car_and_tools": row[5],
            "location": row[6],
            "rate": row[7],
            "media_links": media,
            "created_at": row[9],
            "phone_number": row[10] if len(row) > 10 else None,
            "languages": row[11] if len(row) > 11 else None,
            "team_size": row[12] if len(row) > 12 else None,
            "availability": row[13] if len(row) > 13 else None,
            "candidate_name": row[14] if len(row) > 14 else None,
            "media_phase_started": row[15] if len(row) > 15 else None,
        }
    return None

def upsert_user(user_id, **kwargs):
    """Insert new user if not exists, else update only the given fields."""
    allowed = {
        "state", "conversation_history", "specialization", 
        "legal_status", "car_and_tools", "location", "rate", 
        "phone_number", "media_links", "created_at",
        "languages", "team_size", "availability", "candidate_name",
        "media_phase_started"
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

Твоя роль: Ты — помощник рекрутера Роберта. Твоя задача — провести первичный фильтр строителей для работы в Бельгии. Общайся как занятой, адекватный прораб или менеджер проектов. Коротко, по-деловому, по-мужски. Используй строительный сленг (внутрянка, фасад, гипс, фактура, гевель, интерим), но оставайся профессионалом.

Твоя цель: Органично в ходе диалога узнать 10 фактов о кандидате:
1. Имя кандидата (как к нему обращаться).
2. Точная специализация (что делает лучше всего).
3. Легальный статус (Паспорт ЕС, Карта А, Польская виза, своя фирма BV/TVA/NIP).
4. Наличие своего авто и инструмента.
5. Текущая локация (город).
6. Желаемая ставка (в час или за м²).
7. Номер телефона.
8. Языки на стройке (Английский, Нидерландский, Французский, Польский).
9. Один или бригада (если бригада - сколько человек).
10. Готовность (когда готов выйти на объект).

ТВОИ ПРАВИЛА ОБЩЕНИЯ (КРИТИЧЕСКИ ВАЖНО):
• Первым делом поздоровайся и спроси, как зовут кандидата (если он сам не написал).
• ЗАДАВАЙ ТОЛЬКО 1 ВОПРОС ЗА РАЗ. Никогда не вываливай список. Веди диалог как пинг-понг.
• Зеркаль стиль общения: По умолчанию общайся на «ты», коротко и по-деловому (как прораб). НО если кандидат пишет на «Вы», начинает с «Здравствуйте» или ведет себя подчеркнуто официально — СРАЗУ переходи на уважительное «Вы». Не будь фамильярным с теми, кто держит дистанцию.
• Защита от зацикливания: Если человек увиливает или не понимает вопрос 2 раза подряд — не дави. Запиши "Уточнить при звонке" и иди к следующему пункту.
• Документы — это жесткий фильтр. Без ЕС/Карты А/Своей фирмы мы не работаем. Если он нелегал (виза D, биометрия без права на работу) — вежливо прощайся: "Извини, заказчики берут только легальных, сейчас помочь не смогу".

ШПАРГАЛКА (Если кандидат задает вопросы тебе, отвечай коротко и сразу задавай свой встречный вопрос):
• Про жилье: "Жилье решаемо. Если объект далеко — найдем или вычтем из ЗП. Если рядом — ездишь сам."
• Про объекты: "Объекты разные, вся Фландрия и Валлония. Сначала собираю профиль, потом Роберт подберет адрес под твои навыки."
• Про оформление: "Работаем в белую: напрямую с генподрядчиками (B2B) или через бельгийские интеримы."
• Про точную зарплату: "Ставка зависит от опыта и статуса. Назови свой минимум, чтобы я не предлагал дешевые объекты."

ФИНАЛ И СОХРАНЕНИЕ:
Как только соберешь все 10 фактов, СРАЗУ ЖЕ вызови функцию сохранения данных. 
БЕЗ НОМЕРА ТЕЛЕФОНА НЕ СОХРАНЯЙ ПРОФИЛЬ — спроси номер обязательно.
КРИТИЧЕСКИ: Имя кандидата (candidate_name) должно быть НАСТОЯЩИМ ИМЕНЕМ (2+ букв, не менее 2 символов). НЕ ПЕРЕДАВАЙ вместо имени: названия городов ("Гент", "Брюссель"), профессии ("штукатур"), числа, одиночные буквы, фразы "не важно". Если кандидат не назвал имя — НЕ вызывай функцию, сначала спроси "А как тебя зовут? Имя для профиля нужно."
КРИТИЧЕСКИ: Номер телефона должен содержать МИНИМУМ 5 ЦИФР. Это должен быть реальный номер. Не подставляй "нет", "не важно", "позже" — спроси настоящий номер.
ВАЖНО ДЛЯ ФУНКЦИИ: Переведи все ответы на ИДЕАЛЬНЫЙ ПРОФЕССИОНАЛЬНЫЙ ГОЛЛАНДСКИЙ (Dutch) язык. Эти данные полетят бельгийскому заказчику. (Например: вместо "да, есть машина" пиши "Eigen vervoer (Rijbewijs B) + eigen handgereedschap", вместо "румын" пиши "EU Paspoort (Roemenië)", языки: "Spreekt Pools en basis Engels"). 
После вызова функции напиши ОДНО финальное сообщение: "Всё записал. Закинь сюда 3-4 хороших фотки твоих работ. Как всё скинешь — просто черкани 'всё' или 'готово', и Роберт пустит профиль в работу"."""

MEDIA_PHASE_SYSTEM = """Ты — Роберт, рекрутер-строитель. Данные кандидата уже собраны. Сейчас ты ждёшь от него фотографии работ.

ПРАВИЛА (КРИТИЧЕСКИ ВАЖНО):
• НИКАКИХ ВОПРОСОВ. Вообще. Не спрашивай "Это всё?", "Еще будут?", "Готово?". 
• Живые люди так не общаются. Когда живому человеку шлют пачку фоток, он молчит или отвечает коротко в самом конце.
• СИТУАЦИЯ 1 (Человек впервые прислал фото/альбом, и ты ещё не говорил про "готово"): Ответь коротко, но ОБЯЗАТЕЛЬНО добавь инструкцию. Примеры: "Вижу. Как всё скинешь — черкани готово", "Принял. Ещё будут или готово пиши", "Ок. Скинете всё — напишите готово". Самое важное — кандидат должен узнать, что надо написать "готово" когда закончит.
• СИТУАЦИЯ 2 (Человек уже знает про "готово" и шлёт ещё фото): Ответь максимально коротко, 1-2 слова. Используй фразы: "Вижу", "Принял", "Ок", "👍". И всё — не повторяй инструкцию, не задавай вопросов.
• СИТУАЦИЯ 3 (Человек написал текст "всё", "готово", "вот"): Ответь: "Принял. Роберт просмотрит профиль и наберет тебя, если есть объект под твои запросы. На связи."
• Не будь слишком вежливым. Точки в конце коротких фраз не ставь (пиши как в мессенджере)."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_candidate_data",
            "description": "Call this IMMEDIATELY after you have collected all 10 facts about the candidate. Do not wait for photos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name": {
                        "type": "string",
                        "description": "The candidate's real name. DO NOT TRANSLATE to Dutch, keep original spelling."
                    },
                    "specialization": {
                        "type": "string",
                        "description": "The candidate's exact specialization. Translate to PROFESSIONAL DUTCH (e.g. 'metselaar', 'stukadoor', 'gevelwerker', 'binnenafwerking')."
                    },
                    "legal_status": {
                        "type": "string",
                        "description": "The candidate's legal status/documents. Translate to PROFESSIONAL DUTCH (e.g. 'EU Paspoort (Roemenië)', 'Poolse visa + Kaart A', 'Eigen BV (TVA/BTW actief)')."
                    },
                    "car_and_tools": {
                        "type": "string",
                        "description": "Does the candidate have a car and tools? Translate to PROFESSIONAL DUTCH (e.g. 'Eigen vervoer (Rijbewijs B) + eigen handgereedschap', 'Geen eigen vervoer')."
                    },
                    "location": {
                        "type": "string",
                        "description": "The candidate's current location (city). Translate to PROFESSIONAL DUTCH if city name has a Dutch variant."
                    },
                    "rate": {
                        "type": "string",
                        "description": "Desired hourly or per-meter rate. Translate to PROFESSIONAL DUTCH (e.g. '€22/uur bruto', '€18/m²')."
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "The candidate's phone number (keep as-is)."
                    },
                    "languages": {
                        "type": "string",
                        "description": "Languages the candidate speaks on the construction site. Translate to PROFESSIONAL DUTCH (e.g. 'Spreekt Pools en basis Nederlands', 'Engels en Frans')."
                    },
                    "team_size": {
                        "type": "string",
                        "description": "Solo worker or team? If team, how many people? Translate to PROFESSIONAL DUTCH (e.g. 'Werkt alleen (zzp)', 'Brigade van 3 man')."
                    },
                    "availability": {
                        "type": "string",
                        "description": "When the candidate is ready to start on a project. Translate to PROFESSIONAL DUTCH (e.g. 'Per direct beschikbaar', 'Vanaf 1 juni')."
                    }
                },
                "required": ["candidate_name", "specialization", "legal_status", "car_and_tools", "location", "rate", "phone_number", "languages", "team_size", "availability"]
            }
        }
    }
]

async def generate_chat_response(user_id, text, user_history):
    user_history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_MESSAGE}] + user_history
    
    try:
        logger.info(f"[LLM] user={user_id} calling OpenAI with {len(messages)} messages, last_user_msg='{text[:80]}...'")
        t_start = datetime.now()
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            temperature=0.8,
            max_tokens=250,
            timeout=30.0,
        )
        elapsed = (datetime.now() - t_start).total_seconds()
        logger.info(f"[LLM] user={user_id} got response in {elapsed:.2f}s")
        
        msg = response.choices[0].message
        
        if msg.tool_calls:
            logger.info(f"[LLM] user={user_id} tool_call: {msg.tool_calls[0].function.name}")
            tool_call = msg.tool_calls[0]
            if tool_call.function.name == "save_candidate_data":
                args = json.loads(tool_call.function.arguments)
                
                # --- VALIDATE candidate_name ---
                candidate_name = (args.get("candidate_name") or "").strip()
                name_invalid = False
                name_reject_reason = ""
                
                # List of city names that are often mistaken for names
                CITIES = {"гент", "ghent", "gent", "антверпен", "antwerpen", "брюссель",
                          "brussel", "brussels", "брюгге", "brugge", "люксембург", "luxemburg"}
                name_lower = candidate_name.lower()
                
                if len(candidate_name) < 2:
                    name_invalid = True
                    name_reject_reason = "слишком короткое (1 символ)"
                elif name_lower in CITIES:
                    name_invalid = True
                    name_reject_reason = f"это город ({candidate_name}), а не имя"
                elif candidate_name.isdigit():
                    name_invalid = True
                    name_reject_reason = "это число"
                elif all(c.isdigit() or c in "+-() " for c in candidate_name):
                    name_invalid = True
                    name_reject_reason = "это номер телефона, а не имя"
                
                if name_invalid:
                    logger.warning(f"[LLM] user={user_id} REJECTED save: bad candidate_name='{candidate_name}' — {name_reject_reason}")
                    # Tell LLM the save was rejected so it asks for a real name
                    assistant_msg_dict = msg.model_dump()
                    assistant_msg_dict.pop("function_call", None)
                    if assistant_msg_dict.get("content") is None:
                        assistant_msg_dict["content"] = ""
                    user_history.append(assistant_msg_dict)
                    user_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": "save_candidate_data",
                        "content": f"REJECTED: candidate_name '{candidate_name}' is invalid ({name_reject_reason}). Ask the candidate for their REAL name."
                    })
                    # Generate rejection response
                    rejection_msg = await generate_chat_response(user_id,
                        f"[СИСТЕМА: сохранение отклонено — имя '{candidate_name}' не подходит ({name_reject_reason}). Спроси настоящее имя кандидата и больше ничего.]",
                        user_history)
                    if not rejection_msg or rejection_msg == True:
                        sorry_text = f"'{candidate_name}' — это не похоже на имя. Как тебя зовут по-настоящему?"
                        user_history.append({"role": "assistant", "content": sorry_text})
                        upsert_user(user_id, conversation_history=json.dumps(user_history))
                        return sorry_text, False
                    upsert_user(user_id, conversation_history=json.dumps(user_history))
                    return rejection_msg[0] if isinstance(rejection_msg, tuple) else rejection_msg, False
                
                # --- VALIDATE phone_number ---
                phone_number = (args.get("phone_number") or "").strip()
                phone_digits = ''.join(c for c in phone_number if c.isdigit())
                if len(phone_digits) < 5:
                    logger.warning(f"[LLM] user={user_id} REJECTED save: bad phone_number='{phone_number}' — only {len(phone_digits)} digits")
                    assistant_msg_dict = msg.model_dump()
                    assistant_msg_dict.pop("function_call", None)
                    if assistant_msg_dict.get("content") is None:
                        assistant_msg_dict["content"] = ""
                    user_history.append(assistant_msg_dict)
                    user_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": "save_candidate_data",
                        "content": f"REJECTED: phone_number '{phone_number}' has only {len(phone_digits)} digits. Need at least 5 digits. Ask for the REAL phone number."
                    })
                    rejection_msg = await generate_chat_response(user_id,
                        f"[СИСТЕМА: сохранение отклонено — номер '{phone_number}' невалидный. Спроси настоящий номер телефона кандидата.]",
                        user_history)
                    if not rejection_msg or rejection_msg == True:
                        sorry_text = "Это не похоже на номер. Дай нормальный номер, по которому можно набрать."
                        user_history.append({"role": "assistant", "content": sorry_text})
                        upsert_user(user_id, conversation_history=json.dumps(user_history))
                        return sorry_text, False
                    upsert_user(user_id, conversation_history=json.dumps(user_history))
                    return rejection_msg[0] if isinstance(rejection_msg, tuple) else rejection_msg, False
                
                # --- Save valid data ---
                upsert_user(
                    user_id,
                    candidate_name=candidate_name,
                    specialization=args.get("specialization"),
                    legal_status=args.get("legal_status"),
                    car_and_tools=args.get("car_and_tools"),
                    location=args.get("location"),
                    rate=args.get("rate"),
                    phone_number=phone_number,
                    languages=args.get("languages"),
                    team_size=args.get("team_size"),
                    availability=args.get("availability"),
                    state="ask_media",
                    media_phase_started=datetime.now(timezone.utc).isoformat()
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
                
                # Generate natural transition message via LLM
                fresh_user = get_user(user_id)
                photo_count = len(fresh_user["media_links"]) if fresh_user else 0
                if photo_count > 0:
                    situation = f"Все данные кандидата записаны. У него уже есть {photo_count} фото. Скажи что данные принял, и можно докинуть ещё фото или написать 'готово'."
                else:
                    situation = "Все данные кандидата записаны. Теперь нужны фотографии работ — 3-4 штуки. Попроси скинуть фотки."
                final_msg = await generate_media_response(user_id, situation, user_history)
                if not final_msg:
                    final_msg = "Всё записал. Закинь сюда 3-4 хороших фотки твоих работ. Как всё скинешь — просто черкани 'всё' или 'готово', и Роберт пустит профиль в работу" if photo_count == 0 else f"Записал. У тебя {photo_count} фото, можешь ещё докинуть или напиши готово"
                user_history.append({"role": "assistant", "content": final_msg})
                upsert_user(user_id, conversation_history=json.dumps(user_history))
                return final_msg, True
        
        reply_text = msg.content.strip() if msg.content else "..."
        user_history.append({"role": "assistant", "content": reply_text})
        upsert_user(user_id, conversation_history=json.dumps(user_history))
        return reply_text, False
        
    except openai.APIError as e:
        logger.error(f"[LLM] user={user_id} OpenAI API error: {type(e).__name__}: {e}", exc_info=True)
        return "Секунду, я перезвоню. Что-то связь оборвалась.", False
    except Exception as e:
        logger.error(f"[LLM] user={user_id} unexpected error: {type(e).__name__}: {e}", exc_info=True)
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


async def human_typing_delay(chat_id, response_text):
    """Simulate human typing delay with 'typing...' indicator."""
    char_count = len(response_text)
    typing_time = char_count * random.uniform(0.03, 0.06)
    reading_time = random.uniform(1.5, 3.5)
    total = min(reading_time + typing_time, 12.0)
    total = max(total, 2.0)  # at least 2 seconds
    async with client.action(chat_id, 'typing'):
        await asyncio.sleep(total)


async def generate_media_response(user_id, situation, user_history):
    """Generate natural LLM response for media collection phase."""
    system = MEDIA_PHASE_SYSTEM + f"\nСИТУАЦИЯ: {situation}\nОтветь кандидату ОДНИМ коротким сообщением."
    recent = [m for m in user_history[-6:] if m.get("role") in ("user", "assistant")]
    messages = [{"role": "system", "content": system}] + recent

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.9,
            max_tokens=80,
            timeout=15.0,
        )
        reply = response.choices[0].message.content.strip()
        if reply:
            return reply
    except Exception as e:
        logger.error(f"[LLM-media] user={user_id} error: {e}")
    return None  # caller uses fallback

# --------------------------------------------------------------------
# Dossier PDF generator
# --------------------------------------------------------------------
def create_dossier(user):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    story = []
    styles = getSampleStyleSheet()

    # Register DejaVuSans font (supports Cyrillic and European characters)
    font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
    bold_font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans-Bold.ttf")
    
    if not os.path.exists(font_path):
        raise FileNotFoundError(f"DejaVuSans.ttf not found at {font_path} — PDF will have black squares without it.")
    if not os.path.exists(bold_font_path):
        raise FileNotFoundError(f"DejaVuSans-Bold.ttf not found at {bold_font_path} — PDF will have black squares without it.")
    
    pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold_font_path))
    pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold")
    
    styles["Title"].fontName = "DejaVuSans-Bold"
    styles["Normal"].fontName = "DejaVuSans"

    story.append(Paragraph(f"Kandidaat", styles["Title"]))
    story.append(Spacer(1, 12))
    candidate_label = user.get("candidate_name") or f"Kandidaat {user['user_id']}"
    story.append(Paragraph(f"<b>Kandidaat:</b> {candidate_label}", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>Specialisatie:</b> {user.get('specialization', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Documenten:</b> {user.get('legal_status', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Auto/Gereedschap:</b> {user.get('car_and_tools', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Locatie:</b> {user.get('location', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Tarief:</b> {user.get('rate', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Telefoon:</b> {user.get('phone_number', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Talen:</b> {user.get('languages', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Brigade:</b> {user.get('team_size', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Beschikbaarheid:</b> {user.get('availability', 'N/A')}", styles["Normal"]))

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
# Email profile to Gmail
# --------------------------------------------------------------------
MAX_ATTACH_MB = 20  # stay well under Gmail's 25MB limit

def _send_email_sync(user, pdf_buffer):
    """Synchronous email sending — runs in a thread via asyncio.to_thread."""
    if not GMAIL_USER or not GMAIL_PASS:
        logger.warning("GMAIL_USER or GMAIL_PASS not set — skipping email.")
        return

    user_id = user["user_id"]

    # Build the JSON data payload
    profile_data = {
        "kandidaat_id": user_id,
        "naam": user.get("candidate_name") or f"Kandidaat {user_id}",
        "specialisatie": user.get("specialization", ""),
        "documenten": user.get("legal_status", ""),
        "auto_gereedschap": user.get("car_and_tools", ""),
        "locatie": user.get("location", ""),
        "tarief": user.get("rate", ""),
        "telefoon": user.get("phone_number", ""),
        "talen": user.get("languages", ""),
        "brigade": user.get("team_size", ""),
        "beschikbaarheid": user.get("availability", ""),
        "created_at": user.get("created_at", ""),
        "media_links": user.get("media_links", []),
    }
    json_bytes = json.dumps(profile_data, indent=2, ensure_ascii=False).encode("utf-8")

    # Build MIME multipart message
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg["Subject"] = f"Nieuwe kandidaat profiel – ID {user_id}"

    # Plain text body
    display_name = user.get("candidate_name") or f"Kandidaat {user_id}"
    body = (
        f"Profiel van kandidaat ID {user_id}\n"
        f"Naam: {display_name}\n"
        f"Specialisatie: {profile_data['specialisatie']}\n"
        f"Documenten: {profile_data['documenten']}\n"
        f"Auto/Gereedschap: {profile_data['auto_gereedschap']}\n"
        f"Locatie: {profile_data['locatie']}\n"
        f"Tarief: {profile_data['tarief']}\n"
        f"Telefoon: {profile_data['telefoon']}\n"
        f"Talen: {profile_data['talen']}\n"
        f"Brigade: {profile_data['brigade']}\n"
        f"Beschikbaarheid: {profile_data['beschikbaarheid']}\n"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach JSON profile data
    json_attachment = MIMEApplication(json_bytes, _subtype="json", name=f"profile_{user_id}.json")
    json_attachment.add_header("Content-Disposition", "attachment", filename=f"profile_{user_id}.json")
    msg.attach(json_attachment)

    # Attach PDF dossier
    pdf_attachment = MIMEApplication(pdf_buffer.getvalue(), _subtype="pdf", name=f"dossier_{user_id}.pdf")
    pdf_attachment.add_header("Content-Disposition", "attachment", filename=f"dossier_{user_id}.pdf")
    msg.attach(pdf_attachment)

    # Attach all media files (photos/videos)
    media_links = user.get("media_links", [])
    for filename in media_links:
        full_path = os.path.join(MEDIA_DIR, filename)
        if not os.path.exists(full_path):
            logger.warning(f"Media file not found for email: {full_path}")
            continue

        file_size_mb = os.path.getsize(full_path) / (1024 * 1024)
        if file_size_mb > MAX_ATTACH_MB:
            logger.warning(f"Skipping oversized attachment ({file_size_mb:.1f}MB): {filename}")
            continue

        ext = os.path.splitext(filename)[1].lower()
        try:
            with open(full_path, "rb") as fh:
                data = fh.read()

            if ext in (".jpg", ".jpeg", ".png"):
                mime_img = MIMEImage(data, _subtype=ext.lstrip("."))
                mime_img.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(mime_img)
            elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                main_type = "video"
                sub_type = ext.lstrip(".")
                mime_vid = MIMEBase(main_type, sub_type)
                mime_vid.set_payload(data)
                encoders.encode_base64(mime_vid)
                mime_vid.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(mime_vid)
            else:
                # Generic binary attachment
                mime_gen = MIMEBase("application", "octet-stream")
                mime_gen.set_payload(data)
                encoders.encode_base64(mime_gen)
                mime_gen.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(mime_gen)
        except Exception as e:
            logger.error(f"Failed to attach {filename} to email: {e}")

    # Send via Gmail SMTP
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_PASS)
            server.send_message(msg)
        logger.info(f"Email sent to {GMAIL_USER} for user {user_id}")
    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail SMTP authentication failed. Check GMAIL_USER / GMAIL_PASS and App Password settings.")
    except Exception as e:
        logger.error(f"Failed to send email for user {user_id}: {e}", exc_info=True)


async def email_profile(user, pdf_buffer):
    """Send the full profile (PDF + JSON + media) to Gmail asynchronously."""
    try:
        await asyncio.to_thread(_send_email_sync, user, pdf_buffer)
    except Exception as e:
        logger.error(f"email_profile failed for user {user['user_id']}: {e}", exc_info=True)


# --------------------------------------------------------------------
# Concurrency control
# --------------------------------------------------------------------
user_locks = defaultdict(asyncio.Lock)
# Track how many times a user tried "готово" without photos
empty_done_attempts = defaultdict(int)
# Debounce tasks for album photo responses
photo_debounce_tasks = {}

async def generate_and_send_pdf(user_id, client):
    """Generate dossier PDF and send to recruiter. Lock must be held by the caller."""
    user = get_user(user_id)
    if not user:
        return

    situation = "Все фото получены. Профиль кандидата готов и отправляется рекрутеру. Скажи что Роберт посмотрит и свяжется если есть подходящий объект. Это финальное сообщение."
    reply = await generate_media_response(user_id, situation, user["conversation_history"])
    if not reply:
        reply = "Ок, всё принял. Роберт глянет и наберёт если что-то есть под тебя"
    await human_typing_delay(user_id, reply)
    await client.send_message(user_id, reply)

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

        # Email the full profile (PDF + JSON + media) to Gmail
        await email_profile(user, pdf_buffer)

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
                await human_typing_delay(user_id, reply_msg)
                await event.respond(reply_msg)
                # Note: if tool_called, state is now ask_media, waiting for user response
                
            elif state == "ask_media":
                # Auto-complete check: if user has >=3 photos and been in media phase > AUTO_MEDIA_TIMEOUT
                AUTO_MEDIA_TIMEOUT = 7 * 60  # 7 minutes
                AUTO_MEDIA_MIN_PHOTOS = 3
                media_started_str = user.get("media_phase_started")
                if media_started_str and len(user["media_links"]) >= AUTO_MEDIA_MIN_PHOTOS:
                    try:
                        started_at = datetime.fromisoformat(media_started_str)
                        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                        if elapsed > AUTO_MEDIA_TIMEOUT:
                            logger.info(f"[auto-complete] user={user_id} has {len(user['media_links'])} photos, idle for {elapsed:.0f}s — auto-completing")
                            empty_done_attempts.pop(user_id, None)
                            upsert_user(user_id, state="done")
                            await generate_and_send_pdf(user_id, client)
                            return  # skip further processing for this message
                    except (ValueError, TypeError):
                        pass  # corrupted timestamp, ignore
                
                if event.photo or event.video or (event.document and "video" in getattr(event.document, "mime_type", "")):
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))

                    # Debounce: if this is part of an album (grouped_id), wait for silence before responding
                    if event.grouped_id:
                        # Cancel any pending debounce for this album
                        old_task = photo_debounce_tasks.pop(user_id, None)
                        if old_task and not old_task.done():
                            old_task.cancel()

                        event_ref = event  # capture for the closure
                        captured_uid = user_id

                        async def send_debounced():
                            try:
                                await asyncio.sleep(1.5)
                            except asyncio.CancelledError:
                                return
                            fresh_user = get_user(captured_uid)
                            count = len(fresh_user["media_links"]) if fresh_user else 0
                            history = fresh_user["conversation_history"] if fresh_user else []
                            situation = f"Кандидат прислал альбом фотографий. Всего у него теперь {count} фото. Если это его первые фото — ОБЯЗАТЕЛЬНО скажи 'готово' чтоб завершить. Если уже говорил — просто подтверди коротко."
                            reply = await generate_media_response(captured_uid, situation, history)
                            if not reply:
                                reply = f"Принял, {count} фоток. Как всё скинешь — черкани готово"
                            await human_typing_delay(captured_uid, reply)
                            await event_ref.respond(reply)

                        photo_debounce_tasks[user_id] = asyncio.create_task(send_debounced())
                    else:
                        count = len(user["media_links"])
                        situation = f"Кандидат прислал одно фото. Всего у него {count} фото. Если это его первые фото — ОБЯЗАТЕЛЬНО скажи 'готово' чтоб завершить."
                        reply = await generate_media_response(user_id, situation, user["conversation_history"])
                        if not reply:
                            reply = f"Принял, {count} фоток. Как всё скинешь — черкани готово"
                        await human_typing_delay(user_id, reply)
                        await event.respond(reply)
                else:
                    text_lower = text.strip().lower()
                    if text_lower in ("все", "всё", "готово", "done", "ok", "ок"):
                        if not user["media_links"]:
                            empty_done_attempts[user_id] += 1
                            if empty_done_attempts[user_id] >= 3:
                                situation = "Кандидат 3 раза написал 'готово' без фото. Скажи что отправишь профиль без фото, рекрутер сам запросит если нужно."
                                reply = await generate_media_response(user_id, situation, user["conversation_history"])
                                if not reply:
                                    reply = "Ладно, отправлю без фото. Если надо, рекрутер сам попросит"
                                await human_typing_delay(user_id, reply)
                                await event.respond(reply)
                                upsert_user(user_id, state="done")
                                await generate_and_send_pdf(user_id, client)
                            else:
                                situation = f"Кандидат написал 'готово' но фото нет. Это попытка {empty_done_attempts[user_id]} из 3. Попроси прислать хотя бы 2-3 фотографии работ."
                                reply = await generate_media_response(user_id, situation, user["conversation_history"])
                                if not reply:
                                    reply = "Без фоток не годится, скинь хотя бы пару"
                                await human_typing_delay(user_id, reply)
                                await event.respond(reply)
                        else:
                            empty_done_attempts.pop(user_id, None)  # reset counter on success
                            upsert_user(user_id, state="done")
                            await generate_and_send_pdf(user_id, client)
                    else:
                        old_task = photo_debounce_tasks.pop(user_id, None)
                        if old_task and not old_task.done():
                            old_task.cancel()
                        count = len(user["media_links"])
                        if count > 0:
                            situation = f"Кандидат написал '{text}' вместо фото. У него уже {count} фото. Напомни что ждёшь фотки или может написать 'готово'."
                        else:
                            situation = f"Кандидат написал '{text}' вместо отправки фото. Фото пока нет. Попроси скинуть фотки работ."
                        reply = await generate_media_response(user_id, situation, user["conversation_history"])
                        if not reply:
                            reply = "Жду фотки работ. Как скинешь — напиши готово"
                        await human_typing_delay(user_id, reply)
                        await event.respond(reply)

            elif state == "done":
                # If they send more media after done, just append and regenerate silently
                if event.photo or event.video or (event.document and "video" in getattr(event.document, "mime_type", "")):
                    path = await event.download_media(file=MEDIA_DIR)
                    filename = os.path.basename(path)
                    user["media_links"].append(filename)
                    upsert_user(user_id, media_links=json.dumps(user["media_links"]))
                else:
                    text_lower = text.strip().lower()
                    # Explicit restart phrases
                    if text_lower in ("заново", "сначала", "начнём заново", "restart", "сброс"):
                        upsert_user(
                            user_id,
                            state="chatting",
                            conversation_history=json.dumps([]),
                        )
                        user["state"] = "chatting"
                        user["conversation_history"] = []
                        reply_msg, _ = await generate_chat_response(user_id, text, user["conversation_history"])
                        await human_typing_delay(user_id, reply_msg)
                        await event.respond(reply_msg)
                    else:
                        # User is just messaging after dossier – DON'T call generate_chat_response
                        # (that uses the interview SYSTEM_MESSAGE and restarts the whole interview).
                        # Use generate_media_response with a "post-dossier" situation instead.
                        situation = f"Профиль кандидата уже отправлен рекрутеру. Кандидат спрашивает: '{text}'. Ответь коротко: новостей пока нет, как только что-то будет — сразу сообщишь. Не задавай вопросов, не начинай новый опрос, не спрашивай про навыки/документы/фото — просто подтверди что на связи и ждёшь новостей от Роберта."
                        reply_msg = await generate_media_response(user_id, situation, user["conversation_history"])
                        if not reply_msg:
                            reply_msg = "Пока новостей нет. Как только что-то будет — сразу дам знать"
                        await human_typing_delay(user_id, reply_msg)
                        await event.respond(reply_msg)

            else:
                logger.warning(f"Unknown state {state} for user {user_id}, resetting.")
                upsert_user(user_id, state="chatting")
                reply = "Давай начнем сначала. Какая у тебя специализация?"
                await human_typing_delay(user_id, reply)
                await event.respond(reply)

        except Exception as e:
            logger.error(f"Handler error: {e}", exc_info=True)
            reply = "Ща, тут телефон завис. Секунду"
            await human_typing_delay(user_id, reply)
            await event.respond(reply)

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