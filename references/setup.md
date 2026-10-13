# Контракт настройки

## Пути на целевой машине

По умолчанию root — `~/.local/share/yatorrent-sbs`, app — `~/Applications/YaTorrent.app`. Все пути вычисляются на месте. В ZIP нет абсолютных путей компьютера автора и нет его Codex auth. Имя home также должно позволять абсолютные безопасные entry points; при неподходящем home/root выбери доверенный каталог с безопасным абсолютным путём. Установщик отклоняет неподходящий root до любых изменений, вместо автоматического создания ссылок на старые entry points.

Данные Electron остаются в штатном `~/Library/Application Support/YaTorrent`. `--user-data` не перемещает этот каталог: нестандартное значение при `--apply` отклоняется до изменений. Для read-only диагностики verify может получить путь существующей установки; это не настройка каталога запуска приложения.

Пути executable Node и двух scripts допускают только ASCII буквы, цифры, `_`, `.`, `-` и `/`; пробелы и shell-метасимволы запрещены. Entry files принадлежат текущему пользователю, являются обычными файлами без symlink и не доступны для записи группе/другим; Node исполняемый. Предки проверяются на владение и режим. Каталог public/shared temp не используется как постоянная доверенная установка. Команды выводятся готовыми строками **без кавычек**, помимо массивов argv для программного запуска.

Приватный Node — `root/node/bin/node`, Codex — npm prefix `root/codex`, Playwright — `root/browser-runtime`, Chromium — `root/browsers`, скрипты — `root/lib`, исполняемые оболочки — `root/bin`. Codex auth_home — штатный пользовательский `~/.codex`; отдельное правило — `auth_home/rules/yatorrent-sbs-chromium.rules`.

`runtime-config.json` рядом с browser launcher:
```json
{"runtime":"<root>/browser-runtime","browsers_path":"<root>/browsers"}
```

`install-config.json` рядом с launcher’ами содержит только публичные пути и настройки. Credentials в него не копируются. `node_bin`, `real_codex`, `auth_home`, `rule_dir`, `real_worker`, `scout_threads=10`, `worker_timeout_seconds=1800`, `worker_concurrency=4 (настраивается `--worker-concurrency`)`.

## Импорт и запуск приложения

`install.py --apply` вызывает `startup.py`. Для нового или не подтверждённого профиля он записывает `autoStart=false` и `consume_codex=true`, открывает конкретное приватное приложение с локальным `.aim2`, ждёт атомарного обновления `profile.json` с зашифрованными credentials и совпадения публичных полей, затем штатно закрывает приложение. Квитанция `root/profile-import.json` содержит только SHA-256 исходного файла и зашифрованного файла. Повторный запуск повторно использует подтверждение только при совпадении обоих хэшей и публичных полей. Содержимое credentials не выводится.

Такая фаза импорта необходима: проверенный `main.cjs` выполняет autoStart перед обработкой queuedProfilePath, а `importProfile` не запускает worker и отвергает замену профиля при работающем worker. Включение autoStart перед первым импортом могло бы запустить старый профиль либо оставить новую установку остановленной.

После импорта требуются file-auth и успешный `login status`. При отсутствии авторизации состояние — `awaiting_codex_login`; worker остаётся остановленным. После штатного входа владельца повтори `python3 scripts/startup.py --root ROOT`.

Перед **обычным запуском** сохраняются обе опции `autoStart=true` и `settings.consume_codex=true`, а также `max_browsers=worker_concurrency`, `consume_eliza=false`. Идентификатор worker и прочие настройки сохраняются с backup. Затем выполняется `verify.py --pre-launch` отдельным процессом; проверяются код выхода, `infrastructure_ready=true`, `pre_launch_only=true`, `delivery_ready=false`. До открытия приложения это лишь проверка инфраструктуры. При провале autoStart возвращается в false и приложение не открывается. После неё выполняется `open -a APP` без `.aim2`, затем обязательная проверка действующего worker, описанная ниже. `startup.json` различает запрос открытия и подтверждённую готовность worker; broker end-to-end отдельно не подтверждён. `autoStart` означает запуск worker при открытии YaTorrent; настройка входа в macOS не меняется.

`--plan` не меняет файлы и не запускает приложение. Сборка ZIP также ничего не устанавливает. `--apply` начинает приём broker jobs после инфраструктурной проверки, когда приложение стартует; подтвердить готовность установки можно только после дополнительной проверки этого worker, как предусмотрено поручением на настройку worker. Начатые чужие задания не прерывать; при работающей установке сначала согласовать штатное завершение её работы в рамках поручения.

## Настройки Codex в запуске

```toml
sandbox_mode = "workspace-write"
approval_policy = "never" # исходный запрос worker; MDM может изменить
[sandbox_workspace_write]
network_access = true
[agents]
enabled = true
max_concurrent_threads_per_session = 10 # + основной агент = 11
```

