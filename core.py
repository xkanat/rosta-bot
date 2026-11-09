"""Логика бота: справочник сырья, черновик закупки, разбор речи через OpenAI, расчёты и PDF."""
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DRAFTS_DIR = DATA_DIR / "drafts"
ARCHIVE_DIR = DATA_DIR / "archive"
MSGS_DIR = DATA_DIR / "msgs"
CATALOG_FILE = DATA_DIR / "catalog.json"
for d in (DRAFTS_DIR, ARCHIVE_DIR, MSGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Aqtobe"))
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
PARSE_MODEL = os.getenv("PARSE_MODEL", "gpt-4.1-mini")
HISTORY_LIMIT = 30

KNOWN_UNITS = {
    "кг": "кг", "килограмм": "кг", "кило": "кг",
    "г": "г", "гр": "г", "грамм": "г",
    "л": "л", "литр": "л", "литры": "л",
    "мл": "мл",
    "шт": "шт", "штука": "шт", "штук": "шт",
    "пачка": "пачка", "пачки": "пачка", "пач": "пачка", "уп": "пачка", "упаковка": "пачка",
}

client = AsyncOpenAI()


def now() -> datetime:
    return datetime.now(TZ)


# ---------------------------------------------------------------- справочник

def load_catalog() -> list[dict]:
    if CATALOG_FILE.exists():
        return json.loads(CATALOG_FILE.read_text("utf-8"))
    return []


def save_catalog(items: list[dict]) -> None:
    CATALOG_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")


def _norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    return KNOWN_UNITS.get(u.strip().lower().rstrip("."))


def parse_catalog_rows(rows: list[list[str]]) -> list[dict]:
    """rows: [[название, (ед.)], ...]. Название берётся как есть — со всеми запятыми и скобками."""
    out, seen = [], set()
    for row in rows:
        cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if not cells:
            continue
        name = cells[0]
        if name.lower() in ("наименование", "название", "товар", "номенклатура"):
            continue  # шапка таблицы
        unit = None
        for c in cells[1:]:
            unit = _norm_unit(c)
            if unit:
                break
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "unit": unit})
    return out


def parse_catalog_text(text: str) -> list[dict]:
    # Разделитель между названием и единицей — «|», «;» или таб (запятые есть в самих названиях)
    rows = [re.split(r"\s*[|;\t]\s*", line) for line in text.splitlines()]
    return parse_catalog_rows(rows)


def parse_catalog_xlsx(data: bytes) -> list[dict]:
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
    return parse_catalog_rows([list(r) for r in ws.iter_rows(values_only=True)])


# ---------------------------------------------------------------- черновик

def _draft_path(chat_id: int) -> Path:
    return DRAFTS_DIR / f"{chat_id}.json"


def load_draft(chat_id: int) -> dict:
    p = _draft_path(chat_id)
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    return {"items": [], "history": []}


def save_draft(chat_id: int, draft: dict) -> None:
    draft["history"] = draft["history"][-HISTORY_LIMIT:]
    _draft_path(chat_id).write_text(json.dumps(draft, ensure_ascii=False, indent=1), "utf-8")


def set_items(draft: dict, items: list[dict]) -> None:
    draft["history"].append(draft["items"])
    draft["items"] = items


def mark_finished(draft: dict, info: dict) -> None:
    """Завершение: список уходит в историю с пометкой, чтобы /undo мог убрать итоговые сообщения."""
    draft["history"].append({"items": draft["items"], "finish": info})
    draft["items"] = []


def undo(draft: dict) -> tuple[bool, dict | None]:
    """Возвращает (получилось ли, данные завершения — если отменили именно «Завершить»)."""
    if not draft["history"]:
        return False, None
    entry = draft["history"].pop()
    if isinstance(entry, dict):
        draft["items"] = entry["items"]
        return True, entry.get("finish")
    draft["items"] = entry
    return True, None


