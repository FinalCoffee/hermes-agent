# Accomy AI Travel (Hermes plugin)

A Telegram travel-planning assistant. It searches hotels, flights, event tickets, places, weather and the
public web, prices everything in US dollars, and helps the traveller compare options and plan day by day. It
reads what a traveller sends besides text: photos, voice notes and files. It
can take **demonstration orders** for hotels, flights and event tickets from a private chat. Those orders are
recorded in the order database only: nothing is placed with a supplier, no payment is taken, and no supplier
confirmation number or e-ticket is ever issued.

The plugin is `plugins/accomy_travel/`. The operator profile is `distributions/accomy/`.

## Tools

The twelve search tools, the four order tools and `read_attachment` sit in the `accomy_travel` toolset. A tool
is offered to the model only when the secrets it needs are set; the order tools also need `ACCOMY_DATABASE_URL`.

| Tool | What it does | Needs |
|---|---|---|
| `find_places` | Search places (Google Places, New) | `GOOGLE_MAPS_SERVER_API_KEY` |
| `get_place_details` | Opening hours, ratings and contact details for one place | `GOOGLE_MAPS_SERVER_API_KEY` |
| `get_weather` | Forecast for a location, up to about 10 days ahead | `GOOGLE_MAPS_SERVER_API_KEY` |
| `search_hotels` | Hotel availability and prices (TMC) | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_hotel_details` | Details for one hotel | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_hotel_rooms` | Room types and rates for one hotel | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_airports` | Airport lookup by city or code | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_flights` | Flight offers (TMC): one way, round trip or multi-city, a leg at a time | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `get_flight_offer` | Live price and fare rules for one offer, a whole trip included | `TMC_APP_KEY`, `TMC_APP_SECRET` |
| `search_events` | Events and tickets (Seat Unique) | `SEATUNIQUE_API_TOKEN` |
| `get_event_tickets` | Ticket categories for one event | `SEATUNIQUE_API_TOKEN` |
| `research_web` | Public-web answers with source URLs (Brave) | `BRAVE_SEARCH_API_KEY` |
| `place_order` | Places a demonstration order for 1 to 3 quotes from the search results | `ACCOMY_DATABASE_URL` |
| `list_trips` | This chat's trips, newest first, each with its orders | `ACCOMY_DATABASE_URL` |
| `get_order` | One of this chat's orders, with its items | `ACCOMY_DATABASE_URL` |
| `cancel_order` | Cancels one of this chat's demonstration orders | `ACCOMY_DATABASE_URL` |
| `read_attachment` | Reads a file the user sent: a document's text, or the words spoken in an audio file or a video | nothing; speech needs the two `ACCOMY_TRANSCRIBE_` keys |

Directions are not a tool. The assistant gives Google Maps links instead, because Google Routes is not enabled.

**Prices and quotes.** Every price a tool returns is in US dollars, through `fx.py`. A supplier price in another
currency is converted with a fixed demo table of reference rates, rounded up to the cent, and marked
`"approximate": true` with the supplier's own amount in `supplier_price`. A currency the table lacks stays in
its own currency with `"unconverted": true` and cannot be ordered. Each hotel rate, flight offer and event listing
that can be ordered carries a `quote_id`, which expires after 30 minutes. `quotes.py` keeps what each caller was
shown, bound to that caller, capped per caller and overall, and in memory: a restart forgets the quotes, and the
caller searches again. `place_order` takes quote ids only, and builds the order from what was shown, so no price
or supplier id comes from the model.

**Round trips and multi-city trips.** A journey out and back is one round-trip search (`return_date`), and a
journey that comes home from another city, such as Taipei to Tokyo and then Osaka to Kaohsiung, is one multi-city
search (`more_legs`, up to 4 legs in all). The policy and the tool description tell the model never to search or
order the directions as separate one-way trips, because the supplier sells the whole trip for far less: in a
live probe in November 2026, Hong Kong to London and back cost 765 US dollars as a round trip against 893 as
two one-way fares. TMC answers such a search one leg at a time, and so does the tool:

