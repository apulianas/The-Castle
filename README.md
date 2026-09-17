# The-Castle

A Dockerized Python Discord bot for Baltimore Ravens roster transactions, game
day inactives, injuries, standings, live in-game stats, and upcoming games.

## Features

- Slash commands:
  - `/transactions [date]` — Ravens roster transactions for today or a `YYYY-MM-DD` date.
  - `/inactives [date]` — game day inactive reports as a chart image when ESPN publishes them.
  - `/injuries` — the current Ravens injury report, grouped by status.
  - `/standings` — AFC North standings, with the Ravens highlighted.
  - `/nextgame` — the next Ravens matchup.
  - `/live` — live score, clock, possession, team totals, and leaders for today's game.
  - `/recap [date]` — final score, offensive efficiency, passing/rushing leaders,
    and Ravens-perspective win-probability swings for the latest completed
    regular-season/playoff game, or a game on `YYYY-MM-DD` in `TIME_ZONE`.
  - `/schedule [days]` — upcoming Ravens games over the next 1-366 days, so a full
    schedule is one command.
  - `/snapcounts [player] [weeks]` — snap counts for the last game, or the last 1-42 games.
  - `/fourthdown [team]` — whether the team with the ball in a live fourth down should go for it, kick, or punt, answering the last fourth down seen once the play is over.
  - `/fieldgoal [yards] [team]` — how often a kick of that length is made, and what attempting it is worth; omit the yardage to use the current ball spot.
  - `/help` — command help.
- Rich embeds: team logos, player headshots, and clickable links out to ESPN
  player, team, and game pages.
- Roster-backed player resolution, so names in a transaction become real links.
- Background polling for today's roster transactions, practice-squad standard
  elevations, and injury report changes. The official weekly Ravens chart is posted as an image
  after both clubs publish the same practice day and the chart remains unchanged
  for five minutes. Individual ESPN injury changes are not posted separately,
  and the chart automatically advances with the site's selected week.
- Game day inactives watched on their own clock: ESPN publishes the lists about
  90 minutes before kickoff, so the watcher looks every minute from 90 minutes
  out until a quarter hour past the scheduled start, and otherwise only reads
  the day's schedule every five minutes.
- Trades announced with each side of the deal — who and what the Ravens got,
  who and what they gave up, and which club they dealt with.
- Duplicate announcement prevention across container restarts using `/data/state.json`.
- Discord channel and webhook announcement targets.
- Docker Compose setup for home-server hosting.

## Postgame recaps and data freshness

`/recap` selects completed Ravens regular-season or playoff games from ESPN's
season schedule, including the previous season in the offseason. A supplied
date selects that exact local game date, not the nearest game. Preseason and
in-progress games are not eligible. `/live` and all live feeds remain ESPN-backed
and unchanged.

Recap analytics are calculated from NFLverse's compressed season PBP release
(`pbp/play_by_play_{season}.csv.gz`), not a live API or the stale `player_stats`
release. NFLverse publishes in batches after games; a final score can appear
before analytics are published. Missing releases/games say **Not published**;
missing end-of-game records, mismatched final scores, and missing measurements
are labelled **Partial recap**. HTTP failures and changed/malformed schemas are
reported as errors rather than an empty report.

Offensive EPA/play and success rate (EPA greater than zero) use nflfastR pass/run
plays with a dropback or rush attempt, excluding no-plays, spikes, kneels and
two-point tries. Dropbacks include sacks and scrambles; designed runs exclude
dropbacks. Each line shows measured/eligible play counts. Passing/rushing
production is derived separately from credited PBP statistics, includes spikes
and kneels, and excludes nullified plays and two-point tries. Gross passing yards
do not subtract sacks; team net passing yards do. Player leaders are ranked by
credited yards, including lateral rushing credits without adding a carry to
the lateral recipient. WPA excludes kneels/spikes, which lack model estimates,
and is signed using the **pre-play** possession team, so positive
always helps Baltimore, including opponent turnovers; swings are percentage
points, not relative percentages.

The compressed download is streamed to a temporary file, with 256 MiB compressed
and 2 GiB decompressed limits. CSV decompression and row-by-row aggregation run
off the Discord event loop; only Ravens game aggregates are retained in memory.
Concurrent season requests collapse into one fetch. Up to three seasons are
cached for one hour (including not-yet-published results), then refreshed so late
data and corrections can appear. The embed attributes ESPN and NFLverse/nflfastR,
shows fetch time and source modification time when available, and notes that
postgame statistics may lag or be corrected. Temporary downloads are removed
on completion, failure, or cancellation.

