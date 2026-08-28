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


def _hide_console() -> None:
    """Tuck the console window away when the GUI opens; it is noise behind a window."""
    try:
        import ctypes
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


# The game ships its own "Sample Mod - Classic Pittsburgh" in Team Mods, so "is there a
# folder?" and even "does it have a TEAM.TXT?" are both true on a clean install. Matching
# NFL nicknames is what actually distinguishes an installed NFL mod from the sample.
NFL_NICKNAMES = (
    "cardinals", "falcons", "ravens", "bills", "panthers", "bears", "bengals", "browns",
    "cowboys", "broncos", "lions", "packers", "texans", "colts", "jaguars", "chiefs",
    "raiders", "chargers", "rams", "dolphins", "vikings", "patriots", "saints", "giants",
    "jets", "eagles", "steelers", "seahawks", "49ers", "buccaneers", "titans", "commanders",
)


def installed_team_folders(mods) -> list:
    """Folders that hold a real NFL team from a mod.

    A folder needs a TEAM.TXT to be a team at all, and an NFL nickname to be one of the
    32 rather than the sample the game ships. A folder holding only a stray ROSTER.CSV is
    one this tool created by mistake before any mod was installed.
    """
    if not mods:
        return []
    found = []
    for d in mods.iterdir():
        if not d.is_dir() or not (d / "TEAM.TXT").exists():
            continue
        name = d.name.lower()
        if any(nick in name for nick in NFL_NICKNAMES):
            found.append(d)
    return found


