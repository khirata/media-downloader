# Scheduling & Reservation — Design

Status: **proposal**, not yet implemented. Revision 13.
Researched and written 2026-09-04. All API observations were verified live on
that date.

**Terminology.** The cron origin made this look like *scheduling*, but the thing
being built is 予約録画 — a **reservation**. You reserve a programme; the system
decides when to fetch it. "Schedule" from here on means only the old crontab.
The service is the **reservation service** (one container, `reservation`),
never "the scheduler"; its polling loop is **the poller**.

Revision 13 splits the programme worker **three ways** — one per source, plus a
UI/aggregator (§4.1a). Revision 8's "not yet" mis-scored blast radius as a
future trigger when TVer's undocumented API makes it a present one. All four
containers share one SQLite file on the one host; the ledger's source-namespaced
keys mean they never contend for the same rows, and per-worker files remain the
multi-host fallback.

Revision 12 settled the wake-up policy (§4.8): the SQS consumers long-poll and
are always awake, so only the resolver *sweep* needs an interval — **1 hour,
with jitter**. Verified that Radiko honours `If-None-Match`/`If-Modified-Since`
with a 0-byte 304, so frequency is not bandwidth-bound.

Revision 11 set the publish grace to **2 hours** by default, and scopes it to
Radiko only — らじる and TVer are gated on a flag that is read rather than
computed, so a grace would delay them for nothing (§5.6).

Revision 10 settled **when** a multi-part episode is publishable (§5.6): on its
**last** part, not its first. Gating on the first part would let a mid-broadcast
tick publish a two-part episode of a four-part show — which downloads,
concatenates and passes the truncation check, producing a plausible wrong file.
Also records that Radiko publishes timefree availability flags (`ts_*_ng`,
`failed_record`) with real signal — ~15% non-zero across 4,169 sampled
programmes — used to warn in preview rather than to gate.

Revision 9 traced the multi-part Radiko publish end to end (§5.5) and finds two
things earlier revisions had wrong: the Lambda groups Radiko URLs **by station
across the whole request**, so two episodes batched into one call would
concatenate into a single file — hence **one request per episode, always**; and
the Lambda builds its payload field by field, so `expect_title` would have been
silently dropped and the §4.6 check would never have run.

Revision 8 adopted the review's framing that this is **a new worker for the
programme**, not a control plane (§2.4); gives each source its own programme
queue while keeping one process for now, with the split trigger and the
already-partitioned ledger documented (§4.1a); and settles TVer season
selection against three real series — index 0, confirmed as 本編, stored as an
id, modelled as a list so other sections are a later checkbox (§3.3).

Revision 7 reframed the dispatch model (§2.4): there are **two request types**
— an *episode* URL, which is what runs today, and a *programme* URL, which
denotes a set including episodes not yet published. That is a type distinction
rather than an intent ambiguity, so `api-gw` gains one URL class and routes
programme requests to the reservation service (§4.3). It also demotes
"observe every download" to an explicitly optional bonus (§2.4a) — revision 5
over-claimed it as load-bearing when the workers already dedup.

Revision 6 restructured the problem statement (§1). It previously framed
everything as a defect of the crontab, which missed the larger problem: for
らじる★らじる and TVer there is no crontab at all, and **a person is doing the
polling by hand** — an errand that recurs forever, per series, and silently
loses episodes when the window closes. §1.2 states it, and notes it shares a
root cause with problem 4: both act at a moment when the needed information does
not exist yet.

Revision 5 dropped the Postgres container (§5.1): the workload is ~3 MB after a
decade and 96 write bursts a day, so state is a **SQLite file** and the control
plane collapses to **one container** — with the schema kept dialect-portable so
Postgres stays a swap rather than a rewrite.

Revision 4 corrected the transport (§4.3): worker status travels by
`sqs:SendMessage` to a dedicated queue, **not** through SNS, because publishing
to the dispatcher topic would let the least-trusted component in the system
cause downloads.

Revision 3 responded to seven review points: `force` is one-shot and never
reservable (§2.5); Radiko programmes split into **differently-titled parts**
(§3.1, §5.2); TVer series carry **multiple seasons and live entries** (§3.3);
the `region` field is dropped (§4.2); the **dispatcher fans out to the
reservation service** so it observes every download (§4.1); the data model is
now programme → episode → part (§5); and both inbound links are **SQS**, on two
queues with different trust levels, so no component ever needs an inbound
connection to the control-plane host (§4.3).

---

## 1. The problem

There are two problems here, not one, and they are worth keeping apart because
they have different shapes. Where cron is used, it does the wrong thing. Where
cron cannot be used at all, **a person is doing the job by hand** — and that is
the more tiring of the two.

### 1.1 Where cron is used, it does the wrong thing

Today a recurring recording is a `crontab` line:

```text
10 01 * * 0 d=$(TZ=Asia/Tokyo date -d 'today' -I | tr -d '-') && ~/bin/radiko-download.py \
  --station FMJ --desc "TOKIO HOT 100" ${d}130000 ${d}140000 ${d}150000 ${d}160000 \
  >> ~/logs/radiko-download.log 2>&1
```

That line works, and it should keep working. But it encodes a *wall-clock
schedule*, not a *programme*, and everything wrong with it follows from that:

| # | Problem | Consequence |
| - | ------- | ----------- |
| 1 | The hour is hard-coded | The broadcaster moves the show; cron silently records the wrong hour, or a 拡大版 gets truncated. Nothing reports it. |
| 2 | Multi-part shows are hand-enumerated | `${d}130000 ${d}140000 …`. A fifth part next week means editing the crontab. |
| 3 | A missed firing is lost | Host asleep at 01:10 Sunday → no recording, even though Radiko timefree keeps the programme for another 7 days. |
| 4 | **The schedule is decided before broadcast** | The crontab commits to 13:00–17:00 the moment it is written. Radiko corrects its table *after* the fact — a show that ran long, a 特番 that preempted it — and the frozen timestamps are then simply wrong. |
| 5 | Schedules live in one host's `crontab -e` | Unversioned, invisible, unreviewable, lost with the machine, and editable only over SSH. |
| 6 | `date -d` is a GNU-ism | Breaks on macOS/BSD hosts. |

Problem 4 is the one that matters most and the hardest to notice, because it
fails by producing a **plausible file** rather than an error.

### 1.2 Where cron cannot be used, a person is the scheduler

For らじる★らじる and TVer there is no crontab line to get wrong, because there
is nothing for cron to construct: **the episode URL does not exist until the
episode is published.** 聞き逃し has no timestamp addressing at all, and a TVer
episode id is minted when the episode is posted.

So the automation is a human. The actual current procedure for following a TVer
series is: open `tver.jp/series/…`, look for an episode you have not seen, copy
its URL, publish it — and remember to do that again next week.

| # | Problem | Consequence |
| - | ------- | ----------- |
| 7 | **You are the poller** | Visiting a series page to check for a new episode is a scheduled task being executed by a person. It recurs forever, per series, and no part of it needs judgement. |
| 8 | **The manual poll has a deadline** | TVer keeps an episode about 7 days; らじる 聞き逃し is per-episode but often about a week. Forget for eight days and the episode is simply gone — permanently, and with nothing to tell you it happened. |
| 9 | It scales with the number of series | Each series added is another recurring errand. The cost of following one more show is paid weekly, by hand, which is what keeps the list of followed shows artificially short. |

> **Problems 7–9 have the same root cause as problem 4.** In both cases you are
> forced to act at a moment when the information you need does not exist yet:
> cron commits to a start time *before* broadcast, and a person visits a series
> page *before* the episode is published. Both are guessing, and both are
> guessing because they are looking too early.
>
> That is why one mechanism answers both — **look afterwards, on a window**
> (§2.2). The manual poll is not a workaround to be tolerated; it is a
> specification of the loop to be automated.

### 1.3 What the fix is

Move from *scheduling* to *reserving* — 予約録画. The user declares **what**
they want recorded, and the system decides **when** to fetch it and **which
URL** that turns out to be, as late as it possibly can.

For §1.1 that means the crontab stops guessing. For §1.2 it means the errand
stops being yours.

---

## 2. The observations that shape the design

### 2.1 The reservation service is a publisher — and `url-publisher` stays a URL pusher

`api-gw` + the Chrome extension are a **general-purpose URL publisher**: give
them a URL, they classify it and drop it on SNS. That is worth protecting.

So the reservation service does not teach the Lambda about reservations. Its
only interaction with the dispatch path is the one a human already has: it
POSTs URLs to `/publish`.

> **What changes in `api-gw`:** one new URL class (programme URLs get their own
> type, §2.4/§4.3) and one field forwarded (`expect_title`, §5.5). No new
> route, no reservation logic, no knowledge of schedules; it keeps doing the
> one thing it does, over one more URL shape and one more field. Everything
> else in §4 is new *subscriptions*.

### 2.2 Poll a window; never fire at an instant

Every reservable source is **on-demand with a multi-day availability window**:
Radiko timefree 7 days; らじる per-episode `expires`; TVer per-episode `endAt`.

So the poller never needs to fire at a precise moment. It wakes periodically,
asks *"which reserved episodes are now available and not yet fetched?"* — where
*available* means **every part of the episode has aired**, not merely the first
(§5.6) — and publishes those. Problem 3 disappears: a missed tick, a reboot, a week's
downtime are all recovered on the next scan. Catch-up is what the normal loop
does. It also gives **free bounded retry** — a failed fetch is simply not
marked done, and the window is the natural deadline.

This loop is also, literally, the manual procedure of §1.2 written down: *visit
the page, look for something new, fetch it, remember you did*. The difference is
that a machine can run it every fifteen minutes without getting bored, and the
window means it does not have to be punctual to be correct — which is exactly
where the human version fails (problem 8).

### 2.3 Bind the episode URL as late as possible

A programme table is **mutable history**. Radiko revises entries after broadcast
when a show overran or a 特番 displaced it, so any URL derived from it has a
shelf life.

**Polling already fixes the worst of it.** Cron resolves *before* broadcast and
is always guessing; the poller resolves *after*, so the table it reads already
reflects what aired. **But that is not late enough**, because a published
message can sit in SQS behind other work, be redelivered after a visibility
timeout, or wait for an offline worker. Hours can pass.

| Source | Late binding at download time |
| ------ | ----------------------------- |
| Radiko | **New.** The worker re-reads the station's programme XML, confirms each `ft` still carries the expected title, and re-derives the part set if boundaries moved (§4.6). |
| らじる | **Already.** `download_radiru` calls `resolve_radiru_url` at download time; nothing is frozen at publish. |
| TVer | **Not needed.** An episode id is immutable once posted. |

### 2.4 There are two kinds of request, and the dispatcher already sorts URLs