## Embeds

Every response is an embed built in `ravens_bot/embeds.py` from the plain-text
helpers in `ravens_bot/formatting.py` and `ravens_bot/recap_formatting.py`.
Keeping rendering separate from Discord means the wording is unit tested
without constructing a client.

- **Transactions** list one field per move. ESPN's NFL transaction feed carries
  only prose — no athlete record — so player names and positions are parsed out
  of the description and matched against the team roster to recover ESPN ids.
  A move about a single player is posted with their full-size headshot; a move
  covering several players falls back to a thumbnail, since one face would
  misrepresent the post. The thumbnail is the player joining the roster, because
  a day that pairs an activation with a move to injured reserve is about the
  arrival. A mass roster cut skips link markup entirely, because twenty links
  would crowd the wording out of the field's character budget. A move's title
  links to the club's own transaction log for that year, the page the news is
  published on, rather than to another outlet's copy of it.
- **Cut down day** gets its own layout, because ESPN files a club's cuts as one
  run-on sentence naming thirty players. A move that sends out more players than
  a post can picture is listed by unit instead — quarterbacks, running backs,
  receivers, tight ends and the offensive line, then the defensive line,
  linebackers, defensive backs and specialists — with one player to a line. The
  exact position leads a line only where its group holds more than one code, so
  a field headed "Offensive line" says "G" and "T" but a field headed
  "Quarterbacks" does not repeat "QB". Each line also carries what happened to
  that player whenever the description covered more than one kind of move, since
  ESPN files a placement on injured reserve in the same sentence as a waiver and
  reporting the two alike would say a player was cut who was not. Such a post
  shows the club's own mark rather than a headshot, because the first name ESPN
  wrote is just one of thirty players heading out. A move whose wording yields no
  such list stays prose, and a digest of several moves on one date stays a
  summary.
- **Practice-squad call-ups** come from the official Ravens transaction log,
  because ESPN omits Baltimore's game-day standard elevations. They are merged
  into `/transactions` and posted to configured announcement channels and
  webhooks like every other roster move.
- **Trades** get their own layout, with a "Ravens receive" field and a "Ravens
  send" field listing the players and the picks that went each way, and any move
  ESPN filed in the same item under "Also". The sides are labelled from
  Baltimore's end rather than by club name, so the wording reads the same
  whether ESPN called the partner "Chicago" or "Chicago Bears". A deal moving
  one player leads with their headshot; a deal of picks alone shows the other
  club's logo, since the opponent is what the post is about.
- **Standings** show record, win percentage, games back, and streak per team,
  with the Ravens bolded, plus division, conference, home, and away splits and a
  footer summarising where the Ravens sit.
- **Games** show kickoff, broadcast, venue, week, and both records, and use the
  opponent's logo, since the Ravens appear in every post.
- **Inactives** come from ESPN's game summary for each of the day's Ravens
  games, and are drawn as a chart image in the injury report's style: a
  panel per club in matchup order, away first, with the club's colors and logo.
  Each row is a headshot, position, and name in one column, with the reason
  alongside, and the table carries no headings because a name and a reason need
  no labelling. A club with nothing published shows a "None listed" row. The
  post's embed keeps the matchup, kickoff, and venue; when the chart cannot be
  drawn the written list is posted instead.
- **Injuries** use the official Ravens weekly chart, including both clubs,
  practice participation by day, and game status. The chart is rendered to an
  image sized for Discord with player headshots, team-colored headings, and a
  matchup-color title bar. It is drawn for a phone screen: the type is large and
  each column is only as wide as the widest entry it holds, so a practice-status
  column showing "DNP" takes no more room than that, and both clubs' tables
  share one set of widths so they line up. Its title links back to the live
  report.
- **Roster moves that come with injury news** are one post, not two. A player
  activated off injured reserve shows up as a transaction *and* as a status
  change on the injury report, so the update rides along in the move's post
  under an "Injury report" field. The photo is the player joining the roster,
  and a post about one person keeps the full-size headshot.
- **Live stats** lead with the score, clock, quarter, possession, and down and
  distance, then list both teams' box score totals side by side and a leading
  player per category, Ravens first. A game that has not kicked off points at
  `/nextgame` instead, since there is nothing to report yet, and a finished game
  shows the same layout as a live one, which is what a final box score is. ESPN
  publishes these sections at different points in a game, so the post degrades
  to the score and clock rather than failing when a section is missing. The
  footer states when the snapshot was taken, because the numbers move.
