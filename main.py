import asyncio
import csv
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from urllib.parse import quote_plus, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo


MONTHS = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}


CRM_CACHE_TTL_SECONDS = 60


@dataclass
class Booking:
    booking_id: str
    client_name: str | None
    date_time: datetime | None
    studio: dict | None
    guide: dict | None
    work_price: float | None
    studio_price: float | None


def clean_key(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def load_sheet(sheet_id: str, gid: str) -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"

    rows = list(csv.reader(StringIO(response.text)))
    if not rows:
        return []

    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if "Название студии" in row or "Тип съемки" in row
        ),
        0,
    )

    cleaned_csv = StringIO()
    writer = csv.writer(cleaned_csv)
    writer.writerows(rows[header_index:])
    cleaned_csv.seek(0)
    return list(csv.DictReader(cleaned_csv))


def parse_name(text: str) -> str | None:
    first_part = re.split(r"[,;\n]", text.strip(), maxsplit=1)[0].strip()
    words = first_part.split()
    if not words:
        return None

    ignored = {"съемка", "сьемка", "фотосессия", "клиент", "клиентка"}
    for word in words:
        normalized = clean_key(re.sub(r"[^А-Яа-яA-Za-z-]", "", word))
        if normalized and normalized not in ignored and not normalized.isdigit():
            return word.strip(" ,.;:")
    return None


def parse_price(text: str, labels: list[str]) -> float | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:=-]?\s*(\d+(?:[.,]\d{{1,2}})?)\s*(?:€|евро)?",
        clean_key(text),
    )
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def parse_datetime(text: str, timezone: str) -> datetime | None:
    now = datetime.now(ZoneInfo(timezone))
    time_match = re.search(r"\b([01]?\d|2[0-3])[:. ]([0-5]\d)\b", text)
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))

    numeric_date = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", text)
    if numeric_date:
        day = int(numeric_date.group(1))
        month = int(numeric_date.group(2))
        year = int(numeric_date.group(3)) if numeric_date.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            result = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone))
        except ValueError:
            return None
        return bump_to_future(result, now)

    month_names = "|".join(MONTHS.keys())
    text_date = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b",
        clean_key(text),
    )
    if text_date:
        day = int(text_date.group(1))
        month = MONTHS[text_date.group(2)]
        year = int(text_date.group(3)) if text_date.group(3) else now.year
        try:
            result = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone))
        except ValueError:
            return None
        return bump_to_future(result, now)

    return None


def bump_to_future(value: datetime, now: datetime) -> datetime:
    if value < now:
        try:
            return value.replace(year=value.year + 1)
        except ValueError:
            return value.replace(year=value.year + 1, day=28)
    return value


def find_studio(text: str, studios: list[dict]) -> dict | None:
    normalized = clean_key(text)
    matches = []
    for studio in studios:
        name = studio.get("Название студии", "")
        key = clean_key(name)
        if key and key in normalized:
            matches.append((len(key), studio))
    return sorted(matches, key=lambda item: item[0], reverse=True)[0][1] if matches else None


def find_guide(text: str, guides: list[dict]) -> dict | None:
    normalized = clean_key(text)
    matches = []
    for guide in guides:
        shoot_type = guide.get("Тип съемки", "")
        key = clean_key(shoot_type)
        if key and key in normalized:
            matches.append((len(key), guide))
    return sorted(matches, key=lambda item: item[0], reverse=True)[0][1] if matches else None


def parse_booking(
    text: str,
    studios: list[dict],
    guides: list[dict],
    timezone: str,
) -> Booking:
    return Booking(
        booking_id=uuid.uuid4().hex[:10],
        client_name=parse_name(text),
        date_time=parse_datetime(text, timezone),
        studio=find_studio(text, studios),
        guide=find_guide(text, guides),
        work_price=parse_price(
            text,
            ["работа", "моя стоимость", "фотограф", "съемка", "сьемка"],
        ),
        studio_price=parse_price(
            text,
            ["студия", "аренда студии"],
        ),
    )


