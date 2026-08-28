"""Command line entry point:  python -m activerosterupdate <command>"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import build, config, portraits as portraits_mod
from . import positions
from .axis import detect, roster_csv
from .match import RatingsIndex
from .sources import ea_ratings, espn

ROOT = config.ROOT
OUT = ROOT / "out"
BACKUPS = ROOT / "backups"
NEWLINE = "\n"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- commands


def cmd_status(args, cfg) -> int:
    game = detect.find_game(cfg)
    _p(f"Axis install : {game or 'NOT FOUND (set axis_dir in config.json)'}")
    if game:
        mods = detect.mods_dir(game)
        _p(f"Team mods    : {mods or 'no Mods/Team Mods folder yet'}")
        if mods:
            folders = detect.team_folders(mods)
            _p(f"Team folders : {len(folders)} ({', '.join(folders[:4])}{'...' if len(folders) > 4 else ''})")
    its = ea_ratings.iterations()
    _p(f"EA ratings   : {len(its)} weekly drops available, newest = {its[0]['label'] if its else 'n/a'}")
    teams = espn.teams()
    inj = espn.injuries()
    _p(f"ESPN feeds   : {len(teams)} teams, {len(inj)} players on the injury report")
    links = detect.load_links()
    _p(f"Team linkage : {len(links)} clubs mapped to folders (mapping/teams.json)")
    return 0


def cmd_link(args, cfg) -> int:
    game = detect.find_game(cfg)
    if not game:
        _p("Axis install not found. Set axis_dir in config.json and retry.")
        return 2
    mods = detect.mods_dir(game)
    if not mods:
        _p(f"No team-mod folder under {game}. Install an NFL team mod pack first.")
        return 2
    folders = detect.team_folders(mods)
    links = detect.auto_link(espn.teams(), folders)
    path = detect.save_links(links)
    _p(f"Linked {len(links)}/32 clubs -> {path}")
    missing = [t["abbrev"] for t in espn.teams() if t["abbrev"] not in links]
    if missing:
        _p(f"Unmatched (add by hand): {', '.join(missing)}")
    return 0


def _template_for(folder: Path | None) -> roster_csv.RosterFile:
    """Use the game roster file as the schema template; fall back to the bundled sample."""
    if folder:
        for name in ("ROSTER.CSV", "Roster.csv", "roster.csv"):
            f = folder / name
            if f.exists():
                return roster_csv.parse(f)
    return roster_csv.parse(config.TEMPLATES / "ROSTER.sample.csv")


def cmd_sync(args, cfg) -> int:
    season = build.current_season(cfg)
    iteration = args.week or cfg["ratings_iteration"]
    if iteration == "latest":
        latest = ea_ratings.latest_iteration()
        iteration = latest["id"] if latest else None
        _p(f"Ratings      : {latest['label'] if latest else 'default'}")
    elif iteration == "default":
        iteration = None

    _p("Fetching EA ratings ...")
    ea_players = ea_ratings.fetch_players(iteration)
    ratings = RatingsIndex(ea_players)
    _p(f"             : {len(ea_players)} rated players")

    _p("Fetching ESPN rosters, depth charts and injuries ...")
    league_injuries = espn.injuries()
    teams = espn.teams()

    picked = list(args.team or [])
    if args.all:
        picked = []
    elif args.gameday is not None:
        me = (args.gameday or cfg.get("favorite_team") or "").upper()
        if not me:
            _p("No team named and no favorite_team set in config.json.")
            return 2
        game = espn.next_game(me)
        if not game:
            _p(f"No scheduled game found for {me}.")
            return 2
        kickoff = game["date"].astimezone().strftime("%a %d %b %H:%M")
        _p(f"Next up      : {game['name']}  ({game['season_type']} week {game['week']}, {kickoff})")
        picked += [me, game["opponent"]]
    wanted = [t for t in teams if not picked or t["abbrev"].upper() in picked]

    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    links = detect.load_links()
    if mods and not links:
        links = detect.auto_link(teams, detect.team_folders(mods))
        detect.save_links(links)

    store_p = portraits_mod.PortraitStore(mods.parent, ROOT / "backups") if mods else None
    ledger = portraits_mod.Ledger(ROOT / "data" / "portraits.json")

    target_root = Path(args.out) if args.out else (mods if (args.apply and mods) else OUT)
    target_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report: dict = {
        "generated": stamp, "season": season, "ratings_iteration": iteration or "default",
        "applied_to": str(target_root), "teams": {},
    }

    for team in wanted:
        players = build.gather_team(team, ratings, cfg, season, league_injuries)
        profiles = build.synth_profiles(players)
        for p in players:
            if not p.ratings:
                build.fill_unrated(p, profiles, cfg)

        folder_name = links.get(team["abbrev"], team["display"])
        game_folder = (mods / folder_name) if mods else None
        rf = _template_for(game_folder if (game_folder and game_folder.exists()) else None)
        layout = positions.template_layout(rf)
        squad, benched = build.pick_squad(players, cfg, team["abbrev"], layout)
        rows = build.build_rows(squad, rf, cfg)

        dest_dir = target_root / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "ROSTER.CSV"

        if dest.exists() and cfg["backup"] and args.apply:
            bdir = BACKUPS / stamp / folder_name
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, bdir / "ROSTER.CSV")

        if not args.no_portraits and mods and store_p and store_p.exists():
            added = _sync_portraits(squad, rows, rf, store_p, ledger)
            if added:
                _p(f"     + {added} new portrait{'s' if added != 1 else ''} fetched")

        if args.dry_run:
            _p(f"[dry-run] {team['display']}: {len(rows)} players, {len(benched)} unavailable -> {dest}")
        else:
            roster_csv.write(dest, rf, rows)

        report["teams"][team["abbrev"]] = {
            "folder": folder_name,
            "written": len(rows),
            "unavailable": [
                {"player": p.full, "pos": p.pos_nfl, "reason": why} for p, why in benched
            ],
            "unrated": [p.full for p in squad if p.match in ("synth", "none")],
            "renumbered": [
                f"{p.full} #{p.jersey} -> #{row[rf.column('NUMBER')]}"
                for p, row in zip(squad, rows)
                if rf.column("NUMBER") and str(p.jersey) != row[rf.column("NUMBER")]
            ],
            "fuzzy_matched": [p.full for p in squad if p.match == "fuzzy"],
            "starters": [
                {"pos": p.pos_axis, "player": p.full, "ovr": p.overall}
                for p in squad[:11]
            ],
        }
        out_line = f"{team['abbrev']:>3}  {len(rows):>2} players"
        out_line += f" | {len(benched):>2} out/IR | {len(report['teams'][team['abbrev']]['unrated']):>2} unrated"
        _p(out_line)

    ledger.save()

    rep_path = ROOT / "out" / f"report-{stamp}.json"
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    rep_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (ROOT / "out" / "report-latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    _p("")
    _p(f"Wrote rosters to {target_root}")
    _p(f"Report: {rep_path}")
    if not args.apply:
        _p("(nothing touched the game - re-run with --apply once the team folders are linked)")
    return 0


def _sync_portraits(squad, rows, rf, store, ledger) -> int:
    """Keep every player on his own portrait, and fetch one for anyone new."""
    c_skin, c_portrait = rf.column("SKIN"), rf.column("PORTRAIT")
    if not (c_skin and c_portrait):
        return 0

    fetched = 0
    newcomers = []
    for player, row in zip(squad, rows):
        known = ledger.get(player.espn_id)
        if known:
            # he already owns a slot - make sure the row still points at it
            row[c_skin] = known["skin"]
            row[c_portrait] = str(known["id"])
        else:
            newcomers.append((player, row))

    if not newcomers:
        return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        shots = list(pool.map(lambda pr: portraits_mod.fetch_headshot(pr[0].espn_id), newcomers))

    for (player, row), img in zip(newcomers, shots):
        if img is None:
            continue                      # no photo published - he keeps the generated face
        skin = (row[c_skin] or "1").strip()
        new_id = portraits_mod.next_free_id(store, ledger, skin)
        store.write(skin, new_id, portraits_mod.to_portrait(img))
        ledger.set(player.espn_id, skin, new_id)
        row[c_portrait] = str(new_id)
        fetched += 1
    return fetched


def cmd_report(args, cfg) -> int:
    path = ROOT / "out" / "report-latest.json"
    if not path.exists():
        _p("No report yet - run a sync first.")
        return 2
    rep = json.loads(path.read_text(encoding="utf-8"))
    _p(f"Generated {rep['generated']}  |  ratings: {rep['ratings_iteration']}  |  -> {rep['applied_to']}")
    for abbr, t in rep["teams"].items():
        if args.team and abbr.upper() not in args.team:
            continue
        _p("")
        _p(f"== {abbr} ({t['folder']}) - {t['written']} players")
        if t["unavailable"]:
            _p("   sidelined:")
            for u in t["unavailable"][:40]:
                _p(f"     - {u['player']:<24} {u['pos']:<3} {u['reason']}")
        if t["unrated"]:
            _p(f"   no EA rating (estimated): {', '.join(t['unrated'])}")
        if t.get("renumbered"):
            _p(f"   jersey clash resolved: {', '.join(t['renumbered'])}")
    return 0


def cmd_roster(args, cfg) -> int:
    """Print the roster the game will actually read, straight from its own file."""
    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2
    links = detect.load_links()
    abbrev = (args.team or cfg.get("favorite_team") or "").upper()
    folder = links.get(abbrev)
    if not folder:
        _p(f"No team folder linked for {abbrev}. Run 'link' first.")
        return 2

    rf = _template_for(mods / folder)
    col = {name: rf.column(name) for name in
           ("NUMBER", "FIRST", "LAST", "POS", "AGE", "SPEED", "AWARE", "Height", "Weight")}
    _p(f"{folder}  -  {len(rf.rows)} players")
    _p(f"{'#':>3}  {'pos':<4} {'player':<26} {'age':>3} {'ht':>4} {'wt':>4} {'spd':>4} {'awr':>4}")
    _p("-" * 62)
    for row in rf.rows:
        def g(k):
            return row.get(col[k], "") if col[k] else ""
        ht = g("Height")
        ht = f"{int(ht)//12}-{int(ht)%12}" if ht.isdigit() else ht
        _p(f"{g('NUMBER'):>3}  {g('POS'):<4} {g('FIRST') + ' ' + g('LAST'):<26} "
           f"{g('AGE'):>3} {ht:>4} {g('Weight'):>4} {g('SPEED'):>4} {g('AWARE'):>4}")
    return 0


def cmd_portraits(args, cfg) -> int:
    """Replace a team's portraits with real player photos."""
    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2
    store = portraits_mod.PortraitStore(mods.parent, ROOT / "backups")
    if not store.exists():
        _p(f"No Player Portraits folder under {mods.parent}.")
        return 2

    links = detect.load_links()
    if args.all:
        wanted = sorted(links)
    else:
        wanted = [(args.team or cfg.get("favorite_team") or "").upper()]
    if len(wanted) > 1:
        rc = 0
        for abbr in wanted:
            args.team, args.all = abbr, False
            rc |= _portraits_one(args, cfg, store, links)
        return rc
    return _portraits_one(args, cfg, store, links)