- **Fourth downs** name the call as the title, the live situation as the
  description, and one field per option carrying what it is worth — the win
  probability it leaves behind when the clock is known, expected points when it
  is not — and the reasoning behind it: the conversion rate, the kick distance
  and its make rate, or where a punt leaves the opponent. A call the model rates
  as a coin flip says so instead of picking a side, and anything the model
  cannot see is added as its own field.
- **Snap counts** send separate offence, defence, and special-teams reports,
  each listing every player with snaps in that unit, sorted by snaps. Players
  who contribute to more than one unit appear in each applicable report with
  that unit's counts and share. Reports are separate messages so the Discord
  character limit cannot let offence or defence crowd out special teams.
  Long unit reports continue in numbered pages instead of dropping players.
  This applies to both single-game reports and multi-game totals.
  A player is a line inside a
  unit's field rather than a field of their own, because a full report names
  forty players and Discord allows twenty five fields. Naming a player switches
  to their own embed, with their headshot and, over several games, a week by
  week breakdown. Offensive/defensive/special-team share changes use the previous
  completed Ravens game and are labelled in percentage points (pp). Team totals
  over several games show trends for the latest game, not a change in totals.

Embeds are truncated to Discord's limits rather than being rejected at send
time, and any list longer than 25 fields states how many entries were hidden.

## Caching

`ravens_bot/cache.py` holds a small TTL cache in front of the slower endpoints:
standings for 5 minutes, the injury report for 5, the schedule for 3, the roster
for an hour, a game summary for 45 seconds, a season of snap counts for 6 hours,
the nflverse player-ID crosswalk for 24 hours, and the live scoreboard for
12 seconds, which is only long enough to
collapse a burst of commands without ever answering with last play's down. A
live game moves play by play, so the short summary entry exists to absorb a
burst of `/live` calls rather than to spare ESPN the traffic; the score shown
always comes from the summary, which leads the cached scoreboard. Each key has
its own lock, so a burst of commands on a cold key waits on one in-flight
request instead of issuing several identical ones.

## Setup

1. Create a Discord application and bot at <https://discord.com/developers/applications>.
2. Copy `.env.example` to `.env`.
3. Set `DISCORD_TOKEN`.
4. Set `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, or both for background posts.
5. Start the bot:

```bash
docker compose up --build
```

The compose file mounts a named volume at `/data` so announcement state survives
container restarts.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | | Discord bot token. Never commit it. |
| `DISCORD_CHANNEL_ID` | No | | One channel ID, or several separated by commas, for background announcements. |
| `DISCORD_WEBHOOK_URL` | No | | One Discord webhook URL, or several separated by commas, for background announcements. |
| `POLL_INTERVAL_SECONDS` | No | `300` | Poll interval for automatic announcements. Minimum 30 seconds. Game day inactives keep their own kickoff-based schedule. |
| `TIME_ZONE` | No | `America/New_York` | Time zone used for "today" and display times. |
| `SECONDARY_TEAM` | No | | A second team, by name, city, or abbreviation, that `/fourthdown` and `/fieldgoal` fall back on when the Ravens are not playing. |

## Discord permissions and intents

Use the OAuth2 URL generator in the Discord developer portal:

- Scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Use Slash Commands`
- Privileged gateway intents are not required.

Slash commands are synced globally when the bot starts. Discord can take several
minutes to make new global commands visible.

## Development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run checks:

```bash
python -m pytest tests
python -m compileall ravens_bot tests
```

Tests use local sample payloads and do not call the network.

## Fourth down and field goal calls

`/fourthdown` answers a live game. ESPN's scoreboard publishes a `situation`
block for a game in progress — down, distance, the spot, who has the ball — and
that is read straight, on a twelve second cache, since a down and distance is
stale within a play.

A fourth down lasts under a minute and the argument about it starts once the
play is over, so the last fourth down each live game showed is recorded in
memory and served when the scoreboard has moved on, with the answer stating how
long ago the down came up. The scoreboard is read every 30 seconds while a game
is being played — and once every five minutes when none is, since there is
nothing to record — so the recall works whether or not anyone asked at the time.
The store is in memory only and a restart clears it: it is conversation, not a
record.