def missing_fields(booking: Booking) -> list[str]:
    missing = []
    if not booking.client_name:
        missing.append("имя клиента")
    if not booking.date_time:
        missing.append("дату и время")
    if booking.work_price is None:
        missing.append("стоимость моей работы")
    return missing


def calendar_link(booking: Booking, duration_minutes: int, timezone: str) -> str:
    if booking.date_time is None:
        raise ValueError("Дата и время не указаны")

    start = booking.date_time
    end = start + timedelta(minutes=duration_minutes)
    studio = booking.studio or {}

    studio_name = studio.get("Название студии", "").strip() or "не указана"
    address = studio.get("Адрес", "").strip()

    params = {
        "action": "TEMPLATE",
        "text": f"Фотосессия {booking.client_name}",
        "dates": f"{start:%Y%m%dT%H%M%S}/{end:%Y%m%dT%H%M%S}",
        "ctz": timezone,
        "location": address,
        "details": (
            f"Клиент: {booking.client_name}\n"
            f"Студия: {studio_name}\n"
            f"Моя работа: {format_euro(booking.work_price)}\n"
            f"Студия: {format_euro(booking.studio_price)}"
        ),
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(
        params, quote_via=quote_plus
    )


def delivery_calendar_link(booking: Booking, timezone: str) -> str:
    if booking.date_time is None:
        raise ValueError("Дата съёмки не указана")

    delivery_date = (booking.date_time + timedelta(days=14)).date()
    next_day = delivery_date + timedelta(days=1)

    params = {
        "action": "TEMPLATE",
        "text": f"Отдать фото — {booking.client_name}",
        "dates": f"{delivery_date:%Y%m%d}/{next_day:%Y%m%d}",
        "ctz": timezone,
        "details": f"Съёмка была {booking.date_time:%d.%m.%Y}.",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(
        params, quote_via=quote_plus
    )


def format_euro(value: float | None) -> str:
    if value is None:
        return "не указана"
    if value.is_integer():
        return f"{int(value)} €"
    return f"{value:.2f} €".replace(".", ",")


def format_client_message(booking: Booking) -> str:
    if booking.date_time is None:
        raise ValueError("Дата и время не указаны")

    studio = booking.studio or {}
    guide = booking.guide or {}

    studio_name = studio.get("Название студии", "").strip() or "уточняется"
    address = studio.get("Адрес", "").strip()
    arrive = studio.get("За сколько минут прийти", "").strip()

    lines = [
        f"{booking.client_name}, записала вас на съёмку 🤍",
        "",
        f"Дата: {booking.date_time:%d.%m.%Y}",
        f"Время: {booking.date_time:%H:%M}",
        f"Студия: {studio_name}",
    ]

    if address:
        lines.append(f"Адрес: {address}")

    if arrive:
        lines.extend(["", f"Пожалуйста, приходите за {arrive} минут до начала."])

    lines.extend(
        [
            "",
            f"Стоимость моей работы: {format_euro(booking.work_price)}.",
            (
                "Оплата производится на месте после съёмки. "
                "Пожалуйста, подготовьте сумму без сдачи. "
                "Если вам будет нужна сдача, предупредите меня заранее 🤍"
            ),
        ]
    )

    if booking.studio_price is not None:
        lines.extend(
            [
                "",
                f"Стоимость аренды студии: {format_euro(booking.studio_price)}.",
                (
                    "Эта сумма оплачивается отдельно заранее как предоплата за студию. "
                    "Реквизиты для оплаты я отправлю отдельным сообщением."
                ),
            ]
        )

    for label, key in [
        ("Парковка", "Парковка"),
        ("Как зайти", "Как зайти"),
        ("Дополнительно", "Дополнительно"),
    ]:
        value = studio.get(key, "").strip()
        if value:
            lines.append(f"{label}: {value}" if label != "Дополнительно" else value)

    guide_lines = []
    for label, key in [
        ("Одежда", "Одежда"),
        ("Позы", "Позы"),
        ("Подготовка", "Подготовка"),
    ]:
        value = guide.get(key, "").strip()
        if value:
            guide_lines.append(f"{label}: {value}")

    if guide_lines:
        lines.extend(["", "Гайды для подготовки:", *guide_lines])

    lines.extend(["", "Жду вас 🤍"])
    return "\n".join(lines)


def crm_write(webhook_url: str, payload: dict) -> dict:
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=(10, 30),
            allow_redirects=False,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PhotoBookingBot/1.0",
            },
        )
    except requests.Timeout as error:
        raise RuntimeError(
            "Google Apps Script слишком долго отвечал"
        ) from error
    except requests.RequestException as error:
        raise RuntimeError(
            f"Не удалось связаться с Google CRM: {error}"
        ) from error

    # Google Apps Script выполняет операцию до перенаправления.
    if response.status_code in (200, 201, 202, 204, 301, 302, 303, 307, 308):
        return {"ok": True}

    response.raise_for_status()
    return {"ok": True}


