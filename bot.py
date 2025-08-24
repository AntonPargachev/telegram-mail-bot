import asyncio
import os
import ssl
import smtplib
import mimetypes
from email.message import EmailMessage
from email.utils import make_msgid, formatdate

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    BotCommand, BotCommandScopeAllGroupChats,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from aiogram.enums import ChatType
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# ---------- ENV ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0") or "0")
ALLOWED_CHAT_IDS = [
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]
ALLOWED_USER_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()]

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.ukr.net")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
MAIL_TO    = os.getenv("MAIL_TO", SMTP_USER)
MAIL_FROM  = os.getenv("MAIL_FROM", SMTP_USER)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

BOT_USERNAME: str | None = None  # підставляємо в main()

# ---------- HELPERS ----------
def _allowed(message: Message) -> bool:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    allowed_ids = set(ALLOWED_CHAT_IDS)
    if ALLOWED_CHAT_ID:
        allowed_ids.add(ALLOWED_CHAT_ID)
    if allowed_ids and message.chat.id not in allowed_ids:
        return False
    if ALLOWED_USER_IDS:
        return message.from_user and message.from_user.id in ALLOWED_USER_IDS
    return True

def _allowed_for_wizard(message: Message) -> bool:
    return (message.chat.type == ChatType.PRIVATE) or _allowed(message)

def _subject(prefix: str, theme: str, m: Message) -> str:
    user = f"{m.from_user.full_name} (@{m.from_user.username})" if m.from_user else "Unknown"
    return f"[TG→Mail] {prefix} — {theme.strip()} — від {user}"

def _html_with_meta(text: str, m: Message, note: str = "") -> str:
    chat_title = m.chat.title or str(m.chat.id)
    user = f"{m.from_user.full_name} (@{m.from_user.username})" if m.from_user else "Unknown"
    permalink = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.message_id}" if str(m.chat.id).startswith("-100") else "N/A"
    return f"""
    <html><body>
      <div style="font-family:Arial,Helvetica,sans-serif; font-size:16px; color:#000; line-height:1.5;">
        <p><b>Чат:</b> {chat_title} (id: {m.chat.id})</p>
        <p><b>Відправник:</b> {user} (id: {m.from_user.id if m.from_user else 'N/A'})</p>
        <p><b>Посилання на повідомлення:</b> {permalink}</p>
        {'<p><i>'+note+'</i></p>' if note else ''}
        <hr/>
        <pre style="white-space:pre-wrap; font-family:inherit; font-size:inherit; color:inherit; margin:0;">{text}</pre>
      </div>
    </body></html>
    """

def _html_plain(text: str) -> str:
    return f"""
    <html><body>
      <div style="font-family:Arial,Helvetica,sans-serif; font-size:16px; color:#000; line-height:1.5;">
        <pre style="white-space:pre-wrap; font-family:inherit; font-size:inherit; color:inherit; margin:0;">{text}</pre>
      </div>
    </body></html>
    """

def send_email(subject: str, html_body: str, attachments: list[tuple[str, bytes]] = None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content("Це HTML-лист.")
    msg.add_alternative(html_body, subtype="html")

    for filename, data in (attachments or []):
        ctype, _ = mimetypes.guess_type(filename)
        if ctype is None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    if SMTP_PORT == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

async def _fetch(file_id: str, name: str) -> tuple[str, bytes]:
    f = await bot.get_file(file_id)
    file = await bot.download_file(f.file_path)
    data = file.read()
    return name, data

async def _safe_del(chat_id: int, message_id: int | None):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# персоналізований deep-link у приват
def _private_link_kb(owner_id: int) -> InlineKeyboardMarkup:
    username = BOT_USERNAME or ""
    payload = f"z{owner_id}"
    url = f"https://t.me/{username}?start={payload}" if username else "https://t.me/"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔒 Заповнювати в особистих", url=url)]]
    )

# іменні кнопки в майстрі
def _form_kb(owner_id: int, with_send: bool = False) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"form:{owner_id}:cancel")]
    if with_send:
        row.insert(0, InlineKeyboardButton(text="✅ Відправити", callback_data=f"form:{owner_id}:send"))
    return InlineKeyboardMarkup(inline_keyboard=[row])

async def _ask_next(message: Message, state: FSMContext, html_text: str, with_send: bool = False):
    owner_id = message.from_user.id if message.from_user else 0
    sent = await message.answer(
        html_text,
        reply_markup=_form_kb(owner_id, with_send),
        parse_mode="HTML",
        disable_notification=True
    )
    await state.update_data(bot_q=sent.message_id, owner_id=owner_id)

