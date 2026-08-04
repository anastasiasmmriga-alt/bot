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


def crm_request(webhook_url: str, payload: dict) -> dict:
    response = requests.post(
        webhook_url,
        json=payload,
        timeout=30,
        allow_redirects=True,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 PhotoBookingBot/1.0",
        },
    )

    if response.status_code == 404 and "script.googleusercontent.com" in response.url:
        raise RuntimeError(
            "Google Apps Script вернул 404. Создайте новое развертывание "
            "как веб-приложение, выберите доступ «Все» и используйте ссылку /exec."
        )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as error:
        preview = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            "Google Apps Script вернул не JSON. "
            f"Ответ: {preview}"
        ) from error

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "CRM вернула ошибку"))

    return data


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
    await asyncio.to_thread(crm_request, webhook_url, payload)


async def refresh_sheet_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
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

        await create_crm_record(
            config["crm_webhook_url"],
            booking,
            shoot_type,
        )
    except Exception as error:
        await update.message.reply_text(
            "Не получилось сохранить запись в CRM.\n"
            f"Ошибка: {error}"
        )
        return

    delivery_date = booking.date_time + timedelta(days=14)

    await update.message.reply_text(
        "✅ Запись добавлена в CRM\n\n"
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
            crm_request,
            context.application.bot_data["config"]["crm_webhook_url"],
            payload,
        )
    except Exception as error:
        await query.answer(f"Ошибка CRM: {error}", show_alert=True)
        return

    await query.answer(command["message"], show_alert=True)


def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
    load_dotenv()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    studios_gid = os.environ.get("STUDIOS_GID", "0")
    guides_gid = os.environ["GUIDES_GID"]
    crm_webhook_url = os.environ["CRM_WEBHOOK_URL"]
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
        "timezone": timezone,
        "duration_minutes": duration_minutes,
    }

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_crm_button, pattern=r"^crm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
    