def _portraits_one(args, cfg, store, links) -> int:
    mods = store.root.parent / "Team Mods"
    abbrev = (args.team or cfg.get("favorite_team") or "").upper()
    folder = links.get(abbrev)
    if not folder:
        _p(f"No team folder linked for {abbrev}. Run 'link' first.")
        return 2

    roster_path = mods / folder / "ROSTER.CSV"
    rf = roster_csv.parse(roster_path)
    c_first, c_last = rf.column("FIRST"), rf.column("LAST")
    c_skin, c_portrait = rf.column("SKIN"), rf.column("PORTRAIT")
    if not all((c_first, c_last, c_skin, c_portrait)):
        _p("That roster file has no SKIN/PORTRAIT columns.")
        return 2

    espn_ids = {p.full: p.espn_id for p in espn.roster(abbrev)}
    _p(f"{folder}: fetching {len(rf.rows)} headshots ...")

    # fetch in parallel - a team is ~53 round trips to the same CDN
    names = [f"{row[c_first]} {row[c_last]}".strip() for row in rf.rows]
    with ThreadPoolExecutor(max_workers=8) as pool:
        shots = dict(zip(names, pool.map(
            lambda n: portraits_mod.fetch_headshot(espn_ids[n]) if espn_ids.get(n) else None,
            names)))

    # Portrait folders are shared by every team, so ids have to be unique league-wide or
    # one team's photos land on another's players. Each run continues past the highest id
    # already in that skin tone's folder.
    next_id: dict[str, int] = {}
    if not args.reuse_low_ids:
        for skin in {(row[c_skin] or "1").strip() for row in rf.rows}:
            ids = store.available_ids(skin)
            next_id[skin] = max(ids) if ids else 0
    done = missing = 0
    for row in rf.rows:
        name = f"{row[c_first]} {row[c_last]}".strip()
        skin = (row[c_skin] or "1").strip()
        img = shots.get(name)
        if img is None:
            missing += 1
            _p(f"   no photo: {name}")
            continue
        n = next_id.get(skin, 0) + 1
        next_id[skin] = n
        store.write(skin, n, portraits_mod.to_portrait(img))
        row[c_portrait] = str(n)
        done += 1

    roster_csv.write(roster_path, rf, rf.rows)
    _p("")
    _p(f"photos written : {done}")
    _p(f"no photo found : {missing} (kept their existing portrait)")
    _p(f"backup         : {store.backup}")
    _p("Restart the game to see them.")
    return 0


