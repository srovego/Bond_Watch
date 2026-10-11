# Развертывание на Ubuntu VPS

В этой задаче подключение к серверу и установка на нем ПО не выполнялись. Ниже инструкция для следующего этапа, после проверки доступа к источникам и Telegram.

## Подготовка

На VPS должны быть Python 3.11+, пакет venv, systemd и исходящий HTTPS к iss.moex.com, www.cbr.ru, api.telegram.org. Скопируйте проверенный репозиторий в /opt/bond-watch. Установщик использует editable-установку, чтобы CLI находил конфигурацию проекта. Не переносите локальную базу с тестовыми данными.

~~~bash
sudo apt update
sudo apt install python3 python3-venv
sudo mkdir -p /opt/bond-watch
# Скопируйте файлы проекта в /opt/bond-watch выбранным вами способом.
cd /opt/bond-watch
sudo bash scripts/install_ubuntu.sh
sudoedit /opt/bond-watch/.env
~~~

В .env задайте TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и TELEGRAM_ALLOWED_USER_IDS. Файл должен принадлежать bondwatch и иметь права 0600. AI_ENABLED оставьте false, если не нужен внешний анализ. Порог ОФЗ и корпоративных выпусков можно изменить в config/settings.yaml.

## Проверка перед включением

~~~bash
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch init-db
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch portfolio
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch resolve
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch status
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch check-telegram
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch digest --dry-run
sudo systemd-analyze verify /etc/systemd/system/bond-watch-*.service
sudo systemd-analyze calendar '*-*-* 09:00:00 Europe/Moscow'
~~~

Если resolve сообщает ошибки, сохраненные проверенные карточки остаются в базе, но цены и RSS следует отдельно проверить командой poll. Проверяйте статусы, а не только код завершения команды: частичная ошибка адаптера отмечается в таблицах sources и collector_runs. Первый запуск poll может не создать ценовой сигнал, потому что еще нет предыдущего наблюдения номинала.

## Ручная проверка Gmail SMTP

При уже настроенном `/opt/bond-watch/.env` убедитесь, что указаны `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO` и `SMTP_USE_STARTTLS=true`. Пароль не выводится командами и не сохраняется в SQLite. Исходящее соединение к Gmail должно допускать порт 587; соединение использует STARTTLS и системные доверенные сертификаты. В рамках этой разработки подключение к VPS и реальная SMTP-отправка не выполнялись.

~~~bash
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch check-email
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch digest --email --dry-run
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch digest --email
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch status
~~~

`check-email` отправляет одно тестовое письмо при каждом явном запуске. `digest --email` отправляет текущую сводку вручную и записывает попытку в существующую таблицу `notifications`. Повтор с тем же содержимым и адресатами после статуса `sent` или `uncertain` не отправляет письмо; при `uncertain` сначала проверьте почтовый ящик и журнал. Ошибку подключения, STARTTLS или входа можно исправить и повторить. Расписание Telegram не изменено. Автоматический email-таймер описан ниже.

## Включение

~~~bash
sudo systemctl enable --now bond-watch-resolve.timer
sudo systemctl enable --now bond-watch-poll.timer
sudo systemctl enable --now bond-watch-digest.timer
sudo systemctl enable --now bond-watch-bot.service
sudo systemctl list-timers 'bond-watch*'
sudo journalctl -u bond-watch-poll.service -n 50 --no-pager
~~~

Плановая сводка отправляется в 09:00, 14:00 и 19:00 Europe/Moscow. Пропущенный календарный запуск выполняется после восстановления сервера; уникальный ключ временного слота предотвращает повторную отправку. Срочные события обнаруживаются не чаще цикла опроса: каждые 15 минут после последнего запуска. Команды бота работают через отдельную службу.

Для тестовой отправки после настройки токена используйте одну ручную команду `sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch digest`; она отправит текущую сводку. Резервная копия: `sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch backup`. Внешнее копирование файлов из data/backups настройте средствами VPS. Проверяйте состояние `notifications` при сетевых таймаутах Telegram: статус uncertain автоматически не пересылается во избежание дубля.



## Автоматические email-уведомления: обновление действующего VPS

Команды выполняются на VPS оператором, владеющим checkout `/opt/bond-watch`; в этой задаче соединение с VPS не устанавливалось. Перед обновлением сделайте резервную копию SQLite. Существующие `.env`, база и `bond-watch-poll.timer` сохраняются. После обновления кода `init-db` добавит поля очереди миграцией без удаления прежних уведомлений.

~~~bash
cd /opt/bond-watch
git pull --ff-only origin main
sudo /opt/bond-watch/.venv/bin/pip install -e /opt/bond-watch
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch backup
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch init-db
sudo install -m 0644 deploy/bond-watch-email-digest.service deploy/bond-watch-email-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bond-watch-email-digest.timer
sudo systemctl list-timers bond-watch-poll.timer bond-watch-email-digest.timer
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch email-queue --limit 50
~~~

`poll` теперь формирует оперативные письма после завершения сбора и запускается существующим таймером каждые 15 минут. Новая служба запускает `bond-watch email-daily` в 19:00 Europe/Moscow. Не запускайте `email-daily` вручную для проверки доставки, если хотите сохранить единственный запланированный выпуск за день; её ключ содержит московскую дату. Для теста соединения используйте `bond-watch check-email`, которая отправляет одно отдельное письмо.

Диагностика и безопасное повторение:

~~~bash
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch email-queue --limit 100
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch email-retry --id 123
sudo -u bondwatch /opt/bond-watch/.venv/bin/bond-watch email-retry --all-safe
sudo journalctl -u bond-watch-email-digest.service -n 50 --no-pager
sudo journalctl -u bond-watch-poll.service -n 50 --no-pager
~~~

`email-retry` принимает только `failed` и `retryable`. Записи `uncertain` и оставшиеся после прерывания `attempting` требуют ручной проверки ящика отправителя и адресата. Автоматически их повторять нельзя: Gmail мог принять письмо до потери соединения. Однозначные временные ошибки соединения, STARTTLS и аутентификации с кодом 4xx повторяются не более трёх раз с интервалами 15 и 30 минут. Постоянные ошибки и исчерпанные попытки остаются `failed`. Ни пароль, ни тело письма команда очереди не печатает.