def load_crm_records(sheet_id: str, crm_gid: str) -> list[dict]:
    return load_sheet(sheet_id, crm_gid)


def get_cached_crm_records(config: dict) -> list[dict]:
    now = datetime.now().timestamp()
    cached_at = config.get("_crm_cache_at", 0)
    cached_records = config.get("_crm_cache_records")

    if (
        cached_records is not None
        and now - cached_at < CRM_CACHE_TTL_SECONDS
    ):
        return cached_records

    records = get_cached_crm_records(config)

    config["_crm_cache_records"] = records
    config["_crm_cache_at"] = now

    return records


def invalidate_crm_cache(config: dict) -> None:
    config["_crm_cache_at"] = 0


def parse_crm_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None

    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    return None


def crm_read(config: dict, payload: dict) -> dict:
    records = get_cached_crm_records(config)
    action = payload.get("action")

    if action == "get":
        booking_id = str(payload.get("id", ""))
        record = next(
            (
                row
                for row in records
                if str(row.get("ID", "")) == booking_id
            ),
            None,
        )
        return {"ok": bool(record), "record": record}

    if action == "search":
        query = clean_key(str(payload.get("query", "")))
        found = [
            row
            for row in records
            if query in clean_key(str(row.get("Клиент", "")))
        ]
        return {"ok": True, "records": found[:30]}

    if action == "list":
        list_type = payload.get("list_type", "active")
        today = datetime.now().date()
        found = []

        for row in records:
            status = str(row.get("Статус", ""))
            work_paid = str(row.get("Мне оплачено", ""))
            studio_paid = str(row.get("Студия оплачена", ""))
            photos_delivered = str(row.get("Фото отданы", ""))
            studio_price_raw = str(
                row.get("Стоимость студии, €", "0")
            ).replace("€", "").replace(",", ".").strip()

            try:
                studio_price = float(studio_price_raw or 0)
            except ValueError:
                studio_price = 0

            delivery_date = parse_crm_date(
                str(row.get("Срок отдачи", ""))
            )

            include = False

            if list_type == "active":
                include = status not in ("Закрыта", "Отменена")
            elif list_type == "delivery":
                include = (
                    photos_delivered != "Да"
                    and delivery_date is not None
                    and delivery_date.date() <= today
                )
            elif list_type == "unpaid_work":
                include = (
                    work_paid != "Да"
                    and status != "Отменена"
                )
            elif list_type == "unpaid_studio":
                include = (
                    studio_price > 0
                    and studio_paid != "Да"
                    and status != "Отменена"
                )

            if include:
                found.append(row)

        found.sort(
            key=lambda row: (
                parse_crm_date(str(row.get("Дата съёмки", "")))
                or datetime.max
            )
        )
        return {"ok": True, "records": found[:50]}

    raise RuntimeError("Неизвестная команда чтения CRM")


