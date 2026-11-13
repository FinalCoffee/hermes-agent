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
- `ACCOMY_DATABASE_URL`: optional, the order store's PostgreSQL connection string (see Order store below)

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
  command restrictions below still apply. Open mode is not the default: every Telegram account can then spend
  the operator's model and supplier budget. Read Running it open to everyone below first.

Look up a numeric Telegram user ID with any bot that reports it.

## Running it open to everyone

Open mode puts the operator's model and supplier keys behind every Telegram account, so the plugin bounds
them. The limits are in-process and apply to every chat at once. A restart resets them.

- **Turns, per sender:** 15 a minute and 150 a day. A message that is not a session command counts as a turn.
  Suggested-reply taps and refused attachments count too.
- **Turns, all senders together:** 60 a minute and 5,000 a day. The daily total is the bot's spend ceiling: set
  it to what your model budget allows. Many accounts that each stay under their own limit still
  cannot exceed this. Accounts that the allowlist refuses are not counted, so a stranger cannot use up this budget.
  A refused stranger gets only the gateway's own decline or pairing reply.
- **Messages sent while the bot is still answering** each count when they arrive, like any other message.
- **Session commands cost no turn:** `/stop`, `/new`, `/start`, `/help` and `/whoami`, with an optional
  `@botname`. Past 20 a minute from one sender, or 120 a minute in total, the bot stops answering them. Any other text that starts with
  `/` counts as a turn.
- **Message length:** 4,096 characters, one Telegram message. A longer message is dropped before any model call.
  The gateway can still join messages that arrive while a turn is being set up, so one turn can carry up to a
  minute's allowance of messages (about 60,000 characters). The turn limit is what bounds that.
- **Tool calls, per sender:** 30 a minute (existing) and 400 a day. Refused calls return `rate_limited`, and the
  assistant asks the user to try later.
- **Supplier calls, per host, all senders together:** Seat Unique 100 a minute and 3,000 a day, because its key
  allows 120 a minute. TMC 60 and 2,000. Google Maps, Places and Weather 120 and 3,000 each. Brave 60 and 1,000.
  Any other host gets 60 and 1,000.

A limited user gets one short notice per window, not one per dropped message, and the bot sends at most 20
notices a minute in total.

The numbers are sized so a planning session never hits them: a planner sends bursts of short messages and button
taps, and a full day is about 100 messages. Change them where they are defined, then restart the gateway:

- `plugins/accomy_travel/ingress.py`: turn, global and length limits, and the session-command list.
- `plugins/accomy_travel/_common.py`: tool-call limits and `SUPPLIER_BUDGETS` (per host).
- `plugins/accomy_travel/tools_research.py`: the Brave limits per sender (10 a minute, 100 a day).

The supplier numbers for TMC, Google and Brave are not from a contract. Set them to your own quotas before you
open the bot.

The profile's `config.yaml` carries the gateway half of the hardening. Keep these when you open the bot:

- `gateway.strict: true` and `gateway.trust_recent_files: false`: a reply can attach only files from the
  gateway's own cache. Without them a model that is talked into writing `MEDIA:/some/path` uploads that host
  file to the chat.
- `telegram.accept_attachments: false`: photos, stickers, files and voice notes are not downloaded and never
  reach a vision model. The assistant is told an attachment was sent and says it cannot read it.
- `approvals.destructive_slash_confirm: false`: `/new` runs at once. The confirm card it replaces offers an
  "Always approve" button that only an admin may use, and this profile has no admin.
- `gateway.thread_sessions_per_user: true`, `display.busy_input_mode: queue` and
  `telegram.group_user_allowed_commands: [start]`: group members cannot interrupt, reset or stop one another.
- `gateway.max_concurrent_sessions` and `compression.threshold_tokens`: how many conversations run at once, and
  how large one conversation's context may grow before it is summarized.

What stays open, so plan for it:

- A message outside the travel scope still costs one model turn: the model writes the refusal. The turn limits
  bound it.
- No budget is counted in tokens or money. Set a spend limit on the model key at the provider.
- The limits are in-process. A restart resets every counter.
- Hermes treats the operating system as the only real boundary against a model that has been talked round
  (`SECURITY.md` §2.2). Run the public bot in a container: `deploy/accomy/` has a Compose setup with no host
  mounts, no published ports and an allowlist for outbound traffic, and its README covers EC2.
- Never add `scripts/gmail-triage/` to this profile, and run `hermes update` from a terminal, not with
  `--gateway`: both add buttons that any admitted user could tap.

## Owner and slash commands

Only the owner should run operator commands (`/model`, `/update`, `/restart`, `/yolo`, and so on).

