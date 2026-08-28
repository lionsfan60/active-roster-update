# ActiveRosterUpdate

Pulls the real NFL roster — who's on the team, who's hurt, how good they are this week — and
writes it into Axis Football's roster files. No Madden install, no logins, no paid feeds.

**Built on JayBeeLove90's work.** This needs his *Axis Football 26 NFL Mod* installed — the team
folders, uniforms, helmets, logos, fields and coaching staff are all his. ActiveRosterUpdate only
rewrites the roster files inside it and adds portraits. Without his mod there is nothing for it to
update. He also publishes a *Player Portraits* pack for the mod, if you would rather use his
portraits than the headshots this fetches.

JayBeeLove90's mod, which you need first:
- NFL mod (2025 season rosters): https://www.mediafire.com/file/ep4qoqm4tygwr89/Axis_Football_26_NFL_Mod.zip/file
- Player Portraits pack: https://www.mediafire.com/file/r7k8piufb21xul0/Player_Portraits.zip/file

## Where the data comes from

| What | Source | Notes |
|---|---|---|
| Who is on each roster, jersey, position, height/weight/age | ESPN `teams/{team}/roster` | live, updated on signings/cuts |
| Depth chart order | ESPN `depthcharts` (core API) | drives who starts in Axis |
| Injuries | ESPN `injuries` (league-wide) | ~800 entries: Out / Questionable / IR / PUP / suspension |
| Ratings + X-Factor & Superstar abilities | EA `drop-api.ea.com/rating/madden-nfl` | every attribute for ~2,000 players, **re-published every week of the season** |
| Next opponent | ESPN `teams/{team}/schedule` | powers `--gameday` |

The ratings endpoint is the one that matters: it's the feed behind the published ratings pages,
and it's republished as a new "iteration" each week (`weeks` lists them). Weekly roster and
ratings updates are a thing another football franchise has long promised — this reads them
directly and puts them in the game you're actually playing.

## Install

Python 3.10+ and nothing else — the whole thing is standard library.

```
cd path\to\ActiveRosterUpdate
python -m activerosterupdate status
```

## Commands

```
python -m activerosterupdate status                 # install found? feeds alive? teams linked?
python -m activerosterupdate weeks                  # list EA's weekly ratings drops
python -m activerosterupdate link                   # match the 32 clubs to Axis team-mod folders
python -m activerosterupdate sync --gameday DET     # Lions + next opponent -> ./out
python -m activerosterupdate sync --gameday DET --apply    # ... straight into the game (with backup)
python -m activerosterupdate sync                   # all 32 clubs
python -m activerosterupdate sync --week 9-week-8   # ratings as of a specific week
python -m activerosterupdate report --team DET      # who got left off and why
python -m activerosterupdate roster --team DET      # print a roster as the game has it
python -m activerosterupdate fix-mod                # repair a 2026-era mod for 2027
python -m activerosterupdate fields                 # team fields, endzones, city stadiums
python -m activerosterupdate portraits --all        # real headshots for every player
python -m activerosterupdate index-portraits        # record who owns which portrait slot
python -m activerosterupdate restore                # undo: put the backed-up ROSTER.CSVs back
```

`sync` writes to `./out` unless you pass `--apply`. Every `--apply` run copies the file it is
about to overwrite into `backups/<timestamp>/`.

## How a roster gets built

1. Pull the club's live ESPN roster and depth chart.
2. Drop anyone whose injury status is in `exclude_statuses` (Out, IR, PUP, NFI, suspended,
   doubtful by default) — so the backup automatically starts, same as the real gameday roster.
3. Players listed Questionable stay, with a small `questionable_penalty` haircut to
   SPEED / AGIL / FITNESS.
4. Attach that week's EA ratings, matched on name + birthdate.
5. Blend EA's ~54 attributes into Axis's 17, per `mapping/attributes.json`.
6. Apply X-Factor / Superstar bumps from `mapping/abilities.json` — Axis has no ability system,
   so an ability becomes a targeted attribute boost (Shutdown → COVER, Unstoppable Force → BLK BRK).
7. Fill the 53 by `mapping/squad.json` (3 QB, 4 RB, 6 WR, 3 TE, 9 OL, 8 DL, 7 LB, 10 DB, K, P),
   depth-chart order first, best-available to top up.
8. Write the CSV using the **installed game's own header row** as the schema.