async def create_crm_record(
    webhook_url: str,
    booking: Booking,
    shoot_type: str,
) -> None:
    if booking.date_time is None:
        raise ValueError("Дата съёмки не указана")

    studio_name = (booking.studio or {}).get("Название студии", "").strip()

    payload = {
        "action": "create",
        "id": booking.booking_id,
        "created_at": datetime.now(booking.date_time.tzinfo).isoformat(),
        "shoot_date": booking.date_time.strftime("%Y-%m-%d"),
        "shoot_time": booking.date_time.strftime("%H:%M"),
        "client": booking.client_name or "",
        "shoot_type": shoot_type,
        "studio": studio_name,
        "work_price": booking.work_price or 0,
        "studio_price": booking.studio_price or 0,
        "studio_paid": "Нет",
        "work_paid": "Нет",
        "status": "Записана",
        "photos_delivered": "Нет",
        "comment": "",
    }
    await asyncio.to_thread(crm_write, webhook_url, payload)


async def refresh_sheet_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    now = datetime.now().timestamp()
    last_refresh = context.application.bot_data.get(
        "sheet_cache_at",
        0,
    )

    if now - last_refresh < 60:
        return

    try:
        studios, guides = await asyncio.gather(
            asyncio.to_thread(
                load_sheet,
                config["sheet_id"],
                config["studios_gid"],
            ),
            asyncio.to_thread(
                load_sheet,
                config["sheet_id"],
                config["guides_gid"],
            ),
        )
        context.application.bot_data["studios"] = studios
        context.application.bot_data["guides"] = guides
        context.application.bot_data["sheet_cache_at"] = now
    except (requests.RequestException, csv.Error, ValueError):
        pass


