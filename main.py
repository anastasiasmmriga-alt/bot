import asyncio
import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from urllib.parse import quote_plus, urlencode

import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo


MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}


@dataclass
class Booking:
    client_name: str | None
    date_time: datetime | None
    studio: dict | None
    guide: dict | None


def clean_key(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def load_sheet(sheet_id: str, gid: str) -> list[dict]:
    url = (
        f"https://docs.google.com/spreadsheets/d/"
        f"{sheet_id}/export?format=csv&gid={gid}"
    )

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
            if "Название студии" in row
            or "Тип съемки" in row
        ),
        0,
    )

    cleaned_csv = StringIO()
    writer = csv.writer(cleaned_csv)
    writer.writerows(rows[header_index:])
    cleaned_csv.seek(0)

    return list(csv.DictReader(cleaned_csv))


def parse_name(text: str) -> str | None:
    first_part = re.split(
        r"[,;\n]",
        text.strip(),
        maxsplit=1,
    )[0].strip()

    words = first_part.split()

    if not words:
        return None

    ignored = {
        "съемка",
        "сьемка",
        "фотосессия",
        "клиент",
        "клиентка",
    }

    for word in words:
        normalized = clean_key(
            re.sub(
                r"[^А-Яа-яA-Za-z-]",
                "",
                word,
            )
        )

        if (
            normalized
            and normalized not in ignored
            and not normalized.isdigit()
        ):
            return word.strip(" ,.;:")

    return None


def parse_datetime(
    text: str,
    timezone: str,
) -> datetime | None:
    now = datetime.now(
        ZoneInfo(timezone)
    )

    time_match = re.search(
        r"\b([01]?\d|2[0-3])[:. ]([0-5]\d)\b",
        text,
    )

    if not time_match:
        return None

    hour = int(
        time_match.group(1)
    )

    minute = int(
        time_match.group(2)
    )

    numeric_date = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b",
        text,
    )

    if numeric_date:
        day = int(
            numeric_date.group(1)
        )

        month = int(
            numeric_date.group(2)
        )

        year = (
            int(numeric_date.group(3))
            if numeric_date.group(3)
            else now.year
        )

        if year < 100:
            year += 2000

        try:
            result = datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=ZoneInfo(timezone),
            )
        except ValueError:
            return None

        return bump_to_future(
            result,
            now,
        )

    month_names = "|".join(
        MONTHS.keys()
    )

    text_date = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b",
        clean_key(text),
    )

    if text_date:
        day = int(
            text_date.group(1)
        )

        month = MONTHS[
            text_date.group(2)
        ]

        year = (
            int(text_date.group(3))
            if text_date.group(3)
            else now.year
        )

        try:
            result = datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=ZoneInfo(timezone),
            )
        except ValueError:
            return None

        return bump_to_future(
            result,
            now,
        )

    return None


def bump_to_future(
    value: datetime,
    now: datetime,
) -> datetime:
    if value < now:
        try:
            return value.replace(
                year=value.year + 1
            )
        except ValueError:
            return value.replace(
                year=value.year + 1,
                day=28,
            )

    return value


def find_studio(
    text: str,
    studios: list[dict],
) -> dict | None:
    normalized = clean_key(text)
    matches = []

    for studio in studios:
        name = studio.get(
            "Название студии",
            "",
        )

        key = clean_key(name)

        if key and key in normalized:
            matches.append(
                (len(key), studio)
            )

    return (
        sorted(
            matches,
            key=lambda item: item[0],
            reverse=True,
        )[0][1]
        if matches
        else None
    )


def find_guide(
    text: str,
    guides: list[dict],
) -> dict | None:
    normalized = clean_key(text)
    matches = []

    for guide in guides:
        shoot_type = guide.get(
            "Тип съемки",
            "",
        )

        key = clean_key(
            shoot_type
        )

        if key and key in normalized:
            matches.append(
                (len(key), guide)
            )

    return (
        sorted(
            matches,
            key=lambda item: item[0],
            reverse=True,
        )[0][1]
        if matches
        else None
    )