Launcher передаёт параметры `-c` сразу после `exec`, потому что YaTorrent использует `--ignore-user-config`. Для закреплённого Codex 0.162.1 параметры перед подкомандой не дали нужный лимит; правильное положение проверено по фактическим developer-инструкциям: 11 слотов. Он также передаёт `project_doc_max_bytes=0`, `cli_auth_credentials_store="file"` и свой `sqlite_home`; `CODEX_HOME`/SQLite состояния изолированы в каталоге задания. Правила связываются с существующим пользовательским каталогом, включая отдельные allow для браузера и prompt reader. `--ephemeral` удаляется. Параметры модели и выходная схема YaTorrent сохраняются.

При file-auth launcher временно связывает `auth.json`, не читая/не копируя его содержимое. Ссылка удаляется после задания. При отсутствии file-auth нужен штатный вход; keyring fallback через другую директорию не считается проверенным. Для схемы с подпиской используй ChatGPT login, не подменяй подписку API key без запроса пользователя.

Запрос worker остаётся `never`. Фактическая политика извлекается из rollout и указывается в отчёте; расхождение не исправляй подменой config. Более строгое `forbidden`/`prompt` не отменяется новой записью allow. Не задавай неподдерживаемое `untrusted` и не считай передачу `on-request` доказательством изменения фактической политики.

## Почему нужен worker adapter

В официальном YaTorrent 0.3.5 проверены `main.cjs`, `lib/profile.cjs`, `lib/desktop-worker.cjs`, `lib/codex.cjs`, `lib/auto-updater.cjs` и константы frozen `worker.executors.prompt_executor`:

- Desktop при каждом старте пересоздаёт worker-config.
- Его UI не предоставляет произвольный binary и timeout Codex.
- Runtime запускает `codex exec` с `workspace-write`, `--ignore-user-config`, `--ephemeral`, never и network_access=true.
- timeout_seconds по умолчанию180, поддерживаемый диапазон1..3600.

Чтобы не править конфиг с гонкой, приватная установка сохраняет оригинальный `worker-runtime/aim2-worker` и помещает на его место фиксированную оболочку. Она применяет конфигурацию и заменяет свой процесс оригинальным worker через `execve`, сохраняя PID для Desktop readiness. Аргументы, worker ID, очередь, профиль и env сохраняются; дополнительный worker не запускается. Оболочка `Contents/Resources/codex/codex` обеспечивает обнаружение приватного Codex приложением, включая проверку авторизации.

Конфиг Codex получает binary `root/bin/codex`, timeout1800, max_output_bytes8MiB. Concurrency по умолчанию4; `--worker-concurrency` задаёт 1..16. Worker adapter использует сохранённое значение вместо старого жёсткого лимита1; desktop.max_browsers и метрики должны совпадать с ним. Оригинальный worker хранится рядом; его SHA-256 фиксируется. Оболочка и приложение проверяются после обновления. Пакет не патчит `app.asar`, не меняет глобальный Codex config и не отключает защиты macOS.

## Проверка готовности

Владелец подтвердил наблюдение четырёх разных заданий, выполнявшихся одновременно, в предыдущем запуске. Это подтверждение эксплуатационного сетапа, а не новый нагрузочный тест r5. Проверка установки сверяет лимит4, но не отправляет задания для доказательства параллелизма. См. [доставку через broker](broker.md).

Разделяй стадии: скачано/проверены хэши; установлены binaries; профиль реально импортирован; CLI авторизован; Chromium реально запущен; точный allow совпадает; фактический запуск Codex использует нужные настройки; worker принимает назначение; результат получен.

`verify.py` не стартует приём задач. Без skip обязателен [runtime smoke](runtime.md) через тот же Codex launcher и worker flags. Только `готов`/нулевой exit полной проверки без `--pre-launch` подтверждает инфраструктуру и фактический worker. Нулевой exit `--pre-launch` даёт лишь «подготовлен» и `delivery_ready=false`. Это не доказательство broker end-to-end. Наличие процесса и service.json недостаточно: проверка связывает launch receipt, действующий executable, config SHA-256 и продвигающиеся метрики того же PID. Она не читает bearer token приложения и не делает broker/API mutations. При новом запуске нужен `auth_home/auth.json`: одного login status через Keychain недостаточно для изолированного launcher.

Настроенный лимит ещё не доказывает одновременную работу всех10 разведчиков. Для нагрузочного приёмочного теста бригадир должен сначала создать десять агентов, дождаться их readiness и только затем отпустить их на URL. Иначе первый может завершиться до создания десятого даже при11 доступных слотах.

Реальный предыдущий SBS-тест занимал12,8 минуты с8 разведчиками и3 одновременно. Это ориентир тайм-аута, не SLA. Под нагрузкой следи за памятью, CPU, лимитами подписки и максимальным числом jobs; десять browser-контекстов в каждом из нескольких jobs могут перегрузить одну машину.

## Доказательство фактического worker