def archive(chat_id: int, items: list[dict]) -> str:
    stamp = now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    name = f"{chat_id}_{stamp}.json"
    (ARCHIVE_DIR / name).write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    return name


def unarchive(name: str | None) -> None:
    if name:
        (ARCHIVE_DIR / Path(name).name).unlink(missing_ok=True)


# ---------------------------------------------------------------- сообщения для очистки чата
# Все сообщения после прошлого «Завершить» (голосовые, скрины, ответы бота) записываются сюда,
# чтобы при следующем «Завершить» удалить их и оставить в чате только итоговый список и PDF.
# Функции синхронные, без await внутри — в asyncio чтение+запись файла не пересекаются.

def _msgs_path(chat_id: int) -> Path:
    return MSGS_DIR / f"{chat_id}.json"


def _load_msgs(chat_id: int) -> list[int]:
    p = _msgs_path(chat_id)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            return []
    return []


def track_msgs(chat_id: int, ids: list[int]) -> None:
    cur = _load_msgs(chat_id)
    seen = set(cur)
    cur += [i for i in ids if i not in seen]
    _msgs_path(chat_id).write_text(json.dumps(cur[-3000:]), "utf-8")


def untrack_msgs(chat_id: int, ids: list[int]) -> None:
    drop = set(ids)
    _msgs_path(chat_id).write_text(json.dumps([i for i in _load_msgs(chat_id) if i not in drop]), "utf-8")


def take_msgs(chat_id: int, keep: list[int] = ()) -> list[int]:
    """Забирает накопленные id на удаление (кроме keep) и очищает список."""
    keep = set(keep)
    ids = [i for i in _load_msgs(chat_id) if i not in keep]
    _msgs_path(chat_id).write_text("[]", "utf-8")
    return ids


# ---------------------------------------------------------------- расчёты

def calc(item: dict) -> tuple[float | None, float | None]:
    """Возвращает (цена за единицу, сумма). Цена может быть сказана за единицу или за всё."""
    price, qty = item.get("price"), item.get("qty") or 0
    if price is None:
        return None, None
    if item.get("price_type") == "total":
        return (round(price / qty, 2) if qty else None), round(price, 2)
    return round(price, 2), round(price * qty, 2)


def fmt_num(x: float | None, money: bool = False, digits: int = 2) -> str:
    if x is None:
        return "—"
    s = f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")
    s = s.rstrip("0").rstrip(",")
    return s + (" ₸" if money else "")


def total_sum(items: list[dict]) -> float:
    return sum(calc(i)[1] or 0 for i in items)


# ---------------------------------------------------------------- OpenAI

async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    catalog = load_catalog()
    hint = "Закупка продуктов для кемпинга: название, количество, килограммы, литры, пачки, цена в тенге."
    if catalog:
        hint += " Товары: " + ", ".join(c["name"].strip(" \",(") for c in catalog[:150])
    r = await client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL, file=(filename, audio), prompt=hint[:2000])
    return r.text.strip()