def booking_keyboard(
    booking: Booking,
    shoot_link: str,
    delivery_link: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Добавить съёмку", url=shoot_link)],
            [InlineKeyboardButton("📸 Добавить срок отдачи", url=delivery_link)],
            [
                InlineKeyboardButton(
                    "💵 Мне оплачено",
                    callback_data=f"crm:work_paid:{booking.booking_id}",
                ),
                InlineKeyboardButton(
                    "🏢 Студия оплачена",
                    callback_data=f"crm:studio_paid:{booking.booking_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📤 Фото отданы",
                    callback_data=f"crm:photos_delivered:{booking.booking_id}",
                ),
                InlineKeyboardButton(
                    "✅ Закрыть",
                    callback_data=f"crm:close:{booking.booking_id}",
                ),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Напишите запись одной фразой:\n\n"
        "Анна, 12 августа 14:00, M50, беременность, "
        "работа 85, студия 35\n\n"
        "Студию можно не указывать."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    await refresh_sheet_data(context)

    config = context.application.bot_data["config"]
    studios = context.application.bot_data.get("studios", [])
    guides = context.application.bot_data.get("guides", [])

    booking = parse_booking(
        update.message.text,
        studios,
        guides,
        config["timezone"],
    )

    missing = missing_fields(booking)
    if missing:
        await update.message.reply_text(
            "Мне не хватило: "
            + ", ".join(missing)
            + ".\n\nПример:\n"
            "Анна, 12 августа 14:00, M50, беременность, работа 85, студия 35"
        )
        return

    shoot_type = (
        booking.guide.get("Тип съемки", "").strip()
        if booking.guide
        else ""
    )

    try:
        shoot_link = calendar_link(
            booking,
            config["duration_minutes"],
            config["timezone"],
        )
        delivery_link = delivery_calendar_link(
            booking,
            config["timezone"],
        )
        client_message = format_client_message(booking)
    except Exception as error:
        await update.message.reply_text(
            "Не получилось подготовить запись.\n"
            f"Ошибка: {error}"
        )
        return

    delivery_date = booking.date_time + timedelta(days=14)

    await update.message.reply_text(
        "⏳ Готово. Сохраняю запись в CRM…\n\n"
        f"💰 Моя работа: {format_euro(booking.work_price)}\n"
        f"🏢 Студия: {format_euro(booking.studio_price)}\n"
        f"📸 Срок отдачи: {delivery_date:%d.%m.%Y}\n\n"
        "Сообщение для клиента:\n\n"
        + client_message,
        reply_markup=booking_keyboard(
            booking,
            shoot_link,
            delivery_link,
        ),
    )

    try:
        await asyncio.wait_for(
            create_crm_record(
                config["crm_webhook_url"],
                booking,
                shoot_type,
            ),
            timeout=25,
        )
        await update.message.reply_text("✅ Запись сохранена в CRM")
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⚠️ Google CRM отвечает слишком долго. "
            "Сообщение и календарь уже готовы, но строка в таблице могла не сохраниться."
        )
    except Exception as error:
        await update.message.reply_text(
            "⚠️ Сообщение и календарь готовы, но CRM не сохранилась.\n"
            f"Ошибка: {error}"
        )


async def handle_crm_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    try:
        _, action, booking_id = query.data.split(":", 2)
    except ValueError:
        await query.answer("Неверная команда", show_alert=True)
        return

    update_map = {
        "work_paid": {
            "column": "Мне оплачено",
            "value": "Да",
            "message": "💵 Оплата тебе отмечена",
        },
        "studio_paid": {
            "column": "Студия оплачена",
            "value": "Да",
            "message": "🏢 Оплата студии отмечена",
        },
        "photos_delivered": {
            "column": "Фото отданы",
            "value": "Да",
            "extra": {"Статус": "Фото отправлены"},
            "message": "📤 Фото отмечены как отправленные",
        },
        "close": {
            "column": "Статус",
            "value": "Закрыта",
            "message": "✅ Заказ закрыт",
        },
    }

    command = update_map.get(action)
    if not command:
        await query.answer("Неизвестная команда", show_alert=True)
        return

    payload = {
        "action": "update",
        "id": booking_id,
        "column": command["column"],
        "value": command["value"],
        "extra": command.get("extra", {}),
    }

    try:
        await asyncio.to_thread(
            crm_write,
            context.application.bot_data["config"]["crm_webhook_url"],
            payload,
        )
    except Exception as error:
        await query.answer(f"Ошибка CRM: {error}", show_alert=True)
        return

    await query.answer(command["message"], show_alert=True)



def crm_get(config: dict, payload: dict) -> dict:
    action = payload.get("action")

    if action in {"get", "search", "list"}:
        result = crm_read(config, payload)
    else:
        result = crm_write(
            config["crm_webhook_url"],
            payload,
        )
        invalidate_crm_cache(config)

    if not result.get("ok"):
        raise RuntimeError(
            result.get("error", "CRM вернула ошибку")
        )

    return result


def booking_status_keyboard(booking_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 Записана",
                    callback_data=f"cs:booked:{booking_id}",
                ),
                InlineKeyboardButton(
                    "📸 Съёмка прошла",
                    callback_data=f"cs:done:{booking_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💻 Обработка",
                    callback_data=f"cs:edit:{booking_id}",
                ),
                InlineKeyboardButton(
                    "📤 Фото отправлены",
                    callback_data=f"cs:sent:{booking_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💵 Мне оплачено",
                    callback_data=f"cf:work:{booking_id}",
                ),
                InlineKeyboardButton(
                    "🏢 Студия оплачена",
                    callback_data=f"cf:studio:{booking_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Закрыть",
                    callback_data=f"cs:closed:{booking_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data=f"cs:cancel:{booking_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ К списку",
                    callback_data="crmlist:active",
                )
            ],
        ]
    )