1. The first call returns the flights of leg 1. Each has an `option_id` and `trip_price_from`, the lowest price
   of the whole trip that uses that flight. It is not the price of that flight.
2. A call with `continue_from` set to the chosen `option_id` returns the flights of the next leg. The trip, its
   dates, the passengers and the cabin stay as first searched: a call that passes any of them with
   `continue_from` is refused, and changing one means searching the whole trip again. A flight the trip could
   not go on from (an incomplete record, or a price with no US dollar rate) is not listed on an earlier leg.
3. The flights of the last leg carry a `quote_id` and the price of the whole trip with the flights chosen.

An `option_id` is remembered like a quote (same caller, 30 minutes), but it is not a fare: `get_flight_offer`
and `place_order` refuse it. For a trip, `get_flight_offer` first reads the fares TMC sells for the finished
itinerary and verifies the cheapest one that still holds (up to 3 are tried), because TMC's price check refuses
the key the search itself returned. The live price of a trip is often a little above the searched one, and the
quote then stands for the live price. An order for a trip is one flight item of kind `ROUND_TRIP` or
`MULTI_CITY`, with each flight stored under its leg. A place without an airport of its own is searched by the
nearest city that has one (Kyoto by Osaka).

**Placing an order.** `place_order` needs a private Telegram chat with a real sender, the lead traveller's name
and a phone or an email. Flights need every passenger's name; hotels need at least the lead guest. The order is
stored as `SIMULATED`: its items are confirmed in the database with fulfilment `NOT_APPLICABLE`, and no
`o_supplier_request` row is ever written for it. The database refuses a supplier request for a simulated order.
Placing the same quotes, with the same details, on the same calendar day (UTC) returns the first order.

**Suppliers are only read.** The TMC client enforces the same line on the wire: it sends only the session, lookup, search, itinerary-fare and
price-check endpoints listed in `tmc_client._READ_ENDPOINTS`, and refuses any other path (create order, pay, cancel, change,
refund) before a request is made, with whatever key is configured. The Seat Unique client only reads. Hotels
and flights are searched through these tools only: the policy tells the model never to find or price them with
`research_web`.

**Trips.** Every order belongs to a trip, the master record of one journey. The first order starts a trip (named
by `trip_title`, or after its first item); a later order for the same journey passes that trip's `trip_code`.
`list_trips` shows each trip with the orders under it.

**Demo payment link.** When `plugins.entries.accomy_travel.settings.payment_url` and the secret
`ACCOMY_PAYMENT_LINK_SECRET` are both set, a confirmed order carries `payment_url`: a signed link to the demo
payment page in `deploy/accomy/payment-demo/`. That page charges nothing; pressing Pay there sends the user a
Telegram message. The bot is not told, so it never says an order is paid.

## Attachments

With `telegram.accept_attachments: true` the Telegram adapter downloads what a user sends into the gateway's
media cache (`$HERMES_HOME/cache/`), where a file stays for a day. What happens next depends on what it is:

| The user sends | What the model gets | Done by |
|---|---|---|
| A photo, or an image sent as a file | The picture itself, in the same model call | core, because `config.yaml` declares `supports_vision: true` for the model |
| A voice note | Its transcript, as the user's own words. The user is shown the transcript too | `speech.py`, as the gateway's speech-to-text provider (`stt.provider: accomy-transcribe`) |
| Any other file | A note with the file's name and saved path; the model calls `read_attachment` when the request depends on the content | `tools_attachments.py` |
| A sticker | A one-line description. A static sticker is described once by a model call and remembered | core |

The adapter puts the content of a small text file straight into the message. The inbound gate takes it out
again (`attachments.own_words`), so a text file is read like every other file, on request, and the message is
what the sender typed: the length limit and the out-of-scope screen judge a person's words, never a file's.