*(From the review: "There are two types of requests to the SNS. One is the
episode URL, which is what currently runs. The other is the programme, or group
of episodes including future ones.")*

This is a better frame than revision 5's, and it dissolves an objection I had
raised. The distinction is not *what did the user mean* — it is **what the URL
denotes**:

| Request type | Denotes | Example | Routes to |
| ------------ | ------- | ------- | --------- |
| **Episode** | one downloadable thing that exists **now** | `radiko.jp/#!/ts/FMJ/20260906130000`, `tver.jp/episodes/epliwk4kpb`, `nhk.jp/p/rs/S/episode/re/E/`, any YouTube URL | a **download worker** — exactly as today |
| **Programme** | a **set**, including episodes not yet published | `tver.jp/series/srusndh59f`, `nhk.jp/p/rs/242V3Q87GK/` | a **programme worker** (§4.1a) |

> **The right mental model is a worker, not a control plane.** *(From the
> review: "I'm treating this as creating a new worker for the programme.")*
> That framing is better than revision 5's, and it is adopted throughout. A
> programme worker has the same shape as the two that exist — long-poll a
> queue, do one job, hold no cleverness — and its one job is **turning a
> programme request into episode requests**. Its output is the request type
> that already worked. The UI (§6) rides along with it; it is not a separate
> tier.

That is a **type** distinction, not an intent ambiguity, and sorting URLs by
type is precisely what `api-gw` already does. Teaching it that
`tver.jp/series/…` is a programme is the same kind of rule as
`tver.jp/episodes/…` being a TVer episode. It does not teach the Lambda about
reservations; it teaches it one more URL shape, which is its job (§4.3).

> **Correcting revision 5.** I argued the Lambda should *not* classify
> programme pages, because a series URL is ambiguous between "download now" and
> "follow forever", and because `nhk.jp/p/rs/<series>/` already means the
> former. The ambiguity argument does not survive this framing — a programme
> URL denotes a set, and fetching the currently-available members of that set
> is just the first thing you do with it. The regression argument was also
> overstated; see §4.3.

**Radiko has no programme URL**, which is worth stating because it shapes the
UI. A timefree URL addresses one hour; the `<prog>` entry's own `<url>` points
at the *broadcaster's* site (`j-wave.co.jp/original/tokiohot100/`, verified
2026-09-04) and its `master_id` is empty. So there is nothing publishable that
denotes "TOKIO HOT 100, ongoing". Radiko reservations are created in the UI
(§6) — which is where the programme grid lives anyway.

### 2.4a Recording every download — a bonus, not a foundation

Revision 5 made "the reservation service observes every download" a load-bearing
principle, justified by dedup across the manual/automatic boundary. **That was
over-claimed**, and the review is right to call it a bonus.

The workers already dedup: a re-published episode whose file is still in
`DOWNLOAD_DIR` is skipped by yt-dlp, `process_message` returns `"duplicate"`,
and the message is dropped. So a manual download that a reservation later covers
costs one wasted publish, not a duplicate file. Nothing breaks without
observation.

What it does buy is a **complete history in one place** — every download, manual
or reserved, with the worker that ran it — which makes the History view (§6)
worth opening and saves that wasted publish. Good, but optional.

So it is built as a **separate, deletable subscription** (§4.3) rather than
being wired into the core. Two consequences worth having: the loop hazard below
only exists if the bonus is enabled, and a deployment that does not want it
omits one Terraform resource rather than disabling a code path.

> **The loop hazard, and the rule that removes it.** If the service subscribes
> to episode types, its own publishes come back to it. That is only a loop if
> observing causes publishing, so:
>
> **Observation writes history and nothing else.** A publish happens only from
> (a) a reservation matching on a poll tick, or (b) an explicit human action. No
> inbound message ever triggers a publish. This is a design rule, enforced by
> having no code path from the SQS consumer to the publisher. History rows
> upsert by ledger key, so the service seeing its own publish return costs one
> idempotent write.
>
> A programme request is *not* an exception: it creates a reservation, and the
> reservation is what publishes, on the next tick.

### 2.5 `force` is a human action, one-shot, never reservable

`force: true` deletes the existing file before re-fetching. That is a fine
thing for a person to ask for once and a terrible thing to put on a timer.

* **Reservations have no `force` field.** It is absent from the model (§5.3),
  not merely unset — so it cannot be enabled by editing a record.
* **The poller never sets it.** Re-recording is what the ledger exists to
  prevent.
* **An observed `force` message is recorded as a one-shot manual download and
  never replayed.** It updates history so dedup stays correct, but it never
  creates or modifies a reservation, and the service never re-publishes it.
* **Re-queue from the UI republishes *without* `force`.** If a user genuinely
  wants to overwrite, that stays a deliberate publish through the normal path.

---

## 3. Source research

All sources were probed live on 2026-09-04. What follows is what came back.

### 3.1 Radiko — one episode is several differently-titled parts

`GET https://radiko.jp/v3/program/station/weekly/{STATION}.xml` — verified for
`FMJ`: HTTP 200, 758 KB, `<ttl>1800</ttl>`, `<date>` groups spanning
**20260828 → 20260910** — seven days back and six forward. One request covers
both the timefree window and upcoming programmes.

The review asked how the new design handles TOKIO HOT 100, which is four parts
with different titles. Here is what the table actually says, two consecutive
Sundays:

```text
20260906120000 -> 20260906130000  dur=3600  ADEKA KLEINE WUNDER
20260906130000 -> 20260906140000  dur=3600  SAISON CARD TOKIO HOT 100(PART1)
20260906140000 -> 20260906150000  dur=3600  SAISON CARD TOKIO HOT 100(PART2)
20260906150000 -> 20260906160000  dur=3600  SAISON CARD TOKIO HOT 100(PART3)
20260906160000 -> 20260906170000  dur=3600  SAISON CARD TOKIO HOT 100(PART4)
20260906170000 -> 20260906180000  dur=3600  OTSUKA CORPORATION SAUDE! SAUDADE…
```

Three things fall out of this, and together they answer the question:

1. **The parts share a stem and differ only by suffix.** A substring match on
   `TOKIO HOT 100` returns all four and nothing else. No per-part configuration
   is needed.
2. **The parts are contiguous** — each `to` equals the next `ft`. That is the
   grouping rule (§5.2): a maximal contiguous run of matching parts *is* one
   episode. The count is **discovered, not configured**, so a 拡大版 that adds a
   `PART5` is picked up automatically. This is problem 2 from §1 solved
   properly rather than by editing a crontab.
3. **The sponsor is in the title.** `SAISON CARD …` is a naming risk, not an
   observed change: sponsor prefixes on Japanese radio rotate with the
   sales cycle, and a match anchored on the full title would break silently
   when it does. The UI should steer users toward the distinctive core of a
   title (`TOKIO HOT 100`), and §7's heartbeat catches it if one slips through.

Publishing is unchanged: all four `ft` values go in **one** request, the Lambda
groups them by station into one SQS message, and `record_radiko` concatenates
them with ffmpeg exactly as it does today. **The existing multi-part path is
reused verbatim** — the reservation service only replaces the shell arithmetic
that enumerated the hours by hand.

For the filename, the reservation's `description` wins (it is what the user
typed). Where none is set, strip a trailing `(PART\d+)` / `（PART\d+）` from the
first part's title rather than using it raw.

Also verified 200: `/v3/station/list/{AREA}.xml`, and
`/v3/program/station/date/{YYYYMMDD}/{STATION}.xml` — already used by
`_fetch_radiko_title`, and the natural endpoint for the §4.6 check since it is
one day rather than fourteen. `/v3/api/program/search` returned **415** on every
shape tried; treat it as unavailable.

> **Day boundary.** Radiko's broadcast day runs 05:00–29:00. A "Sunday 25:00"
> programme sits in the `<date>20260906</date>` group with `ft=20260907010000`.
> Match on `<date>` + `ftl`; compute availability from `to`.
>
> **Resolution is not authorisation.** The programme XML answers fine from
> outside Japan — this research was done from a US host. Only the *download*
> needs regional access.

### 3.2 らじる★らじる — the missing piece

`https://www.nhk.or.jp/radio/config/config_web.xml` (200) advertises
`//api.nhk.jp/r8/pg/date/{service}/{area}/[YYYY-MM-DD].json`. Against
`area=130`: `r1` and `r3` return 200; `r2`, `fm`, `n1` return 400 — only two
services exist, agreeing with `_RADIRU_STATION_PATTERNS` having no `NHKR2`.
Date horizon: 200 from `2026-08-20` to `2026-09-11`. Roughly −15/+7 days.

**Finding 1 — the canonical URL is handed to you.** Each entry's
`about.canonical` is exactly the URL the worker already handles:
`https://www.nhk.jp/p/rs/242V3Q87GK/episode/re/K65RLNYQZ7/`.

**Finding 2 — a three-state availability signal, published ahead of
broadcast.** `about.audio[].detailedContentStatus.contentStatus`:

| State | Meaning |
| ----- | ------- |
| `about.audio` absent/empty | Never going to 聞き逃し. Do not let a reservation match it. |
| `contentStatus: "notyet"` | Announced, not yet fetchable — and `expires` is *already populated*. Keep polling. |
| `contentStatus: "ready"` | Fetch it now. |

Measured on the r3/130 listing for **2026-09-05, a future date** (fetched
09-04): 41 programmes, 30 with an `audio` entry, some already `ready`:

```text
音楽の泉 シューマンのピアノ協奏曲   listed 09-05 05:00  notyet  expires 2026-09-12
みんなのうた「ぼくのプルー」        listed 09-05 05:50  ready   expires 2026-10-03
マイあさ！ ▽ＮＨＫけさのニュース     listed 09-05 07:00  notyet  expires 2026-09-12
```

So a reservation can be **validated before the programme airs**. And the
already-`ready` rows are **rebroadcasts**: both みんなのうた entries are listed on
09-05 but their `broadcastEventId` is `r3-130-2026080175223` — the 1 August
airing. The listing date and the asset's broadcast can differ, so the ledger
key must come from the episode id, never the listing date.

> **The one-week rule is wrong as a hard assumption.** Both windows appear in
> the same day's data: the `notyet` rows expire 7 days after broadcast, the
> みんなのうた rebroadcasts run to 2026-10-03 — about two months. Read `expires`.

### 3.3 TVer — yes, and a series is messier than it looks