SYSTEM_PROMPT = """Ты ведёшь список закупки для оприходования товара в учётной программе кемпинга (Казахстан, тенге).
Тебе дают текущий список (JSON), справочник сырья и новое сообщение пользователя (расшифровка голосового, бывает с ошибками распознавания, русский/казахский вперемешку).
Верни ПОЛНЫЙ обновлённый список.

Правила:
1. Новые товары добавляй в конец. Если пользователь исправляет или удаляет («убери лук», «не 35, а 30», «лук по 160», «последнее отмени») — меняй/удаляй существующие позиции. Уже имеющиеся позиции, которых сообщение не касается, верни без изменений (те же name, qty, unit, price, price_type).
2. Если тот же товар с той же ценой за единицу называют повторно как докупку — можно сложить количество в одну строку. Иначе отдельной строкой.
3. name: если справочник не пустой — выбери ТОЧНОЕ название из справочника, символ в символ, включая кавычку в начале (например "лук, "баранина), и поставь in_catalog=true. Кавычка — часть названия, не убирай её. Если подходящего нет — напиши название как сказано, с заглавной буквы, in_catalog=false.
4. unit: если у товара в справочнике указана единица — используй её и переведи количество (500 г при единице кг → 0.5). Иначе: «кг», «л», «шт», «пачка», «г», «мл» — как сказано.
5. Цена: «по 170», «170 за кило», «по 300 за пачку» → price=170, price_type="unit". «35 кг за 5950», «на 6 тысяч», «всего 6000», «отдал 6000» → price=5950, price_type="total". Если цену не назвали — price=null, price_type="unit".
6. Числа словами переводи в цифры: «полторы тысячи»=1500, «пять девятьсот пятьдесят»=5950, «полкило»=0.5 кг, «сто семьдесят»=170.
7. comment: коротко по-русски, только если что-то непонятно или пришлось угадывать (например «не понял цену на хлеб»). Иначе пустая строка. Если сообщение вообще не про закупку — верни список без изменений и напиши об этом в comment.
8. Если пришло ФОТО/скрин заказа или чека из магазина (например, приложение гипермаркета):
   - добавь каждую строку заказа как позицию; названия магазина длинные («Огурцы Теплица», «Масло Белес подсолнечное рафинированное 5 л») — сопоставь со справочником по смыслу («огурцы», «масло подсолнечное»);
   - бери ФАКТИЧЕСКОЕ количество/вес (например, 1,324 кг, а не «из 1,2 кг») и ИТОГОВУЮ сумму строки как price с price_type="total" (в ней уже учтены скидки);
   - если в справочнике единица кг/л, а товар продаётся штуками с весом/объёмом в названии — пересчитай: 1 шт «5 л» → qty=5, unit="л"; 2 шт по 340 г → qty=0.68 кг. Если вес/объём не указан — оставь штуки и напиши об этом в comment;
   - если товар не похож ни на одну позицию справочника (например, консервированный горошек — это не сухой горох) — НЕ подгоняй, напиши как в чеке коротко, in_catalog=false;
   - если на фото не заказ/чек или ничего не разобрать — верни список без изменений и скажи об этом в comment."""

SCHEMA = {
    "name": "purchase_list",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "comment"],
        "properties": {
            "comment": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "in_catalog", "qty", "unit", "price", "price_type"],
                    "properties": {
                        "name": {"type": "string"},
                        "in_catalog": {"type": "boolean"},
                        "qty": {"type": "number"},
                        "unit": {"type": "string"},
                        "price": {"type": ["number", "null"]},
                        "price_type": {"type": "string", "enum": ["unit", "total"]},
                    },
                },
            },
        },
    },
}


async def update_items(items: list[dict], message: str, image: bytes | None = None) -> tuple[list[dict], str]:
    catalog = load_catalog()
    cat_text = "\n".join(f"{c['name']}" + (f" | {c['unit']}" if c.get("unit") else "") for c in catalog) or "(пусто)"
    user = (f"Справочник сырья:\n{cat_text}\n\n"
            f"Текущий список:\n{json.dumps(items, ensure_ascii=False)}\n\n"
            f"Новое сообщение:\n{message}")
    r = await client.chat.completions.create(
        model=PARSE_MODEL,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": SCHEMA},
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": _content(user, image)}],
    )
    data = json.loads(r.choices[0].message.content)
    # Модель может потерять кавычку/скобку или сменить регистр — подставляем точное название из справочника
    exact = {_key(c["name"]): c for c in catalog}
    for it in data["items"]:
        c = exact.get(_key(it["name"]))
        if c:
            it["name"] = c["name"]
            if c.get("unit") and not it.get("unit"):
                it["unit"] = c["unit"]
        it["in_catalog"] = c is not None
    return data["items"], data["comment"].strip()


