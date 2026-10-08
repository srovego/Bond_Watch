# Bond Watch

Мониторинг заданного портфеля облигаций MOEX: проверенные карточки выпусков, официальная RSS-лента Банка России, контроль чистой цены через MOEX ISS, правила кредитного риска и уведомления Telegram. Python 3.11+, SQLite, без веб-интерфейса и Docker.

## Быстрый локальный запуск

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
bond-watch init-db
bond-watch portfolio
bond-watch digest --dry-run
pytest -q
```

Все 17 исходных выпусков внесены в `config/verified_bonds.yaml` по официальным карточкам или документам MOEX. `init-db` загружает их в SQLite. Новые ISIN остаются `unresolved` до сверки через `bond-watch resolve`. Статический снимок датирован 8 октября 2026 года; запуск `resolve` обновляет данные ISS, когда сеть доступна. Шесть рейтингов корпоративных выпусков сверены по публичным карточкам агентств и сохранены с датой и ссылкой; неподтвержденные поля оставлены пустыми. Новые рейтинговые действия автоматически пока не собираются.

## Настройка Telegram

Создайте бота через BotFather, получите chat ID и числовые user ID администраторов. Заполните `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` (через запятую). Файл не попадает в Git. Проверьте `bond-watch check-telegram`, затем отправьте боту `/start`. Команды: `/status`, `/portfolio`, `/digest`, `/add ISIN`, `/remove ISIN`, `/sources`. Команды обрабатываются только от разрешенных user ID.

`bond-watch poll` опрашивает доступные источники и сразу отправляет подтвержденные критические события, если настроен чат. `bond-watch digest --dry-run` только печатает сводку. `bond-watch digest` отправляет ее вручную. Плановые рассылки `09:00`, `14:00`, `19:00 Europe/Moscow` выполняет systemd timer; опрос идет каждые 15 минут. При отсутствии новостей сводка показывает успешно проверенные и недоступные источники.

## Параметры

`config/portfolio.yaml` — ISIN. `config/settings.yaml` — пороги чистой цены: 3% для корпоративных облигаций и 2% для ОФЗ, таймауты, повторные HTTP-запросы и источники. `AI_ENABLED=false` по умолчанию; при включении нужен `OPENAI_API_KEY`, модель через `OPENAI_MODEL`. ИИ создает только вспомогательное резюме, не инвестиционную рекомендацию.

База: `data/bond_watch.db`. Лог с ротацией: `data/bond_watch.log`. Резервная копия: `bond-watch backup`. История уведомлений и ключи дедупликации сохраняются между перезапусками.

## Развертывание на Ubuntu VPS

Пошаговые команды и проверка systemd: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Скрипт [scripts/install_ubuntu.sh](scripts/install_ubuntu.sh) готовит пользователя, venv и unit-файлы, но в этой работе не запускался на VPS. Исследование источников и подтверждения ISIN: [docs/SOURCES_REVIEW.md](docs/SOURCES_REVIEW.md). Устройство сервиса: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Проверки: [docs/TESTING.md](docs/TESTING.md).


