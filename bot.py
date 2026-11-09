"""Telegram-бот для оприходования закупок голосом."""
import asyncio
import io
import logging
import os
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.middlewares.base import BaseRequestMiddleware  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402
from aiogram.filters import Command, CommandStart  # noqa: E402
from aiogram.methods import Response  # noqa: E402
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,  # noqa: E402
                           InlineKeyboardMarkup, Message)

import core  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
router = Router()
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

HELP = """Привет! Я записываю закупки для оприходования в ROSTA.

🎙 <b>Просто надиктуй голосовым</b>, что купил, например:
• «Лук 35 кило по 170»
• «Масло сливочное 2 пачки, за обе 600»
• «Кола 12 литров на 4800, картошка 20 кг по 150»

Можно исправлять так же голосом или текстом: «лук не 35, а 30», «убери колу».
Цену можно говорить за единицу или общую сумму — посчитаю сам.

<b>Кнопки под списком:</b>
✅ Завершить — пришлю итоговый список и PDF, а всю переписку по этой закупке удалю
↩️ Отменить — вернуть как было до последнего сообщения
🗑 Очистить — начать заново

В чате остаются только итоговые списки и PDF. Найти закупку за день — поиск по чату (например «07.11») или нажми на #оприходование.

<b>Команды:</b> /list — текущий список, /done — завершить, /undo, /clear
/catalog — справочник сырья (точные названия как в ROSTA)
/id — узнать свой Telegram ID"""

KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Завершить", callback_data="done"),
    InlineKeyboardButton(text="↩️ Отменить", callback_data="undo"),
    InlineKeyboardButton(text="🗑 Очистить", callback_data="clear"),
]])


def allowed(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


# ---------------------------------------------------------------- учёт сообщений для очистки чата

class TrackIncoming(BaseMiddleware):
    """Запоминает каждое входящее сообщение (голосовые, скрины, текст, команды)."""

    async def __call__(self, handler, event: Message, data):
        if event.from_user and allowed(event.from_user.id):
            core.track_msgs(event.chat.id, [event.message_id])
        return await handler(event, data)


class TrackOutgoing(BaseRequestMiddleware):
    """Запоминает каждое сообщение, которое отправил бот."""

    async def __call__(self, make_request, bot, method):
        response = await make_request(bot, method)
        # aiogram отдаёт здесь сам результат (Message), но на всякий случай понимаем и обёртку Response
        res = response.result if isinstance(response, Response) else response
        for r in (res if isinstance(res, list) else [res]):
            if isinstance(r, Message):
                core.track_msgs(r.chat.id, [r.message_id])
        return response


async def wipe(bot: Bot, chat_id: int, ids: list[int]) -> None:
    """Удаляет сообщения пачками по 100. Telegram не даёт удалять сообщения старше 48 часов — их пропускаем."""
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
        except TelegramAPIError:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                except TelegramAPIError:
                    pass


_bg: set[asyncio.Task] = set()


def delete_later(bot: Bot, chat_id: int, msg_id: int, delay: int = 120) -> None:
    async def _run():
        await asyncio.sleep(delay)
        await wipe(bot, chat_id, [msg_id])
        core.untrack_msgs(chat_id, [msg_id])
    t = asyncio.create_task(_run())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")


# Всё ниже — только для разрешённых пользователей
@router.message(lambda m: m.from_user and not allowed(m.from_user.id))
async def deny(m: Message):
    await m.answer("Доступ закрыт. Попроси владельца добавить твой ID: "
                   f"<code>{m.from_user.id}</code>")


@router.callback_query(lambda c: not allowed(c.from_user.id))
async def deny_cb(c: CallbackQuery):
    await c.answer("Доступ закрыт", show_alert=True)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message):
    await m.answer(HELP)


@router.message(Command("list"))
async def cmd_list(m: Message):
    draft = core.load_draft(m.chat.id)
    await m.answer(core.render_list(draft["items"]), reply_markup=KB if draft["items"] else None)


async def do_finish(chat_id: int, bot: Bot, reply_to: Message):
    async with locks[chat_id]:
        draft = core.load_draft(chat_id)
        items = draft["items"]
        if not items:
            await reply_to.answer("Список пуст — нечего завершать.")
            return
        no_price = [i["name"] for i in items if i.get("price") is None]
        date = core.now()
        pdf = core.build_pdf(items, date)
        caption = (f"Оприходование {date:%d.%m.%Y} — {len(items)} поз., "
                   f"итого {core.fmt_num(core.total_sum(items), True)}")
        if no_price:
            caption += "\n❗ Без цены: " + ", ".join(no_price)
        # Сначала итог: если отправка сорвётся, ничего не удалим и список останется как был
        list_msg = await bot.send_message(chat_id, core.render_list(items, date, final=True))
        doc_msg = await bot.send_document(
            chat_id, BufferedInputFile(pdf, f"oprihodovanie_{date:%Y-%m-%d_%H-%M}.pdf"), caption=caption)
        final_ids = [list_msg.message_id, doc_msg.message_id]
        core.mark_finished(draft, {"msg_ids": final_ids, "archive": core.archive(chat_id, items)})
        core.save_draft(chat_id, draft)
        old_ids = core.take_msgs(chat_id, keep=final_ids)
    await wipe(bot, chat_id, old_ids)
    hint = await bot.send_message(chat_id, "✅ Готово, переписку почистил. Завершил случайно — /undo")
    delete_later(bot, chat_id, hint.message_id)


@router.message(Command("done"))
async def cmd_done(m: Message, bot: Bot):
    await do_finish(m.chat.id, bot, m)