Непосредственно перед `execve` worker launcher записывает `worker-state/sbs-worker-launch.json` (0600): schema, PID, время, пути и SHA-256 original worker и worker-config. PID сохраняется при exec. Профиль, config values, env и tokens в receipt не копируются. Receipt сам по себе не доказывает запуск.

`worker_check.py` требует совпадения PID в receipt, service.json и metrics.json, живого процесса с executable установленного original worker и подходящим временем старта (защита от старого receipt/повторного PID). Проверяются хэш реально запущенного бинарника и конфигурации, `codex.binary` установленного launcher, `codex_home`, `timeout_seconds=1800`, max_output_bytes=8MiB, enabled, concurrency=worker_concurrency, work_dir. Пути к файлам сравниваются по их идентичности, учитывая регистр на case-insensitive macOS.

Метрики читаются из ожидаемого `worker-state/worker/metrics.json`: тот же PID, heartbeat не старше 20 секунд, uptime соответствует процессу, consume_codex=true, consume_eliza=false, max_browsers=worker_concurrency. Требуется два наблюдения с продвижением heartbeat и uptime при неизменных PID/receipt/config. При startup receipt обязан быть новее запроса обычного открытия приложения. Ожидание ограничено 45 секундами.

До запуска полная инфраструктурная проверка выполняется с `--pre-launch`; после запуска `startup.py` добавляет обязательное доказательство worker и только тогда сохраняет `verification.json` со статусом «готов». Обычная `verify.py` без skip требует оба доказательства. Ошибочный timeout/CLI, недоступный ps, отсутствие receipt/metrics, остановленный worker или неправильная конфигурация не дают статус «готов» даже при успешном отдельном Codex smoke. Нужен обычный доступ установки к сведениям о её процессах; новая общая allow-запись для shell/ps/Node разведчикам не добавляется.

При провале после открытия возвращается `StartupFailure`, ненулевой код, «не готов» и правдивое app_launch_requested=true. Будущий autoStart отключается на диске, если это удалось; при работающем приложении его внутреннее состояние может отличаться. Скрипт не прекращает чужие/уже начатые jobs. Если worker проверен, но итоговый verification.json/startup.json не удалось сохранить, установка также возвращает ненулевой код и «не готов», сохраняя правдивые worker_ready=true и app_launch_requested=true в выводе. Этот уже проверенный worker автоматически не убивается. Сбой публикации результата в broker не диагностируется этим тестом. 1800 — лимит одного Codex-элемента, а не всего батча или доставки результатов.

## Версии и источники

Установщик разрешает официальный `releases/latest` при каждом `--plan`/`--apply`, выбирает macOS arm64 DMG и проверяет SHA-256 из метаданных релиза. Последний релиз, проверенный при сборке, — YaTorrent0.3.5. Перед копированием приложения сверяет Info.plist, оригинальный worker и пять модулей ASAR с проверенным контрактом. Более новый релиз с неизменным контрактом допустим; при изменении контракта установка останавливается для проверки адаптера, без fallback на старую версию. Недоступность API и отсутствие SHA-256 также останавливают установку. Node24.21.0 LTS, Codex0.162.1, Playwright1.64.0, Chromium156.0.8078.4 остаются закреплены. Chromium устанавливается Playwright соответствующей версии, без ручного имени executable. Runtime сверяет и метаданные Playwright, и версию реально запущенного браузера с явным pin.

- [Официальный YaTorrent0.3.5](https://github.com/RudenkoAD/YaTorrent-releases/releases/tag/yatorrent-v0.3.5).
- [Node.js24.21.0 и SHA256SUMS](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt).
- [Playwright: соответствие версии библиотеке и установка полного Chromium](https://playwright.dev/docs/browsers).
- [Codex: правила команд](https://learn.chatgpt.com/docs/agent-configuration/rules).
- [Codex: лимит субагентов](https://learn.chatgpt.com/docs/agent-configuration/subagents#global-settings).
- [Codex exec: ephemeral и ignore-user-config](https://learn.chatgpt.com/docs/developer-commands#codex-exec).
- [CODEX_HOME и SQLite](https://learn.chatgpt.com/docs/config-file/environment-variables#core-locations).

Установка в чистую macOS и broker end-to-end не выполнялись при сборке ZIP. Результаты реальных проверок ресурсов и fixture-интеграций лежат рядом с архивом в отчёте сборки, а не подменяют проверку на целевой машине.

В 0.3.5 импорт по-прежнему выполняется после проверки автозапуска, но `resumeAfterUpdate=true` также может запустить worker при autoStart=false. Поэтому подготовка/отказ очищает resumeAfterUpdate и выключает autoUpdate: автоматическая замена bundle стирает приватный адаптер. Обновление через установщик заново разрешает latest и проверяет контракт. При проверке релиза просмотрены изменения main/desktop-worker; profile/codex modules неизменны, нормализованные code objects prompt_executor совпадают с 0.3.4 (flags, timeout, SIGKILL, модель/schema/protocol).