def calendar_link(
    booking: Booking,
    duration_minutes: int,
    timezone: str,
) -> str:
    if booking.date_time is None:
        raise ValueError(
            "Дата и время обязательны"
        )

    start = booking.date_time
    end = start + timedelta(
        minutes=duration_minutes
    )

    studio = booking.studio or {}

    studio_name = (
        studio.get(
            "Название студии",
            "",
        ).strip()
        or "не указана"
    )

    address = studio.get(
        "Адрес",
        "",
    ).strip()

    title = (
        f"Фотосессия {booking.client_name} "
        f"{start:%d.%m.%Y} "
        f"{start:%H:%M}"
    )

    guide_lines = []

    if booking.guide:
        for label, key in [
            ("Одежда", "Одежда"),
            ("Позы", "Позы"),
            ("Подготовка", "Подготовка"),
        ]:
            value = booking.guide.get(
                key,
                "",
            ).strip()

            if value:
                guide_lines.append(
                    f"{label}: {value}"
                )

    details_lines = [
        f"Съемка для {booking.client_name}",
        f"Студия: {studio_name}",
    ]

    if guide_lines:
        details_lines.extend(
            ["", *guide_lines]
        )

    details = "\n".join(
        details_lines
    )

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": (
            f"{start:%Y%m%dT%H%M%S}/"
            f"{end:%Y%m%dT%H%M%S}"
        ),
        "ctz": timezone,
        "location": address,
        "details": details,
    }

    return (
        "https://calendar.google.com/calendar/render?"
        + urlencode(
            params,
            quote_via=quote_plus,
        )
    )


def delivery_calendar_link(
    booking: Booking,
    timezone: str,
) -> str:
    if booking.date_time is None:
        raise ValueError(
            "Дата съёмки не указана"
        )

    delivery_date = (
        booking.date_time
        + timedelta(days=14)
    ).date()

    next_day = (
        delivery_date
        + timedelta(days=1)
    )

    title = (
        f"Отдать фото — "
        f"{booking.client_name}"
    )

    details = (
        f"Срок отдачи фотографий клиенту "
        f"{booking.client_name}.\n"
        f"Съёмка была "
        f"{booking.date_time:%d.%m.%Y}."
    )

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": (
            f"{delivery_date:%Y%m%d}/"
            f"{next_day:%Y%m%d}"
        ),
        "ctz": timezone,
        "details": details,
    }

    return (
        "https://calendar.google.com/calendar/render?"
        + urlencode(
            params,
            quote_via=quote_plus,
        )
    )


def format_client_message(
    booking: Booking,
) -> str:
    if booking.date_time is None:
        raise ValueError(
            "Дата и время обязательны"
        )

    studio = booking.studio or {}
    guide = booking.guide or {}

    studio_name = (
        studio.get(
            "Название студии",
            "",
        ).strip()
        or "уточняется"
    )

    address = studio.get(
        "Адрес",
        "",
    ).strip()

    arrive = studio.get(
        "За сколько минут прийти",
        "",
    ).strip()

    lines = [
        (
            f"{booking.client_name}, "
            f"записала вас на съемку 🤍"
        ),
        "",
        (
            f"Дата: "
            f"{booking.date_time:%d.%m.%Y}"
        ),
        (
            f"Время: "
            f"{booking.date_time:%H:%M}"
        ),
        f"Студия: {studio_name}",
    ]

    if address:
        lines.append(
            f"Адрес: {address}"
        )

    if arrive:
        lines.extend(
            [
                "",
                (
                    f"Пожалуйста, приходите "
                    f"за {arrive} минут "
                    f"до начала."
                ),
            ]
        )

    parking = studio.get(
        "Парковка",
        "",
    ).strip()

    entrance = studio.get(
        "Как зайти",
        "",
    ).strip()

    extra = studio.get(
        "Дополнительно",
        "",
    ).strip()

    if parking:
        lines.append(
            f"Парковка: {parking}"
        )

    if entrance:
        lines.append(
            f"Как зайти: {entrance}"
        )

    if extra:
        lines.append(extra)

    guide_lines = []

    for label, key in [
        ("Одежда", "Одежда"),
        ("Позы", "Позы"),
        ("Подготовка", "Подготовка"),
        ("Дополнительно", "Дополнительно"),
    ]:
        value = guide.get(
            key,
            "",
        ).strip()

        if value:
            guide_lines.append(
                f"{label}: {value}"
            )

    if guide_lines:
        lines.extend(
            [
                "",
                "Гайды для подготовки:",
                *guide_lines,
            ]
        )

    lines.extend(
        [
            "",
            "Жду вас 🤍",
        ]
    )

    return "\n".join(lines)