def cmd_index_portraits(args, cfg) -> int:
    """Record the portrait slot every current player owns, so syncs can keep them."""
    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2
    ledger = portraits_mod.Ledger(ROOT / "data" / "portraits.json")
    links = detect.load_links()
    recorded = unmatched = 0
    for abbrev, folder in sorted(links.items()):
        rf = _template_for(mods / folder)
        c_first, c_last = rf.column("FIRST"), rf.column("LAST")
        c_skin, c_portrait = rf.column("SKIN"), rf.column("PORTRAIT")
        ids = {p.full: p.espn_id for p in espn.roster(abbrev)}
        for row in rf.rows:
            name = f"{row[c_first]} {row[c_last]}".strip()
            espn_id = ids.get(name)
            if espn_id:
                ledger.set(espn_id, row[c_skin], int(row[c_portrait] or 0))
                recorded += 1
            else:
                unmatched += 1
    ledger.save()
    _p(f"recorded {recorded} portrait slots ({unmatched} rows had no ESPN match)")
    _p(f"ledger: {ledger.path}")
    return 0


def cmd_fix_mod(args, cfg) -> int:
    """Repair a 2026-era team mod so Axis 2027 will load it.

    Two things stop the older NFL mods dead on 2027: uniform files written before
    FACEMASK_COLOR existed (the game throws KeyNotFoundException the moment it builds a
    uniform set, so every match freezes at kickoff), and playbook names no longer in the
    game's list, which silently fall back to playbook 0.
    """
    import re
    import shutil

    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = ROOT / "backups" / f"modfix-{stamp}"
    valid_playbooks = {"west coast", "power run", "air raid"}

    OUTLINE_KEYS = ("NUMBER_OUTLINE1_COLOR", "NUMBER_OUTLINE2_COLOR", "LETTER_OUTLINE_COLOR",
                    "ARM_BAND_COLOR", "ARM_SLEEVE_COLOR", "VISOR_COLOR")

    faces = books = colours = helmets = 0
    for team in sorted(d for d in mods.iterdir() if d.is_dir()):
        for f in team.rglob("*.txt"):
            text = f.read_text(encoding="utf-8", errors="replace")
            if "UNIFORM_NAME" not in text:
                continue

            # 2026 wrote "N" for "no outline"; 2027 parses these as colours and rejects it,
            # so match the number fill - an outline you cannot see - and give it something valid
            before = text
            fill = re.search(r"^NUMBER_FILL_COLOR=(.+)$", text, re.M)
            fill_value = fill.group(1).strip() if fill else "255, 255, 255"
            for key in OUTLINE_KEYS:
                if re.search("^" + key + r"=N\s*$", text, re.M):
                    text = re.sub("^" + key + r"=N\s*$", key + "=" + fill_value, text, flags=re.M)
                    colours += 1
            if re.search(r"^HELMET_TYPE=N\s*$", text, re.M):
                text = re.sub(r"^HELMET_TYPE=N\s*$", "HELMET_TYPE=0", text, flags=re.M)
                helmets += 1
            if text != before:
                rel = f.relative_to(mods)
                (bak / rel).parent.mkdir(parents=True, exist_ok=True)
                if not (bak / rel).exists():
                    shutil.copy2(f, bak / rel)
                f.write_text(text, encoding="utf-8")

            if "FACEMASK_COLOR" in text:
                continue
            m = re.search(r"^HELMET_COLOR1=(.+)$", text, re.M)
            rel = f.relative_to(mods)
            (bak / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, bak / rel)
            colour = m.group(1).strip() if m else "0, 0, 0"
            if not text.endswith(NEWLINE):
                text += NEWLINE
            f.write_text(text + "FACEMASK_COLOR=" + colour + NEWLINE, encoding="utf-8")
            faces += 1

        tf = team / "TEAM.TXT"
        if tf.exists():
            text = tf.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^OffPlaybook=(.+)$", text, re.M)
            # A file the game has written names the playbook in OffPlaybookSource and leaves
            # OffPlaybook as a slot - "Default" is correct there. Rewriting it would undo an
            # edit made in Team Suite, so leave those files alone.
            source = re.search(r"^OffPlaybookSource=(.+)$", text, re.M)
            game_written = bool(source and source.group(1).strip().lower() in valid_playbooks)
            if game_written:
                pass
            elif m and m.group(1).strip().lower() not in valid_playbooks:
                rel = tf.relative_to(mods)
                (bak / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tf, bak / rel)
                tf.write_text(re.sub(r"^OffPlaybook=.+$", "OffPlaybook=West Coast", text, flags=re.M),
                              encoding="utf-8")
                books += 1

    _p(f"uniform files given a facemask colour : {faces}")
    _p(f"invalid outline colours repaired      : {colours}")
    _p(f"HELMET_TYPE=N corrected               : {helmets}")
    _p(f"team playbooks pointed at a real one  : {books}")
    _p(f"backup: {bak}" if (faces or books or colours or helmets) else "nothing needed fixing")
    return 0