# ---------- STATES ----------
class Zayavka(StatesGroup):
    wait_fullname = State()
    wait_shop_addr = State()
    wait_tax_id    = State()
    wait_phone     = State()
    wait_product   = State()
    wait_price     = State()
    wait_downpay   = State()
    wait_grace     = State()
    wait_attachments = State()

# ---------- COMMANDS ----------
@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    data = await state.get_data()
    await _safe_del(message.chat.id, data.get("bot_q"))
    await state.clear()
    await message.reply(
        "❌ Скасовано. Можна почати знову командою /zayavka",
        reply_markup=ReplyKeyboardRemove(),
        disable_notification=True
    )

@dp.callback_query(F.data.startswith("form:"))
async def form_buttons(call: CallbackQuery, state: FSMContext):
    try:
        _, owner_id_str, action = call.data.split(":")
        owner_id = int(owner_id_str)
    except Exception:
        await call.answer("Невірні дані кнопки.", show_alert=True)
        return
    if not call.from_user or call.from_user.id != owner_id:
        await call.answer("Ця кнопка не для вас 🙂", show_alert=True)
        return

    data = await state.get_data()

    if action == "cancel":
        await _safe_del(call.message.chat.id, data.get("bot_q"))
        await state.clear()
        await call.message.answer("❌ Скасовано. Можна почати знову командою /zayavka", disable_notification=True)
        await call.answer()
        return

    if action == "send":
        files = data.get("files", [])
        subject = f"(заявка) {data.get('fullname','').strip()} mobiletrend.com.ua"
        body_text = (
            f"Заявка від @{call.from_user.username or call.from_user.full_name} (id: {call.from_user.id})\n"
            f"ПІБ клієнта: {data.get('fullname','')}\n"
            f"Адреса ТТ: {data.get('shop_addr','')}\n"
            f"ІПН клієнта: {data.get('tax_id','')}\n"
            f"Моб. телефон: {data.get('phone','')}\n"
            f"Товар (повна назва): {data.get('product','')}\n"
            f"Вартість товару: {data.get('price','')}\n"
            f"Перший внесок: {data.get('downpay','')}\n"
            f"Кількість платежів (Грейс): {data.get('grace','')}\n"
            f"Кількість вкладень: {len(files)}\n"
        )
        summary = (
            "✅ <b>Заявку відправлено на пошту.</b>\n\n"
            "<b>Що відправлено:</b>\n"
            f"• <b>ПІБ клієнта:</b> {data.get('fullname','')}\n"
            f"• <b>Адреса ТТ:</b> {data.get('shop_addr','')}\n"
            f"• <b>ІПН клієнта:</b> {data.get('tax_id','')}\n"
            f"• <b>Моб. телефон:</b> {data.get('phone','')}\n"
            f"• <b>Товар:</b> {data.get('product','')}\n"
            f"• <b>Вартість товару:</b> {data.get('price','')}\n"
            f"• <b>Перший внесок:</b> {data.get('downpay','')}\n"
            f"• <b>Кількість платежів (Грейс):</b> {data.get('grace','')}\n"
            f"• <b>Вкладень:</b> {len(files)}"
        )
        try:
            await asyncio.to_thread(send_email, subject, _html_plain(body_text), files)
            await _safe_del(call.message.chat.id, data.get("bot_q"))
            await state.clear()
            await call.message.answer(summary, parse_mode="HTML", disable_notification=True)
        except Exception as e:
            await call.message.answer(f"❌ Помилка надсилання: {e}", disable_notification=True)
        finally:
            await call.answer()
        return

# приватний старт із payload
@dp.message(CommandStart())
async def start_private(message: Message, state: FSMContext):
    payload = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    # персональний deep-link типу z<user_id>
    if payload.startswith("z"):
        want_id = payload[1:]
        if want_id.isdigit() and message.from_user and message.from_user.id == int(want_id):
            await zayavka_start(message, state)
            return
        else:
            await message.answer(
                "Це посилання не для вас. Запустіть /zayavka у групі і натисніть власну кнопку.",
                disable_notification=True
            )
            return

    await message.answer("Привіт! Напишіть /zayavka, щоб подати заявку.", disable_notification=True)

# ---------- /ZAYAVKA WIZARD ----------
@dp.message(Command("zayavka"))
async def zayavka_start(message: Message, state: FSMContext):
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        note = "Щоб дані були приватні, запустіть майстер в особистих повідомленнях."
        owner_id = message.from_user.id if message.from_user else 0
        await message.answer(note, reply_markup=_private_link_kb(owner_id))
        return

    if not _allowed_for_wizard(message):
        return

    await state.clear()
    txt = (
        "📝 <b>Відправити заявку</b>\n\n"
        "Тема листа буде такою:\n"
        "<code>(заявка) ПІБ_клієнта mobiletrend.com.ua</code>\n\n"
        "Введіть, будь ласка, <b>ПІБ клієнта</b> одним повідомленням."
    )
    await state.set_state(Zayavka.wait_fullname)
    await _ask_next(message, state, txt)