def parse_booking(
    text: str,
    studios: list[dict],
    guides: list[dict],
    timezone: str,
) -> Booking:
    return Booking(
        client_name=parse_name(text),
        date_time=parse_datetime(
            text,
            timezone,
        ),
        studio=find_studio(
            text,
            studios,
        ),
        guide=find_guide(
            text,
            guides,
        ),
    )


def missing_fields(
    booking: Booking,
) -> list[str]:
    missing = []

    if not booking.client_name:
        missing.append(
            "имя клиента"
        )

    if not booking.date_time:
        missing.append(
            "дату и время"
        )

    return missing


async def refresh_sheet_data(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    config = (
        context.application.bot_data[
            "config"
        ]
    )

    try:
        studios = await asyncio.to_thread(
            load_sheet,
            config["sheet_id"],
            config["studios_gid"],
        )

        guides = await asyncio.to_thread(
            load_sheet,
            config["sheet_id"],
            config["guides_gid"],
        )

        context.application.bot_data[
            "studios"
        ] = studios

        context.application.bot_data[
            "guides"
        ] = guides

    except (
        requests.RequestException,
        csv.Error,
        ValueError,
    ):
        pass


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Напишите съемку одной фразой.\n\n"
        "Например:\n"
        "Анна, 12 августа 14:00, "
        "студия M50, беременность\n\n"
        "Студию можно не указывать."
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if (
        not update.message
        or not update.message.text
    ):
        return

    await refresh_sheet_data(
        context
    )

    config = (
        context.application.bot_data[
            "config"
        ]
    )

    studios = (
        context.application.bot_data.get(
            "studios",
            [],
        )
    )

    guides = (
        context.application.bot_data.get(
            "guides",
            [],
        )
    )

    booking = parse_booking(
        update.message.text,
        studios,
        guides,
        config["timezone"],
    )

    missing = missing_fields(
        booking
    )

    if missing:
        await update.message.reply_text(
            "Мне не хватило: "
            + ", ".join(missing)
            + ".\n\n"
            "Попробуйте так:\n"
            "Анна, 12 августа 14:00, "
            "беременность"
        )
        return

    try:
        shoot_link = calendar_link(
            booking,
            config[
                "duration_minutes"
            ],
            config["timezone"],
        )

        delivery_link = (
            delivery_calendar_link(
                booking,
                config["timezone"],
            )
        )

        client_message = (
            format_client_message(
                booking
            )
        )

        delivery_date = (
            booking.date_time
            + timedelta(days=14)
        )

    except ValueError:
        await update.message.reply_text(
            "Не получилось создать запись. "
            "Проверьте дату и время."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 Добавить съёмку",
                    url=shoot_link,
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "📸 Отдать фото "
                        f"{delivery_date:%d.%m.%Y}"
                    ),
                    url=delivery_link,
                )
            ],
        ]
    )

    await update.message.reply_text(
        "Готово, вот сообщение "
        "для клиента:\n\n"
        + client_message
        + "\n\n"
        + "📸 Срок отдачи фотографий: "
        + f"{delivery_date:%d.%m.%Y}",
        reply_markup=keyboard,
    )


def main() -> None:
    asyncio.set_event_loop(
        asyncio.new_event_loop()
    )

    load_dotenv()

    token = os.environ[
        "TELEGRAM_BOT_TOKEN"
    ]

    sheet_id = os.environ[
        "GOOGLE_SHEET_ID"
    ]

    studios_gid = os.environ.get(
        "STUDIOS_GID",
        "0",
    )

    guides_gid = os.environ[
        "GUIDES_GID"
    ]

    timezone = os.environ.get(
        "TIMEZONE",
        "Europe/Riga",
    )

    duration_minutes = int(
        os.environ.get(
            "SHOOT_DURATION_MINUTES",
            "60",
        )
    )

    try:
        studios = load_sheet(
            sheet_id,
            studios_gid,
        )
    except (
        requests.RequestException,
        csv.Error,
        ValueError,
    ):
        studios = []

    try:
        guides = load_sheet(
            sheet_id,
            guides_gid,
        )
    except (
        requests.RequestException,
        csv.Error,
        ValueError,
    ):
        guides = []

    app = (
        Application.builder()
        .token(token)
        .build()
    )

    app.bot_data[
        "studios"
    ] = studios

    app.bot_data[
        "guides"
    ] = guides

    app.bot_data[
        "config"
    ] = {
        "sheet_id": sheet_id,
        "studios_gid": studios_gid,
        "guides_gid": guides_gid,
        "timezone": timezone,
        "duration_minutes": duration_minutes,
    }

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