*(From the review: "TVer has a programme page like
`https://tver.jp/series/srusndh59f`. Are you able to parse and pick new
episodes?")*

**Yes.** Verified against that exact series on 2026-09-04:

```
POST /v2/api/platform_users/browser/create   → { platform_uid, platform_token }
GET  /service/api/v1/callSeriesSeasons/srusndh59f  → 5 seasons
GET  /service/api/v1/callSeasonEpisodes/{seasonID} → 18 entries total
```

But "follow the series" is the wrong unit, and the real data shows why:

```text
srusndh59f  Ｔシャツが乾くまで
  ss8ehuif2y  本編            3 entries   第９話, 第１話, + one live entry
  ssg4in0trk  サイドストーリー  3 entries   7.5話, 3.5話, 1.5話
  ssk9uny8nr  ダイジェスト      4 entries   10-minute digests
  ss6brvx81f  解説放送版        2 entries   【解説放送版】第９話, 第１話
  ssk3orcahd  ナビ・予告        6 entries   30-second trailers, ティザー
```

Three findings that change the design:

1. **Reserve a (series, season), not a series.** A naive series follow would
   fetch 10-minute digests, 30-second trailers, and 解説放送版 — which is the
   *same episode again* with audio description.
2. **`callSeasonEpisodes` returns a mixed list.** Of the 18 entries, 17 are
   `type: "episode"` and one is `type: "live"` — a DVR/simulcast entry for
   第１０話 with `startAt`/`onairStartAt`, **no `isAvailable`, and no VOD
   `endAt`**. A resolver must filter on `type == "episode"` first. The probe
   script written for this research crashed on exactly that
   (`KeyError: 'isAvailable'`), which makes it a test case rather than a
   footnote.
3. **Windows are per-episode.** 第９話 ends 2026-09-11; 第１話 ends 2026-10-16.
   Read `endAt` per entry; never assume seven days.

#### Which season is the main one?

*(From the review: "sections could differ for other programmes — focus on 本編
and optionally add other sections later, maybe in the UI.")* The sections do
differ. Three series, checked 2026-09-04:

```text
srusndh59f  [0] 本編   [1] サイドストーリー  [2] ダイジェスト  [3] 解説放送版  [4] ナビ・予告
srufa4d98z  [0] 本編   [1] メイキング        [2] ダイジェスト  [3] 解説放送版  [4] ナビ・予告
sry374aoat  [0] 本編
```

The *extra* sections vary — サイドストーリー in one, メイキング in the other —
and a simple programme has only the one. But **本編 is at index 0 in all three**,
which gives two independent signals rather than one:

* **Select index 0**, because a title is free text and a series could name its
  main section something else.
* **Check that it is titled `本編`**, as a sanity signal — if it is not, the
  series is unusual and the UI should say so rather than guessing silently.

Then **store the resolved `seasonID`**, not the rule that found it. The id is
the stable handle; order and title are presentation.

Model it as a **list from the start**, holding one element:

```jsonc
"selector": { "series": "srufa4d98z", "seasons": ["ssnfef8wr6"] }
```

Adding ダイジェスト later is then a UI checkbox and a second array element — no
schema change, no migration. That is the whole cost of taking the review's
"optionally add other sections" seriously now instead of later.

> **New-season detection belongs in §7.** Following a stored `seasonID` means a
> season added afterwards is silently not followed. Comparing the live season
> list against the covered ids on each tick is nearly free, and *"this series
> now has a section you are not following"* is exactly the silent-failure class
> §7 exists for. It also covers the case these three samples cannot rule out:
> a series that uses seasons chronologically (シーズン1, シーズン2) rather than
> as content categories.

```jsonc
{ "type": "episode",
  "content": { "id": "epliwk4kpb",        // → tver.jp/episodes/epliwk4kpb
    "seriesID": "srusndh59f", "seriesTitle": "Ｔシャツが乾くまで",
    "title": "第９話 言えなかったこと", "broadcastDateLabel": "9月4日(金)放送分",
    "duration": 2656, "isAvailable": true, "endAt": 1789131599 } }
```

> The `platform_uid`/`platform_token` pair is anonymous and cheap to mint, but
> undocumented and unversioned. Treat 4xx as *"re-mint once, then back off"*,
> and keep the TVer resolver isolated so a breakage cannot take the poller down.

### 3.4 YouTube — explicitly unchanged

YouTube is **not a reservable source**, and must keep working exactly as it
does today: the Lambda routes `youtube.com`/`youtu.be` to `{type: 'youtube'}`;
the video queue filters `type = ["tver", "youtube"]` with
`filter_policy_scope = "MessageBody"`; `record_video` is source-agnostic and
honours `force`.

Three commitments:

1. **No resolver claims a YouTube URL.** One reaching `/publish` came from a
   human.
2. **No message field changes.** Revision 2's `region` field is dropped (§4.2),
   and status travels off-topic entirely (§4.3), so a YouTube message on the
   dispatcher topic is byte-identical to today's.
3. **`force` still works** — and per §2.5 it stays a human, one-shot action.

> If YouTube ever should be reservable, it is the easiest of the four: every
> channel publishes `youtube.com/feeds/videos.xml?channel_id=…`, a plain RSS
> feed that slots into the same catalogue abstraction (§4.7). Not built now.

### 3.5 What each source can be reserved by

| Source | Reserve by | Availability | Expiry | Parts per episode |
| ------ | ---------- | ------------ | ------ | --- |
| Radiko | station + title match | **last** part's `to` + 2 h grace (§5.6) | `ft` + 7 days | **1..n**, contiguous |
| らじる | `radioSeriesId` | `contentStatus == "ready"` | `audio[].expires` | 1 |
| TVer | `seriesID` + `seasonID`(s) | `type=="episode"` and `isAvailable` | `endAt` | 1 |
| YouTube | *not reservable* | — | — | — |

---

## 4. Architecture

### 4.1 One entry point, two request types

All requests go to `api-gw`, which classifies the URL and publishes to SNS.
Subscriptions then sort by type — which is what the topic already does, with one
new destination:

| Type on the topic | Queue | Consumer |
| ----------------- | ----- | -------- |
| `radiko` | radio | radio download worker |
| `tver`, `youtube` | video | video download worker |
| **`tver_series`** *(new)* | **programme-tver** | the TVer resolver (§4.1a) |
| **`radiru_series`** *(new)* | **programme-radiru** | the らじる resolver (§4.1a) |
| *(the first three again)* | **observe** *(optional, §2.4a)* | `reservations-ui` |

The first four rows are the system; the last is the bonus. A deployment that
does not want a complete download history simply does not create that
subscription, and nothing else changes.

```mermaid
graph TD
    classDef client   fill:#f9d0c4,stroke:#333,stroke-width:2px,color:#000;
    classDef router   fill:#d4e6f1,stroke:#333,stroke-width:2px,color:#000;
    classDef queue    fill:#d5f5e3,stroke:#333,stroke-width:2px,color:#000;
    classDef worker   fill:#fcf3cf,stroke:#333,stroke-width:2px,color:#000;
    classDef prog     fill:#fadbd8,stroke:#333,stroke-width:2px,color:#000;
    classDef storage  fill:#e8daef,stroke:#333,stroke-width:2px,color:#000;
    classDef external fill:#eaeded,stroke:#666,stroke-width:2px,stroke-dasharray:5 3,color:#000;

    subgraph Clients ["📤 Publishers"]
        EXT["Chrome extension / curl / cron"]:::client
    end

    subgraph Dispatcher ["🚦 Central router (api-gw/) — unchanged"]
        API["API Gateway + Lambda<br/>classifies URLs"]:::router
        SNS["SNS dispatcher"]:::router
    end

    subgraph Queues ["📥 Message queues"]
        QR["radio SQS"]:::queue
        QV["video SQS"]:::queue
        QP["programme-tver SQS"]:::queue
        QN["programme-radiru SQS"]:::queue
        QO["observe SQS<br/>(optional)"]:::queue
        QS["status SQS"]:::queue
    end

    subgraph Prog ["🗓️ Programme workers (new) — one host"]
        R1["programme-radiko"]:::prog
        R2["programme-radiru"]:::prog
        R3["programme-tver"]:::prog
        UI["reservations-ui<br/>UI + REST API"]:::prog
        DB[("reservations.sqlite")]:::storage
    end

    subgraph Down ["⚙️ Download workers — anywhere"]
        W1["radio worker · JP"]:::worker
        W2["video worker"]:::worker
    end

    subgraph Ext ["🌐 Not ours — the broadcasters' own metadata APIs"]
        CAT["radiko.jp/v3 · weekly programme XML<br/>api.nhk.jp/r8 · らじる day listings<br/>platform-api.tver.jp · series and seasons"]:::external
    end

    EXT -->|"episode or programme URL"| API
    API --> SNS
    SNS -->|"radiko"| QR
    SNS -->|"tver / youtube"| QV
    SNS -->|"tver_series"| QP
    SNS -->|"radiru_series"| QN
    SNS -.->|"radiko / tver / youtube"| QO

    QR --> W1
    QV --> W2
    QP -->|"long poll"| R3
    QN -->|"long poll"| R2
    QO -.->|"long poll"| UI
    W1 & W2 -->|"SendMessage"| QS
    QS -->|"long poll"| UI

    R1 & R2 & R3 -.->|"GET, conditional"| CAT
    R1 & R2 & R3 ==>|"POST /publish (episode URLs)"| API
    R1 & R2 & R3 <--> DB
    UI <--> DB
```

Reading it: the **dashed grey box** is the only thing in the picture that is not
ours — the broadcasters' own programme metadata, read over plain HTTP and never
written to. Everything a programme worker knows about what is on the air comes
from there (§3), and because those reads answer from anywhere (§3.1) the
programme workers can sit on one host while the download workers do not.

The **heavy arrow** is the point of the whole design: a programme worker's
output is a `/publish` call carrying episode URLs — the same request a human
makes, so nothing downstream can tell the difference.

Note what a programme worker publishes: **episode URLs**. It converts a
programme request into episode requests, which is the whole job. §4.1a splits
that job three ways, one container per source.

### 4.1a One programme worker, or three?

*(From the review: "evaluate single reservation worker vs three — TVer, らじる
and Radiko. I think wake-up control and maintenance are easier with separate
workers.")*

**Three. The review is right, and revision 8's "not yet" was wrong** — not
because the arguments changed, but because I mis-scored one of them. Here is
the evaluation.

#### The case for splitting

| | One worker | Three workers |
| --- | --- | --- |
| **Blast radius** | A hung fetch, a leak, or an exception outside the per-pass `try/except` degrades all three sources | A TVer failure cannot stop Radiko recordings |
| **Deploy granularity** | Fixing the fragile resolver restarts the stable ones mid-sweep | Rebuild and restart one container |
| **Wake-up control** | Per-source intervals need a scheduler *inside* the process | An env var per compose service |
| **Log legibility** | Three sources interleaved | `docker compose logs programme-tver` |
| **Shape of the repo** | A new kind of component | Identical to `radiko-downloader` / `tver-downloader` |
| **Shared state** | Trivial — one process, one file | Needs an answer (below) |
| **Container count** | 1 | 4, counting the UI |

**Blast radius is the argument that decides it, and I under-weighted it.**
Revision 8 filed it as a future trigger. It is not: §3.3 already records that
TVer's `platform_uid` flow is *undocumented and unversioned*, and API drift is
the top operational risk in this repo — `ensure_yt_dlp_current` exists for
exactly that reason. The fragile resolver is fragile **today**, so isolating it
is a present-tense need, not a contingency. Process isolation is also a
categorically stronger guarantee than a `try/except`, which does nothing for a
hung socket or a leak.

**Wake-up control is a real second-order win**, and it fits the review's
framing. With three services the interval is `TICK_INTERVAL` in each `.env` —
configuration, not code — which is how everything else in this repo is
configured. It also gives restart granularity while tuning: changing TVer's
interval no longer interrupts a Radiko sweep.

#### The cost, and how it is paid

**Shared state is the only real one.** Three workers plus a UI all need
reservations, the ledger and health. The answer keeps §5.1's conclusion intact:

> **One SQLite file, shared by all four containers on the one host.** The write
> rate is what made SQLite right in the first place — 96 bursts a day across
> *all* sources (§5.1) — and splitting the writers across processes does not
> change that. WAL mode plus a `busy_timeout` handles four occasional writers
> exactly as it handled four threads.

Two properties make this comfortable rather than merely workable:

* **The ledger is already partitioned by source** (`radiko:…`, `radiru:…`,
  `tver:…`, §5.4), so the workers never contend for the same rows — only, very
  occasionally, for the write lock.
* **Per-worker files remain the fallback.** If the workers ever move to
  separate hosts, that same namespacing means each can own its own file with
  no schema change and no migration. The shared file is a convenience, not a
  coupling.

The constraint from §5.1 tightens slightly and should be stated: all four
containers must be on **one host, sharing a local-filesystem volume**. Not a
NAS mount.

#### What the four containers are

| Container | Inbound queue | Tick | Job |
| --------- | ------------- | ---- | --- |
| `programme-radiko` | *(none — Radiko has no programme URL, §2.4)* | ✓ | resolve Radiko reservations from the weekly XML |
| `programme-radiru` | `radiru_series` | ✓ | follow らじる series |
| `programme-tver` | `tver_series` | ✓ | follow TVer (series, season) |
| `reservations-ui` | `status`, `observe` | — | the UI and REST API, plus the two **cross-source** consumers (§4.3) |

Putting the status and observe consumers in the UI container is not
arbitrary: both carry messages spanning every source, so they belong with the
component that already holds the aggregate view. It also means the three
resolvers have exactly one shape — read a catalogue, publish episode URLs —
and nothing else.

Note the asymmetry: `programme-radiko` has a tick but no queue, because there
is no Radiko programme URL to publish. That is a property of the source, not a
design wart.

#### The thing that would make maintenance *worse*, if neglected

Three workers become three codebases unless the common parts stay common. The
publisher, the ledger DAL, the title matcher (§5.5), the health writer and the
episode/part model all belong in **`shared/`**, next to `worker_common.py` —
which is the pattern this repo already uses for exactly this reason.

Split the *sources*, share the *machinery*. A resolver should be a small module
that turns a fetched catalogue into `list[Episode]` (§4.7) and nothing more; if
one starts growing its own publisher or its own ledger access, the split has
begun costing what it was meant to save.

### 4.2 No `region` field

*(From the review: "Workers are placed in the right region according to
download type. YouTube could be the only worker placed in both regions. A new
`region` field may not be required at this time." — agreed; revision 2's
proposal is withdrawn.)*

Regional need already follows from type, so **type-based routing is region
routing**, and nothing new is needed.

The single case that strains it is the one the review names: a video worker
outside Japan for YouTube, sharing the video queue with TVer. SQS delivers to
exactly one consumer, so the non-JP worker could win a TVer job it cannot
serve. If that day comes, the fix uses the mechanism already in place —
**split `youtube` onto its own queue** with `filter_policy = {type: ["youtube"]}`
and narrow the video queue to `["tver"]`. A Terraform change, no message field,
no Lambda change, nothing to migrate.

Recorded here so the trap is visible before someone deploys the second worker.

### 4.3 The transport: SQS in, HTTP out, no inbound connections

*(From the review: "How do you plan to establish the connection from dispatcher
to the reservation system? This is not a real-time system, so the communication
can be another SQS message — and the same for worker status, as workers could
run in different regions.")*

Agreed on both counts, and it is the only transport that survives §4.5's
distributed workers. Neither link is real-time: the dispatcher echo feeds a
ledger, and a status message updates a row. Minutes of lag cost nothing, and
**no component ever needs an inbound connection to the control-plane host** —
which is what makes a home-hosted control plane workable at all.

Revision 3 said workers would publish status *through SNS*. That was wrong, and
checking the Terraform shows why: a worker's IAM user today holds exactly

```hcl
Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
Resource = aws_sqs_queue.radiko_queue.arn
```

Publishing to the dispatcher topic would require `sns:Publish` on it — and a
worker holding that could publish `{"type": "radiko", …}` and cause arbitrary
downloads. The worker is the **least-trusted component in the system**: it runs
yt-dlp against remote content, in a container, on a host that may not be the
user's. It should not be able to make the system fetch things.

So the inbound links use different mechanisms, and get their own queues:

| Queue | Fed by | Carries | Grant |
| ----- | ------ | ------- | ----- |
| `programme-tver`, `programme-radiru` | **SNS subscriptions**, `{type:["tver_series"]}` / `{type:["radiru_series"]}` | programme requests (§2.4, §4.1a) | none — SNS writes via a queue policy |
| `observe` | **SNS subscription**, the three episode types — *optional* (§2.4a) | every dispatched download | none — same |
| `status` | **Download workers, `sqs:SendMessage`** | job outcomes | `sqs:SendMessage` on this one queue ARN |

Keeping worker status on a queue of its own is worth the extra Terraform: it makes a
**trust boundary** out of the transport. Anything arriving on
`reservation-observe` was delivered by SNS and is a faithful echo of what the
dispatcher routed. Anything on `reservation-status` was written by a worker and
is only a *claim* about an outcome. With a single shared queue a worker could
inject a message shaped like a dispatch echo and manufacture ledger entries;
with two, it cannot — the grant does not reach the other queue.

(One queue would work if the extra resource is unwanted. The cost is that the
service must then distinguish the two by content rather than by which queue
they arrived on, which is a weaker guarantee for no real saving.)

**Terraform delta**, all of it additive:

* Four `aws_sqs_queue` resources plus queue policies — three, if the §2.4a bonus is skipped.
* Three `aws_sns_topic_subscription` resources — `{type:["tver_series"]}`,
  `{type:["radiru_series"]}`, and `{type:["radiko","tver","youtube"]}` on the
  optional `observe` queue. All use
  `filter_policy_scope = "MessageBody"` and `raw_message_delivery = true`, the
  same shape as the two that already exist. Note `status` appears in neither:
  it never touches the topic.
* One statement added to each worker's existing IAM user policy:
  `sqs:SendMessage` on the `reservation-status` ARN. Nothing removed.
* An IAM user for the reservation service with `ReceiveMessage` /
  `DeleteMessage` on those two queues, and **nothing else** — notably no
  `sns:Publish`, because it publishes over HTTP (§4.4).

**Cross-region is a non-issue, and already proven.** An SQS queue lives in one
AWS region and is called from anywhere; that is exactly how a worker works
today, with `AWS_REGION` and `SQS_QUEUE_URL` pointing at the queue's region
regardless of where the container runs. A worker in Japan and one in Oregon
both just call the same regional endpoint. No replication, no multi-region
anything.

**Message shape** for status — small, and enough to close the loop:

```jsonc
{ "type": "status",
  "worker": "radiko-downloader",
  "worker_id": "jp-nuc-1",           // stable per deployment
  "key": "radiko:FMJ:20260906130000",// the ledger key (§5.4)
  "status": "succeeded",             // succeeded | failed | duplicate
  "attempt": 1,
  "title": "TOKIO HOT 100",
  "at": "2026-09-06T17:12:44+09:00" }
```

The worker can compute `key` from the message it was handed — the same
derivation the resolver uses — so no new correlation id has to be threaded
through the dispatch path.

**Consuming both.** Each resolver long-polls its own programme queue alongside its tick; the
UI container long-polls `status` and `observe`, which are the cross-source ones
(§4.1a). All four write the same SQLite file, which serialises them (§5.1).
This is deliberately the same 20-second `WaitTimeSeconds` loop the workers
already use in `run_main`, so there is one polling pattern in the repo, not
three.

**At-least-once, unordered — so handling must not assume otherwise.** SQS
standard queues can redeliver and reorder, which means a `failed` from attempt
1 can arrive after a `succeeded` from attempt 2. Two rules cover it:

* **Status advances state monotonically.** `(key, attempt)` is the ordering
  handle; a status older than what the row already records is dropped. A
  terminal `succeeded` is never overwritten by a late `failed`.
* **Observation upserts by ledger key.** The service seeing an echo of its own
  publish, or the same echo twice, costs one idempotent write (§2.4).

**Failures are non-fatal, in the direction that matters.** A worker that cannot
reach the status queue logs and moves on — the download already succeeded, and
the consequence is only that the ledger row stays `published` until the expiry
warning (§7) fires. Conversely an unparseable message is logged and deleted
rather than left to redeliver forever; nothing on these queues is precious
enough to justify a stuck consumer, and a dropped status is recoverable while a
blocked queue is not.

**The Lambda gains programme-URL classification** — one rule per source, the
same kind it already applies:

```js
// alongside the existing episode rules
/^https?:\/\/tver\.jp\/series\/[0-9a-z]+/i                         → "tver_series"
/^https?:\/\/www\.nhk\.jp\/p\/(?:[^/?#]+\/)?rs\/[\dA-Za-z]+\/?$/i  → "radiru_series"
//   note the anchored end: with /episode/re/… it stays an episode request
```

Radiko contributes no rule, per §2.4.

> **The behaviour change this makes, stated plainly.** Today
> `nhk.jp/p/rs/<series>/` publishes as `type: radiko` and the radio worker
> downloads every episode currently in 聞き逃し. Under this rule it becomes a
> programme request instead, and creates a reservation.
>
> Revision 5 called that a silent regression and used it to argue against the
> whole idea. That was wrong on both counts. The new behaviour is a
> **superset** — the reservation resolves immediately on creation, so the same
> currently-available episodes are still fetched, and future ones are too. And
> it is not silent: the reservation appears in the UI and can be deleted in one
> click.
>
> The one real difference is that publishing a series URL now leaves something
> behind. That is worth a line in the README, not a reason to avoid the
> feature — and for the らじる series URL specifically it is almost certainly
> what the user wanted in the first place.

### 4.4 Publishing: HTTP to `/publish`, not direct SNS

The poller POSTs to the existing endpoint, as a client: it **reuses the
Lambda's real logic** — Radiko start-time grouping, the らじる regex set,
`force` validation — instead of duplicating it in Python where it would drift;
it needs no `sns:Publish` at all; and it reuses the credential the cron helper
already uses. To `api-gw`, the poller is indistinguishable from a human with
curl.

### 4.5 Control plane on one host; workers anywhere

Regional access differs by source — らじる is JP-only in yt-dlp, Radiko needs
areafree, YouTube needs nothing — so workers are distributed and stateless,
while the reservation service is centralised and stateful:

Four containers (§4.1a) — three source resolvers plus the UI/aggregator —
sharing one SQLite file on a local volume (§5.1). Each resolver is the same
shape as the two download workers that already exist.

Centralising the control plane works only because of the §3.1 finding:
**resolution is region-free** — the programme tables answer from anywhere, and
only the download needs regional access.

### 4.6 The Radiko late-binding check

The concrete form of §2.3. Revision 7 called this "the only worker change the
design needs"; §5.5 shows that was wrong — the Lambda must forward the new
field or the check silently never runs. The
published message gains one **optional** field:

```jsonc
{ "station_id": "FMJ",
  "start_times": ["202609061300","202609061400","202609061500","202609061600"],
  "description": "TOKIO HOT 100",
  "expect_title": "TOKIO HOT 100" }   // ← new, optional
```

When `expect_title` is present, `record_radiko` re-reads
`/v3/program/station/date/{date}/{station}.xml` before downloading and:

1. **Confirms** each `ft` still carries a matching title.
2. **Re-derives the contiguous part run** from the title if boundaries moved —
   the authoritative answer to "the show ran long" and "a 特番 shifted
   everything by an hour". Note this is the same grouping rule as §5.2, applied
   a second time and later.
3. **Fails loudly** if the title is gone, rather than recording whatever now
   occupies the slot.

Why this shape: **backward compatible** — without `expect_title`, every
hand-published message, every cron line and `radiko-download.py` behave
byte-for-byte as today; **it reuses code that exists** — the worker already
calls `_fetch_radiko_title` against this endpoint to name the file; and **it
makes the filename trustworthy** — the recording is named after a title
confirmed at download time, so a mismatch surfaces as a failure instead of a
plausible wrong file.

> **Not a re-resolve from scratch.** The worker trusts the reservation's intent
> — this station, this title, around this time — and re-derives only the part
> boundaries. It never searches the whole week, so a corrupted or hostile
> message cannot make it record something unrelated.

### 4.7 The one unifying abstraction

> A reservation is a **query over a source's catalogue**. On each tick the
> poller runs every query and publishes the episodes that are available,
> unexpired, and not already in the ledger.

Covers all three reservable sources with one loop — and would cover a YouTube
channel feed unchanged, if that is ever wanted.

```python
# Pure: no I/O, no clock of its own. Testable from a fixture.
def resolve(reservation, catalogue, now) -> list[Episode]: ...

@dataclass(frozen=True)
class Part:
    url: str                  # what gets published
    start: datetime
    end: datetime
    title: str                # the part's own title, e.g. "…(PART2)"

@dataclass(frozen=True)
class Episode:
    key: str                  # stable identity, see §5.4
    parts: tuple[Part, ...]   # 1 for らじる/TVer, 1..n for Radiko
    title: str                # the episode's name, part suffix stripped
    available: bool
    expires_at: datetime | None
```

I/O lives at the edges — one fetcher per source, one publisher, one store.
Resolvers stay pure functions of `(catalogue, now)`, which is what makes the
whole thing testable offline against recorded fixtures (§8).

---

### 4.8 The tick: two clocks, and why nothing urgent depends on either

*(From the review: "how do you design the reservation queues' wake-up policy? I
think every 30 min or 1 hour is reasonable.")*

Reasonable, and 1 hour is the recommendation. But the question needs splitting
first, because the programme worker has **two clocks** and only one of them is
an interval at all.

#### Clock 1 — the SQS consumers do not wake up; they are always awake

The programme queues and the status queue are **long-polled**, not sampled:
`receive_message(WaitTimeSeconds=20)` in a loop, exactly as `run_main` does
today. A programme request or a status message is picked up within seconds of
arriving. There is no interval to choose and no policy to design.

Cost is a non-issue: 20-second waits are ~4,300 receive calls per queue per
day, which is the same order as the two workers already running, and long
polling exists precisely so that idle waiting is not billed as spinning.

This is what keeps the **interactive** paths prompt regardless of the tick:

* Publishing a series URL from the browser reaches the worker in seconds.
* A reservation resolves **immediately on creation** (§4.3), rather than
  waiting for a sweep.
* The UI has a *"check now"* action for a single reservation.

So the tick is only the *background sweep*, and background sweeps do not need
to be fast.

#### Clock 2 — the resolver tick: **1 hour**, with jitter

This is the one that needs a number: fetch catalogues, evaluate reservations,
publish whatever is ready.

**Nothing in the system is urgent.** The tightest deadline anywhere is the
7-day timefree window, and Radiko episodes are not even *eligible* until 2 h
after broadcast (§5.6). An hourly tick spends 0.6% of the tightest budget. A
daily tick would still be correct; hourly simply leaves a wide margin for a
host that was asleep.

**Radiko's own guidance is the other anchor.** The weekly XML carries
`<ttl>1800</ttl>` in the body, so 30 minutes is the floor it asks for and an
hour sits comfortably above it. 24 ticks a day is also pleasantly legible in a
log.

Between the review's two options: **60 minutes**, as the default for all three.
All that 30 buys is fetching a TVer episode up to half an hour sooner against a
7-day window, which is not worth doubling the request volume.

Because §4.1a gives each source its own container, the interval is a
`TICK_INTERVAL` env var per compose service rather than a scheduler inside one
process — which is what makes per-source tuning worth having at all. One
counter-intuitive note if you do tune: the source to make *more* frequent is
Radiko, not TVer. Radiko is the best-behaved (a published `ttl`, working
ETags, a stable table), while TVer's is the undocumented one it would be least
wise to poll harder.

**Add ±5 minutes of jitter.** A fixed hourly tick lands every deployment on
:00; jitter spreads retries and keeps a restart from re-synchronising
everything.

#### Frequency is not bandwidth-bound, because conditional GETs work

Worth checking rather than assuming, since the Radiko catalogue is 758 KB per
station. Verified 2026-09-04:

```http
Cache-Control: public; s-maxage=86400, max-age=60
last-modified: Sat, 05 Sep 2026 18:55:08 GMT
etag: W/"6b43e885cbdc58681ee58f00c7e2c433"
```

and both revalidation forms are honoured:

```
If-None-Match:     … → 304, 0 bytes
If-Modified-Since: … → 304, 0 bytes
```

So an unchanged catalogue costs a round trip and nothing else. **Store the
`ETag` and `Last-Modified` per URL and send them on every fetch** — that one
habit makes the tick interval a politeness and legibility decision rather than
a bandwidth one, and it is the difference between ~90 MB/day and a few hundred
kilobytes for a five-station setup.

Two related economies, both from the same instinct:

* **Fetch per station, not per reservation.** The weekly XML is per station, so
  a tick fetches one catalogue per *distinct* station across all reservations.
  Same for the NHK day listings, which are per (service, date).
* **Skip the tick entirely when there is nothing enabled to resolve.** Obvious,
  but it means an idle install makes no outbound requests at all.

#### Bounded work per tick

`max_per_tick` (§10) caps publishes so a first run cannot exhaust the API
Gateway day quota — which matters more now that §5.5 makes that one call *per
episode*. At an hourly tick and a cap of 10, a 140-episode backfill drains in
about 14 hours, entirely in the background, well inside every window. The cap
is a throttle, not a limit: nothing is dropped, only deferred to the next tick.

#### A refinement worth naming, not building

For Radiko the worker knows *exactly* when each pending episode becomes
eligible — `max(part.to) + grace` — so it could sleep until the earliest such
moment instead of ticking. Tempting, but it only half works: らじる and TVer
readiness flags flip at times nobody publishes in advance, so a periodic poll
is still required for them, and the deadline wake would be an optimisation
layered on top of a loop that has to exist anyway.

Not worth it while the tick costs a handful of 304s. Worth remembering if the
catalogue set ever grows enough that the sweep stops being free.

---

## 5. Data model

*(From the review: "a programme could have multiple episodes, and an episode
could be multiple parts, for Radiko.")* Revision 2's flat ledger was wrong.
Three levels:

```
Programme (番組)      what you reserve.  1 row per reservation
   └─ Episode (回)    one airing.  the unit of dedup, and of one delivered file
        └─ Part       Radiko only.  contiguous <prog> segments, concatenated
```

| | Programme | Episode | Part |
| --- | --- | --- | --- |
| Radiko | station + title pattern | a contiguous run of matching parts | each `<prog>` (`ft`→`to`) |
| らじる | `radioSeriesId` | `radioEpisodeId` | — (always 1) |
| TVer | `seriesID` + one or more `seasonID` | episode `id` | — (always 1) |

### 5.1 Storage: SQLite in the same container, not a database server

*(From the review: "How do you plan for Postgres? Can it be a lightweight
version running in a container along with the reservation system?")*

It can — `postgres:17-alpine` with `shared_buffers=16MB` and
`max_connections=20` idles around 30–50 MB and is a perfectly reasonable
container. But having sized the workload, **I do not think this should be a
database server at all.** Revision 4's Postgres was cargo-culted from "it has a
UI, so it needs a DBMS", and the numbers do not support it.

**How much data is there, actually?** A ledger row is a key (~40 B), a title
(~60 B), a part list (~60 B), a status and a few timestamps — call it 300 bytes.
Twenty weekly reservations produce `20 × 52 = 1,040` episodes a year:

| Horizon | Rows | Size |
| ------- | ---- | ---- |
| 1 year | ~1,000 | ~0.3 MB |
| 10 years | ~10,400 | **~3 MB** |

Three megabytes after a decade. Plus perhaps 100 reservation rows. That is not
a database-server workload; it is a file.

**And how much concurrency?** Four writers — the tick, the two SQS consumers
(§4.3), and the web app — none of them hot. A tick every 15 minutes is 96 write
bursts a day; status and observation messages arrive a handful of times a day.
SQLite in WAL mode serialises writers with a `busy_timeout`, and at this rate
two writers colliding is a once-in-a-long-while event that resolves in
milliseconds.

**What would actually justify Postgres** — and none of it is true here:

| Reason | Applies? |
| ------ | -------- |
| Writers on more than one host | No — the control plane is one host by §4.5 |
| Sustained concurrent writes | No — 96 bursts/day |
| Data beyond a laptop's RAM | No — 3 MB per decade |
| Queries needing a planner worth the name | No — lookups by key, lists by date |
| Replication, PITR, roles | No |

**So: SQLite.** Revision 4's justification for a separate web container ("the
UI must stay responsive while a tick pulls a 758 KB XML") does not hold up
either — Python releases the GIL during I/O, so a fetch does not block a
request. The containers that do exist are split for *blast radius*, per §4.1a,
which is a different and better reason:

| Was (rev 4) | Now |
| ----------- | --- |
| `reservation-web` + `reservation-poller` + `postgres` | three source resolvers + `reservations-ui` (§4.1a) |
| Postgres volume + credentials + major-version upgrades | one `.sqlite` file on a shared local volume |

Each of them is in exactly the shape of the two workers that exist: a container
with a `.env` and a volume, doing one job.

**Keep the schema portable anyway**, so Postgres stays a swap rather than a
rewrite. It costs nothing if the SQL avoids both dialects' specialities:

* **Natural primary keys** — the ledger key (§5.4) and the reservation id. No
  `AUTOINCREMENT` / `SERIAL` divergence, and it is the right modelling choice
  regardless.
* **Timestamps as ISO-8601 UTC text.** SQLite has no date type; ISO-8601 sorts
  correctly as a string and reads identically in both. (JST is derived for
  display, per §10's clock note.)
* **The selector stored as JSON in a TEXT column.** SQLite's JSON1 and
  Postgres's `jsonb` both read it; only the indexing differs, and there is
  nothing here worth indexing.
* **`INSERT … ON CONFLICT … DO UPDATE`** for the upserts of §2.4 — identical
  syntax in SQLite 3.24+ and Postgres 9.5+.

No ORM is needed to hold that line; a thin data-access module with plain SQL is
enough, and it keeps the dependency list as short as the workers'.

**Backup is now a one-liner**, which matters because reservations are the only
irreplaceable data (the ledger regenerates itself from the sources, at the cost
of some re-downloads):

```
sqlite3 reservations.sqlite "VACUUM INTO '/backup/reservations-$(date +%F).sqlite'"
```

`VACUUM INTO` is atomic and safe against a live database, so this runs on a
timer with nothing stopped. `GET /api/reservations` (§5.3) remains the
human-readable export for keeping reservations in git.

**Two constraints worth stating rather than discovering.** The database file
must live on a **local filesystem** — POSIX locking is unreliable over NFS and
SMB, so a NAS mount will corrupt it — and every writer must be on that host.
Both follow from §4.5 anyway, but they are the conditions under which this
choice is correct, and the moment either stops holding, the portable schema
above is the escape hatch.

Since §4.1a the writers are four *containers* rather than four threads. That
changes nothing material: WAL mode plus a `busy_timeout` serialises processes
the same way it serialises threads, the shared `-shm` file works for processes
on one host, and the write rate is unchanged — 96 bursts a day is 96 bursts a
day however many processes produce them. The ledger's source-namespaced keys
mean the workers never contend for the same *rows*, only very occasionally for
the write lock.

**One new failure mode comes with the collapse.** In a single process an
unhandled exception kills a *thread*, not the process — so the tick could die
while the web app keeps cheerfully serving. Two mitigations: the tick loop
wraps each pass in `try/except` and sleeps, exactly as `run_main` already does;
and it records a `last_tick_at` that the UI shows and §7 alerts on. A
reservation service that has silently stopped polling is the same class of
failure as a reservation that has silently stopped matching, and it deserves
the same treatment.

#### Who reads and writes what

Worth stating, because "four containers share one file" does not say who owns
which rows.

| Table | Written by | Read by |
| ----- | ---------- | ------- |
| `reservations` | `reservations-ui` (create / edit / delete / enable), and a programme worker when a **programme request** arrives on its queue (§2.4) | every resolver, filtered to its own source |
| `ledger` | the owning source's resolver, on publish (`published`, `parts`, `expires_at`); `reservations-ui`, applying status messages (`status`, `attempts`, `worker`) | `reservations-ui` for History; every resolver, to know what is already done |
| `health` | each resolver writes its own `last_tick_at` and match counts | `reservations-ui` for the health chips (§7) |

A tick, concretely:

1. Read this source's enabled reservations.
2. Fetch the catalogue — conditional GET, usually a 304 (§4.8).
3. Resolve to `list[Episode]` (§4.7) and drop anything already in the ledger,
   expired, or not yet ready (§5.6).
4. For each survivor: `POST /publish`, then write its ledger row.
5. Write `last_tick_at`.

Two writers touch a ledger row — the resolver that created it and the UI
applying a status message — but on **disjoint columns**, and the status update
only ever advances state monotonically by `(key, attempt)` (§4.3). Combined with
the source-namespaced keys, no two containers ever contend for the same row;
they contend, rarely, only for SQLite's write lock.

### 5.2 How Radiko parts become an episode

The grouping rule, and the answer to the TOKIO HOT 100 question:

1. Within one station-day, select every `<prog>` whose title matches the
   reservation.
2. Group the matches into **maximal contiguous runs** — `prog[i].to ==
   prog[i+1].ft`.
3. Each run is **one episode**; its parts are the progs, in `ft` order.

Properties worth having:

* **The part count is discovered.** Four parts this week, five next week for a
  拡大版 — no configuration changes. Problem 2 of §1, gone.
* **Two airings the same day split correctly.** A rerun that is not adjacent to
  the original forms its own run, hence its own episode and its own file.
* **It degenerates correctly.** A single-part programme is a run of one.
* **The existing download path is untouched.** All part `ft`s go in one
  `/publish` request; the Lambda groups them by station into one SQS message;
  `record_radiko` concatenates with ffmpeg. This is exactly what the crontab
  produced by hand, minus the hand.

Episode title: the reservation's `description` if set, else the first part's
title with a trailing `(PART\d+)` / `（PART\d+）` stripped.

### 5.3 Reservations

A reservation is a small, exportable record — `GET /api/reservations` returns
the YAML-equivalent, so it can be kept in git alongside the code (§5.1).

```jsonc
{
  "id": "tokio-hot-100",
  "source": "radiko",             // radiko | radiru | tver
  "enabled": true,
  "selector": {
    "station": "FMJ",
    "match": { "title": "TOKIO HOT 100" },   // the distinctive core, §3.1
    "weekday": "sun"                          // optional guard against reruns
  },
  "description": "TOKIO HOT 100", // episode title / filename stem
  "grace": null,                  // override the 2 h default; Radiko only (§5.6)
  "once": false,
  "until": null
}
```

Selectors per source: **Radiko** — `station` + `match.title`, or `at` +
`duration` for a time block when titles are inconsistent. **らじる** —
`series`. **TVer** — `series` + `seasons` (a list; §3.3 explains how the main season is chosen and why it is stored as an id).

There is **no `force` field**, by §2.5.

Matching defaults to **substring, case-insensitive, after NFKC normalisation** —
Japanese titles mix full- and half-width freely, and the TOKIO HOT 100 case
shows why substring rather than equality is the default. The trade-off belongs
in the UI, not hidden: too loose and you record 特番 you did not want; too
tight and you miss the 拡大版, or the week the sponsor prefix changed.

### 5.4 The ledger

One row per **episode**, keyed by a stable identity:

| Source | Ledger key |
| ------ | ---------- |
| Radiko | `radiko:{station}:{ft of first part}` |
| らじる | `radiru:{seriesId}:{episodeId}` |
| TVer | `tver:{episodeId}` |

Columns: `key`, `reservation_id` (**null for a manual download** — that is what
makes the §2.4a history complete), `title`, `parts` (the `ft` list, so a
boundary change is visible after the fact), `published_at`, `status`
(`published` / `succeeded` / `failed`), `manual_oneshot` (set for an observed
`force`, §2.5), `attempts`, `expires_at`, `worker`.

The key is derivable from a *published URL* as well as from a resolver, which
is what lets the optional observation (§2.4a) match a manual download to a
reservation rather than just logging it. The workers' filename-based duplicate
detection is the actual guarantee against a duplicate file, so losing the
ledger — or skipping observation entirely — degrades to some wasted publishes,
not to duplicate recordings.

### 5.5 Publishing a multi-part Radiko episode

*(From the review: "how do you plan to send a request to start downloading and
concatenating from the reservation worker?")*

The short answer is that the existing path already does it, and the programme
worker sends what the crontab sent. But tracing it through the real Lambda
turned up two things that were not right in earlier revisions, so here is the
whole hop.

**The request.** One `POST /publish`, carrying every part of one episode:

```jsonc
{ "urls": [
    "https://radiko.jp/#!/ts/FMJ/20260906130000",
    "https://radiko.jp/#!/ts/FMJ/20260906140000",
    "https://radiko.jp/#!/ts/FMJ/20260906150000",
    "https://radiko.jp/#!/ts/FMJ/20260906160000"
  ],
  "description":  "TOKIO HOT 100",   // filename stem
  "expect_title": "TOKIO HOT 100" }  // late-binding check, §4.6
```

**What the Lambda does with it.** `handleRadikoUrls` parses each URL with
`/radiko\.jp\/#!\/ts\/([A-Za-z0-9_-]+)\/(\d{14})/`, truncates `ft` to 12
digits, buckets by station, then emits **one SNS message per station** with the
start times sorted:

```jsonc
{ "type": "radiko", "station_id": "FMJ",
  "start_times": ["202609061300","202609061400","202609061500","202609061600"],
  "description": "TOKIO HOT 100" }
```

**What the worker does.** `record_radiko(station_id, start_times, description)`
downloads each part to `part{i}-{ft}-{station}`, then — because there is more
than one — writes a concat list and runs
`ffmpeg -f concat -safe 0 -i … -c copy` into
`{first_ft}-{station}-{description}.m4a`, checks it for truncation, delivers it,
and removes the parts. That is unchanged code; it is what the four-argument
crontab line has always driven.

#### Finding 1 — one request per episode, never a batch

The Lambda groups by **station**, across the whole request. So if a tick makes
two FMJ episodes available and the worker batches them into one `/publish`
call, they land in **one** message with five or eight start times, and
`record_radiko` concatenates them into a **single file**. Two programmes, one
recording, no error.

So this is a hard rule for the programme worker:

> **One `/publish` request per episode.** Never batch episodes, not even for
> the same station on the same tick. The unit of a request is the unit of a
> file.

It also settles `description`, which is per-request and names the output: with
one episode per request there is exactly one right value for it.

The cost is one API Gateway call per episode instead of per tick. At a handful
of episodes a day that is nothing — but it is why the `max_per_tick` cap in
§10 exists, because a first-run backfill across twenty reservations and a
seven-day window could otherwise walk into the 100/day quota.

#### Finding 2 — `expect_title` needs a Lambda change after all

§4.6 claimed the late-binding check was "the only worker change the design
needs". That was wrong. The Lambda builds the Radiko payload field by field:

```js
const payload = { type: 'radiko', station_id: stationId, start_times: startTimes };
if (description) payload.description = description;
```

Unknown keys are not forwarded, so `expect_title` would be **silently
dropped** and the check would never run — a failure mode that looks exactly
like success. It needs the matching two lines, with the same strictness
`force` already gets:

```js
if (typeof expect_title === 'string' && expect_title.length <= 200) {
    payload.expect_title = expect_title;
}
```

Validated as a string and length-capped because it crosses a trust boundary;
it is only ever compared against programme-table titles, never passed to a
shell or to yt-dlp.

> **Why not reuse `description`, which already passes through?** Because they
> are not the same thing. For TOKIO HOT 100 they happen to coincide, but a
> reservation may well be described as "Sunday Long Show" while the programme
> is titled "SAISON CARD TOKIO HOT 100(PART1)". `description` is what the user
> wants the file called; `expect_title` is what the resolver matched on.
> Collapsing them would break the check on exactly the reservations whose
> naming is least predictable.

#### The matcher must be shared code, not reimplemented

`expect_title` is `TOKIO HOT 100` while the table says
`SAISON CARD TOKIO HOT 100(PART1)`, so the worker's check is the same
substring-after-NFKC comparison the resolver used (§5.3) — and it must be
*literally* the same function, in `shared/`, not a second implementation. Two
copies of a fuzzy matcher drift, and when they disagree the symptom is a
recording that fails for no visible reason.

The same applies to the contiguity grouping of §5.2: the worker re-derives the
part run at download time using the rule the resolver applied at publish time.
One implementation, called from both ends.

#### The other two sources need none of this

```jsonc
// らじる — one canonical episode URL; the worker resolves it fresh at download
{ "urls": ["https://www.nhk.jp/p/rs/242V3Q87GK/episode/re/K65RLNYQZ7/"] }

// TVer — one episode id
{ "urls": ["https://tver.jp/episodes/epliwk4kpb"] }
```

Both are single-part, so there is nothing to group and nothing to concatenate.
Never `force`, on any of them (§2.5).

#### Edge: a shifted first part changes the ledger key

The key is `radiko:{station}:{ft of first part}` (§5.4), so if the table is
later corrected to start the programme half an hour earlier, the key changes
and the episode looks new. The `parts` column is the cheap fix: a candidate
whose part list **overlaps** an existing row's is the same episode under a
corrected boundary, not a new one. Worth having, and worth a test, because the
symptom is a silent duplicate recording rather than an error.

### 5.6 When an episode becomes publishable

*(From the review: "all parts have to be available when the worker sends the
request, so the reservation worker has to wait until all parts are aired and
available.")*

Exactly so, and it is worth stating as a rule because the naive version is
quietly wrong:

> An episode is publishable when its **last** part has finished airing, plus a
> grace — not its first.
>
> ```
> publish_after = max(part.to for part in episode.parts) + grace
> ```

**Why the naive rule produces a plausible wrong file.** Suppose a tick runs at
15:30 on Sunday and the resolver selects "parts whose `to` is in the past". It
finds PART1 and PART2, groups them (they are contiguous), and publishes a
two-part episode. The download succeeds, the concat succeeds, the truncation
check passes — the file is internally consistent — and you get a two-hour
recording of a four-hour show with nothing anywhere reporting a problem. This
is the same failure shape as problem 4 in §1.1, arriving by a different route.

So the gating is **per episode, not per part**: contiguity grouping (§5.2) runs
over the whole episode as the table describes it, and the episode as a unit is
then gated on its last part.

**The table already contains the future parts, which is what makes this
possible.** At 15:30 the weekly XML — six days forward (§3.1) — already lists
PART4 at `ft=20260906160000`. The episode's full shape is known before it
finishes airing, so the resolver can tell "four parts, one still to come" from
"two parts, complete". Without the forward window this would be undecidable.

#### Three states, and they unify across sources

| State | Radiko | らじる | TVer | Action |
| ----- | ------ | ------ | ---- | ------ |
| **announced** | some part's `to` is still in the future | `contentStatus: "notyet"` | not listed yet | show in preview, publish nothing |
| **ready** | all parts' `to` + grace (2 h, §5.6) have passed | `contentStatus: "ready"` | `isAvailable: true` | publish, as one request (§5.5) |
| **expired** | first `ft` + 7 days | `audio[].expires` | `endAt` | give up and alert (§7) |

Radiko is the only source without an explicit readiness flag, which is why it
is the only one that needs a clock rule at all. For the other two the state is
read, not computed.

#### Grace: 2 hours, and it applies to Radiko only

**Default: `grace = 2h`.** It is deliberately generous, and generosity is
close to free here.

The grace covers two things, neither of which has a documented duration:
Radiko's timefree encode appearing after broadcast, and the **programme table
settling** after a show that overran or was displaced.

What it costs is latency — the file lands up to two hours after the show ends.
That is worth almost nothing for an on-demand recorder: nobody is waiting on
it, and two hours out of a seven-day timefree window is 1.2% of the budget.

What it buys is the removal of two failure modes that are genuinely annoying:

* **A wasted multi-part re-download.** If the last part is not yet fetchable,
  the whole episode fails and the next tick re-downloads *all* of it, not just
  the missing part. A tight grace makes that a routine event; two hours makes
  it rare.
* **A part added after publishing.** The residual risk below. At 10 minutes it
  is a real possibility; at two hours the table has long since settled.

> **Correctness never depended on this number.** If the last part is still not
> fetchable, `run_download` exhausts its attempts, the episode fails, the
> ledger row stays undone, and the next tick tries the whole episode again —
> §2.2's free retry doing exactly its job. Tuning the grace is a cost
> optimisation; it is not what makes the design correct. That is why picking a
> large value carries no risk, and why revision 10's tighter 10–15 minutes was
> the wrong instinct: it optimised the cheap axis.

**It is a Radiko-only concept.** らじる and TVer are gated on a flag that is
read, not computed — `contentStatus: "ready"` and `isAvailable` — so a grace
would delay them for no reason at all. Applying one globally would be a bug,
not a conservative setting.

`grace` is a per-reservation override on top of the default, for a station that
turns out to be unusually slow, or for a programme someone wants promptly.

> **A late-added part is the residual risk, now much reduced.** If the table
> gains a PART5 *after* we publish, the ledger key (first `ft`) already matches
> and the episode is not republished, so the recording is short by an hour.
> Two hours of settling makes this unlikely rather than merely improbable, and
> the part-count drift check (§7) catches what still slips through. Worth
> knowing it degrades to a *short* recording rather than a wrong one.

> **The UI has to show this**, or it reads as a bug. An episode that aired 40
> minutes ago and has not been fetched looks broken unless the reservation view
> says when it will be: *"airs Sun 13:00–17:00 · fetches ~19:00"*. Cheap to
> render, and it turns a two-hour silence from a worry into a schedule.

Waiting also makes §4.6's late-binding check sharper: by publish time the
broadcast is over, so the table the worker re-reads is the corrected, final one
rather than a mid-broadcast guess.

#### Radiko publishes availability flags — use them to warn, not to gate

Each `<prog>` carries five fields that look like exactly what this section
needs. Sampled across 8 stations / 4,169 programmes on 2026-09-04:

| Field | `0` | `1` | `2` |
| ----- | --- | --- | --- |
| `failed_record` | 4169 | — | — |
| `ts_in_ng` / `ts_out_ng` | 3539 | 3 | 627 |
| `tsplus_in_ng` / `tsplus_out_ng` | 3526 | 3 | 640 |

So roughly **15% of programmes are flagged non-zero** — this is real signal, not
a vestigial field. The programmes it lands on are recognisable ones:
`テレフォン人生相談` (LFR) has `ts_*_ng = 2` on both in- and out-of-area, and
`オードリーのオールナイトニッポン` has `tsplus_*_ng = 2` while `ts_*_ng` stays 0
— consistent with the naming, where `ts` is timefree and `tsplus` the Premium
extended window, `in`/`out` being in-area and areafree.

**But the semantics are inferred from the field names, not from documentation**,
and the `1` value appears only 3 times in 4,169 — too rare to interpret. So
they are used as a **pre-flight warning, never as a gate**:

* **In `preview` and the UI**, when a reservation matches programmes flagged
  NG, say so: *"this programme is marked unavailable for timefree; recording
  will probably fail."* Caught at reservation time, which is when it is
  cheap.
* **At publish time**, log the flag and publish anyway — because a misread flag
  silently suppressing a recording is a worse failure than a wasted attempt,
  and §7's heartbeat catches persistent failure regardless.

If experience shows the reading holds, promoting it to a hard skip is a
one-line change. Getting it wrong in that direction is not recoverable, which
is why it starts as advice.


---

## 6. The UI

A `docker compose run` CLI is the wrong primary interface for something whose
core difficulty is *"will this actually match my show next Sunday?"*. That
question needs to be answered visually, against real programme data, before the
reservation is saved.

`reservation-web` serves a small web app:

**Reservations** — the home view. One row per reservation: source, what it
matches, when it last recorded, when it next expects to, and a health chip
(§7). Sorted so anything unhealthy floats up.

**Add by browsing, not by typing.** This is where the "the Radiko site has a
programme table" idea lands, but in our UI rather than by scraping theirs. Pick
a station and a day, get the real grid from `v3/program/station/weekly`, click
a programme, and the selector is pre-filled. For TVer the browser is a **season
picker** (§3.3) — five seasons with episode counts, 本編 preselected — because
that is the choice a user must actually make and would otherwise get wrong.

**Live preview, showing episodes and their parts.** Beside the match field, the
next 7 days of matches, updating as you type. Crucially it shows the *grouping*,
not just the hits, so a user can see that four rows became one episode:

```text
Sun 06 Sep   TOKIO HOT 100   4 parts  13:00–17:00   fetches ~19:00   → 1 file
Sun 13 Sep   TOKIO HOT 100   4 parts  13:00–17:00   fetches ~19:00   → 1 file
```

That single view is what makes §5.2 trustworthy: it makes the contiguity rule
visible, so a stray sponsor-prefix change or an accidental match shows up as
"5 parts" or "2 episodes" before anything is recorded. For らじる it also shows
`contentStatus` and `expires` per episode (§3.2).

**History** — the ledger, filterable, showing manual downloads alongside
reserved ones (§2.4a, when enabled) and the worker that reported each result, with a
one-click re-queue that republishes **without** `force` (§2.5).

**Workers** — last-seen and last-result per worker, from the status messages of
§4.3. With workers spread across regions this is the only place a user can see
that the JP host has been down since Tuesday.

Notes on shape:

* **Read-mostly, small data.** Server-rendered with a little JS is enough; this
  does not need a SPA build step in a repo that currently has none.
* **Auth.** It can trigger downloads and holds an API key, so it is not
  internet-facing by default: bind to LAN, put it behind Tailscale or a reverse
  proxy, require a token on the REST API. Say so in the README.
* **The CLI stays** for scripting and debugging (`preview`, `run --dry-run`,
  `backfill`), sharing the resolver library with the web app. It is just no
  longer what a person uses day to day.

The **Chrome extension is untouched** and stays a URL publisher — and now gets
reserve-from-the-page for free: publishing a series URL from the page you are
looking at *is* the reservation (§2.4). No new button, no second endpoint. The
UI's programme browser remains the only route for Radiko, which has no
programme URL to publish.

---

## 7. Health, and the enemy of silent failure

A recorder that stops recording and says nothing is worse than one that
crashes. Three defences, surfaced as the health chip in §6:

1. **Heartbeat.** A reservation matching nothing for N consecutive expected
   occurrences (default 2) is marked unhealthy and notified. This catches the
   season ending, the sponsor prefix changing (§3.1), a TVer season being
   retired, and the programme API changing shape.
2. **Part-count drift.** A Radiko episode that resolves to a different number
   of parts than the reservation's recent history is *recorded but flagged*.
   This is also what catches a part added to the table after publishing (§5.6).
   Usually a 拡大版 — occasionally a match that has gone wrong. Cheap to
   compute from the `parts` column (§5.4) and it is the earliest visible sign
   of the failure mode §3.1 warns about.
3. **A new TVer season appears.** A reservation follows stored `seasonID`s, so
   a section added later is silently uncovered. Comparing the live season list
   against the covered ids each tick is nearly free (§3.3).
4. **Expiry warning.** A ledger row still `published` or `failed` with
   `expires_at` inside 24 h escalates. Last chance to intervene by hand.
5. **Worker liveness.** No status message from a download worker in N hours
   means jobs may be queueing for a host that is gone.

---

## 8. Testing

The existing suite is entirely offline, stubbing transports. Hold that line;
the pure-resolver split in §4.7 is what makes it possible.

* **Fixtures** — trimmed real responses captured during this research: the
  `FMJ` weekly XML **including the four `(PART1..4)` rows**, an
  `api.nhk.jp/r8/pg/date` day covering all three らじる states, and the
  `srusndh59f` season list plus its episode pages **including the `type:
  "live"` entry**.
* **Radiko part grouping (§5.2)** — the highest-value resolver tests. Four
  contiguous parts become one episode; a five-part 拡大版 becomes one episode
  without configuration; two non-adjacent airings become two episodes; a
  single-part programme still works; the `(PART\d+)` suffix is stripped from
  the episode title.
* **TVer season selection and entry filtering (§3.3)** — index 0 is chosen and
  its `本編` title confirmed across the three captured series, including the
  single-season one; a series whose index 0 is *not* `本編` is surfaced rather
  than silently followed; only the reserved `seasonID`s yield episodes; a
  `type: "live"` entry is skipped rather than crashing; per-episode `endAt` is
  honoured rather than assuming seven days. The probe script for this research
  crashed on the live entry, so that one is a regression test for a real
  defect.
* **Late binding (§4.6)** — the path that prevents a wrong recording. A message
  whose `ft` still matches; one whose programme moved 30 minutes; one whose
  programme is gone. Assert re-derivation, and assert a clean failure rather
  than a wrong file.
* **`force` containment (§2.5)** — an observed `force` message writes history
  and creates no reservation; a re-queue publishes without `force`; a
  reservation record rejects a `force` key.
* **Programme-URL routing (§4.3)** — `tver.jp/series/…` and a bare
  `nhk.jp/p/rs/<id>/` classify as `programme`; `nhk.jp/p/rs/<id>/episode/re/<id>/`
  and every episode URL still classify exactly as they do today. This is the
  test that protects the §4.3 behaviour change from widening.
* **Programme request → reservation (§2.4)** — a programme message creates a
  reservation and resolves immediately, so the currently-available episodes are
  published straight away; a repeat of the same programme URL does not create a
  second reservation.
* **Observation, when enabled (§2.4a)** — observing the service's own publish
  is idempotent; no inbound message ever causes an outbound publish. And with
  the observe subscription absent, everything else still works.
* **YouTube non-regression (§3.4)** — the Lambda still routes
  `youtube.com`/`youtu.be` to `type: 'youtube'`, and the new reservation
  subscription does not change what the video queue receives.
* **Ledger** — same tick twice publishes once; restart mid-tick does not
  double-publish; a `failed` row retries until `expires_at` and then stops.
* **Contract canaries** — a small opt-in (`-m network`) suite hitting the three
  live APIs, asserting only the fields §3 depends on. Not in CI. API drift is
  the top operational risk in this repo — `ensure_yt_dlp_current` exists for
  exactly that reason.

---

## 9. Phasing

| Phase | Scope | Delivers |
| ----- | ----- | -------- |
| **0** | Resolver library + fixtures + `preview` CLI. Radiko only, including part grouping (§5.2). | Proof that TOKIO HOT 100 resolves to one episode of four parts, on real data, before anything can record. |
| **1** | `programme-radiko` container + SQLite + ledger, publishing via `/publish`. Plus the §4.6 late-binding check in the radio worker. | **Replaces the crontab.** Fixes problems 1–6. |
| **2** | `reservations-ui`: reservations, programme browser, live preview with part grouping, history. | The UI. Makes the feature usable by a person rather than by `crontab -e`. |
| **3** | Programme-URL classification in the Lambda; the `reservation` queue + subscription; worker `SendMessage` grant and status emit; health chips. Optionally the `reservation-observe` subscription. | Publishing a series URL starts following it. Distributed workers become observable. |
| **4** | `programme-radiru` and `programme-tver` containers (season-aware). | Fixes problems 7, 8 and 9 — the manual poll of §1.2 stops being a person's job. |

Phase 1 pays for the project. Phase 2 makes it pleasant. Phase 4 is what could
not be attempted at all before this research.

> **Compatibility.** `radiko-download.py`, the crontab recipe, `SCHEDULING.md`,
> the Chrome extension, `force`, and every YouTube/TVer publish keep working
> untouched. The reservation service is additive — a user can run one
> reservation and keep twelve cron lines.

---

## 10. Risks

| Risk | Likelihood | Mitigation |
| ---- | ---------- | ---------- |
| Title match too loose / too tight | High | Live preview showing part grouping (§6); `weekday` and `duration` guards; part-count drift alert (§7); §4.6 turns a mismatch into a failure instead of a wrong file. |
| Sponsor prefix changes in a Radiko title | Medium | Match the distinctive core, not the full title (§3.1). Heartbeat catches it within two occurrences. |
| Programme API changes shape → silent stop | Medium | Heartbeat (§7); contract canaries (§8); resolvers isolated so one breakage does not stop the others. |
| Radiko corrects the table between publish and download | Medium | §4.6 — exactly what it is for. |
| A TVer season is retired, renamed, or added mid-run | Medium | The reservation stores `seasonID`s, which survive renaming; the heartbeat fires when one stops yielding episodes, and new-season detection (§7) reports one that appears. |
| Observation causes a publish loop | Low but severe | Only possible with the §2.4a bonus enabled. Structural rule: observation writes history only, enforced by having no code path from the SQS consumer to the publisher. |
| A programme URL creates an unwanted standing reservation | Medium | Visible in the UI and one-click removable (§4.3); open question 2 asks whether it should land pending instead. |
| A compromised worker triggers downloads | Low but severe | Workers get `sqs:SendMessage` on the status queue only — never `sns:Publish` (§4.3). A worker cannot make the system fetch anything. |
| A status message is lost or arrives out of order | Medium | Non-fatal by design: the row stays `published` and the expiry warning fires (§7). Status advances monotonically by `(key, attempt)`, so a late `failed` cannot overwrite a `succeeded` (§4.3). |
| A worker wins a job it cannot serve regionally | Low today | Type-based routing already separates regions (§4.2). Split `youtube` onto its own queue if a video worker is ever deployed outside Japan. |
| The one host is down | Medium | Only costs recordings if downtime exceeds the shortest window (~7 days). Workers keep draining their queues. |
| TVer `platform_uid` flow breaks | Medium | Undocumented API. Re-mint once, back off, disable that resolver only. |
| UI reachable from the internet | Low but severe | Not internet-facing by default; token on the REST API; documented in the README. |
| Database file lost or corrupted | Low | Degrades to re-downloads, not loss: the ledger regenerates from the sources. Reservations are the irreplaceable part — `VACUUM INTO` on a timer, plus the API export (§5.1). |
| SQLite on a NAS mount | Low but severe | POSIX locking is unreliable over NFS/SMB. Documented constraint (§5.1); the volume must be local. |
| A resolver dies silently | Medium | `try/except` per pass, plus a per-source `last_tick_at` surfaced in the UI and alerted on (§5.1, §7). Since §4.1a each resolver is its own container, so a crash-loop is visible in `docker ps` and cannot take the others with it. |
| Three workers drift into three codebases | Medium | The publisher, ledger DAL, matcher, health writer and episode model live in `shared/` (§4.1a). A resolver that grows its own publisher is the signal the split has started costing what it saved. |
| API GW quota (100/day, 2 rps, burst 5) | Low | Ticks do not call the API; only publishes do — but §5.5 makes that one call *per episode*, so a first-run backfill is the realistic risk. Cap publishes per tick. |
| Clock | — | Japan has no DST. Store aware datetimes; normalise to JST once, at the edge. |

---

## 11. Open questions

1. **Where does the late-binding check live long-term?** §4.6 puts it in the
   radio worker, which keeps the message a plain URL set. The alternative is a
   resolve step in the poller immediately before publish — simpler to test, but
   it re-opens the publish→download gap that §2.3 exists to close. Leaning
   worker-side.
2. **Does a published programme URL want a confirmation step?** §4.3 has it
   create a reservation outright, which is the useful default and matches what
   a series URL denotes. But it does mean an idle click leaves a standing
   reservation. The UI makes it visible and one-click removable; whether that
   is enough, or whether a first-time programme URL should land as a *pending*
   reservation awaiting confirmation, is worth deciding before Phase 3.
3. **Should a manual download satisfy a reservation, or only suppress it?**
   §5.4 makes them share a ledger key, so a hand-published episode suppresses
   the reserved fetch. That is almost certainly right, but it means a manual
   download at lower quality silently becomes *the* recording.
4. **Should the tick be per-source?** §4.8 settles on one hourly sweep for
   everything. A case could be made for polling TVer more often than a Radiko
   weekly table — but with conditional GETs making an unchanged catalogue
   nearly free, the saving is small and the extra moving part is not obviously
   worth it.
5. **What should `description` carry for らじる and TVer** — series name or
   episode name? Radiko's is settled by §5.2; the other two are not.

---

## 12. Out of scope

* **YouTube as a reservable source.** §3.4 records what must keep working and
  sketches the channel-RSS route if it is ever wanted.
* **Live recording.** Everything here is on-demand — including TVer's
  `type: "live"` entries, which §3.3 filters out. Radiko live and らじる
  simulcast are excluded, and §2.2 does not apply to them: they *would* need
  instant-accurate triggers.
* **Japanese translation.** The repo is bilingual, but this proposal will
  change during review and translating now means translating twice.
  `RESERVATION_DESIGN_ja.md` belongs with the Phase 1 user-facing docs.
* **Implementation.** No code beyond the interface sketches in §4.6 and §4.7.