def _content(text: str, image: bytes | None):
    if not image:
        return text
    import base64
    b64 = base64.b64encode(image).decode()
    return [{"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}}]


def _key(name: str) -> str:
    """Ключ для сравнения названий: без кавычек, лишних знаков по краям, регистра и ё."""
    s = name.lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \"'«»“”„,.(")


# ---------------------------------------------------------------- вывод

FINAL_TAG = "#оприходование"


def render_list(items: list[dict], date: datetime | None = None, final: bool = False) -> str:
    """HTML для Telegram. final=True — итоговый список, который остаётся в чате после «Завершить»."""
    from html import escape
    date = date or now()
    if not items:
        return f"🧾 <b>Закупка {date:%d.%m.%Y}</b>\n\nСписок пуст. Надиктуй, что купил."
    has_catalog = bool(load_catalog())
    header = (f"📋 <b>Оприходование {date:%d.%m.%Y}</b> · {date:%H:%M}" if final
              else f"🧾 <b>Закупка {date:%d.%m.%Y}</b>")
    lines = [header + "\n"]
    no_price = not_found = 0
    for n, it in enumerate(items, 1):
        unit_price, total = calc(it)
        mark = ""
        if has_catalog and not it.get("in_catalog"):
            mark, not_found = " ⚠️", not_found + 1
        if unit_price is None:
            no_price += 1
            price_part = "<i>цена?</i>"
        else:
            price_part = f"{fmt_num(unit_price)} ₸/{escape(it['unit'])} = <b>{fmt_num(total, True)}</b>"
        lines.append(f"{n}. {escape(it['name'])}{mark} — {fmt_num(it['qty'], digits=3)} {escape(it['unit'])} × {price_part}")
    lines.append(f"\n<b>Итого: {fmt_num(total_sum(items), True)}</b>")
    if no_price:
        lines.append(f"❗ Без цены: {no_price} поз.")
    if not_found:
        lines.append(f"⚠️ Нет в справочнике: {not_found} поз. — проверь название")
    if final:
        lines.append(f"\n{FINAL_TAG}")
    return "\n".join(lines)


_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    dirs = [BASE_DIR / "fonts", Path("/usr/share/fonts/truetype/dejavu")]
    font_dir = next(d for d in dirs if (d / "DejaVuSans.ttf").exists())
    pdfmetrics.registerFont(TTFont("DejaVu", str(font_dir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(font_dir / "DejaVuSans-Bold.ttf")))
    _fonts_ready = True


def build_pdf(items: list[dict], date: datetime | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _register_fonts()
    date = date or now()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Оприходование {date:%d.%m.%Y}")
    cell = ParagraphStyle("cell", fontName="DejaVu", fontSize=16, leading=20)
    head = ParagraphStyle("head", fontName="DejaVu-Bold", fontSize=13, leading=16, alignment=1)
    title = ParagraphStyle("title", fontName="DejaVu-Bold", fontSize=24, leading=30)

    rows = [[Paragraph(h, head) for h in ("Наименование", "Кол-во", "Закупочная<br/>цена", "Сумма")]]
    for it in items:
        unit_price, total = calc(it)
        rows.append([
            Paragraph(it["name"].replace("&", "&amp;").replace("<", "&lt;"), cell),
            f"{fmt_num(it['qty'], digits=3)} {it['unit']}",
            fmt_num(unit_price),
            fmt_num(total),
        ])
    rows.append(["ИТОГО", "", "", fmt_num(total_sum(items))])

    t = Table(rows, colWidths=[68 * mm, 40 * mm, 36 * mm, 36 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 16),
        ("LEADING", (0, 0), (-1, -1), 20),
        ("FONTNAME", (0, -1), (-1, -1), "DejaVu-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 18),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
        ("GRID", (0, 0), (-1, -1), 1.2, colors.black),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("SPAN", (0, -1), (2, -1)),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    doc.build([Paragraph(f"Оприходование от {date:%d.%m.%Y}", title), Spacer(1, 6 * mm), t])
    return buf.getvalue()
