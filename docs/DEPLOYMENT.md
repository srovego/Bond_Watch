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