async def do_undo(chat_id: int, bot: Bot) -> str:
    async with locks[chat_id]:
        draft = core.load_draft(chat_id)
        ok, finished = core.undo(draft)
        if not ok:
            return "Отменять нечего."
        core.save_draft(chat_id, draft)
        if finished:
            core.unarchive(finished.get("archive"))
    if finished:
        # Отменили «Завершить» — убираем итоговый список и PDF, чтобы за день не было дублей
        await wipe(bot, chat_id, finished.get("msg_ids", []))
        return "↩️ Отменил завершение, итоговый список и PDF убрал. Продолжаем:\n\n" + core.render_list(draft["items"])
    return "↩️ Вернул как было.\n\n" + core.render_list(draft["items"])


async def do_clear(chat_id: int) -> str:
    async with locks[chat_id]:
        draft = core.load_draft(chat_id)
        core.set_items(draft, [])
        core.save_draft(chat_id, draft)
    return "🗑 Список очищен. Передумал — /undo."


@router.message(Command("undo"))
async def cmd_undo(m: Message, bot: Bot):
    await m.answer(await do_undo(m.chat.id, bot), reply_markup=KB)


@router.message(Command("clear"))
async def cmd_clear(m: Message):
    await m.answer(await do_clear(m.chat.id))


@router.callback_query(F.data.in_({"done", "undo", "clear"}))
async def on_button(c: CallbackQuery, bot: Bot):
    await c.answer()
    chat_id = c.message.chat.id
    if c.data == "done":
        await do_finish(chat_id, bot, c.message)
    elif c.data == "undo":
        await c.message.answer(await do_undo(chat_id, bot), reply_markup=KB)
    else:
        await c.message.answer(await do_clear(chat_id))


# ---------------------------------------------------------------- справочник

@router.message(Command("catalog"))
async def cmd_catalog(m: Message):
    text = m.text.split(maxsplit=1)
    if len(text) > 1:  # /catalog + список строками
        items = core.parse_catalog_text(text[1])
        core.save_catalog(items)
        await m.answer(f"📚 Справочник обновлён: {len(items)} позиций.")
        return
    cat = core.load_catalog()
    info = (f"📚 В справочнике {len(cat)} позиций." if cat else "📚 Справочник пока пуст.")
    await m.answer(
        info + "\n\nЧтобы загрузить/заменить: пришли файл Excel (.xlsx) или .txt — "
        "в первой колонке точное название как в ROSTA, во второй (необязательно) единица: кг / л / пачка / шт.\n"
        "Или текстом: <code>/catalog</code> и дальше каждая позиция с новой строки, единица через «|»:\n"
        "<code>/catalog\nЛук, ( | кг\nКола, ( | л\nМасло сливочное, ( | пачка</code>")


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(m: Message):
    """Фото не читаем и не отвечаем — по ним пользователь сам надиктовывает голосовое."""
    return


@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    name = (m.document.file_name or "").lower()
    if not name.endswith((".xlsx", ".txt", ".csv")):
        await m.answer("Справочник принимаю в .xlsx, .txt или .csv.")
        return
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    data = buf.getvalue()
    try:
        if name.endswith(".xlsx"):
            items = core.parse_catalog_xlsx(data)
        else:
            text = data.decode("utf-8-sig", errors="replace")
            if name.endswith(".csv"):
                text = text.replace(";", "|")
            items = core.parse_catalog_text(text)
    except Exception as e:  # noqa: BLE001
        log.exception("catalog parse")
        await m.answer(f"Не смог прочитать файл: {e}")
        return
    if not items:
        await m.answer("В файле не нашёл ни одной позиции.")
        return
    core.save_catalog(items)
    with_unit = sum(1 for i in items if i["unit"])
    preview = "\n".join(f"• {i['name']}" + (f" — {i['unit']}" if i["unit"] else "") for i in items[:10])
    await m.answer(f"📚 Справочник обновлён: {len(items)} позиций (с единицей: {with_unit}).\n\n"
                   f"Первые:\n{preview}")


# ---------------------------------------------------------------- голос и текст

async def process(m: Message, text: str, heard: bool, image: bytes | None = None, label: str = ""):
    chat_id = m.chat.id
    async with locks[chat_id]:
        draft = core.load_draft(chat_id)
        try:
            items, comment = await core.update_items(draft["items"], text, image)
        except Exception:  # noqa: BLE001
            log.exception("parse")
            await m.answer("Не получилось разобрать, попробуй ещё раз.")
            return
        core.set_items(draft, items)
        core.save_draft(chat_id, draft)
    from html import escape
    out = f"🎙 <i>{escape(text)}</i>\n\n" if heard else (f"{label}\n\n" if label else "")
    out += core.render_list(items)
    if comment:
        out += f"\n\n💬 {escape(comment)}"
    await m.answer(out, reply_markup=KB)


@router.message(F.voice | F.audio)
async def on_voice(m: Message, bot: Bot):
    media = m.voice or m.audio
    await bot.send_chat_action(m.chat.id, "typing")
    buf = io.BytesIO()
    await bot.download(media, destination=buf)
    try:
        text = await core.transcribe(buf.getvalue(), "voice.ogg" if m.voice else (m.audio.file_name or "audio.mp3"))
    except Exception:  # noqa: BLE001
        log.exception("transcribe")
        await m.answer("Не смог распознать голосовое, попробуй ещё раз.")
        return
    if not text:
        await m.answer("Голосовое пустое — ничего не расслышал.")
        return
    await process(m, text, heard=True)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    await bot.send_chat_action(m.chat.id, "typing")
    await process(m, m.text, heard=False)


async def main():
    if not ALLOWED:
        log.warning("ALLOWED_USER_IDS не задан — ботом может пользоваться кто угодно!")
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(TrackOutgoing())
    dp = Dispatcher()
    dp.message.outer_middleware(TrackIncoming())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
