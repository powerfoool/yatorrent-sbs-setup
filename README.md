# YaTorrent SBS setup — r5

Публичный навык подготовки YaTorrent worker на macOS Apple Silicon: Codex CLI, Playwright/Chromium, узкие allow-правила, 11 агентов на задание, 4 параллельных задания, таймаут 1800 секунд и обязательная runtime-проверка.

[Инструкция навыка](SKILL.md) · [Внешний профиль](references/profile.md) · [Границы проверки broker](references/broker.md)

Скачайте ZIP и файл SHA-256 из [GitHub Releases](https://github.com/powerfoool/yatorrent-sbs-setup/releases), проверьте контрольную сумму и распакуйте. Профиль AIM2 владелец передаёт отдельным приватным файлом. В репозитории и публичном ZIP credentials нет. Установку запускает только поручение пользователя на настройку целевой машины.

Для обновления публичного архива используйте `scripts/build-release.py --output /path/outside-skill/release.zip`: явный список файлов, запрет приватных профилей и проверка MANIFEST. Приёмку установленного worker выполняет полная `scripts/verify.py` без skip. Тесты сборки не заменяют эту проверку на целевом Mac и end-to-end доставку через broker.