Step 8 is deliberate: Axis changed this file between releases (2026 added the
`KickReturnerIndex=` / `PuntReturnerIndex=` directive lines). Whatever header and directive lines
your copy ships with are preserved verbatim; only player rows are rewritten. Cosmetic columns
(SKIN, PORTRAIT, VISOR, SLEEVES, BANDS, WRAPS) are carried over for players already in the file,
and otherwise picked deterministically from the values your game already uses.

## Gameday inactives

The injury feed knows who's Out, but the official inactives list drops 90 minutes before kickoff.
Put those names in `mapping/overrides.json` and re-run:

```json
{ "DET": { "out": ["Player Name"], "in": ["Player Wrongly Listed Hurt"] } }
```

## Tuning

Everything shapeable lives in `mapping/` as plain JSON — edit and re-run, no code changes:

- `attributes.json` — how EA attributes blend into Axis columns, with per-position overrides
  (a QB's AWARE shouldn't be diluted by playRecognition, a defender's stat).
- `positions.json` — NFL position → Axis's 10 coarse positions.
- `squad.json` — how many of each position to carry.
- `abilities.json` — ability → attribute bumps.
- `overrides.json` — manual in/out.
- `teams.json` — club → Axis team-mod folder (written by `link`, editable).

`config.json` holds injury policy, roster size, and the Axis install path if auto-detect misses.

## Running it weekly

Windows Task Scheduler, Wednesday nights after the first injury reports:

```
schtasks /create /tn "Axis roster sync" /tr "python -m activerosterupdate sync --apply" ^
  /sc weekly /d WED /st 20:00 /rl limited
```

## Axis 2027 mod compatibility

The 2026-era NFL team mods freeze Axis 2027 at kickoff. Three separate incompatibilities, all
repaired by `fix-mod`, which backs up everything it touches:

1. **Missing `FACEMASK_COLOR`.** Every uniform file predates the key 2027 requires, so the game
   throws `KeyNotFoundException` while building the uniform set and hangs. This is the freeze.
2. **Invalid colour values.** 2026 wrote `N` for "no outline" on jersey numbers, and `HELMET_TYPE=N`
   where an index belongs. 2027 parses both as real values and logs
   `ConvertToColor("N"): Invalid Color String` — 195 of them across the pack. Outlines are set to
   match the number fill, preserving the intent.
3. **Dead playbook names.** Several teams ask for `Singleback`, which 2027 doesn't have, so they
   fall back to playbook 0.

Position codes `LB` and `S` were also dropped in 2027 — the sync rewrites those rows to `ILB`/`OLB`
and `FS`/`SS` automatically.

## Fields and stadiums

`fields` writes the part of the 2027 field block that actually looks right: `HomeStadium`, endzone
colours from each team's own `TEAM.TXT`, and the turf settings. 26 of the 32 NFL cities have a
matching built-in stadium; the rest keep the default. Axis's stadiums are the game's own venues —
`HomeStadium` chooses among them, it cannot produce a real one.

Two key groups are deliberately **not** written. `FieldMaskColor*` with `FieldColorMaskIndex` and
`LineMaskIndex` paints a coloured stripe across the field, and the `Upright*` keys recolour the
goalposts. Both were tried, looked wrong in game, and were rolled back.

Editing a team in the in-game **Team Suite** works on modded teams, contrary to the usual community
warning: it rewrites that team's `TEAM.TXT` in the game's own format and leaves `ROSTER.CSV`
untouched. `fix-mod` recognises those files (`OffPlaybookSource` naming a real playbook) and leaves
them alone rather than undoing the edit.

## Player portraits

`portraits --team DET` (or `--all`) fetches ESPN headshots, crops them to the 128×128 RGBA the game
wants, and writes them into `Mods/Player Portraits/Skin Tone N/{Large,Small}/<id>.png`, pointing each
roster row at its own slot. `index-portraits` then records ESPN id → slot in `data/portraits.json`,
so later syncs keep players on their own face and fetch one for anyone new. Slots are unique
league-wide; a player with no published headshot keeps the generated face he had.

## Caveats

- Axis needs NFL team-mod folders to exist before `--apply` has anywhere to write. `link` pairs
  them up; unmatched clubs get listed so you can fix `mapping/teams.json` by hand.
- EA's ratings lag real transactions by a few days — a player traded Tuesday still carries his old
  team's ratings, which is fine, since team assignment comes from ESPN, not EA.
- Players EA doesn't rate (rookies, camp bodies) get an estimated profile built from a real player
  at the same position and tier, with deterministic jitter. `report` lists exactly who those are.
