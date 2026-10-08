# Исследование источников и портфеля

Проверка выполнена 8 октября 2026 года. Для всех 17 исходных ISIN найдено точное совпадение ISIN и эмитента в официальной карточке или документе Московской биржи. Проверенные данные сохранены в [verified_bonds.yaml](../config/verified_bonds.yaml), новые бумаги без подтверждения получают статус `unresolved`. Прямой вызов ISS из этой рабочей среды завершился таймаутом TLS, поэтому работа HTTPS-адаптера в реальной сети пока не подтверждена.

## Портфель

| ISIN | Проверенный эмитент · ИНН | Номер выпуска | Тип | Первоисточник |
| --- | --- | --- | --- | --- |
| RU000A0JV4M0 | Минфин России · 7710168360 | 29007RMFS | ОФЗ-ПК | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A0JV4M0) |
| RU000A108EF8 | Минфин России · 7710168360 | 26247RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A108EF8) |
| RU000A1038V6 | Минфин России · 7710168360 | 26238RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=26238RMFS) |
| RU000A10D533 | Минфин России · 7710168360 | 26254RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A10D533) |
| RU000A10DYG9 | АО «Атомэнергопром» · 7706664260 | 4B02-10-55319-E-001P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A10DYG9) |
| RU000A1095W4 | ПАО «Ростелеком» · 7707049388 | 4B02-09-00124-A-001P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A1095W4) |
| RU000A100A82 | Минфин России · 7710168360 | 26228RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A100A82) |
| RU000A109E71 | ПАО «ТрансКонтейнер» · 7708591995 | 4B02-01-55194-E-002P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A109E71) |
| RU000A1014N4 | Минфин России · 7710168360 | 26232RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=26232RMFS) |
| RU000A0ZYUB7 | Минфин России · 7710168360 | 26225RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A0ZYUB7) |
| RU000A1090N4 | АО «Аэрофьюэлз» · 7714216826 | 4B02-03-29449-H-002P | Биржевая | [MOEX](https://www.moex.com/n72195) |
| RU000A0JV4P3 | Минфин России · 7710168360 | 29008RMFS | ОФЗ-ПК | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A0JV4P3) |
| RU000A10AZ60 | ОАО «РЖД» · 7708503727 | 4B02-38-65045-D-001P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A10AZ60) |
| RU000A105FZ9 | Минфин России · 7710168360 | 26241RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A105FZ9) |
| RU000A1098F3 | ПАО АФК «Система» · 7703104630 | 4B02-31-01669-A-001P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A1098F3) |
| RU000A107X96 | ВЭБ.РФ · 7750004150 | 4B02-46-00004-T-002P | Биржевая | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A107X96) |
| RU000A1038Z7 | Минфин России · 7710168360 | 26237RMFS | ОФЗ-ПД | [MOEX](https://www.moex.com/ru/listing/securities-cards.aspx?code=RU000A1038Z7) |

У «Аэрофьюэлз» номер выпуска и ISIN подтверждены [сообщением MOEX](https://www.moex.com/n72195), ИНН 7714216826 — [документом эмитента, опубликованным MOEX](https://fs.moex.com/emidocs/2024/07/15/2829290_%D0%90%D1%8D%D1%80%D0%BE%D1%84%D1%8C%D1%8E%D1%8D%D0%BB%D0%B7%20%282024.07.12%29%20-%20%D1%80%D0%B5%D1%88%D0%B5%D0%BD%D0%B8%D0%B5%20%D0%BE%20%D0%B2%D1%8B%D0%BF%D1%83%D1%81%D0%BA%D0%B5%20002P-03.pdf). Для ОФЗ «эмитент» в карточках указан как Министерство финансов РФ. Шесть рейтингов корпоративных выпусков сверены по публичным карточкам рейтинговых агентств. Для «Аэрофьюэлз» рейтинг выпуска не подтвержден. Для ОФЗ поле рейтинга выпуска оставлено пустым.

## Проверенные рейтинги выпусков

Поля рейтинга — статический снимок публичных карточек на 8 октября 2026 года. Дата в таблице — дата рейтингового действия, а не дата автоматического обновления. Публичные карточки позволяют проверить исходное значение, но мониторинг новых рейтинговых действий пока не подключен.

| ISIN | Рейтинг | Агентство | Дата действия | Первоисточник |
| --- | --- | --- | --- | --- |
| RU000A10DYG9 | ruAAA | Эксперт РА | 2026-09-02 | [Карточка выпуска](https://www.raexpert.ru/database/securities/bonds/1000069007/) |
| RU000A1095W4 | AAA(RU) | АКРА | 2025-12-03 | [Карточка выпуска](https://www.acra-ratings.ru/ratings/emissions/1206/?lang=ru) |
| RU000A109E71 | ruAA-, под наблюдением | Эксперт РА | 2026-09-16 | [Карточка выпуска](https://www.raexpert.ru/database/securities/bonds/1000058971/) |
| RU000A10AZ60 | AAA(RU) | АКРА | 2026-08-13 | [Карточка эмитента с выпуском](https://acra-ratings.ru/ratings/issuers/101/?lang=ru) |
| RU000A1098F3 | ruA+ | Эксперт РА | 2026-06-30 | [Карточка выпуска](https://www.raexpert.ru/database/securities/bonds/1000058728/) |
| RU000A107X96 | AAA(RU) | АКРА | 2026-03-25 | [Карточка выпуска](https://www.acra-ratings.ru/ratings/emissions/1150/?lang=ru) |

## Примеры публичных запросов

~~~text
https://iss.moex.com/iss/securities.json?q=RU000A10DYG9&iss.meta=off
https://iss.moex.com/iss/securities/RU000A10DYG9.json?iss.meta=off
https://iss.moex.com/iss/engines/stock/markets/bonds/securities/RU000A10DYG9.json?iss.meta=off
https://www.cbr.ru/rss/eventrss
https://www.cbr.ru/rss/RssPress
~~~

Поиск ISS требует точного совпадения ISIN в ответе; цену сервис читает только для досок облигаций и при наличии сделок. URL рейтинговых карточек для каждого выпуска приведены выше. Для остальных приоритетных источников публичный интерфейс автоматического опроса не подтвержден или требует доступа.

## Доступность источников

| Источник | Проверенный интерфейс | Состояние адаптера | Ограничение |
| --- | --- | --- | --- |
| MOEX ISS | [Описание API](https://www.moex.com/a2920), `/iss/securities/{ISIN}.json`, `/iss/engines/stock/markets/bonds/securities/{SECID}.json` | Реализованы справочник и цена; mock-тесты прошли | Прямой HTTPS из текущей среды истек по таймауту. Биржа предоставляет задержанные данные бесплатно, real-time по подписке. Исторические данные могут требовать подписки. |
| Банк России | [Официальные RSS](https://www.cbr.ru/development/RSS/): `eventrss`, `RssPress` | Реализован; прямой live-опрос двух RSS-лент успешен | Публикации о конкретных корпоративных эмитентах могут отсутствовать. |
| e-disclosure | [Шлюз API](https://e-disclosure.ru/poluchenie-informacii/shlyuz-api) | Отключен, диагностируется как недоступный | Требует платной авторизации; [условия использования](https://e-disclosure.ru/usloviya-ispol%27zovaniya-informacii) ограничивают использование материалов. |
| Эксперт РА | [Выгрузка рейтингов](https://quiz.raexpert.ru/all-services/rating-export/) | Отключен | Официальная автоматическая выгрузка предоставляется по подписке. |
| АКРА | [Публичные рейтинговые релизы](https://www.acra-ratings.ru/press-releases/) | Отключен | Подтвержденной стабильной публичной API/RSS для автоматического опроса не найдено. |
| Федресурс | [Спецификация сервиса](https://download.fedresurs.ru/files/sSpecificationMessagesService_3.9.pdf) | Отключен | API использует авторизацию; доступа в проекте нет. |
| Другие RSS/новости | — | Не подключены | Надежный официальный интерфейс и права на повторное использование не подтверждены. |

Адаптеры не обходят CAPTCHA, авторизацию или платные ограничения. Ограничение частоты задано паузой между запросами и 15-минутным периодом; окончательную допустимую частоту следует сверить с условиями соответствующего источника перед запуском на VPS. Ошибки фиксируются в `sources` и `collector_runs`. Новости e-disclosure, рейтинговые действия Эксперт РА/АКРА и Федресурс пока не покрываются — это существенное ограничение мониторинга кредитного риска.