`/fieldgoal` reads the same kick curve on its own. Give it a distance — the
number a person says out loud, as in a "fifty two yarder" — or leave it out and
it takes the distance from where the ball is now, which is the yards to the goal
line plus the seventeen the snap and the spot cost. A stated distance is
answered even with no game on; the ball spot needs a live one, chosen by the
same order of preference as `/fourthdown`.

Which game gets answered is a stated order of preference rather than a guess.
Discord tells the bot nothing about where a person is sitting and ESPN publishes
no regional broadcast map, so "the game on near me" cannot be answered honestly.
Instead: the Ravens if they are playing, then whichever team `SECONDARY_TEAM`
names, then whatever kicked off most recently. Naming a team in the command
overrides all of it.

The recommendation itself is computed in `ravens_bot/fourthdown.py`. The
published fourth down bot, `nfl4th`, is an R package, and nflverse distributes
no per-situation decision feed, so there is nothing to look the answer up in.
The module uses bundled, historically estimated league-average curves: ordinary
fourth-down conversion by distance (goal-to-go separately), field-goal success
by kick distance, the receiving team's next field position after an ordinary
punt, and first-down expected points of the next score in the same half.
`ravens_bot/data/decision_calibration.json` contains the compact model,
sample counts, source SHA-256 hashes, release/retrieval dates, filtering,
smoothing assumptions, and holdout losses. Scoring is deterministic and offline;
neither command downloads seasons or needs runtime ML dependencies. Docker's
existing package copy includes the artifact. Missing/corrupt artifacts fail
explicitly rather than silently falling back.

The estimates use complete **2022-2024 regular seasons and postseason**, with
**2025 held out from fitting**. Triangular local smoothing shrinks toward the
original hand-set curves (10 pseudo-observations; 20 for goal-to-go), followed by
weighted monotone regression. Goal-to-go's prior uses the training-only open-field
fit. Sparse extreme distances therefore remain prior-sensitive rather than
turning one long make into a confident recommendation. The command shows
nearby training support (`n` counts observations with positive local smoothing
weight, not independent games or effective sample size), vintage and limits.
Distances outside supported nodes clamp to endpoints; kicks beyond 66 yards
remain intentionally out of range, not a claim that longer kicks are impossible.

Each curve ships only if its overall 2025 loss improves over the original
hand-set baseline. Binary curves must improve both Brier score and log loss;
continuous outcomes must improve mean squared error (MSE). Current results:

| Component | Training / holdout observations | Baseline → historical holdout loss |
|---|---:|---:|
| Open-field fourth-down conversion | 2,100 / 807 | Brier 0.225991 → 0.224767 |
| Goal-to-go conversion | 204 / 87 | Brier 0.260073 → 0.257880 |
| Field goals | 3,326 / 1,120 | Brier 0.113670 → 0.111124 |
| Ordinary punts | 5,916 / 1,786 | MSE 80.943998 → 74.996551 yards squared |
| First-down next-score EP | 29,691 / 9,562 | MSE 21.841367 → 21.783385 points squared |

These are descriptive component checks, **not a causal backtest of go/kick/punt
recommendations**, proof of improvement in every situation, or a significance
claim. Attempt selection is observational, plays within games are correlated,
and 2025 is a model-selection holdout, not an untouched final test. Small gains,
especially EP and goal-to-go, should not be read as precise advantages.

Filtering excludes preseason/OT, deleted/no-play records and games without a
known final result. Conversion includes sacks as failures but excludes kneels,
spikes, penalties and special-teams/fake plays. Some fake kicks have
`special_teams_play=0`, so descriptions containing "fake", "punt formation" or
"field goal formation" are also excluded from scrimmage samples.
Field-goal attempts on any down
include blocks as misses and exclude penalties. Punt training excludes blocks,
fumbles, scores and penalties and requires a subsequent receiving-team first
down in the same half. EP uses first-and-10/goal with at least five minutes in
the half and a score margin of at most 14; its observed signed next score is
TD 6.95, FG 3, safety 2 or zero for no remaining score. It does **not** train
against nflfastR's published EP predictions. Nonfinite numeric data is rejected
as missing; final scores are outcome labels, never WP predictors.

Expected points is the right objective until the clock decides the result: a
team down eight with a minute left should go for it on fourth-and-goal from
anywhere, and no arrangement of points curves says so. So `ravens_bot/winprob.py`
adds a win probability layer on top of the curves rather than inside them. It
reads ESPN's display clock as a number of seconds, counts the periods still to
come, and prices a margin against the spread of the points still to be scored,
which grows with the square root of the time left. Each option's outcomes —
convert or fail, make or miss, the punt's landing spot — are carried through to
the game state they leave behind and scored there, and the ranking is on win
probability. The embed prices each option in win probability instead of points
and the footer says which model answered.

