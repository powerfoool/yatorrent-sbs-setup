# Нативная настройка r7

## Пути и ownership

root `~/.local/share/yatorrent-sbs`, app `~/Applications/YaTorrent.app`, штатные данные Electron `~/Library/Application Support/YaTorrent`. `--user-data` не перемещает приложение: при apply нестандартный каталог отклоняется. Доверенные команды только по ASCII путям без пробелов/метасимволов; файлы и предки не writable группой/другими, принадлежат root/текущему пользователю. root не symlink. Неподходящий root отклоняется до изменений.

Приватные Node в root/node, Codex в root/codex, Playwright в root/browser-runtime, Chromium в root/browsers, scripts в root/lib, wrappers в root/bin. auth_home — вычисленный на месте ~/.codex. runtime-config и install-config содержат публичные пути/настройки, credentials не копируют.

## executor-runtime.json

YaTorrent 0.3.9 поддерживает отдельную постоянную настройку. Приложение и нативный worker не патчатся, worker adapter отсутствует, app.asar и подпись не меняются. Стоковый app этой версии можно переиспользовать после сверки ресурсов и подписи. Известную установку r5 при поручении обновить заменяют полной проверенной официальной копией с backup; неизвестное старое/изменённое приложение сохраняют и предлагают отдельный путь.

В штатных данных приложения файл0600:
```json
{
  "schema_version":1,
  "codex":{
    "mode":"custom_launcher",
    "binary":"<root>/bin/codex",
    "timeout_seconds":1800,
    "required_contract":"sbs-codex-v1"
  }
}
```
Здесь нет concurrency или entry_points. concurrency задаёт desktop.settings.max_browsers, по умолчанию4 (1..16); он превращается в worker.concurrency. 11 агентов задаёт Codex launcher:10 дочерних + основной. Нативный worker владеет таймером одного Codex-элемента1800; launcher не добавляет второй общий таймер. Descriptor probe10с и smoke до300с — отдельные проверки установки.

JSON отсутствует → стандартный Codex/180с; неверный JSON → ошибка готовности. Установщик записывает файл при остановленном приложении до первого импорта. После изменения настройки работающему worker нужен штатный apply/restart с drain: старые jobs не прерывать. При rollback восстанавливай JSON на диске вместе с launcher; откат только внутреннего состояния не достаточен для следующего старта.

## Descriptor

`<root>/bin/codex --sbs-print-config` возвращает ровно9 полей:
```json
{
  "schema_version":1,
  "contract":"sbs-codex-v1",
  "launcher_version":"7.0.0",
  "real_codex":{"binary":"<root>/bin/codex-real","version":"codex-cli 0.162.1"},
  "config_fingerprint":"<64 hex characters>",
  "total_agent_slots":11,
  "sandbox":"workspace-write",
  "network_access":true,
  "entry_points":{
    "browser":"<root>/bin/browser",
    "foreman_reader":"<root>/bin/foreman-reader",
    "scout_reader":"<root>/bin/scout-reader"
  }
}
```
Это ответ launcher, не настройка пользователя. Все entry_points — отдельные executable wrappers с фиксированными scripts; настоящий CLI wrapper запускает JS через явный приватный Node и не зависит от PATH GUI. Fingerprint учитывает публичную эффективную конфигурацию и файлы зависимостей; auth.json/значения секретов не читаются. YaTorrent проверяет точную схему, ownership/mode/executable, версии, sandbox/network/slots и стабильность fingerprint, но не доказывает успешный browser action. Поэтому smoke обязателен. Агентам по-прежнему доступны exact Node+script commands без кавычек; общий Node/shell не разрешается.

## Импорт, авторизация, запуск

startup при остановленном worker проверяет исходный внешний профиль0600/hash, сохраняет JSON, при необходимости импортирует AIM2 с autoStart=false и resumeAfterUpdate=false; первый импорт происходит после проверки автозапуска в YaTorrent. Дождаться нового зашифрованного profile.json, сверить публичные поля и хэши, штатно закрыть приложение. Credentials импортируются через safeStorage.

autoUpdate сохраняет выбор владельца. Перед обычным открытием сохраняются autoStart=true, consume_codex=true, consume_eliza=false, max_browsers=concurrency. Это запуск worker при открытии YaTorrent, не login item macOS. file-auth Codex выполняет владелец на этой машине. Prelaunch verify запускает реальный Codex/Chromium и даёт только «подготовлен». После open обязателен readback фактического worker. При сбое ненулевой exit/«не готов», будущий autoStart отключается по возможности; чужие уже начатые jobs не убиваются.

## Фактический worker

Источник — нативные service.json, worker-config.json, worker-state/worker/metrics.json и живой процесс. Старый sbs-worker-launch.json не используется. Сверяются PID и время старта процесса из ps lstart и codex_runtime (service.json содержит только pid/port), executable/хэш штатного worker, фактические binary/mode/timeout1800/concurrency и enabled. codex_runtime должен быть свежим ≤15с, ready=true, accepting=true, error=null, restart_required=false. Его launcher_identity, contract/version, real_codex, config_fingerprint и config_hash совпадают с текущим descriptor и каноническим hash JSON. Требуются два продвинувшихся heartbeat/uptime того же процесса; при startup он должен быть новее запроса запуска.

Готовность не выводится из одного файла, launch request или healthy API. verify не читает bearer token приложения и не делает broker claims. Полная verify без skip требует реальный Codex/Chromium и фактический worker. [Smoke](runtime.md). Под нагрузкой44агента/40браузеров могут потребовать уменьшения jobs; настроенный лимит4 сам по себе не подтверждает четыре активных задания.

## Обновления и источники

Latest stable разрешается через официальный GitHub API; SHA256 из release metadata обязателен. Нативный worker и проверенные ASAR модули сверяются с контрактом0.3.9. Новое несовместимое приложение не устанавливается вслепую; Codex pin/integrity отдельно не меняются. Автообновление может поменять контракт: повтори приёмку фактического runtime, не полагайся на прежний verification.json. Не переподписывать bundle, не отключать Gatekeeper/quarantine.

[YaTorrent 0.3.9](https://nda.ya.ru/t/UH7A5RKD7ub4o7), [Node 24.21.0](https://nda.ya.ru/t/JYqU8xp27ub4o3), [Playwright Chromium](https://playwright.dev/docs/browsers). Контракт взят из официальных ресурсов релиза; сборка ZIP не заменяет полную verify на чистой целевой macOS и broker end-to-end.

Readers в descriptor — технические entry points для нативной совместимости, не источник инструкций оценки. SBS packet проверяется launcher7 до модели. Принимающие SBS peers должны пройти instruction smoke; версия приложения0.3.9 сама по себе этого не доказывает.