def require_team_mod(cfg: dict):
    """Return (mods, teams) or (None, []) after explaining what is missing."""
    game = detect.find_game(cfg)
    if not game:
        _p("Axis Football not found. Set axis_dir in config.json.")
        return None, []
    mods = detect.mods_dir(game)
    teams = installed_team_folders(mods)
    if not teams:
        _p("No NFL team mod is installed, so there is nothing to update.")
        _p("")
        _p("This tool rewrites the rosters inside a team mod - it is not a team mod")
        _p("itself. Install one first:")
        _p(f"   {cfg.get('team_mod_page', '')}")
        _p("")
        _p("Unzip it into:")
        _p(f"   {(game / 'Mods' / 'Team Mods')}")
        _p("")
        _p("Or run:  ActiveRosterUpdate.exe install-mod --from <the zip you downloaded>")
        return None, []
    return mods, teams


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
    # check before spending 8 seconds on downloads we cannot use
    if args.apply and not require_team_mod(cfg)[0]:
        return 2
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
        if args.apply and not (dest_dir / "TEAM.TXT").exists():
            # writing a roster into a folder the mod does not have would produce a team
            # the game cannot load; skip it rather than leave junk behind
            _p(f"{team['abbrev']:>3}  no folder in the mod for this club - skipped")
            continue
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
    if not require_team_mod(cfg)[0]:
        return 2
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

    # A player who already owns a portrait keeps it. Without this, re-running hands
    # everyone a fresh id, re-downloads ~1,700 photos and orphans the old files.
    ledger = portraits_mod.Ledger(ROOT / "data" / "portraits.json")
    kept = 0
    todo = []
    for row in rf.rows:
        name = f"{row[c_first]} {row[c_last]}".strip()
        known = ledger.get(espn_ids.get(name, ""))
        if known and not args.refetch:
            large = store.root / f"Skin Tone {known['skin']}" / "Large" / f"{known['id']}.png"
            if large.exists():
                row[c_skin] = known["skin"]
                row[c_portrait] = str(known["id"])
                kept += 1
                continue
        todo.append(row)

    if not todo:
        roster_csv.write(roster_path, rf, rf.rows)
        _p(f"{folder}: all {kept} players already have their own photo")
        return 0
    _p(f"{folder}: {kept} already done, fetching {len(todo)} ...")

    # fetch in parallel - a team is ~53 round trips to the same CDN
    names = [f"{row[c_first]} {row[c_last]}".strip() for row in todo]
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
    for row in todo:
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
        ledger.set(espn_ids.get(name, ""), skin, n)
        done += 1

    roster_csv.write(roster_path, rf, rf.rows)
    ledger.save()
    _p("")
    _p(f"photos written : {done}")
    if kept:
        _p(f"already had one: {kept}")
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
    if not require_team_mod(cfg)[0]:
        return 2
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

    WHITE = "RGB: 255, 255, 255, 255"

    def rgba(value: str, fallback: str) -> str:
        """Normalise a colour to the game's format.

        Values arrive two ways: the mod's bare "0, 120, 182" and the game's own
        "RGB: 0, 120, 182, 255" (Team Suite rewrites files in that form). Strip the
        prefix before splitting, or the first channel is read as text and dropped -
        which silently turns 0,120,182 into 120,182,255.
        """
        text = re.sub(r"(?i)^\s*RGB\s*:", "", value or "").strip()
        parts = [p.strip() for p in text.split(",") if p.strip().isdigit()]
        if len(parts) < 3:
            parts = [p.strip() for p in fallback.split(",")]
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
        # Rewrite whenever the block is incomplete, not merely absent. An older version of
        # this tool wrote fewer keys, and a team left in that state keeps the magenta
        # sidelines and the field stripe - so "already has an endzone colour" is not
        # good enough to skip on.
        needed = {"FieldMaskColor0", "FieldColorMaskIndex", "LineMaskIndex",
                  "UprightColor", "UprightPaddingColor", "LogoBorderColor",
                  "NumbersOutlineColor", "OtherLinesColor", "EndzoneColor0"}
        if needed.issubset(values) and not args.force:
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
        # Two lessons from seeing this in game:
        #
        #   A colour key that is absent is not "left alone" - the game paints the missing
        #   colour magenta (252, 0, 255). LogoBorderColor, NumbersOutlineColor and
        #   OtherLinesColor must all be written or sidelines turn magenta.
        #
        #   FieldColorMaskIndex left unset draws a coloured mask across the field - the
        #   stripe - tinted with the team colour. Setting it to 0 asks for no mask, which
        #   is why it is written explicitly rather than skipped.
        #
        # The FieldMaskColor* and Upright* keys stay out: they tint that mask and recolour
        # the goalposts, and both looked wrong.
        block += [
            # These colour the painted area around the field - the sidelines. Without them
            # the game has no colour to use and paints them magenta. The stripe that came
            # with them earlier was not the colours, it was FieldColorMaskIndex choosing a
            # patterned mask to paint them with; 0 asks for the plain one.
            ("FieldMaskColor0", primary),
            ("FieldMaskColor1", secondary),
            ("FieldMaskColor2", alternate),
            ("FieldColorMaskIndex", "0"),
            ("LineMaskIndex", "0"),
            # Goalposts. Absent, these render magenta like any missing colour; set to the
            # team colour they looked wrong. HUE:0 is the game's own default (yellow posts)
            # and white padding is what a real set has.
            ("UprightColor", "HUE:0"),
            ("UprightPaddingColor", WHITE),
            ("UprightIndex", "1"),
            ("GrassColorIndex", "0"),
            ("MowedEffect", "0"),
            ("PressNormalIndex", "10"),
            ("LogoBorderColor", primary),
            ("NumbersOutlineColor", WHITE),
            ("OtherLinesColor", WHITE),
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


def _steam_configs() -> list[Path]:
    """Every user's localconfig.vdf, where Steam keeps per-game launch options."""
    out = []
    for lib in detect.steam_libraries():
        userdata = lib / "userdata"
        if not userdata.is_dir():
            continue
        for user in userdata.iterdir():
            cfg = user / "config" / "localconfig.vdf"
            if cfg.is_file() and user.name != "0":
                out.append(cfg)
    return out


def _steam_running() -> bool:
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                             capture_output=True, text=True, timeout=15).stdout.lower()
        return "steam.exe" in out
    except Exception:
        return False