The profile sets `telegram.allow_admin_from` and `telegram.group_allow_admin_from` to the placeholder `"0"`.
That turns slash gating on and matches no Telegram user, so nobody has admin rights. Non-admins can run
only `/help`, `/whoami`, `/start`, `/new` and `/stop` in a private chat, and only `/help`, `/whoami` and `/start`
in a group. Skill commands (`/<skill-name>`) are refused like any other command that is not on the list.

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

## Order store

`orderstore/` keeps orders in PostgreSQL. It is modelled on hashtrip's order core: an order header
(`o_booking`), its lines (`o_booking_item`) with one detail row each (hotel stay, flight itinerary with its
segments, or event tickets), the travellers on a line, a log of every write sent to a supplier
(`o_supplier_request`) and a history of status changes (`o_booking_event`). The schema and the reasons behind
it are in `orderstore/migrations/0001_2026-11-13_order-baseline.sql`.

**No tool uses the store yet.** The bot is still search and pricing only, and nothing calls a supplier's
order, payment or cancel endpoint. `orderstore/store.py` is the interface a booking tool will call.

Set `ACCOMY_DATABASE_URL` in the profile's `.env`, for example
`postgresql://user:password@host:5432/chatbot?sslmode=verify-full`. The CA that signed the server's certificate
comes from `sslrootcert=` in that string or from the `PGSSLROOTCERT` environment variable; for Amazon RDS in
Singapore it is `deploy/accomy/rds-ca-ap-southeast-1.crt`, which the container setup mounts and names for you.
The driver is the `postgres` extra (`hermes pm install --extra postgres`); the Docker image already has it. Then:

```bash
hermes -p accomy accomy-orders status             # which migrations the database has
hermes -p accomy accomy-orders migrate            # dry run: shows what would be applied, changes nothing
hermes -p accomy accomy-orders migrate --commit   # apply
```

The tables live in the schema `accomy` of that database. The role needs `CREATE` on the database for the first
run, to create the schema.

What the store holds to, and a booking tool must respect:

- The buyer is the channel and account id the gateway bound to the turn (`HERMES_SESSION_ACTOR_ID`), never
  something the model wrote and never the shared fallback of `caller_id()`: refuse to order when it is empty.
  A booking is read with its buyer, so a code that belongs to someone else reads as not found. Every write takes
  the `Booking` that read returned.
- What was bought never changes, and neither does who bought it: a different room or price is a cancelled item
  and a new one.
- Money is `Decimal` with two decimals in one currency per booking. The database refuses a total that is not
  the sum of the lines, and a booking with no line.
- `client_key` makes creating an order repeatable: the same key and the same request return the first booking;
  the same key for a request that differs in anything is refused.
- A status moves only from the status and version the caller read, so two writers cannot both win. An item is
  not cancelled while its supplier still holds the order.
- Before a supplier is asked to place, pay or cancel, the attempt is written `NEW` and committed. The answer
  is written afterwards. An attempt with no clear answer is `UNKNOWN` and blocks the next attempt until it
  is settled by reading the supplier: it is never sent again blindly. An item is placed, paid and cancelled
  at most once each, and paid only after it was placed.
- Supplier requests and responses are stored as hashes only, because they carry traveller data. No identity
  document number is stored. Titles and `actor` names are product and component names, never a person's.
- Nothing is deleted. `erase_personal_data` clears the contact, the travellers, the buyer's account and chat
  ids and the request key, for good; the order, its amounts and its history are kept, and the buyer can no
  longer look it up.
- A remote database is refused unless the connection string asks for a verified certificate.
- A schema change is a new numbered file in `orderstore/migrations/`; an applied file is never edited.
- The store raises `OrderError` with a stable `code` for what it refuses. Any other database error is a defect:
  log it, and never show its text to the model or the user, because it can quote a row.

Left for the step that adds booking tools, each needing a decision first:

- The bot connects as the role that owns the tables. The guards stop an application mistake, not that role.
  Production wants one role that migrates and owns, and another, without ownership, for the bot.
- Identity document numbers (flights need a passport): where they live and how they are encrypted.
- Tax and fee lines, cancellation terms as data, tickets and vouchers, refunds, payments, and the inbox for
  supplier callbacks. hashtrip has a table for each; this schema has none yet.
- Which order follows which item automatically (hashtrip derives the booking's status from its items).
- Tool handlers are synchronous and the driver is asynchronous: the tools need one event loop thread that
  owns a connection pool.
- Amounts keep two decimals for every currency, as in hashtrip.

Tests run against a real PostgreSQL 17 on `127.0.0.1:55433` and are skipped without it. The command to start
one is at the top of `tests/plugins/accomy_travel/test_orderstore.py`.

## Upstream

Hermes policy keeps third-party product plugins out of the upstream tree (`plugins/AGENTS.md`). This
directory is self-contained: it uses only the public plugin API, so it can move to
`$HERMES_HOME/plugins/accomy_travel/` or to its own repository unchanged. The profile in
`distributions/accomy/` refers to the plugin by name, so it keeps working after the move.