def cmd_fields(args, cfg) -> int:
    """Give each modded team its own field, endzones and home stadium.

    Axis 2027 honours a block of field keys the 2026-era mods never had, so their teams
    play on a default field in a default stadium. This writes that block using the colours
    already in each team's TEAM.TXT - the mod author's own choices, not a guess - and points
    each club at the built-in stadium for its city where the game has one.
    """
    import re
    import shutil

    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2

    # cities Axis ships a stadium for; anything else keeps the default
    STADIUM_CITIES = {
        "ARIZONA", "ATLANTA", "BALTIMORE", "BUFFALO", "CAROLINA", "CHICAGO", "CINCINNATI",
        "CLEVELAND", "DALLAS", "DENVER", "DETROIT", "GREEN BAY", "HOUSTON", "INDIANAPOLIS",
        "KANSAS CITY", "LAS VEGAS", "LOS ANGELES", "MIAMI", "MINNESOTA", "PHILADELPHIA",
        "PITTSBURGH", "SAN FRANCISCO", "SEATTLE", "TENNESSEE", "WASHINGTON", "ST LOUIS",
    }

    def rgba(value: str, fallback: str) -> str:
        parts = [p.strip() for p in (value or "").split(",") if p.strip().isdigit()]
        if len(parts) < 3:
            parts = fallback.split(",")
        return "RGB: " + ", ".join(parts[:3]) + ", 255"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = ROOT / "backups" / f"fields-{stamp}"
    done = skipped = no_stadium = 0

    for team in sorted(d for d in mods.iterdir() if d.is_dir()):
        tf = team / "TEAM.TXT"
        if not tf.exists():
            continue
        text = tf.read_text(encoding="utf-8", errors="replace")
        values = dict(
            re.findall(r"^([A-Za-z0-9_]+)=(.*)$", text, re.M)
        )
        if "EndzoneColor0" in values and not args.force:
            skipped += 1
            continue

        primary = rgba(values.get("PrimaryColor", ""), "0,0,0")
        secondary = rgba(values.get("SecondaryColor", ""), "255,255,255")
        alternate = rgba(values.get("AlternateColor", ""), "255,255,255")
        city = (values.get("TeamCity", "") or "").strip().upper()

        block = []
        if city in STADIUM_CITIES:
            block.append(("HomeStadium", city))
        else:
            no_stadium += 1
        # Only the keys that actually look right in game. The field mask keys
        # (FieldMaskColor*/FieldColorMaskIndex/LineMaskIndex) paint a stripe across the
        # field, and the upright keys recolour the goalposts - both were rolled back after
        # seeing them, so they are deliberately not written here.
        block += [
            ("GrassColorIndex", "0"),
            ("MowedEffect", "0"),
            ("PressNormalIndex", "10"),
            ("EndzoneColor0", primary),
            ("EndzoneColor1", secondary),
            ("EndzoneColor2", alternate),
        ]

        rel = tf.relative_to(mods)
        (bak / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tf, bak / rel)

        lines = [ln for ln in text.splitlines() if ln.strip()]
        for key, value in block:
            lines = [ln for ln in lines if not ln.lower().startswith(key.lower() + "=")]
            lines.append(key + "=" + value)
        tf.write_text(NEWLINE.join(lines) + NEWLINE, encoding="utf-8")
        done += 1

    _p(f"teams given a field block : {done}")
    _p(f"already had one           : {skipped} (use --force to rewrite)")
    _p(f"no matching stadium city  : {no_stadium} (kept the default stadium)")
    if done:
        _p(f"backup: {bak}")
    return 0