def format_crm_card(record: dict) -> str:
    return (
        f"📸 {record.get('Клиент', 'Без имени')}\n\n"
        f"📅 {record.get('Дата съёмки', '')}\n"
        f"🕒 {record.get('Время', '')}\n"
        f"🏢 {record.get('Студия', '') or 'Студия не указана'}\n"
        f"🎞 {record.get('Тип съёмки', '') or 'Тип не указан'}\n\n"
        f"💰 Моя работа: {record.get('Моя стоимость, €', 0)} €\n"
        f"🏢 Студия: {record.get('Стоимость студии, €', 0)} €\n"
        f"💵 Мне оплачено: {record.get('Мне оплачено', 'Нет')}\n"
        f"🏢 Студия оплачена: {record.get('Студия оплачена', 'Нет')}\n\n"
        f"📌 Статус: {record.get('Статус', '')}\n"
        f"📤 Фото отданы: {record.get('Фото отданы', 'Нет')}\n"
        f"⏳ Срок отдачи: {record.get('Срок отдачи', '')}"
    )


async def crm_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 Активные съёмки",
                    callback_data="crmlist:active",
                )
            ],
            [
                InlineKeyboardButton(
                    "📤 Пора отдавать",
                    callback_data="crmlist:delivery",
                )
            ],
            [
                InlineKeyboardButton(
                    "💵 Не оплачено мне",
                    callback_data="crmlist:unpaid_work",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏢 Не оплачена студия",
                    callback_data="crmlist:unpaid_studio",
                )
            ],
        ]
    )

    await update.message.reply_text(
        "📋 CRM\n\nВыберите раздел:",
        reply_markup=keyboard,
    )


async def client_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    query_text = " ".join(context.args).strip()

    if not query_text:
        await update.message.reply_text(
            "Напишите имя после команды.\nНапример:\n/client Анна"
        )
        return

    try:
        data = await asyncio.to_thread(
            crm_get,
            context.application.bot_data["config"],
            {
                "action": "search",
                "query": query_text,
            },
        )
    except Exception as error:
        await update.message.reply_text(f"Ошибка CRM: {error}")
        return

    records = data.get("records", [])

    if not records:
        await update.message.reply_text("Клиент не найден.")
        return

    if len(records) == 1:
        record = records[0]
        await update.message.reply_text(
            format_crm_card(record),
            reply_markup=booking_status_keyboard(record["ID"]),
        )
        return

    keyboard = []
    for record in records[:20]:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{record.get('Дата съёмки', '')} — {record.get('Клиент', '')}",
                    callback_data=f"crmopen:{record['ID']}",
                )
            ]
        )

    await update.message.reply_text(
        "Нашла несколько записей:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_crm_navigation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""
    webhook = context.application.bot_data["config"]["crm_webhook_url"]

    try:
        if data.startswith("crmlist:"):
            list_type = data.split(":", 1)[1]
            response = await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "list",
                    "list_type": list_type,
                },
            )
            records = response.get("records", [])

            if not records:
                await query.edit_message_text("В этом разделе пока пусто ✅")
                return

            keyboard = []
            for record in records[:30]:
                label = (
                    f"{record.get('Дата съёмки', '')} "
                    f"{record.get('Время', '')} — "
                    f"{record.get('Клиент', '')}"
                )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            label,
                            callback_data=f"crmopen:{record['ID']}",
                        )
                    ]
                )

            keyboard.append(
                [InlineKeyboardButton("⬅️ В CRM", callback_data="crmmenu")]
            )
            await query.edit_message_text(
                "Выберите съёмку:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if data == "crmmenu":
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📅 Активные съёмки", callback_data="crmlist:active")],
                    [InlineKeyboardButton("📤 Пора отдавать", callback_data="crmlist:delivery")],
                    [InlineKeyboardButton("💵 Не оплачено мне", callback_data="crmlist:unpaid_work")],
                    [InlineKeyboardButton("🏢 Не оплачена студия", callback_data="crmlist:unpaid_studio")],
                ]
            )
            await query.edit_message_text(
                "📋 CRM\n\nВыберите раздел:",
                reply_markup=keyboard,
            )
            return

        if data.startswith("crmopen:"):
            booking_id = data.split(":", 1)[1]
            response = await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "get",
                    "id": booking_id,
                },
            )
            record = response.get("record")
            if not record:
                await query.edit_message_text("Запись не найдена.")
                return

            await query.edit_message_text(
                format_crm_card(record),
                reply_markup=booking_status_keyboard(booking_id),
            )
            return

        if data.startswith("cs:"):
            _, status_code, booking_id = data.split(":", 2)

            status_map = {
                "booked": "Записана",
                "done": "Съёмка прошла",
                "edit": "Обработка",
                "sent": "Фото отправлены",
                "closed": "Закрыта",
                "cancel": "Отменена",
            }

            status = status_map.get(status_code)
            if not status:
                await query.answer("Неизвестный статус", show_alert=True)
                return

            extra = {}
            if status_code == "sent":
                extra["Фото отданы"] = "Да"

            await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "update",
                    "id": booking_id,
                    "column": "Статус",
                    "value": status,
                    "extra": extra,
                },
            )

            response = await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "get",
                    "id": booking_id,
                },
            )
            record = response.get("record")
            await query.edit_message_text(
                format_crm_card(record),
                reply_markup=booking_status_keyboard(booking_id),
            )
            return

        if data.startswith("cf:"):
            _, field_code, booking_id = data.split(":", 2)

            field_map = {
                "work": "Мне оплачено",
                "studio": "Студия оплачена",
            }

            column = field_map.get(field_code)
            if not column:
                await query.answer("Неизвестное поле", show_alert=True)
                return

            await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "update",
                    "id": booking_id,
                    "column": column,
                    "value": "Да",
                    "extra": {},
                },
            )

            response = await asyncio.to_thread(
                crm_get,
                context.application.bot_data["config"],
                {
                    "action": "get",
                    "id": booking_id,
                },
            )
            record = response.get("record")
            await query.edit_message_text(
                format_crm_card(record),
                reply_markup=booking_status_keyboard(booking_id),
            )
            return

    except Exception as error:
        await query.answer(f"Ошибка CRM: {error}", show_alert=True)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    print(f"Telegram bot error: {context.error!r}")

    if isinstance(update, Update):
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    "Ошибка при обработке кнопки. Проверьте Railway Logs.",
                    show_alert=True,
                )
            except Exception:
                pass
        elif update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Произошла ошибка. Посмотрите Railway Logs."
                )
            except Exception:
                pass

