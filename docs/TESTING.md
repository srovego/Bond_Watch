# Проверка

Локально: создайте venv, установите проект через pip install -e '.[test]' и запустите pytest -q. Тринадцать тестов проверяют точное сопоставление MOEX ISIN и поиск SECID, отказ от неверного ISIN, загрузку 17 проверенных выпусков и шести рейтингов с первоисточниками, сделки/номинал/амортизацию, разбор RSS, классификацию, дедупликацию, разбиение длинной сводки, осторожную оценку гипотетического дефолта и защиту от повторной отправки Telegram. HTTP-ответы и Telegram подменены; реальные токены не нужны.

Также выполнены python -m compileall -q src, bond-watch init-db, bond-watch portfolio и bond-watch digest --dry-run. Прямой HTTPS-запрос к iss.moex.com из текущей среды завершился таймаутом TLS, поэтому end-to-end проверка ISS не заявляется. Адаптер Банка России реально получил 6 публикаций из двух официальных RSS-лент, а локальный collector_run завершился со статусом ok. Telegram и OpenAI без пользовательских ключей не вызывались.

Перед запуском на VPS:

1. bond-watch resolve и bond-watch status: проверить доступность ISS и отсутствие unresolved.
2. bond-watch poll: проверить collector_runs, ошибки источников и цены.
3. bond-watch check-telegram, затем bond-watch digest --dry-run и тестовая ручная сводка.
4. systemd-analyze calendar '*-*-* 09:00:00 Europe/Moscow' и systemd-analyze verify для unit-файлов на целевой версии Ubuntu.
5. Проверить journalctl, таблицу notifications и резервную копию.

Ни один из этих шагов на продуктивном VPS здесь не выполнялся.