def _set_launch_options(text: str, appid: str, value: str) -> tuple[str, str]:
    """Set LaunchOptions inside the apps -> <appid> block. Returns (text, what_happened)."""
    import re

    # the app id appears in several places; the one we want is inside an "apps" block
    apps = text.find('"apps"')
    if apps < 0:
        return text, "no apps section"
    start = text.find('"' + appid + '"', apps)
    if start < 0:
        return text, "game not listed - launch it once from Steam first"

    brace = text.find("{", start)
    if brace < 0:
        return text, "malformed entry"

    depth, i = 0, brace
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[brace:i]

    indent = "\t" * 6
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    line = indent + '"LaunchOptions"\t\t"' + escaped + '"'

    existing = re.search(r'^\s*"LaunchOptions"\s+".*"\s*$', block, re.M)
    if existing:
        new_block = block[:existing.start()] + line + block[existing.end():]
        return text[:brace] + new_block + text[i:], "replaced"
    new_block = block[:1] + "\n" + line + block[1:]
    return text[:brace] + new_block + text[i:], "added"


def cmd_steam_launch(args, cfg) -> int:
    """Make pressing Play in Steam sync the rosters first."""
    appid = str(cfg.get("steam_appid") or "5026790")
    exe = Path(sys.executable)
    if exe.name.lower().startswith("python"):
        # running from source rather than the packaged exe
        exe = ROOT / "ActiveRosterUpdate.exe"
    scope = " --all" if getattr(args, "all_teams", False) else ""
    value = "" if args.remove else '"' + str(exe) + '" play' + scope + " %command%"

    configs = _steam_configs()
    if not configs:
        _p("No Steam user config found.")
        return 2

    if _steam_running():
        _p("Steam is running, and it overwrites this file when it closes.")
        _p("Close Steam completely, then run this again.")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    done = 0
    for cfgfile in configs:
        text = cfgfile.read_text(encoding="utf-8", errors="replace")
        updated, what = _set_launch_options(text, appid, value)
        if updated == text:
            _p(f"   {cfgfile.parent.parent.name}: {what}")
            continue
        bak = ROOT / "backups" / f"steam-{stamp}" / cfgfile.parent.parent.name
        bak.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfgfile, bak / "localconfig.vdf")
        cfgfile.write_text(updated, encoding="utf-8")
        _p(f"   {cfgfile.parent.parent.name}: {what}")
        done += 1

    if not done:
        return 1
    _p("")
    if args.remove:
        _p("Launch option removed. Steam will start the game normally again.")
    else:
        _p("Done. Start Steam and press Play - it will sync your rosters first,")
        _p("then launch the game.")
        _p(f"Launch option set to: {value}")
    _p(f"Backup: {ROOT / 'backups' / ('steam-' + stamp)}")
    return 0


