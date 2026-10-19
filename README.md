# Olian: харнес для проєктних агентів на базі Hermes Agent

Один оркестратор, багато ізольованих проєктів. На кожен проєкт свій агент ([Hermes](https://github.com/nousresearch/hermes-agent)
у власному профілі), свої люди з ролями, свої скрапери й аутріч. Люди, скрапери та самі агенти працюють із проєктом
через **один фасад з правами та аудитом**.

**Hermes не форкнуто.** Він використовується як є, як рушій агента, тож оновлення Hermes не конфліктують з нашим кодом.
Уся додана логіка живе в `olian/`.

```
 люди (Telegram / WhatsApp / CLI)    скрапери, монітори, вебхуки
              │                                  │
              ▼                                  ▼
   ┌─────────────────────  Olian (цей репозиторій)  ─────────────────────┐
   │ роутер чатів → проєкт + роль   │  ingest / webhooks (HMAC)          │
   │              ФАСАД: дії · права · аудит · MCP · OpenAPI             │
   │ цілі й завдання │ воронка лідів + аутріч │ скрапери │ сигнали-правила│
   └───────┬───────────────────────────┬───────────────────────┬─────────┘
           ▼                           ▼                       ▼
   Hermes проєкту A            Hermes проєкту B         Hermes проєкту C
   (власний HERMES_HOME, порт, ключі, пам'ять, skills, sandbox)
```

## Ідеї, закладені в дизайн

| Ідея | Як реалізовано |
|---|---|
| Агент лише один із клієнтів операційного API, без доступу до БД | `olian/facade.py`: уся зміна стану лише через іменовані дії. Агент бачить їх як MCP-інструменти. |
| Права по людині, не «усе або нічого» (Олеся лише reception) | Роль + `scopes` (`leads.*,tasks.list`) на людині/токені. Поки агент відповідає людині, його права звужуються до її прав (`narrow`). |
| «Кожен крок записується» | Таблиця `audit`: хто, що, з якими аргументами, успіх чи відмова. Секрети редагуються на будь-якій глибині. |
| Агент не може → ставить людині завдання з кроками | `tasks.add executor=human steps=[{text,link,copy}]`. Людина лише тицяє й вставляє. `/sweep`: «зроби сам, що можеш». |
| Завдання під цілі, дедлайни підсвічуються | `goals` + сортування завдань: прив'язані до цілі й найближчий дедлайн зверху. |
| Speed-to-lead: лід «гарячий» хвилини | Сигнал → правило → AI-кваліфікація → чернетка за секунди. Лід старший за `max_lead_age_minutes` стає `stale` і не отримує повідомлення. |
| Ліміти, щоб не забанили | Пул відправників: денний ліміт на акаунт, ротація, пейсинг, вікно годин, стоп-слова (опт-аут остаточний). |
| CRM теплих контактів | `source=warm`, `warmth`, `next_touch`: агент готує чернетку check-in, людина схвалює. |
| Той самий фасад для Claude та інших | `/mcp` (streamable HTTP) і `/p/openapi.json`. |

## Швидкий старт (локально)

Потрібні Python ≥ 3.11 для Olian і **Python 3.14 для Hermes** (так зроблено в самому Hermes).

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest -q                                   # 62 тести

# Hermes: офіційний інсталятор, або з вихідників у Python 3.14
#   uv venv --python 3.14 hv && uv pip install --python hv/bin/python -e "/path/to/hermes-agent[messaging,mcp]"
# extras messaging (API server) та mcp (клієнт до фасаду) обов'язкові.
export OLIAN_HERMES_BIN=/path/to/hermes OLIAN_API_KEY=$(openssl rand -hex 24)

bash examples/quickstart.sh                 # демо без зовнішніх сервісів (dry-run відправник)
```

Реальний проєкт із моделлю та Telegram-ботом:

```bash
olian project create bali --name "Bali Villas" --goal "40 бронювань до грудня" \
      --telegram-token <BOT_TOKEN> --provider openrouter --model anthropic/claude-opus-4.6 --sandbox local \
      --config '{"env": {"OPENROUTER_API_KEY": "sk-or-..."}}'
olian member bali --platform telegram --user <ВАШ_TELEGRAM_ID> --role admin --name boss
olian member bali --platform telegram --user <ID_КОЛЕГИ> --role manager --scopes 'tasks.*,leads.*,outreach.outbox'
olian serve --start-agents                  # API + воркери + Telegram + Hermes кожного проєкту
```

Напишіть боту `/help`. Вільний текст іде агенту проєкту. Команди (`/tasks`, `/approve 7`, `/sweep`, `/plan`) ідуть через
той самий фасад із перевіркою ролі. Приклад повної конфігурації аутрічу: `examples/outreach-project.json`.

## Люди й ролі

`viewer` читає й говорить з агентом · `manager` ще й змінює завдання/ліди, схвалює чернетки · `admin` ще й керує людьми,
відправниками, скраперами, бачить аудит. `--scopes` звужує роль до конкретних дій. Чат прив'язується до проєкту
(`olian bind`); приватний чат працює, якщо людина в одному проєкті, інакше `#проєкт текст`. Чужим нічого не показується.

**Важливо.** Telegram за замовчуванням обслуговує Olian (щоб діяли права по людині). `channels.telegram.via: "hermes"`
віддає бота рідному gateway Hermes. Тоді агент діє з правами проєкту для всіх у allowlist, тож це лише для проєктів,
де всі довірені. Те саме стосується cron-завдань усередині Hermes: вони виконуються правами агента (`manager`).

## WhatsApp: два шляхи, не плутайте

* **Холодний аутріч: лише офіційний Cloud API.** `olian sender <proj> wa whatsapp_cloud --config '{"phone_number_id":"…","template_name":"intro"}' --cap 20`.
  Поза 24-годинним вікном Meta дозволяє лише схвалений шаблон, і Olian відмовляє голосно, якщо шаблону нема. Вхідні
  відповіді йдуть на `/wa/webhook` (підпис `WHATSAPP_APP_SECRET`, потрібен публічний HTTPS).
* **Командний чат із агентом:** teammate (member з `--platform whatsapp`) пише на той самий номер, і це розмова з агентом, а не лід.
* Неофіційний міст Hermes (Baileys) не для аутрічу: ризик бану. Для власної ферми Telegram-акаунтів використовуйте
  `webhook`-відправник (`olian sender … webhook --config '{"url":"https://ваша-ферма/send","serves":["telegram"]}'`) і `/ingest` для подій.

## Скрапінг і сигнали

`olian scraper <proj> <name> rss|http|command|external --spec '{…}' --every 15`.
`command` запускається в Docker (пам'ять/CPU/pids обмежені, лише `/data` проєкту), `local` потрібно дозволити явно.
Монітори, що не живуть в Olian (ваша Telegram-ферма), шлють події на `POST /ingest` зі scoped-токеном
(`olian token <proj> --label farm --scopes events.ingest`). Правила в `config.signals.rules` перетворюють події на
лід + чернетку, завдання або сповіщення. Дедуплікація скраперів зберігається в теці проєкту.

## Доступ для зовнішніх клієнтів

```bash
olian token bali --label claude --role manager          # → olp_…
claude mcp add --transport http bali http://127.0.0.1:8600/mcp --header "Authorization: Bearer olp_…"
curl -H "Authorization: Bearer olp_…" localhost:8600/p/openapi.json
```

## Деплой

`docker compose up -d` (див. `Dockerfile`, `.env.example`). Слухає лише `127.0.0.1`. Для вебхуків поставте TLS-проксі.
Docker-socket для sandbox закоментований свідомо: він еквівалентний root на хості.

## Що перевірено, а що ні

**Перевірено автоматично (62 тести):** ізоляція профілів і даних між проєктами, права/scopes/звуження агента, аудит і
редакція секретів, контролер завдань, вся воронка аутрічу (ліміти, вікно, пейсинг, опт-аут, stale, шаблони WhatsApp),
скрапери й правила, MCP/REST/вебхуки з підписами, життєвий цикл процесів без «осиротілих» gateway.

**Перевірено вручну на справжньому Hermes (Python 3.14):** мій згенерований профіль піднімає `hermes gateway run --force`,
API server слухає свій порт з авторизацією, а `hermes mcp test` підключається до фасаду Olian по токену з `.env` і бачить
17 інструментів (без заборонених агенту).

**НЕ перевірено (потрібні ваші ключі/інфраструктура):**
* Повний цикл з реальною LLM: у мене не було ключа моделі. Шлях `Olian → Hermes /v1/chat/completions` перевірено лише
  проти заглушки з тими самими заголовками.
* Реальні Telegram Bot API і Meta WhatsApp Cloud API (лише проти заглушок).
* Збірка `Dockerfile` (у моєму середовищі не було Docker-демона).
* Hermes `terminal.backend: docker` (sandbox) і те, як контейнер бачить фасад: може знадобитися `OLIAN_AGENT_API_URL`.

**Відомі обмеження:** немає веб-інтерфейсу; один вузол і SQLite; права «агент = мінімум(агент, людина)» тримаються на
серіалізації ходів агента на проєкт, тож довгий хід одного блокує інших у цьому проєкті; AI-класифікація відповідей має
консервативний ключовий fallback, але автоматично нічого не надсилає.
