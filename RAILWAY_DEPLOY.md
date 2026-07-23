# Как поставить бота на Railway

## Что уже готово

Папка бота уже подготовлена для Railway:

- `main.py` - бот
- `requirements.txt` - библиотеки
- `railway.json` - команда запуска
- `Procfile` - запасная команда запуска
- `.env.railway.example` - список переменных для Railway

## 1. Создать проект

1. Откройте https://railway.app
2. Войдите в аккаунт.
3. Нажмите `New Project`.
4. Выберите загрузку проекта из GitHub или через Railway CLI.

## 2. Добавить переменные

В Railway откройте проект, затем `Variables`.

Добавьте:

```text
TELEGRAM_BOT_TOKEN=токен от BotFather
GOOGLE_SHEET_ID=1mJ0eLh9tmZXQNfxzr3dihChoJKN7htsqFllekjzFV3g
STUDIOS_GID=760751850
GUIDES_GID=267343101
TIMEZONE=Europe/Riga
SHOOT_DURATION_MINUTES=60
```

Важно: Telegram-токен лучше хранить только в Railway Variables, не в GitHub.

## 3. Команда запуска

Railway возьмет команду из `railway.json`:

```text
python main.py
```

Если Railway спросит Start Command вручную, вставьте:

```text
python main.py
```

## 4. Проверка

После деплоя откройте Telegram и напишите боту:

```text
Анна, 12 августа 14:00, студия M50, беременность
```

Если бот отвечает - все работает.

## 5. Чтобы бот не засыпал

В настройках Railway выберите тариф/режим, где сервис не выключается. На бесплатных или trial-режимах постоянный запуск может быть ограничен.