def cmd_restore(args, cfg) -> int:
    if not BACKUPS.exists() or not any(BACKUPS.iterdir()):
        _p("No backups saved.")
        return 2
    stamp = args.stamp or sorted(d.name for d in BACKUPS.iterdir() if d.is_dir())[-1]
    src = BACKUPS / stamp
    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found - cannot restore.")
        return 2
    n = 0
    for folder in src.iterdir():
        f = folder / "ROSTER.CSV"
        if f.exists():
            shutil.copy2(f, mods / folder.name / "ROSTER.CSV")
            n += 1
    _p(f"Restored {n} roster files from backup {stamp}")
    return 0


def cmd_weeks(args, cfg) -> int:
    for it in ea_ratings.iterations():
        _p(f"{it['id']:<34} {it['label']}")
    return 0


# ---------------------------------------------------------------- parser


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ActiveRosterUpdate",
        description="Build gameday-accurate Axis Football rosters from live NFL data.",
    )
    ap.add_argument("--config", help="path to a config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show install, feeds and linkage")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("link", help="pair NFL clubs with Axis team-mod folders")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("weeks", help="list EA weekly ratings drops")
    s.set_defaults(fn=cmd_weeks)

    s = sub.add_parser("sync", help="fetch data and write rosters")
    s.add_argument("--apply", action="store_true", help="write into the game instead of ./out")
    s.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    s.add_argument("--team", nargs="*", help="limit to these ESPN abbreviations (e.g. KC BUF)")
    s.add_argument("--gameday", nargs="?", const="", metavar="TEAM",
                   help="sync a club and its next opponent; omit TEAM to use favorite_team")
    s.add_argument("--all", action="store_true",
                   help="sync all 32 clubs (the default when no team is named)")
    s.add_argument("--week", help="EA ratings iteration id, or 'latest'/'default'")
    s.add_argument("--out", help="write to this folder instead")
    s.add_argument("--no-portraits", action="store_true",
                   help="skip fetching photos for players new to the roster")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("roster", help="print a team's roster as the game has it")
    s.add_argument("--team", help="ESPN abbreviation (default: favorite_team)")
    s.set_defaults(fn=cmd_roster)

    s = sub.add_parser("portraits", help="replace a team's portraits with real photos")
    s.add_argument("--team", help="ESPN abbreviation (default: favorite_team)")
    s.add_argument("--all", action="store_true", help="every linked team")
    s.add_argument("--reuse-low-ids", action="store_true",
                   help="start at id 1, overwriting existing portraits (single-team testing only)")
    s.set_defaults(fn=cmd_portraits)

    s = sub.add_parser("index-portraits", help="record who owns which portrait slot")
    s.set_defaults(fn=cmd_index_portraits)

    s = sub.add_parser("fix-mod", help="repair a 2026-era team mod for Axis 2027")
    s.set_defaults(fn=cmd_fix_mod)

    s = sub.add_parser("fields", help="give each team its own field, endzones and stadium")
    s.add_argument("--force", action="store_true", help="rewrite teams that already have one")
    s.set_defaults(fn=cmd_fields)

    s = sub.add_parser("report", help="show what the last sync did")
    s.add_argument("--team", nargs="*")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("restore", help="put the backed-up roster files back")
    s.add_argument("--stamp", help="backup timestamp (default: newest)")
    s.set_defaults(fn=cmd_restore)

    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    team = getattr(args, "team", None)
    if isinstance(team, list):
        args.team = [t.upper() for t in team]
    elif isinstance(team, str):
        args.team = team.upper()
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