def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
    load_dotenv()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    studios_gid = os.environ.get("STUDIOS_GID", "0")
    guides_gid = os.environ["GUIDES_GID"]
    crm_webhook_url = os.environ["CRM_WEBHOOK_URL"]
    crm_gid = os.environ.get("CRM_GID", "918273645")
    timezone = os.environ.get("TIMEZONE", "Europe/Riga")
    duration_minutes = int(os.environ.get("SHOOT_DURATION_MINUTES", "60"))

    try:
        studios = load_sheet(sheet_id, studios_gid)
    except Exception:
        studios = []

    try:
        guides = load_sheet(sheet_id, guides_gid)
    except Exception:
        guides = []

    app = Application.builder().token(token).build()

    app.bot_data["studios"] = studios
    app.bot_data["guides"] = guides
    app.bot_data["config"] = {
        "sheet_id": sheet_id,
        "studios_gid": studios_gid,
        "guides_gid": guides_gid,
        "crm_webhook_url": crm_webhook_url,
        "crm_gid": crm_gid,
        "timezone": timezone,
        "duration_minutes": duration_minutes,
    }

    try:
        app.bot_data["config"]["_crm_cache_records"] = load_crm_records(
            sheet_id,
            crm_gid,
        )
        app.bot_data["config"]["_crm_cache_at"] = datetime.now().timestamp()
    except Exception:
        app.bot_data["config"]["_crm_cache_records"] = []
        app.bot_data["config"]["_crm_cache_at"] = 0

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("crm", crm_command))
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CallbackQueryHandler(handle_crm_button, pattern=r"^crm:"))
    app.add_handler(
        CallbackQueryHandler(
            handle_crm_navigation,
            pattern=r"^(crmlist:|crmopen:|cs:|cf:|crmmenu$)",
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