The current **WP layer is still the original score/clock approximation**, not
historically calibrated WP. A training-only two-slope logistic fit was evaluated
on 43,257 training and 14,038 holdout first-down states. Its Brier score improved
overall (0.174989 → 0.173757) and in the final five minutes
(0.109429 → 0.103629), but regressed in the final two minutes of the first half
(0.172743 → 0.176925; 1,013 states). Log loss regressed there too. The candidate
also capped possession value at the half boundary. It was **rejected**: both
original slopes and the original WP possession-value curve/clock treatment are
retained, so the deployed WP state estimator matches the evaluated baseline.
The historical conversion, kick and punt estimates still weight decision
outcomes; the historical EP curve supplies the expected-points alternative.
The footer explicitly distinguishes these from the retained WP approximation.

The clock is not always published: between periods, and on a down ESPN has not
filled in, there is no time and sometimes no score. Those downs fall back to the
expected points ranking exactly as before, keep the caveats explaining what the
answer cannot see, and say so in the footer.

Three limits are stated in the embed footer rather than hidden:

- Every number is a league average, so it knows nothing about the two teams
  actually playing. This is the sharpest difference from `nfl4th`, which reads
  the closing point spread to know who is on the field.
- Nobody publishes timeouts on the scoreboard route this reads, so two minutes
  with three timeouts and two minutes with none are the same game here.
- Play duration, touchdown/PAT value, post-score kickoff treatment and outcome
  transitions remain heuristics. The points curve is **not** a net kickoff
  valuation. End-half drive timing, timeouts, conversion strategy and OT rules
  are not calibrated. Existing zero-clock and overtime approximations remain;
  they should not be mistaken for rule-complete late-game strategy.

### Regenerating the decision estimates

Run from the repository root with Python 3.12+; no Discord configuration, bot
startup, pandas, numpy or scipy is needed. Keep the raw cache **outside** the
repository (about 80 MB compressed). In PowerShell:

```powershell
python tools\calibrate_decisions.py --cache-dir "$env:TEMP\castle-nflverse-pbp" --download
```

With the four gzip files cached, omit `--download` for a fully offline rebuild.
Use `--output <path>` to write a comparison artifact; identical pinned inputs
produce identical output bytes. Source URLs, hashes, release times and the
2026-09-16 retrieval provenance are pinned in the generator/artifact, not replaced
with a new timestamp on every rebuild. If nflverse revises a release asset, a
hash mismatch stops regeneration: review and repin source provenance deliberately,
then rerun and review all holdout evidence before shipping a new vintage. No
holdout outcomes enter curve smoothing, goal priors or WP parameter fitting.
Do not tune parameters repeatedly to the same holdout and call it independent
validation. The generator writes only the compact artifact, never raw PBP into
the repository.