def _ask(question: str, default_yes: bool = True) -> bool:
    """Yes/no prompt that treats a bare Enter as the recommended answer."""
    suffix = " [Y/n] " if default_yes else " [y/N] "
    while True:
        try:
            answer = input(question + suffix).strip().lower()
        except EOFError:
            return default_yes
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _choose(question: str, options: list[str], default: int = 1) -> int:
    """Numbered choice, Enter takes the default."""
    _p("")
    for i, text in enumerate(options, 1):
        mark = "  (recommended)" if i == default else ""
        _p(f"   {i}. {text}{mark}")
    _p("")
    while True:
        try:
            answer = input(f"{question} [{default}] ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)


def _save_config(cfg: dict) -> None:
    path = ROOT / "config.json"
    keep = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    keep.update(cfg)
    path.write_text(json.dumps(keep, indent=2) + NEWLINE, encoding="utf-8")


def _pick_club(cfg: dict) -> str:
    """Ask which club they follow, and remember it. Beats editing config.json by hand."""
    teams = {t["abbrev"]: t["display"] for t in espn.teams()}
    current = (cfg.get("favorite_team") or "").upper()

    _p("")
    _p("  Which team do you follow? This is the one \"sync my team\" uses.")
    _p("")
    names = sorted(teams.items(), key=lambda kv: kv[1])
    for i in range(0, len(names), 4):
        row = "   ".join(f"{abbr:<4}{display[:20]:<22}" for abbr, display in names[i:i + 4])
        _p("   " + row.rstrip())
    _p("")

    while True:
        prompt = f"  Team abbreviation [{current}]: " if current else "  Team abbreviation: "
        try:
            answer = (input(prompt).strip().upper() or current)
        except EOFError:
            answer = current or "DET"
        if answer in teams:
            if answer != current:
                cfg["favorite_team"] = answer
                _save_config({"favorite_team": answer})
                _p(f"  Set to {teams[answer]}.")
            else:
                _p(f"  Keeping {teams[answer]}.")
            return answer
        _p("  Not one of the 32 - try again, e.g. DET, KC, PHI.")


def _rule(title: str) -> None:
    _p("")
    _p("=" * 66)
    _p("  " + title)
    _p("=" * 66)


def cmd_setup(args, cfg) -> int:
    """Guided first-run setup: does the required parts, asks about the rest."""
    _rule("ActiveRosterUpdate setup")
    _p("This sets up your Axis Football install once. Nothing here is destructive -")
    _p("every step backs up whatever it changes.")

    # ---------------------------------------------------------------- 1. find the game
    _rule("Step 1 of 6  -  finding your game")
    game = detect.find_game(cfg)
    if not game:
        _p("Could not find Axis Football.")
        _p("Set \"axis_dir\" in config.json to your install folder and run this again.")
        return 2
    mods = detect.mods_dir(game)
    _p(f"Found: {game}")
    if not installed_team_folders(mods):
        _p("")
        _p("No team mod is installed yet. This tool updates the rosters inside an NFL")
        _p("team mod - it is not a team mod itself, so one has to be there first.")
        _p("")
        if cfg.get("team_mod_url") and _ask(
                f"Download and install {cfg.get('team_mod_name', 'the NFL mod')} now? (about 245 MB)"):
            args.force = False
            if cmd_install_mod(args, cfg):
                return 2
            mods = detect.mods_dir(detect.find_game(cfg))
        else:
            _p("")
            _p("Get it here, unzip into the folder below, then run setup again:")
            _p(f"   {cfg.get('team_mod_page', '')}")
            _p(f"   {game / 'Mods' / 'Team Mods'}")
            return 2
    _p(f"Team mods: {len(installed_team_folders(mods))} NFL teams installed")

    # ---------------------------------------------------------------- 1b. which club
    _rule("Step 2 of 6  -  your team")
    club_now = _pick_club(cfg)

    # ---------------------------------------------------------------- 2. link clubs
    _rule("Step 3 of 6  -  matching the 32 NFL clubs to your mod folders")
    existing = detect.load_links()
    if len(existing) >= 32:
        # someone may have fixed a folder name by hand; do not throw that away
        _p(f"Already matched: {len(existing)} clubs. Leaving mapping/teams.json alone.")
        _p("(Run \"Tools\\Link Teams.bat\" if your mod folders have changed.)")
    else:
        rc = cmd_link(args, cfg)
        if rc:
            return rc

    # ---------------------------------------------------------------- 3. repair for 2027
    _rule("Step 4 of 6  -  repairing the mod for Axis 2027")
    _p("The 2026-era mods freeze the game at kickoff on 2027 and play on default")
    _p("fields. This fixes both. Skipping it is not recommended.")
    _p("")
    cmd_fix_mod(args, cfg)
    _p("")
    cmd_fields(args, cfg)

    # ---------------------------------------------------------------- 4. photos
    _rule("Step 5 of 6  -  real player photos (optional)")
    _p("Fetches every player's actual headshot to use as his in-game portrait.")
    _p("About 1,700 photos - takes a few minutes. Without this, players keep the")
    _p("generated faces the mod shipped with.")
    _p("")
    if _ask("Get real player photos now?"):
        args.team, args.all, args.reuse_low_ids, args.refetch = None, True, False, False
        cmd_portraits(args, cfg)
        cmd_index_portraits(args, cfg)
    else:
        _p("Skipped. Run \"Tools\\Get Player Photos.bat\" any time to do it later.")

    # ---------------------------------------------------------------- 5. steam
    _rule("Step 6 of 6  -  sync automatically when you press Play (optional)")
    _p("Sets the Steam launch option for Axis Football so pressing Play syncs your")
    _p("rosters first, then starts the game. Then there is nothing to remember.")
    _p("")
    if _ask("Set that up now?"):
        scope = _choose(
            "Which sync should pressing Play run?",
            [f"All 32 clubs - about 15 seconds, every matchup correct",
             f"Just {(cfg.get('favorite_team') or 'your club').upper()} and its next opponent - quicker"],
            default=1,
        )
        args.all_teams = scope == 1
        while True:
            if not _steam_running():
                break
            _p("")
            _p("Steam is running. It rewrites its config when it closes, so the change")
            _p("would be thrown away. Close Steam completely - check the system tray.")
            if not _ask("Closed it? Check again", default_yes=True):
                _p("Skipped. Run \"Tools\\Add To Steam.bat\" later with Steam closed.")
                break
        else:
            pass
        if not _steam_running():
            args.remove = False
            cmd_steam_launch(args, cfg)
    else:
        _p("Skipped. Run \"Tools\\Add To Steam.bat\" any time, with Steam closed.")

    # ---------------------------------------------------------------- done
    _rule("Setup complete")
    club = club_now
    _p(f"Your club is {club}. Change it any time from the menu.")
    _p("")
    _p("From here on, all you do is:")
    _p("   \"Sync My Team.bat\"        your club and its next opponent")
    _p("   \"Sync All 32 Teams.bat\"   the whole league, about 15 seconds")
    _p("")
    _p("One last thing: a sync, so the rosters are current before you play.")
    scope = _choose(
        "What should I sync now?",
        ["All 32 clubs - about 15 seconds",
         f"Just {club} and its next opponent",
         "Nothing, I will run it myself later"],
        default=1,
    )
    if scope == 3:
        _p("")
        _p("Fine - run \"Sync All 32 Teams.bat\" whenever you are ready.")
        return 0
    args.team, args.dry_run, args.week, args.out = None, False, None, None
    args.apply, args.no_portraits = True, False
    if scope == 1:
        args.all, args.gameday = True, None
    else:
        args.all, args.gameday = False, ""
    return cmd_sync(args, cfg)


def cmd_menu(args, cfg) -> int:
    """The screen you get when you just run the program with no arguments."""
    while True:
        club = (cfg.get("favorite_team") or "DET").upper()
        game = detect.find_game(cfg)
        mods = detect.mods_dir(game) if game else None
        linked = len(detect.load_links())

        _p("")
        _p("=" * 66)
        _p("  ACTIVE ROSTER UPDATE" + " " * 24 + "play the game before the game")
        _p("=" * 66)
        if game:
            _p(f"  Game    {game.name}")
            _p(f"  Mods    {len(detect.team_folders(mods)) if mods else 0} team folders, {linked} clubs matched")
        else:
            _p("  Game    NOT FOUND - set axis_dir in config.json")
        _p(f"  Club    {club}")
        _p("-" * 66)
        _p("")
        def item(key, label, hint):
            _p(f"   {key}   {label:<29}{hint}")

        item("1", "Sync all 32 clubs", "the whole league, about 15 seconds")
        item("2", f"Sync {club} and its opponent", "your matchup only")
        _p("")
        item("3", "First-time setup", "run this once, on a new install")
        item("4", "Player photos", "real headshots for every player")
        item("5", "Repair mod for Axis 2027", "fixes the kickoff freeze, fields, stadiums")
        item("6", "Sync when I press Play", "set the Steam launch option")
        _p("")
        item("7", "Show a roster", "what the game will actually load")
        item("8", "Last sync report", "who was left off, and why")
        item("9", "Undo the last sync", "restore from backup")
        _p("")
        item("c", "Change my team", "currently " + club)
        item("0", "Quit", "")
        _p("")

        try:
            pick = input("   Choose: ").strip()
        except EOFError:
            return 0
        if pick in ("0", "q", "quit", "exit"):
            return 0
        if pick.lower() == "c":
            _pick_club(cfg)
            continue

        _p("")
        try:
            if pick == "1":
                args.team, args.all, args.gameday = None, True, None
                args.apply, args.dry_run, args.week, args.out = True, False, None, None
                args.no_portraits = False
                cmd_sync(args, cfg)
            elif pick == "2":
                args.team, args.all, args.gameday = None, False, ""
                args.apply, args.dry_run, args.week, args.out = True, False, None, None
                args.no_portraits = False
                cmd_sync(args, cfg)
            elif pick == "3":
                cmd_setup(args, cfg)
            elif pick == "4":
                args.team, args.all, args.reuse_low_ids, args.refetch = None, True, False, False
                cmd_portraits(args, cfg)
                cmd_index_portraits(args, cfg)
            elif pick == "5":
                cmd_fix_mod(args, cfg)
                _p("")
                cmd_fields(args, cfg)
            elif pick == "6":
                scope = _choose(
                    "Which sync should pressing Play run?",
                    ["All 32 clubs - about 15 seconds, every matchup correct",
                     f"Just {club} and its next opponent - quicker"],
                    default=1,
                )
                args.all_teams, args.remove = scope == 1, False
                cmd_steam_launch(args, cfg)
            elif pick == "7":
                args.team = input("   Team abbreviation [" + club + "]: ").strip().upper() or club
                cmd_roster(args, cfg)
                args.team = None
            elif pick == "8":
                args.team = None
                cmd_report(args, cfg)
            elif pick == "9":
                args.stamp = None
                cmd_restore(args, cfg)
            else:
                _p("   Not one of the options.")
                continue
        except Exception as exc:                      # never dump a traceback at a player
            _p("")
            _p(f"   That did not work: {exc}")

        _p("")
        try:
            input("   Press Enter to go back to the menu ")
        except EOFError:
            return 0


def cmd_play(args, cfg) -> int:
    """Sync, then start the game. This is what the Steam launch option runs.

    Steam appends the real launch command after our own arguments, so whatever follows
    is executed once the sync is done. Run without that, and we ask Steam to start the
    game itself.
    """
    import subprocess

    args.team, args.dry_run, args.week, args.out = None, False, None, None
    args.apply, args.no_portraits = True, False
    if args.all:
        args.gameday = None
    else:
        args.all, args.gameday = False, ""

    try:
        cmd_sync(args, cfg)
    except Exception as exc:
        _p(f"Sync failed ({exc}) - starting the game with the rosters already on disk.")

    passthrough = list(getattr(args, "command", []) or [])
    if passthrough:
        _p("")
        _p("Starting the game ...")
        try:
            return subprocess.call(passthrough)
        except Exception as exc:
            _p(f"Could not start the game: {exc}")
            return 1

    appid = str(cfg.get("steam_appid") or "5026790")
    _p("")
    _p("Starting the game ...")
    try:
        import os
        os.startfile(f"steam://rungameid/{appid}")
    except Exception as exc:
        _p(f"Could not start the game: {exc}")
        return 1
    return 0


def cmd_install_mod(args, cfg) -> int:
    """Download and install the NFL team mod this tool updates.

    The mod is someone else's work (JayBeeLove90). We fetch it from whatever URL the
    config points at and unpack it into the game's Team Mods folder - we never rebuild
    or modify his artwork, only the roster files inside it once it is installed.
    """
    import tempfile
    import urllib.request
    import zipfile

    game = detect.find_game(cfg)
    if not game:
        _p("Axis install not found.")
        return 2
    target = game / "Mods" / "Team Mods"
    target.mkdir(parents=True, exist_ok=True)

    already = [d for d in target.iterdir() if d.is_dir() and d.name.lower().startswith("nfl")]
    if already and not args.force:
        _p(f"An NFL team mod is already installed ({len(already)} teams).")
        _p("Use --force to install it again over the top.")
        return 0

    # a zip already on disk beats any download
    local = getattr(args, "from_zip", None)
    if local:
        src = Path(local)
        if not src.is_file():
            _p(f"No such file: {src}")
            return 2
        _p(f"Installing from {src.name} ...")
        return _unpack_team_mod(src, target, cfg, keep=True)

    url = cfg.get("team_mod_url")
    if not url:
        _p("This tool updates the rosters inside an NFL team mod - it is not one itself.")
        _p("")
        _p(f"Get {cfg.get('team_mod_name', 'the NFL mod')} here:")
        _p(f"   {cfg.get('team_mod_page', 'https://www.mediafire.com/file/ep4qoqm4tygwr89/Axis_Football_26_NFL_Mod.zip/file')}")
        _p("")
        _p("Unzip it into:")
        _p(f"   {target}")
        _p("Then run setup again.")
        return 2

    _p(f"Downloading {cfg.get('team_mod_name', 'the NFL team mod')} ...")
    _p(f"   {url}")
    tmp = Path(tempfile.gettempdir()) / "activerosterupdate-teammod.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            step = max(1, total // 20) if total else 5_000_000
            nextmark = step
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if got >= nextmark:
                    nextmark += step
                    if total:
                        _p(f"   {got // 1048576} MB of {total // 1048576} MB")
                    else:
                        _p(f"   {got // 1048576} MB")
    except Exception as exc:
        _p(f"Download failed: {exc}")
        _p("You can install the mod by hand instead - see START HERE.txt.")
        return 1

    return _unpack_team_mod(tmp, target, cfg, keep=False)


def _unpack_team_mod(archive, target, cfg: dict, keep: bool) -> int:
    """Unpack a team-mod zip into Team Mods, unwrapping a single top folder if present."""
    import zipfile

    _p("Unpacking ...")
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            # some packs wrap everything in a single top folder; unwrap it if so
            tops = {n.split("/")[0] for n in names if "/" in n}
            if len(tops) == 1 and not any(n.count("/") == 0 and n for n in names):
                root_prefix = tops.pop() + "/"
                for member in names:
                    if not member.startswith(root_prefix) or member.endswith("/"):
                        continue
                    dest = target / member[len(root_prefix):]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
            else:
                z.extractall(target)
    except Exception as exc:
        _p(f"Could not unpack it: {exc}")
        return 1
    finally:
        if not keep:
            try:
                Path(archive).unlink()
            except OSError:
                pass

    teams = [d for d in target.iterdir() if d.is_dir()]
    _p("")
    _p(f"Installed: {len(teams)} team folders in {target}")
    _p("Credit: the team folders, uniforms, helmets, logos and coaching staff are")
    _p(f"{cfg.get('team_mod_credit', 'JayBeeLove90')}'s work, used with permission.")
    return 0


def cmd_clean(args, cfg) -> int:
    """Remove team folders this tool created before a mod was installed."""
    game = detect.find_game(cfg)
    mods = detect.mods_dir(game) if game else None
    if not mods:
        _p("Axis install not found.")
        return 2

    junk = [d for d in mods.iterdir()
            if d.is_dir() and not (d / "TEAM.TXT").exists()
            and {f.name.upper() for f in d.iterdir()} <= {"ROSTER.CSV"}]
    if not junk:
        _p("Nothing to clean - every team folder belongs to a mod.")
        return 0

    _p(f"These {len(junk)} folders hold only a stray ROSTER.CSV and no team files:")
    for d in junk:
        _p(f"   {d.name}")
    if not args.yes:
        _p("")
        _p("Re-run with --yes to delete them.")
        return 0

    for d in junk:
        shutil.rmtree(d, ignore_errors=True)
    _p("")
    _p(f"Removed {len(junk)} folders.")
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

    s = sub.add_parser("gui", help="open the window (default when double-clicked)")
    s.set_defaults(fn=lambda a, c: __import__("activerosterupdate.gui", fromlist=["gui"]).run(c),
                   team=None, all=False, force=False, reuse_low_ids=False, remove=False,
                   refetch=False, all_teams=False, stamp=None)

    s = sub.add_parser("menu", help="the text menu, if you prefer it")
    s.set_defaults(fn=cmd_menu, team=None, all=False, force=False, reuse_low_ids=False,
                   remove=False, refetch=False, all_teams=False, stamp=None)

    s = sub.add_parser("install-mod", help="download and install the NFL team mod")
    s.add_argument("--force", action="store_true", help="install again over an existing one")
    s.add_argument("--from", dest="from_zip", metavar="ZIP",
                   help="install from a mod zip you have already downloaded")
    s.set_defaults(fn=cmd_install_mod, team=None, all=False, reuse_low_ids=False,
                   remove=False, refetch=False, all_teams=False, stamp=None)

    s = sub.add_parser("play", help="sync, then start the game (used by the Steam launch option)")
    s.add_argument("--all", action="store_true", help="sync all 32 clubs, not just yours")
    s.add_argument("command", nargs=argparse.REMAINDER,
                   help="the launch command Steam appends; run without it to start via Steam")
    s.set_defaults(fn=cmd_play, team=None, force=False, reuse_low_ids=False, remove=False,
                   refetch=False, all_teams=False, stamp=None, gameday="")

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
    s.add_argument("--refetch", action="store_true",
                   help="download again even for players who already have a photo")
    s.set_defaults(fn=cmd_portraits)

    s = sub.add_parser("index-portraits", help="record who owns which portrait slot")
    s.set_defaults(fn=cmd_index_portraits)

    s = sub.add_parser("fix-mod", help="repair a 2026-era team mod for Axis 2027")
    s.set_defaults(fn=cmd_fix_mod)

    s = sub.add_parser("fields", help="give each team its own field, endzones and stadium")
    s.add_argument("--force", action="store_true", help="rewrite teams that already have one")
    s.set_defaults(fn=cmd_fields)

    s = sub.add_parser("steam-launch",
                       help="make pressing Play in Steam sync first")
    s.add_argument("--remove", action="store_true", help="undo it")
    s.add_argument("--all-teams", action="store_true",
                   help="sync all 32 clubs on launch instead of just yours")
    s.set_defaults(fn=cmd_steam_launch)

    s = sub.add_parser("setup", help="guided first-run setup")
    s.set_defaults(fn=cmd_setup, team=None, all=False, force=False,
                   reuse_low_ids=False, remove=False, refetch=False,
                   all_teams=False)

    s = sub.add_parser("clean", help="remove team folders left behind before a mod was installed")
    s.add_argument("--yes", action="store_true", help="actually delete them")
    s.set_defaults(fn=cmd_clean, team=None, all=False, force=False, reuse_low_ids=False,
                   remove=False, refetch=False, all_teams=False, stamp=None, from_zip=None)

    s = sub.add_parser("report", help="show what the last sync did")
    s.add_argument("--team", nargs="*")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("restore", help="put the backed-up roster files back")
    s.add_argument("--stamp", help="backup timestamp (default: newest)")
    s.set_defaults(fn=cmd_restore)

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # double-clicked from Explorer: open the window. Fall back to the text menu if
        # tkinter is missing, and honour --console for anyone who prefers it.
        try:
            from . import gui
            cfg = config.load(None)
            _hide_console()
            return gui.run(cfg)
        except ImportError:
            pass
        args = ap.parse_args(["menu"])
    else:
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
