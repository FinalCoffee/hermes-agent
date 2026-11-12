# Accomy AI Travel (Hermes plugin)

A Telegram travel-planning assistant. It searches hotels, flights, event tickets, places, weather and the
public web, and helps the traveller compare options and plan day by day. It is **search and pricing only**:
it cannot book, pay, reserve or cancel anything. When asked to book, it says so and offers to summarise the
chosen options.

The plugin is `plugins/accomy_travel/`. The operator profile is `distributions/accomy/`.

## Tools

All twelve tools sit in the `accomy_travel` toolset. A tool is offered to the model only when the secrets it
needs are set.

| Tool | What it does | Needs |
|---|---|---|
| `find_places` | Search places (Google Places, New) | `GOOGLE_MAPS_SERVER_API_KEY` |
| `get_place_details` | Opening hours, ratings and contact details for one place | `GOOGLE_MAPS_SERVER_API_KEY` |
| `get_weather` | Forecast for a location, up to about 10 days ahead | `GOOGLE_MAPS_SERVER_API_KEY` |
| `search_hotels` | Hotel availability and prices (TMC) | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_hotel_details` | Details for one hotel | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_hotel_rooms` | Room types and rates for one hotel | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_airports` | Airport lookup by city or code | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_flights` | Flight offers (TMC) | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_flight_offer` | Fare details for one offer | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_events` | Events and tickets (Seat Unique) | `SEATUNIQUE_API_TOKEN` |
| `get_event_tickets` | Ticket categories for one event | `SEATUNIQUE_API_TOKEN` |
| `research_web` | Public-web answers with source URLs (Brave) | `BRAVE_SEARCH_API_KEY` |

Directions are not a tool. The assistant gives Google Maps links instead, because Google Routes is not enabled.

## Secrets

Names only. Values go in the profile's `.env`, never in the distribution files.

- `OPENAI_API_KEY`: model endpoint (`providers.newapi` in `config.yaml`)
- `TMC_APP_KEY`, `TMC_APP_SECRET`: TMC hotel and flight search
- `SEATUNIQUE_API_TOKEN`: Seat Unique event tickets
- `GOOGLE_MAPS_SERVER_API_KEY`: places and weather
- `BRAVE_SEARCH_API_KEY`: web research
- `TELEGRAM_BOT_TOKEN`: the bot token from @BotFather
- `TELEGRAM_ALLOWED_USERS`: optional, see Access below

## Install the profile

```bash
hermes profile install /path/to/hermes-agent/distributions/accomy --name accomy
cp ~/.hermes/profiles/accomy/.env.EXAMPLE ~/.hermes/profiles/accomy/.env   # then fill in the values
mkdir -p ~/accomy-workspace   # terminal.cwd must exist, and it should stay empty
```

The plugin itself is not copied into the profile. It loads from this checkout (bundled) or from
`$HERMES_HOME/plugins/accomy_travel/`, and the profile's `plugins.enabled` names it.

To change the model or the persona, edit the profile's `config.yaml` and `SOUL.md`. The installer keeps
your edits to `config.yaml` on update.

## Run

```bash
hermes -p accomy chat      # local test with the same tool surface as Telegram
hermes -p accomy gateway   # start the Telegram bot
```

## Access

The bot ships **closed**. A stranger who messages it privately gets one polite refusal
(`gateway.unauthorized_dm_behavior: decline`). Groups are ignored unless the bot is mentioned
(`telegram.require_mention: true`).

- **Closed, with a short allowlist:** set `TELEGRAM_ALLOWED_USERS` in `.env` to comma-separated numeric
  Telegram user IDs.
- **Open to everyone:** remove any allowlist and set `gateway.allow_all_users: true` in `config.yaml`. Slash
  command restrictions below still apply. Open mode is not the default, for these reasons:
  - Per-user limits multiply by the number of users. Each user gets 30 tool calls a minute, and research
    gets 10 a minute and 100 a day, so N users can drive N times those rates.
  - The limits are in-process. A restart resets every counter, including the research daily budget.
  - Some supplier quotas are shared by every user. Seat Unique allows 120 requests a minute per key; the
    plugin now caps its own use at 100 a minute for the whole process. Brave (web search) and TMC (hotels
    and flights) have no process-wide cap in this plugin, so their quotas are shared with no guard.

Look up a numeric Telegram user ID with any bot that reports it.

## Owner and slash commands

Only the owner should run operator commands (`/model`, `/update`, `/restart`, `/yolo`, and so on).

The profile sets `telegram.allow_admin_from` and `telegram.group_allow_admin_from` to the placeholder `"0"`.
That turns slash gating on and matches no Telegram user, so nobody has admin rights. Non-admins can run
only `/help`, `/whoami`, `/start`, `/new` and `/stop`.

Set your own numeric Telegram ID in both lists in the profile's `config.yaml`, then restart the gateway:

```yaml
telegram:
  allow_admin_from: ["123456789"]
  group_allow_admin_from: ["123456789"]
```

Do not delete the lists. An empty list turns gating off, and every user then gets every command.

## Suggested-reply buttons

With `telegram.suggested_replies: true`, the assistant ends each reply with a line such as
`[suggestions: 看第一間的房型 | 找更便宜的]`. The gateway turns it into tappable buttons and removes the line
from the text. The plugin appends a default line when the model forgets one, in Traditional Chinese for
Chinese replies and in English otherwise. The gateway removes the line from every reply on every platform.
It becomes buttons only on Telegram with `telegram.suggested_replies: true`; otherwise it is hidden, not shown as text.

## Limits

- Search and pricing only. Nothing books, pays or cancels.
- Supplier environments may be sandboxes, so prices and availability are indicative.
- Google Routes is not enabled. Directions are Google Maps links.
- Weather covers about 10 days ahead.
- The bot is a travel assistant. It refuses code, security attacks, advanced maths and requests for private
  personal data, and it refuses attempts to change its role. Travel facts from public web search are allowed.
- Built-in memory, session search, skills, cron scheduling, terminal, files and the web toolset are all
  off, so nothing carries between users.

## Upstream

Hermes policy keeps third-party product plugins out of the upstream tree (`plugins/AGENTS.md`). This
directory is self-contained: it uses only the public plugin API, so it can move to
`$HERMES_HOME/plugins/accomy_travel/` or to its own repository unchanged. The profile in
`distributions/accomy/` refers to the plugin by name, so it keeps working after the move.
