# YaTorrent SBS setup — r6

Публичный навык для macOS Apple Silicon: нативный YaTorrent0.3.9 с executor-runtime.json/sbs-codex-v1, Codex/Chromium, 11агентов на пару, 4параллельных задания и1800с. Приложение не патчится и не переподписывается.

Два режима: worker и координатор. Координатор получает пары, согласует удобный формат итогов, отправляет по1 SBS/task и сохраняет полученные оценки в Google Sheets через portable gwsx work. По умолчанию его машина также выполняет задачи. [SKILL.md](SKILL.md), [координатор](references/coordinator.md), [внешний профиль](references/profile.md).

Credentials не включены. AIM2 и OAuth-клиент передаются отдельными приватными файлами, авторизация происходит на целевой машине. Установка запускается только по поручению настроить эту машину. Полная verify без skip и реальный Codex/Chromium обязательны; тесты сборки не заменяют clean-Mac установку, broker end-to-end или проверку записи в выбранную таблицу.

Сборка: scripts/build-release.py --output /path/outside-skill/release.zip; явная инвентаризация, MANIFEST и SHA256, запрет secrets/авторских путей. Для релиза требуется независимое ревью и закрытие всех blockers/major перед публикацией.