`read_attachment` chooses how to read a file by its extension (`attachments.py`):

| Extension | Read as |
|---|---|
| `.pdf` | Text, page by page (pypdf). A page with no text in it is a scan: it is drawn as a picture and read by the model (see "Scanned pages" below) |
| `.docx` `.xlsx` `.pptx` `.doc` `.xls` `.ppt` `.odt` `.ods` `.odp` `.rtf` `.epub` | Markdown: headings, tables, every sheet, dates as dates (anydoc) |
| `.txt` `.md` `.csv` `.tsv` `.json` `.xml` `.yaml` `.yml` `.log` `.ics` `.vcf` `.srt` | Text, in UTF-8 or the encoding it most looks like (a CSV from a Chinese or Japanese Windows is neither) |
| `.html` `.htm` | The page's words, without tags, scripts or styles |
| `.eml` | Sender, recipients, date, subject and body; attached files are named, not read |
| `.ogg` `.oga` `.opus` `.mp3` `.m4a` `.wav` `.flac` `.aac` `.amr` | The words spoken (Amazon Transcribe) |
| `.mp4` `.mov` `.webm` `.mkv` `.avi` | The words spoken on the sound track; the picture is not seen |
| An image extension | Nothing: the model is told the picture is already in the conversation |
| Anything else (`.zip`, `.exe`, ...) | Refused, with the kinds that can be read |

What keeps this safe for a bot anyone can message:

- **A file belongs to its sender.** The inbound gate notes each file of an admitted message under the user who
  sent it. `read_attachment` opens only a file noted for the caller of the turn, found by its saved path or
  its name. A made-up path, another user's file and a file older than a day are all unknown. The notes are in
  memory: after a restart the user sends the file again.
- **A document is parsed in a process of its own** (`_document_worker.py`, started with `python -I`), with 30
  seconds to finish and, on Linux, 2 GB of memory. A parser that hangs or crashes on a hostile file takes nothing
  else with it. Reading opens no network connection of its own; a scanned page goes to the model the bot
  already talks to.
- **Audio is decoded by ffmpeg with a fixed list of container formats and the file protocol only**, so a
  playlist or a subtitle file named like audio cannot make it open other files or addresses.
- **File content is data.** It reaches the model only as a tool result, never as part of the user's message.
  The scope section tells the model so, and every `read_attachment` result repeats it: no instruction in a
  file is followed, and a price for an order comes only from a search tool's quote.
- **Messages that go no further leave no file behind.** The adapter has already written a message's files when
  the gate runs, so the gate deletes the files of every message it drops, of a sender that auth refuses, and of
  a `/new` or `/stop` (which still runs). A flood therefore fills no disk.