@dp.message(Zayavka.wait_fullname)
async def z_fullname(message: Message, state: FSMContext):
    fio = (message.text or "").strip()
    if not fio:
        await message.reply("Будь ласка, введіть ПІБ клієнта.", disable_notification=True)
        return
    data = await state.get_data()
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.update_data(fullname=fio)
    await state.set_state(Zayavka.wait_shop_addr)
    await _ask_next(message, state, "📍 <b>Адресса ТТ</b>\nВведіть адресу торгової точки.")

@dp.message(Zayavka.wait_shop_addr)
async def z_shop_addr(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(shop_addr=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_tax_id)
    await _ask_next(message, state, "🧾 <b>ІПН клієнта (податковий код)</b>")

@dp.message(Zayavka.wait_tax_id)
async def z_tax(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(tax_id=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_phone)
    await _ask_next(message, state, "📱 <b>Мобільний телефон клієнта</b>")

@dp.message(Zayavka.wait_phone)
async def z_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(phone=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_product)
    await _ask_next(message, state, "📦 <b>Повна назва товару</b>\n(якщо це телефон — вкажіть обсяг пам’яті та колір)")

@dp.message(Zayavka.wait_product)
async def z_product(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(product=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_price)
    await _ask_next(message, state, "💵 <b>Вартість товару</b>")

@dp.message(Zayavka.wait_price)
async def z_price(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(price=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_downpay)
    await _ask_next(message, state, "💳 <b>Перший внесок</b>")

@dp.message(Zayavka.wait_downpay)
async def z_downpay(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(downpay=(message.text or "").strip())
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_grace)
    await _ask_next(message, state, "📆 <b>Кількість платежів Грейс</b> (4 чи 6)")

@dp.message(Zayavka.wait_grace)
async def z_to_attachments(message: Message, state: FSMContext):
    data_prev = await state.get_data()
    await state.update_data(grace=(message.text or "").strip(), files=[])
    await _safe_del(message.chat.id, data_prev.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)
    await state.set_state(Zayavka.wait_attachments)
    intro = (
        "📎 <b>Додайте файли для заявки</b>\n"
        "• Фото клієнта на ТТ з паспортом у руках\n"
        "• Фото ІПН\n"
        "• Фото паспорта (усі сторінки з даними) або ID-картки (2 боки)\n"
        "• Фото витяга (якщо ID-картка)\n\n"
        "Надсилайте фото/документи окремими повідомленнями.\n"
        "Коли закінчите — натисніть <b>✅ Відправити</b> або введіть <b>/done</b>."
    )
    await _ask_next(message, state, intro + "\n\n<b>Додано файлів:</b> 0", with_send=True)

@dp.message(Zayavka.wait_attachments, F.photo | F.document)
async def z_collect_files(message: Message, state: FSMContext):
    data = await state.get_data()
    files: list[tuple[str, bytes]] = data.get("files", [])

    if message.photo:
        ph = message.photo[-1]
        name, content = await _fetch(ph.file_id, f"photo_{message.message_id}.jpg")
        files.append((name, content))
    elif message.document:
        d = message.document
        name = d.file_name or f"document_{message.message_id}"
        name, content = await _fetch(d.file_id, name)
        files.append((name, content))

    await state.update_data(files=files)
    await _safe_del(message.chat.id, message.message_id)
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _ask_next(
        message, state,
        "📎 <b>Додайте файли для заявки</b>\n"
        "Надсилайте ще, або натисніть <b>✅ Відправити</b> / введіть <b>/done</b>.\n\n"
        f"<b>Додано файлів:</b> {len(files)}",
        with_send=True
    )

@dp.message(Zayavka.wait_attachments, Command("done"))
async def z_finish_attachments(message: Message, state: FSMContext):
    data = await state.get_data()
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _safe_del(message.chat.id, message.message_id)

    subject = f"(заявка) {data.get('fullname','').strip()} mobiletrend.com.ua"
    body_text = (
        f"Заявка від @{message.from_user.username or message.from_user.full_name} (id: {message.from_user.id})\n"
        f"ПІБ клієнта: {data.get('fullname','')}\n"
        f"Адреса ТТ: {data.get('shop_addr','')}\n"
        f"ІПН клієнта: {data.get('tax_id','')}\n"
        f"Моб. телефон: {data.get('phone','')}\n"
        f"Товар (повна назва): {data.get('product','')}\n"
        f"Вартість товару: {data.get('price','')}\n"
        f"Перший внесок: {data.get('downpay','')}\n"
        f"Кількість платежів (Грейс): {data.get('grace','')}\n"
        f"Кількість вкладень: {len(data.get('files', []))}\n"
    )
    summary = (
        "✅ <b>Заявку відправлено на пошту.</b>\n\n"
        "<b>Що відправлено:</b>\n"
        f"• <b>ПІБ клієнта:</b> {data.get('fullname','')}\n"
        f"• <b>Адреса ТТ:</b> {data.get('shop_addr','')}\n"
        f"• <b>ІПН клієнта:</b> {data.get('tax_id','')}\n"
        f"• <b>Моб. телефон:</b> {data.get('phone','')}\n"
        f"• <b>Товар:</b> {data.get('product','')}\n"
        f"• <b>Вартість товару:</b> {data.get('price','')}\n"
        f"• <b>Перший внесок:</b> {data.get('downpay','')}\n"
        f"• <b>Кількість платежів (Грейс):</b> {data.get('grace','')}\n"
        f"• <b>Вкладень:</b> {len(data.get('files', []))}"
    )

    try:
        await asyncio.to_thread(send_email, subject, _html_plain(body_text), data.get('files', []))
        await message.answer(summary, parse_mode="HTML", disable_notification=True)
    except Exception as e:
        await message.answer(f"❌ Помилка надсилання: {e}", disable_notification=True)
    finally:
        await state.clear()

@dp.message(Zayavka.wait_attachments)
async def z_ignore_other(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/'):
        return
    data = await state.get_data()
    await _safe_del(message.chat.id, message.message_id)
    await _safe_del(message.chat.id, data.get("bot_q"))
    await _ask_next(
        message, state,
        "📎 Надішліть фото/документ як повідомлення, або завершіть <b>/done</b> / натисніть <b>✅ Відправити</b>.",
        with_send=True
    )

# ---- інші ваші команди збережено як було (без зміни логіки) ----

@dp.message(Command("toemail"))
async def forward_reply(message: Message):
    if not _allowed(message):
        return
    if not message.reply_to_message:
        await message.reply("Зробіть реплай на повідомлення. Приклад: /toemail Тема", disable_notification=True)
        return

    theme = message.text.split(" ", 1)[1] if " " in message.text else "Без теми"
    origin = message.reply_to_message
    body_text = origin.text or origin.caption or "[Без тексту — вкладення]"

    attachments = []
    if origin.photo:
        ph = origin.photo[-1]
        attachments.append(await _fetch(ph.file_id, f"photo_{origin.message_id}.jpg"))
    if origin.document:
        d = origin.document
        name = d.file_name or f"document_{origin.message_id}"
        attachments.append(await _fetch(d.file_id, name))
    if origin.voice:
        v = origin.voice
        attachments.append(await _fetch(v.file_id, f"voice_{origin.message_id}.ogg"))
    if origin.audio:
        a = origin.audio
        attachments.append(await _fetch(a.file_id, a.file_name or f"audio_{origin.message_id}.mp3"))
    if origin.video:
        v = origin.video
        attachments.append(await _fetch(v.file_id, f"video_{origin.message_id}.mp4"))
    if origin.video_note:
        vn = origin.video_note
        attachments.append(await _fetch(vn.file_id, f"videonote_{origin.message_id}.mp4"))

    try:
        await asyncio.to_thread(
            send_email,
            _subject("REPLY", theme, origin),
            _html_with_meta(body_text, origin, "Відправлено з реплаю."),
            attachments
        )
        await message.reply("✅ Відправлено на пошту.", disable_notification=True)
    except Exception as e:
        await message.reply(f"❌ Помилка надсилання: {e}", disable_notification=True)

@dp.message(F.text.startswith("!mail"))
async def trigger_mail(message: Message):
    if not _allowed(message):
        return
    raw = message.text[len("!mail"):].strip()
    theme, text = ("Без теми", raw or "[порожньо]") if "|" not in raw else [x.strip() for x in raw.split("|", 1)]
    try:
        await asyncio.to_thread(
            send_email,
            _subject("MSG", theme, message),
            _html_with_meta(text, message, "Відправлено тригером !mail."),
            []
        )
        await message.reply("✅ Відправлено на пошту.", disable_notification=True)
    except Exception as e:
        await message.reply(f"❌ Помилка надсилання: {e}", disable_notification=True)

# ---------- "/" MENU ----------
async def setup_commands():
    cmds = [
        BotCommand(command="zayavka", description="Відправити заявку"),
        BotCommand(command="cancel",  description="Скасувати заповнення заявки"),
    ]
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())

# ---------- RUN ----------
async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await setup_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