Derived data attribution: [nflverse/nflverse-data](https://github.com/nflverse/nflverse-data),
including nflfastR play-by-play, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
([source license](https://github.com/nflverse/nflverse-data/blob/master/LICENSE.md)).
The bundled artifact modifies that data by filtering, aggregating, smoothing and
calibrating it. nflverse is credited, not represented as endorsing this model.

## Data source

This project uses ESPN's public NFL site/core APIs and the official Ravens
injury report page. It does not require API keys.

Two ESPN behaviours are worth knowing, since both look like bugs otherwise:

- A transactions query for one date also returns the following day's moves. Each
  item is stamped at midnight Pacific on the day it happened, so the parser keeps
  only the date it asked for; accepting the extra day would report the same move
  on two consecutive dates.
- The `teams` query parameter on the transactions endpoint is ignored — the feed
  comes back league-wide either way — so Ravens moves are filtered client side.

### Trades

A trade arrives on the same feed as every other move, but it is the one move
whose verb does not say which way a player travelled. ESPN writes the same kind
of deal several ways — "Traded *player* to *club* for *pick*", "Traded *pick* to
*club* for *player*", "Acquired *player* from *club* in exchange for *pick*",
"Acquired *pick* from *club* for *player*", "Received *player* from *club* in
exchange for *pick*", "Received *pick* in a trade with *club*" — so reading the
opening verb calls half of them arrivals and the other half departures.
`ravens_bot/trades.py` takes the direction from which side of the sentence each
asset sits on instead, which is what keeps a departing player from being
announced with the arriving player's billing.

Three more details make a trade unlike the moves around it:

- The feed carries a team reference for the Ravens only, never for the other
  club, so the partner is resolved from the words. ESPN names it by city, in
  full, or by nickname, and under whatever name the club traded as at the time,
  so the directory covers all of those. "New York" and "Los Angeles" are left
  unresolved, since each names two clubs and ESPN spells those out anyway.
- Picks are carried through in ESPN's own wording rather than parsed into
  structured data. "A conditional 2023 sixth-round draft pick which could become
  a fifth-round pick" cannot be restated without either losing the condition or
  inventing one, and an unfamiliar asset — cash, a swap — survives into the post
  the same way.
- The feed is copy, not a database, and it is published with typos: "Trade"
  without the *d* alongside "Singed" and "Re-singed" elsewhere. The opening verb
  is matched loosely enough to survive that, while a sentence still has to name
  a real club or spell out an exchange before it counts as a deal, so a practice
  squad signing or a waiver claim is never mistaken for one.

A description often runs a trade together with unrelated moves in one item, so
the trade's own sentence is separated out first and the rest is posted under
"Also" rather than being read as part of the deal.

### Injuries

The injury report shown at `espn.com/nfl/team/injuries/_/name/bal` is served by
`{site}/teams/bal/injuries`. The route answers with either a flat `injuries`
list or one group per team, each holding its own list, and a core-API query
answers with `$ref` links instead, so the parser accepts all three. Names come
back without a position or headshot often enough that the team roster is merged
in for art, the same way transactions are; a roster outage drops the art rather
than the report.

The feed only moves on practice-report days, so most polls are no-ops. A player
is re-announced when their status or ESPN's update stamp changes, and a target
with no injury history is sent one consolidated report rather than a message per
player already on the list.

An activation reaches the bot on both feeds, minutes apart, so each poll pairs
an update with the roster move naming that player and posts the two together.
Players are matched on the athlete id when both feeds carry one and on the name
otherwise, since a description spells a name the way ESPN wrote it. An update is
claimed by at most one move, and one that no move accounts for — or that arrives
after its move was already posted — is announced on its own as before.

### Snap counts

`ravens_bot/snapcounts.py` reads **Pro Football Reference via nflverse**, the
source identified by [nflreadr's `load_snap_counts` documentation](https://nflreadr.nflverse.com/reference/load_snap_counts.html).
Season CSVs come from the nflverse-data `snap_counts` release
(`snap_counts_{season}.csv`), not NFL GSIS game books or ESPN.

Player links and headshots use `pfr_player_id` from the snap CSV, joined to
`pfr_id` and `espn_id` in the `players/players.csv` release crosswalk. This works
for historical players who are no longer on the current ESPN roster. A missing
ID can use an unambiguous normalized full-name fallback; known conflicting IDs
are never overridden by names. A crosswalk transport outage logs a warning and
keeps snap counts available with safe roster fallback. Malformed CSV schemas
or measurements are reported as source errors rather than unpublished data.

Details worth knowing:

- Snap counts trail the final whistle by hours, so a game with no published
  numbers is reported as pending rather than as an error.
- The immediately preceding completed regular-season or postseason Ravens game
  is the comparison (preseason is excluded), even
  across a bye, playoffs, or a season boundary. An extra game is fetched for
  context but excluded from requested totals. An unpublished game is never
  skipped to compare against an older published game, and an unpublished latest
  game is never replaced by the prior game.
- Changes use the published unit shares: 60% after 45% is **+15.0 pp**, not a
  15% relative increase. Missing shares, a missing prior report, and players
  absent from either report show **N/A**, not zero. Explicit zero shares can
  produce a real decline. Previously listed players are called out in the team
  report, while named-player breakdowns mark unlisted and unpublished games.
  Aggregate shares continue to include only games where the player is listed.
- Source corrections and new games can take up to six hours to appear because
  the season CSV is cached.
- The file states each player's share of a unit rather than the unit's total, so
  the denominator is rebuilt from the counts and shares and the value most of a
  unit agrees on is used. A count larger than that total is printed on its own
  rather than as a share above 100%.
- A Ravens game is matched to the file by season, opponent, and whether the
  Ravens were at home, with the regular season flag separating a playoff rematch
  from the regular season meeting it repeats.
