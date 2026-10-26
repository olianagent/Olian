# Встановлення, оновлення, резервні копії

## Вимоги
Linux-сервер 2 vCPU / 4 ГБ RAM, Docker і Docker Compose, домен із TLS (зворотний проксі), вихідний доступ до `api.telegram.org`.
Для агентів Hermes: Python 3.14 (є в образі). Для режиму `rules` ключ мовної моделі не потрібен.

## Встановлення (Docker)
```bash
git clone https://github.com/olianagent/Olian && cd Olian
pip install -e . && olian setup            # або вручну: cp .env.example .env і заповніть
docker compose up -d --build
curl -s localhost:8600/health              # {"status":"ok",...}
```
Порт опубліковано лише на `127.0.0.1`. Поставте TLS-проксі, наприклад Caddy:
```
olian.example.com {
    reverse_proxy 127.0.0.1:8600
}
```
Панель `/admin` і API мають бути лише за TLS. Обмежте `/admin` за IP або VPN, якщо можливо.

## Конфігурація проєкту (приклад `monitor`)
```json
{"monitor": {"enabled": true, "bot_token_env": "MONITOR_BOT_TOKEN", "mode": "hybrid", "profile": "…",
             "threshold": 0.5, "review_below": 0.65, "retention_days": 14, "lead_retention_days": 180,
             "author": "hash", "window_days": 30, "author_window_days": 14,
             "llm": {"base_url": "https://api.openai.com/v1", "model": "…", "api_key_env": "QUALIFIER_API_KEY"},
             "contact": {"allowed_ways": ["публічна відповідь у гілці, якщо дозволяють правила групи"]}}}
```

## Оновлення
1. `olian backup` (зашифрований).
2. `git pull && docker compose up -d --build`. Міграції схеми застосовуються автоматично й повторювано (`schema_version`).
3. `curl /health`, перегляньте розділ «Звіти» (помилки джерел).
Відкат: `docker compose down`, повернути попередній тег, `olian restore <бекап> --to <каталог>`.

## Резервні копії
`olian backup --out /backups/olian-$(date +%F).tar.gz.enc` (cron щодня). Знімок БД узгоджений навіть під навантаженням,
шифрується `OLIAN_BACKUP_KEY` (Fernet); у архів входить `.pii_salt`. **Ключ зберігайте окремо від копій.** Перевіряйте
відновлення щокварталу: `olian restore … --to /tmp/check`.

## Моніторинг справності
`/health`; у «Звітах» поля «Проблемні джерела» й «Помилки джерел»; сповіщення адмінам при спрацюванні аварійного вимикача;
логи структуровані й маскують токени.

## Hermes
Версію Hermes фіксуйте (`--build-arg HERMES_REF=<тег>`); потрібні extras `messaging` і `mcp`. Образ не збирався в середовищі
розробки (не було Docker-демона): перший `docker compose build` на вашому сервері це перевірка.