**Scanned pages.** A scan is a PDF whose pages are photographs of paper, so it holds no text to extract.
`ocr.py` has the worker draw each such page as a person would see it (PDFium, 1,600 pixels on the longer side)
and asks the profile's own model to copy out what is written, through the plugin interface (`ctx.llm`): no
other service, no extra key, and it reads Chinese, Japanese and Korean as well as Latin script. Up to four
pages go into one model call, at about 2,200 input tokens a page; a two-page scan takes about nine seconds.
The first 8 scanned pages of a file are read, and what was read is kept in memory for as long as the file is,
so reading the same file again costs nothing; two requests for one file wait for each other, so neither pays
twice. One page gives at most 8,000 characters. In a PDF that mixes text pages and scanned pages, the text comes
out in page order. The result says which pages were read this way, because a character can be misread: the
assistant is told to confirm a name, a number or a date when the answer depends on it. A page that was not
read (past the first 8, past the day's allowance, or when the model call failed) is listed, and the assistant
asks for a photo of it. The model is given no tool for this call, so words on a page cannot make it do
anything: at worst they come back as text, which is file content like any other.

**Speech-to-text.** `speech.py` converts the audio to 16 kHz mono PCM, signs a WebSocket address with the
profile's own key pair (AWS Signature Version 4, no AWS library) and streams it to Amazon Transcribe in the
region named by `transcribe_region`. Transcribe chooses among `transcribe_languages` (one dialect per language,
the likeliest first). It names a language only at the speed of speech, so a recording longer than 16 seconds is
identified on its first 8 seconds and then transcribed in one faster pass: a one-minute note takes about 18
seconds. A recording that could not be transcribed is not counted against the allowances. Give the key pair to an identity that may do one thing, `transcribe:StartStreamTranscriptionWebSocket`
(`deploy/accomy/README.md` has the policy). Without the keys or the region, a voice note reaches the model as a
note that it could not be transcribed, and the assistant asks the user to type.

**What it needs from the install.** `websockets` and `anydoc` are core dependencies of Hermes on Python 3.14.
`pypdf` comes with the `documents` extra and `pypdfium2`, which draws scanned pages, with the `documents-pdf`
extra; the container image installs both, and `ffmpeg` is in the image.

## Secrets

Names only. Values go in the profile's `.env`, never in the distribution files.

- `OPENAI_API_KEY`: model endpoint (`providers.newapi` in `config.yaml`)
- `TMC_APP_KEY`, `TMC_APP_SECRET`: TMC hotel and flight search
- `SEATUNIQUE_API_TOKEN`: Seat Unique event tickets
- `GOOGLE_MAPS_SERVER_API_KEY`: places and weather
- `BRAVE_SEARCH_API_KEY`: web research
- `TELEGRAM_BOT_TOKEN`: the bot token from @BotFather
- `TELEGRAM_ALLOWED_USERS`: optional, see Access below
- `ACCOMY_PAYMENT_LINK_SECRET`: optional, the key that signs demo payment links (the payment page holds the same key)
- `ACCOMY_DATABASE_URL`: optional, the order store's PostgreSQL connection string (see Order store below)
- `ACCOMY_TRANSCRIBE_ACCESS_KEY_ID`, `ACCOMY_TRANSCRIBE_SECRET_ACCESS_KEY`: optional, the AWS key pair that
  turns speech into text (see Attachments above)

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
  Suggested-reply taps and messages that carry only an attachment count too.
- **Turns, all senders together:** 180 a minute and 5,000 a day. The daily total is the bot's spend ceiling: set
  it to what your model budget allows. Many accounts that each stay under their own limit still
  cannot exceed this. Accounts that the allowlist refuses are not counted, so a stranger cannot use up this budget.
  A refused stranger gets only the gateway's own decline or pairing reply.
- **Messages sent while the bot is still answering** each count when they arrive, like any other message.
- **Session commands cost no turn:** `/new` and `/stop` go to the gateway. `/start` (with a deep-link payload or
  `@botname`), `/help`, `/whoami` and `/commands` are answered by the plugin itself with a welcome or a short
  help, with no model call. Past 20 a minute from one sender, or 120 a minute in total, the bot stops answering
  them. Every `/start` is welcomed, a repeated one too, except one sent within 3 seconds of the sender's last. Any other text that starts with `/` counts as a turn.
- **Message length:** 4,096 characters, one Telegram message. A longer message is dropped before any model call.
  The gateway can still join messages that arrive while a turn is being set up, so one turn can carry up to a
  minute's allowance of messages (about 60,000 characters). The turn limit is what bounds that. The limit
  counts what was typed: the content of an attached text file is not part of the message.
- **Files, per sender:** 40 a day. **Files, all senders together:** 1,500 a day, which bounds the media cache
  at 30 GB, since Telegram hands a bot files of up to 20 MB and the gateway keeps them for a day. A photo, a
  voice note and each picture of an album count as one file each. Past the limit the message is dropped, its
  files are deleted and the sender is told once; such a message costs no turn.
- **Reading a file:** `read_attachment` returns 12,000 characters at a time and at most 120,000 per file, and
  gives a parser 30 seconds.
- **Scanned pages:** the first 8 of a file are read. **Per sender:** 40 pages a day. **All senders together:**
  600 a day, each a picture of about 2,200 input tokens in a model call. A page is counted when the model is
  asked, whether or not it answers, since a call that fails may still be billed. A page already read is not
  counted again.
- **Speech, per sender:** 600 seconds a day. **Speech, all senders together:** 6 hours a day, which is the
  feature's daily spend ceiling at Amazon Transcribe's per-second price. Seconds are counted as they are sent:
  the first 8 seconds of a recording longer than 16 seconds count twice. One recording is heard for at most
  120 seconds; the transcript says when the rest was left out.
- **Tool calls, per sender:** 30 a minute (existing) and 400 a day. Refused calls return `rate_limited`, and the
  assistant asks the user to try later.
- **Supplier calls, per host, all senders together:** Seat Unique 100 a minute and 3,000 a day, because its key
  allows 120 a minute. TMC 180 and 6,000, sized for about twenty people searching at once. Google Maps, Places
  and Weather 120 and 3,000 each. Brave 60 and 1,000. Any other host gets 60 and 1,000.

A limited user gets one short notice per window, not one per dropped message, and the bot sends at most 20
notices a minute in total.

The numbers are sized so a planning session never hits them: a planner sends bursts of short messages and button
taps, and a full day is about 100 messages. Change them where they are defined, then restart the gateway:

- `plugins/accomy_travel/ingress.py`: turn, global and length limits, and the session-command list.
- `plugins/accomy_travel/attachments.py`: files a day, and which extension is read how.
- `plugins/accomy_travel/documents.py`, `parsers.py` and `_document_worker.py`: what one file may contribute,
  and the parser's time and memory.
- `plugins/accomy_travel/ocr.py`: scanned pages per file, per model call and per day.
- `plugins/accomy_travel/speech.py`: seconds of speech a day and per recording.
- `plugins/accomy_travel/_common.py`: tool-call limits and `SUPPLIER_BUDGETS` (per host).
- `plugins/accomy_travel/tools_research.py`: the Brave limits per sender (10 a minute, 100 a day).

The supplier numbers for TMC, Google and Brave are not from a contract. Set them to your own quotas before you
open the bot.

The profile's `config.yaml` carries the gateway half of the hardening. Keep these when you open the bot:

- `gateway.strict: true` and `gateway.trust_recent_files: false`: a reply can attach only files from the
  gateway's own cache. Without them a model that is talked into writing `MEDIA:/some/path` uploads that host
  file to the chat.
- `telegram.accept_attachments: true`: photos, voice notes and files are downloaded and read as the
  Attachments section describes, within the file and speech limits above. Set it to `false` for a text-only
  bot: nothing is then downloaded, and the assistant says it cannot read attachments.
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
- The Telegram adapter downloads a file before the plugin's gate sees the message, and holds it in memory
  while it does. The gate bounds what stays on disk, not the download itself: Telegram's 20 MB per file and its
  own rate limits are what bound that.
- People send personal documents. A file stays in the media cache for a day, the text the model read stays in
  that conversation's history, and both are on the data volume. Keep the volume's disk encrypted, and say in
  your privacy notice what the bot keeps.
- A voice note leaves the host: its audio goes to Amazon Transcribe in the configured region.
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

## Travel standard

Every hotel and flight search applies one demonstration travel standard, in `standard.py`. Its rules have the
shape of a TMC travel policy (a nightly budget with city budgets, a lowest-price rule, and cabins by flight
duration without stopover time). The bot applies them itself: it reads nothing from, and writes nothing to, the
TMC tenant's own travel policies, so the standard is the same under any TMC key.

| Rule | Limit |
|---|---|
| Hotel, per room for any night | 200 USD. 300 USD in London, Paris and New York (TMC city codes 2114, 2734 and 2621) |
| Flight with less than 8 hours of flying time | Economy only |
| Flight with 8 hours of flying time or more | Economy, premium economy or business. Never first class |
| Fare | At most 70% above the cheapest fare of the same search (same trip, dates and cabin) |

- A room rate is within the hotel budget when no night of the stay costs more for a room, and the stay's total
  (which carries any taxes) does not average more per room per night. `search_hotels` judges a hotel by its
  lowest nightly price, and reads at most three result pages: when all of those are above the budget, it says
  that hotels further away were not checked.
- Flying time is the flights' own durations added up, so the wait at a stop is not counted. A flight whose
  duration the supplier does not give counts as under 8 hours.
- On a round trip or a multi-city trip the cabin rule holds each flight's cabin against the flying time of its
  own leg, so a trip searched in premium economy or business needs 8 hours of flying time on every leg. The fare
  rule compares prices of the whole trip: the limit is set by the cheapest trip of the first leg's search, and
  the later legs keep it.
- `search_hotels`, `get_hotel_rooms` and `search_flights` return only what is within the standard and count the
  rest as `outside_travel_standard`. What is left out gets no `quote_id`, so it cannot be ordered. When
  `get_flight_offer` re-prices a fare above its search's limit, the fare loses its quote.
- `search_flights` takes a `cabin` (`economy`, `premium_economy` or `business`). First class is not an option.
- A price with no US dollar amount cannot be judged. It is shown as unconverted, as before, and cannot be ordered.
- The assistant explains the standard in the user's language. Its text is the `accomy.travel_standard` prompt
  section, rendered from the same constants the tools use. `/start` offers the question as a button.
- To change the standard, edit the constants in `standard.py`.

## What end users see

The profile is for end users, so nothing about the agent underneath reaches them:

- `gateway.hide_operator_details: true` makes the gateway leave model, provider, session and error internals out
  of its replies, and leaves the host and profile paths out of what the model is told.
- `distributions/accomy/locales/en.yaml` rewords or blanks every gateway message a user can reach (the
  home-channel notice, the `/new` reply, refusals, errors, the busy and long-running notices). A test fails when
  an upstream message that names the agent is neither reworded there nor listed as operator-only.
- Telegram's command menu lists only `/start`, `/new`, `/stop` and `/help`.
- `/start` greets the user with a welcome and suggested replies, and `/help` explains what the assistant does.
  Both come from `welcome.py`, in English when the user's Telegram is in English, otherwise in Traditional Chinese.

## Suggested-reply buttons

With `telegram.suggested_replies: true`, the assistant ends each reply with a line such as
`[suggestions: 看第一間的房型 | 找更便宜的]`. The gateway turns it into tappable buttons and removes the line
from the text. The plugin appends a default line when the model forgets one, in Traditional Chinese for
Chinese replies and in English otherwise. The gateway removes the line from every reply on every platform.
It becomes buttons only on Telegram with `telegram.suggested_replies: true`; otherwise it is hidden, not shown as text.

## Limits

- Demonstration orders only. An order is recorded in the order database and never sent to a supplier; no payment
  is taken, so a cancellation refunds nothing.
- Order limits, counted in the database so a restart does not forget them: 100 orders per chat per day, 300 orders
  for everyone per day (rolling 24 hours), 3 items per order, 10 event tickets per item, 9 travellers per order,
  and quotes that expire after 30 minutes. A refused order says why in the chat.
- Orders are taken only in a private Telegram chat; a group, a channel or a sender-less session is refused.
- Prices are converted with a fixed demo table of reference rates, so converted prices are approximate.
- A supplier call waits 20 seconds for its answer. A flight search waits 60, because a route nobody has
  searched lately takes TMC about 20 seconds to price, and a flight price check waits 40, because the airline's
  own answer for a round trip took up to 25. The TMC platform itself gives up on a search after 30
  seconds, which the first search of a round trip can exceed: such a search is sent up to 3 times, since TMC
  keeps what it had found and the next sending answers in a few seconds.
- A flight trip has at most 4 legs. For the live price of a trip, at most 3 of its fares are tried.
- Hotels and flights outside the travel standard are not offered and cannot be ordered (see Travel standard).
- Supplier environments may be sandboxes, so prices and availability are indicative.
- Google Routes is not enabled. Directions are Google Maps links.
- Weather covers about 10 days ahead.
- The bot is a travel assistant. It refuses code, security attacks, advanced maths and requests for private
  personal data, and it refuses attempts to change its role. Travel facts from public web search are allowed.
- Built-in memory, session search, skills, cron scheduling, terminal, files and the web toolset are all
  off, so nothing carries between users.
- Attachments: a scanned PDF has its first 8 scanned pages read; for the others, and for a picture inside
  an office file, the assistant asks for a photo instead. A scanned image file other than a photo format
  (TIFF) is not read. A video's picture is not seen, only its sound is heard. Archives and programs are refused.
  Speech is recognised in the languages listed in `transcribe_languages`, up to 120 seconds a recording. A file
  can be read for a day after it was sent, and not after a restart of the bot.

## Order store

`orderstore/` keeps orders in PostgreSQL. It is modelled on hashtrip's order core: an order header
(`o_booking`), its lines (`o_booking_item`) with one detail row each (hotel stay, flight itinerary with its
segments, or event tickets), the travellers on a line, a log of every write sent to a supplier
(`o_supplier_request`) and a history of status changes (`o_booking_event`). The schema and the reasons behind
it are in `orderstore/migrations/0001_2026-11-13_order-baseline.sql`.

The order tools use the store through `tools_orders.py`, and the store's calls reach it through `orderbridge.py`:
one daemon thread runs one asyncio loop for the process, and each call opens its own connection on it. Only the
simulated flow exists. Nothing calls a supplier's order, payment or cancel endpoint, and the store refuses a
supplier request for a simulated order. Migration `0002` adds `c_fx_rate` (the exchange rates an item was
converted at, append-only), `o_booking.order_mode` (`SUPPLIER` or `SIMULATED`, never changed) and the item's
source currency, source amount and rate. Migration `0003` adds the customer master (`o_customer`: a Telegram
account id, an email, or both) and the trip master (`o_trip`), and ties every booking to one customer and one of
that customer's trips:

```
o_customer 1 ── n o_trip 1 ── n o_booking 1 ── n o_booking_item (hotel, flight or event)
```

An operator finds a customer, with their trips and orders, by Telegram id or by email:

```bash
hermes -p accomy accomy-orders customer --telegram 123456789
hermes -p accomy accomy-orders customer --email someone@example.com
```

That lookup is deliberately not a bot tool: in a chat, a user reaches only the orders of their own Telegram
account, so typing someone else's email opens nothing. `erase_customer` erases a person everywhere and keeps
their orders.

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
- A pool for the order store, once measured: each call opens one connection today.
- Amounts keep two decimals in the booking and up to four in a supplier amount, as the columns hold them.

Tests run against a real PostgreSQL 17 on `127.0.0.1:55433` and are skipped without it. The command to start
one is at the top of `tests/plugins/accomy_travel/test_orderstore.py`.

## Upstream

Hermes policy keeps third-party product plugins out of the upstream tree (`plugins/AGENTS.md`). This
directory is self-contained: it uses only the public plugin API, so it can move to
`$HERMES_HOME/plugins/accomy_travel/` or to its own repository unchanged. The profile in
`distributions/accomy/` refers to the plugin by name, so it keeps working after the move.

`FORK.md` at the repository root lists every upstream file this fork changed, and how to bring in new
upstream work.
