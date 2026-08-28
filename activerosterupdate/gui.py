"""A window, rather than a menu of nine things.

Work is grouped by why you would do it: the weekly job at the top in one big button,
first-time setup next, and the occasional tools last. Everything runs on a worker
thread with its output streamed into the log pane, so the window never freezes and you
can watch what it is doing.
"""
from __future__ import annotations

import io
import queue
import re
import threading
import tkinter as tk
from contextlib import redirect_stdout
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from . import config
from .axis import detect

# Lions-ish blue works as an accent without being anyone's brand
INK = "#12181d"
PANEL = "#1b242c"
LINE = "#2b3843"
TEXT = "#e8eef3"
MUTED = "#93a3b0"
ACCENT = "#0f88d0"
ACCENT_DIM = "#0a6ba6"
GOOD = "#3fa66a"
WARN = "#c8892b"


# Lines the commands print when they finish a club, so the bar can follow along:
#   "DET  53 players |  6 out/IR |  8 unrated"      (sync)
#   "NFL - Detroit Lions: 12 already done, ..."     (portraits)
TEAM_LINE = re.compile(r"^\s*([A-Z]{2,3})\s+\d+\s+players")
FOLDER_LINE = re.compile(r"^(NFL - [^:]+):")


class Runner:
    """Runs a CLI command on a thread, streaming its output into the log and the bar."""

    def __init__(self, log_widget: tk.Text, on_done, on_progress=None):
        self.log = log_widget
        self.on_done = on_done
        self.on_progress = on_progress
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.busy = False
        self.partial = ""
        self.seen = 0
        self.done_message = ""

    def start(self, label: str, fn, *args, total: int = 0,
              done_message: str = "") -> bool:
        if self.busy:
            return False
        self.done_message = done_message
        self.busy = True
        self.seen = 0
        self.partial = ""
        if self.on_progress:
            self.on_progress(label, 0, total)
        self.write(f"\n=== {label} ===\n")

        def work():
            buffer = io.StringIO()

            class Tee(io.TextIOBase):
                def write(_self, text):
                    self.queue.put(text)
                    return len(text)

            try:
                with redirect_stdout(Tee()):
                    fn(*args)
            except Exception as exc:
                self.queue.put(f"\nThat did not work: {exc}\n")
            finally:
                buffer.close()
                self.queue.put(None)

        threading.Thread(target=work, daemon=True).start()
        return True

    def pump(self, root: tk.Misc) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                if item is None:
                    self.busy = False
                    self.on_done()
                else:
                    self.write(item)
                    self._watch(item)
        except queue.Empty:
            pass
        root.after(80, lambda: self.pump(root))

    def _watch(self, chunk: str) -> None:
        """Pick the club being worked on out of the output stream."""
        if not self.on_progress:
            return
        self.partial += chunk
        *lines, self.partial = self.partial.split("\n")
        for line in lines:
            hit = TEAM_LINE.match(line) or FOLDER_LINE.match(line)
            if hit:
                self.seen += 1
                self.on_progress(hit.group(1).replace("NFL - ", ""), self.seen, None)

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")


class SetupDialog(tk.Toplevel):
    """Everything first-run setup asks, on one screen instead of a console interrogation."""

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.result = None
        self.title("Set up this PC")
        self.configure(bg=INK)
        self.resizable(False, False)
        self.transient(app.root)
        self.grab_set()

        cfg = app.cfg
        game = detect.find_game(cfg)
        mods = detect.mods_dir(game) if game else None
        from .cli import installed_team_folders
        has_mod = bool(installed_team_folders(mods))

        tk.Label(self, text="Set up this PC", bg=INK, fg=TEXT,
                 font=app.head).pack(anchor="w", padx=20, pady=(18, 2))
        tk.Label(self, text="Tick what you want. Everything is backed up, and you can re-run it later.",
                 bg=INK, fg=MUTED, font=app.small).pack(anchor="w", padx=20, pady=(0, 14))

        body = tk.Frame(self, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        body.pack(fill="x", padx=20)

        # ---------------------------------------------------------- team
        row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(row, text="My team", bg=PANEL, fg=TEXT, font=app.base).pack(side="left")
        self.team = tk.StringVar(value=(cfg.get("favorite_team") or "DET").upper())
        try:
            from .sources import espn
            self.teams = {t["abbrev"]: t["display"] for t in espn.teams()}
        except Exception:
            self.teams = {self.team.get(): self.team.get()}
        picker = ttk.Combobox(row, textvariable=self.team, width=28, state="readonly",
                              values=[f"{a}  {n}" for a, n in sorted(self.teams.items(),
                                                                     key=lambda kv: kv[1])])
        picker.set(f"{self.team.get()}  {self.teams.get(self.team.get(), '')}")
        picker.pack(side="left", padx=(12, 0))

        # ---------------------------------------------------------- mod
        self.want_mod = tk.BooleanVar(value=not has_mod)
        if not has_mod:
            box = tk.Frame(body, bg=PANEL)
            box.pack(fill="x", padx=16, pady=(8, 0))
            if cfg.get("team_mod_url"):
                self._check(box, self.want_mod,
                            f"Download and install {cfg.get('team_mod_name', 'the NFL team mod')}",
                            "About 245 MB. Nothing to update without it.")
            else:
                self.want_mod.set(False)
                tk.Label(box, text="No team mod installed yet - there is nothing to update without one",
                         bg=PANEL, fg=WARN, font=app.small, justify="left").pack(anchor="w")
                buttons = tk.Frame(box, bg=PANEL)
                buttons.pack(anchor="w", pady=(6, 2))
                app._button(buttons, "Get the mod (opens the download page)",
                            self._open_mod_page).pack(side="left", ipady=3)
                app._button(buttons, "I already downloaded it - install from zip",
                            self._pick_mod_zip).pack(side="left", padx=(8, 0), ipady=3)
                self.mod_zip = None

        # ---------------------------------------------------------- the steps
        self.want_repair = tk.BooleanVar(value=True)
        self._check(body, self.want_repair, "Repair the mod for Axis 2027",
                    "Fixes the freeze at kickoff, and gives each club its stadium and endzones.")

        self.want_photos = tk.BooleanVar(value=True)
        self._check(body, self.want_photos, "Get real player photos",
                    "About 1,700 headshots. Takes a few minutes. Skips anyone already done.")

        self.want_steam = tk.BooleanVar(value=False)
        self._check(body, self.want_steam, "Sync when I press Play in Steam",
                    "Sets the Steam launch option. Steam has to be closed for this.")

        scope = tk.Frame(body, bg=PANEL)
        scope.pack(fill="x", padx=44, pady=(0, 8))
        self.steam_all = tk.BooleanVar(value=True)
        self._radio(scope, self.steam_all, True, "all 32 teams")
        self._radio(scope, self.steam_all, False, "just my team")

        # ---------------------------------------------------------- first sync
        tk.Frame(body, bg=LINE, height=1).pack(fill="x", padx=16, pady=(8, 0))
        row2 = tk.Frame(body, bg=PANEL)
        row2.pack(fill="x", padx=16, pady=(10, 14))
        tk.Label(row2, text="Sync now", bg=PANEL, fg=TEXT, font=app.base).pack(side="left")
        self.sync_choice = tk.StringVar(value="all")
        for value, label in (("all", "all 32 teams"), ("mine", "my team"), ("none", "not yet")):
            tk.Radiobutton(row2, text=label, value=value, variable=self.sync_choice,
                           bg=PANEL, fg=TEXT, selectcolor=INK, activebackground=PANEL,
                           activeforeground=TEXT, font=app.small, bd=0,
                           highlightthickness=0).pack(side="left", padx=(12, 0))

        # ---------------------------------------------------------- buttons
        buttons = tk.Frame(self, bg=INK)
        buttons.pack(fill="x", padx=20, pady=16)
        app._button(buttons, "Cancel", self.destroy).pack(side="right", ipady=3)
        app._button(buttons, "Run setup", self._go, primary=True).pack(
            side="right", padx=(0, 10), ipady=5, ipadx=8)

        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + 60
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _check(self, parent, var, title, hint):
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="x", padx=16, pady=(8, 0))
        tk.Checkbutton(wrap, text=title, variable=var, bg=PANEL, fg=TEXT, selectcolor=INK,
                       activebackground=PANEL, activeforeground=TEXT, font=self.app.base,
                       bd=0, highlightthickness=0).pack(anchor="w")
        tk.Label(wrap, text=hint, bg=PANEL, fg=MUTED, font=self.app.small,
                 wraplength=420, justify="left").pack(anchor="w", padx=(24, 0))

    def _radio(self, parent, var, value, label):
        tk.Radiobutton(parent, text=label, value=value, variable=var, bg=PANEL, fg=MUTED,
                       selectcolor=INK, activebackground=PANEL, activeforeground=TEXT,
                       font=self.app.small, bd=0, highlightthickness=0).pack(side="left",
                                                                            padx=(0, 14))

    def _open_mod_page(self):
        import webbrowser
        page = self.app.cfg.get("team_mod_page")
        if page:
            webbrowser.open(page)
            messagebox.showinfo(
                "Get the mod",
                "Download the NFL mod in your browser, then come back and click\n"
                "\"I already downloaded it - install from zip\".")

    def _pick_mod_zip(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self, title="Choose the team mod zip",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        if path:
            self.mod_zip = path
            self.want_mod.set(True)
            messagebox.showinfo("Ready", f"Will install from:\n{path}")

    def _go(self):
        # nothing below works without a team mod, so do not pretend otherwise
        from .cli import installed_team_folders
        mods = detect.mods_dir(detect.find_game(self.app.cfg))
        if not installed_team_folders(mods) and not (self.want_mod.get() and
                                                     (self.app.cfg.get("team_mod_url") or
                                                      getattr(self, "mod_zip", None))):
            messagebox.showwarning(
                "No team mod",
                "There is no NFL team mod installed, so there are no rosters to update.\n\n"
                "Use \"Get the mod\" to download it, then \"install from zip\" - or unzip "
                "it into the game's Mods\\Team Mods folder yourself and run setup again.")
            return
        abbrev = self.team.get().split()[0].upper()
        self.result = {
            "team": abbrev,
            "mod": self.want_mod.get(),
            "mod_zip": getattr(self, "mod_zip", None),
            "repair": self.want_repair.get(),
            "photos": self.want_photos.get(),
            "steam": self.want_steam.get(),
            "steam_all": self.steam_all.get(),
            "sync": self.sync_choice.get(),
        }
        self.destroy()


class App:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        root.title("Active Roster Update")
        root.configure(bg=INK)
        root.minsize(760, 620)

        self.total = 0
        self.base = tkfont.nametofont("TkDefaultFont").copy()
        self.base.configure(size=10)
        self.head = self.base.copy()
        self.head.configure(size=17, weight="bold")
        self.section = self.base.copy()
        self.section.configure(size=9, weight="bold")
        self.small = self.base.copy()
        self.small.configure(size=9)

        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 18}

        header = tk.Frame(self.root, bg=INK)
        header.pack(fill="x", pady=(16, 6), **pad)
        tk.Label(header, text="ACTIVE ROSTER UPDATE", bg=INK, fg=TEXT,
                 font=self.head).pack(anchor="w")
        tk.Label(header, text="play the game before the game", bg=INK, fg=ACCENT,
                 font=self.small).pack(anchor="w")

        self.status = tk.Label(self.root, text="", bg=INK, fg=MUTED, font=self.small,
                               justify="left", anchor="w")
        self.status.pack(fill="x", pady=(4, 6), **pad)

        # hidden until a newer build is found
        self.update_bar = tk.Frame(self.root, bg="#123449", highlightbackground=ACCENT,
                                   highlightthickness=1)
        self.update_text = tk.Label(self.update_bar, text="", bg="#123449", fg=TEXT,
                                    font=self.base, anchor="w")
        self.update_text.pack(side="left", padx=12, pady=8)
        self._button(self.update_bar, "Update now", self._do_update, primary=True).pack(
            side="right", padx=10, pady=6, ipady=2)
        self._pending_update = None

        # ---------------------------------------------------------- every week
        weekly = self._card("EVERY WEEK")
        row = tk.Frame(weekly, bg=PANEL)
        row.pack(fill="x", padx=14, pady=(4, 12))
        self.btn_all = self._button(row, "Sync all 32 teams", self._sync_all, primary=True)
        self.btn_all.pack(side="left", ipady=6, ipadx=10)
        self.btn_mine = self._button(row, "Sync my team only", self._sync_mine)
        self.btn_mine.pack(side="left", padx=(10, 0), ipady=6, ipadx=6)
        tk.Label(weekly, text="Takes about 15 seconds for the league. Run it before you play.",
                 bg=PANEL, fg=MUTED, font=self.small).pack(anchor="w", padx=16, pady=(0, 12))

        # ---------------------------------------------------------- first time
        first = self._card("FIRST TIME ON THIS PC")
        grid = tk.Frame(first, bg=PANEL)
        grid.pack(fill="x", padx=14, pady=(4, 12))
        self._tile(grid, 0, "Run setup", "Does everything below, in order, asking as it goes",
                   self._setup)
        self._tile(grid, 1, "Repair mod for Axis 2027",
                   "Fixes the kickoff freeze, fields and stadiums", self._repair)
        self._tile(grid, 2, "Get player photos",
                   "Real headshots for every player, a few minutes", self._photos)
        self._tile(grid, 3, "Sync when I press Play",
                   "Sets the Steam launch option. Steam must be closed", self._steam)

        # ---------------------------------------------------------- tools
        tools = self._card("IF SOMETHING LOOKS WRONG")
        trow = tk.Frame(tools, bg=PANEL)
        trow.pack(fill="x", padx=14, pady=(4, 12))
        self._button(trow, "Show a roster", self._roster).pack(side="left", ipady=3)
        self._button(trow, "Last sync report", self._report).pack(side="left", padx=8, ipady=3)
        self._button(trow, "Undo last sync", self._undo).pack(side="left", ipady=3)
        self._button(trow, "Change my team", self._change_team).pack(side="right", ipady=3)

        # ---------------------------------------------------------- log
        bar = tk.Frame(self.root, bg=INK)
        bar.pack(fill="x", padx=18, pady=(0, 2))
        self.progress_label = tk.Label(bar, text="", bg=INK, fg=MUTED, font=self.small,
                                       anchor="w")
        self.progress_label.pack(fill="x")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("ARU.Horizontal.TProgressbar", troughcolor=PANEL, bordercolor=LINE,
                        background=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        self.progress = ttk.Progressbar(bar, style="ARU.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(2, 6))

        logwrap = tk.Frame(self.root, bg=LINE)
        logwrap.pack(fill="both", expand=True, pady=(4, 16), **pad)
        self.log = tk.Text(logwrap, bg="#0d1319", fg="#cfe0ec", bd=0, height=10,
                           insertbackground=TEXT, wrap="word",
                           font=("Consolas", 9), state="disabled")
        scroll = ttk.Scrollbar(logwrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=1, pady=1)

        self.runner = Runner(self.log, self._finished, self._progress)
        self.runner.pump(self.root)
        self.refresh()
        self.runner.write("Ready. Sync all 32 teams is the one you want most weeks.\n")
        if self.cfg.get("check_for_updates", True):
            threading.Thread(target=self._check_update, daemon=True).start()

    def _card(self, title: str) -> tk.Frame:
        wrap = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(wrap, text=title, bg=PANEL, fg=ACCENT, font=self.section).pack(
            anchor="w", padx=16, pady=(10, 0))
        return wrap

    def _button(self, parent, text, command, primary=False) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bd=0, cursor="hand2",
            bg=ACCENT if primary else LINE, fg="white" if primary else TEXT,
            activebackground=ACCENT_DIM if primary else "#374857",
            activeforeground="white", font=self.base, padx=14, pady=2,
            highlightthickness=0,
        )

    def _tile(self, parent, index, title, hint, command) -> None:
        r, c = divmod(index, 2)
        cell = tk.Frame(parent, bg=PANEL)
        cell.grid(row=r, column=c, sticky="ew", padx=(0, 12), pady=4)
        parent.columnconfigure(c, weight=1)
        self._button(cell, title, command).pack(anchor="w", ipady=3)
        tk.Label(cell, text=hint, bg=PANEL, fg=MUTED, font=self.small,
                 wraplength=300, justify="left").pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------ state
    def refresh(self) -> None:
        self.cfg = config.load()
        game = detect.find_game(self.cfg)
        mods = detect.mods_dir(game) if game else None
        club = (self.cfg.get("favorite_team") or "DET").upper()
        folders = len(detect.team_folders(mods)) if mods else 0
        linked = len(detect.load_links())

        from .cli import installed_team_folders
        real = len(installed_team_folders(mods)) if mods else 0
        ready = bool(game and real)
        for button in (self.btn_all, self.btn_mine):
            button.configure(state="normal" if ready else "disabled",
                             bg=(ACCENT if button is self.btn_all else LINE) if ready else "#243039",
                             fg="white" if ready else MUTED)
        if not game:
            text = "Game not found - set axis_dir in config.json"
            colour = WARN
        elif not real:
            text = (f"{game.name}   ·   no team mod installed - click Run setup, "
                    "there is nothing to update without one")
            colour = WARN
        else:
            text = f"{game.name}   ·   {real} teams from the mod, {linked} matched   ·   my team: {club}"
            colour = GOOD
        self.status.configure(text=text, fg=colour)
        self.btn_mine.configure(text=f"Sync {club} only")

    def _check_update(self) -> None:
        """Ask GitHub whether there is a newer build; show a banner if so."""
        from . import updates
        release = updates.check(self.cfg)
        if release:
            self.root.after(0, lambda: self._offer_update(release))

    def _offer_update(self, release: dict) -> None:
        from . import __version__
        self._pending_update = release
        self.update_text.configure(
            text=f"{release['tag']} is available - you are on v{__version__}")
        self.update_bar.pack(fill="x", padx=18, pady=(0, 10), after=self.status)

    def _do_update(self) -> None:
        release = self._pending_update
        if not release:
            return
        if not messagebox.askokcancel(
                "Update",
                f"Download and install {release['tag']}?\n\n"
                "The app will close and reopen itself. Your settings, team matches and "
                "manual overrides are left alone."):
            return

        from . import updates

        def work(args, cfg):
            print(f"Downloading {release['tag']} ...")

            def progress(got, total):
                if total and got % (5 * 1024 * 1024) < 262144:
                    print(f"   {got // 1048576} MB of {total // 1048576} MB")

            archive = updates.download(release, on_progress=progress)
            staged = updates.stage(archive)
            print("Installing - the app will restart itself.")
            updates.apply_and_restart(staged)

        self._run(f"Updating to {release['tag']}", work, self._args(), self.cfg)

    def _progress(self, label: str, done: int, total: int | None) -> None:
        """total is set when a job starts; after that only the club name changes."""
        if total is not None:
            self.total = total
            self.progress.configure(mode="determinate" if total else "indeterminate",
                                    maximum=total or 100, value=0)
            if not total:
                self.progress.start(14)
            self.progress_label.configure(text=label + " ...")
            return
        if self.total:
            self.progress.configure(value=min(done, self.total))
            self.progress_label.configure(text=f"{label}   ({done} of {self.total})")
        else:
            self.progress_label.configure(text=label)

    def _finished(self) -> None:
        self.refresh()
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.configure(mode="determinate", value=self.total or 0,
                                maximum=self.total or 100)
        self.progress_label.configure(text="Finished")
        # say what it means, not just that it stopped
        message = getattr(self.runner, "done_message", "") or "Finished. You can close this window."
        self.runner.write("\n" + message + "\n")

    def _args(self, **kw):
        from types import SimpleNamespace
        base = dict(team=None, all=False, gameday=None, apply=True, dry_run=False,
                    from_zip=None,
                    week=None, out=None, no_portraits=False, force=False,
                    reuse_low_ids=False, refetch=False, remove=False, all_teams=False,
                    stamp=None, config=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def _run(self, label, fn, *args, total: int = 0, done_message: str = "") -> None:
        if not self.runner.start(label, fn, *args, total=total,
                                 done_message=done_message):
            messagebox.showinfo("Busy", "Something is already running - let it finish first.")

    # ------------------------------------------------------------------ actions
    def _sync_all(self) -> None:
        from .cli import cmd_sync
        self._run("Syncing all 32 teams", cmd_sync, self._args(all=True), self.cfg, total=32,
                  done_message="Rosters are up to date. Start Axis Football and play.\n\n"
                  "Remember: a game already in progress keeps the rosters it was created "
                  "with, so start a NEW game to see these.")

    def _sync_mine(self) -> None:
        from .cli import cmd_sync
        self._run("Syncing my team and its next opponent",
                  cmd_sync, self._args(gameday=""), self.cfg, total=2,
                  done_message="Rosters are up to date. Start Axis Football and play.\n\n"
                  "Remember: a game already in progress keeps the rosters it was created "
                  "with, so start a NEW game to see these.")

    def _setup(self) -> None:
        dialog = SetupDialog(self)
        self.root.wait_window(dialog)
        choices = dialog.result
        if not choices:
            return

        from .cli import (_save_config, _steam_running, cmd_fields, cmd_fix_mod,
                          cmd_index_portraits, cmd_install_mod, cmd_portraits,
                          cmd_steam_launch, cmd_sync)

        if choices["team"] != (self.cfg.get("favorite_team") or "").upper():
            self.cfg["favorite_team"] = choices["team"]
            _save_config({"favorite_team": choices["team"]})

        while choices["steam"] and _steam_running():
            again = messagebox.askretrycancel(
                "Close Steam first",
                "Steam is running, and it rewrites its settings when it closes - so the "
                "launch option would be thrown away.\n\n"
                "Close Steam completely (check the system tray), then press Retry.\n\n"
                "Cancel skips just this step; everything else still runs.")
            if not again:
                choices["steam"] = False
                self.skipped_steam = True

        def work(args, cfg):
            if choices["mod"]:
                print("--- installing the team mod ---")
                cmd_install_mod(self._args(force=False, from_zip=choices.get("mod_zip")), cfg)
            if choices["repair"]:
                print("\n--- repairing the mod for Axis 2027 ---")
                cmd_fix_mod(self._args(), cfg)
                print("")
                cmd_fields(self._args(), cfg)
            if choices["photos"]:
                print("\n--- player photos ---")
                cmd_portraits(self._args(all=True), cfg)
                cmd_index_portraits(self._args(), cfg)
            if choices["steam"]:
                print("\n--- Steam launch option ---")
                cmd_steam_launch(self._args(all_teams=choices["steam_all"]), cfg)
            if choices["sync"] != "none":
                print("\n--- first sync ---")
                if choices["sync"] == "all":
                    cmd_sync(self._args(all=True), cfg)
                else:
                    cmd_sync(self._args(gameday=""), cfg)
            print("\nSetup finished.")
            if getattr(self, "skipped_steam", False):
                print("")
                print("NOT done: the Steam launch option was skipped because Steam was open.")
                print("Close Steam, then click \"Sync when I press Play\" on the main window.")

        self._run("Setting up", work, self._args(), self.cfg, total=32,
                  done_message="Setup is complete. You can close this window and play.\n\n"
                               "Start a NEW game - a saved one keeps the rosters, stadium "
                               "and field it was created with.")

    def _repair(self) -> None:
        from .cli import cmd_fields, cmd_fix_mod

        def both(args, cfg):
            cmd_fix_mod(args, cfg)
            print("")
            cmd_fields(args, cfg)

        self._run("Repairing the mod for Axis 2027", both, self._args(), self.cfg,
                  done_message="Mod repaired. Restart Axis Football, and start a NEW game - "
                               "a saved one keeps the field it was made with.")

    def _photos(self) -> None:
        from .cli import cmd_index_portraits, cmd_portraits

        def both(args, cfg):
            cmd_portraits(args, cfg)
            cmd_index_portraits(args, cfg)

        self._run("Fetching player photos", both, self._args(all=True), self.cfg, total=32,
                  done_message="Photos are in. Restart Axis Football to see them.")

    def _steam(self) -> None:
        from .cli import _steam_running, cmd_steam_launch
        while _steam_running():
            if not messagebox.askretrycancel(
                    "Close Steam first",
                    "Steam is running, and it rewrites its settings when it closes.\n\n"
                    "Close Steam completely (check the system tray), then press Retry."):
                return
        everything = messagebox.askyesno(
            "Sync when I press Play",
            "Sync all 32 teams when you press Play?\n\n"
            "Yes  -  all 32, about 15 seconds\n"
            "No   -  just your team and its next opponent")
        self._run("Setting the Steam launch option", cmd_steam_launch,
                  self._args(all_teams=everything), self.cfg,
                  done_message="Start Steam and press Play - it syncs first, then launches.")

    def _roster(self) -> None:
        from .cli import cmd_roster
        club = (self.cfg.get("favorite_team") or "DET").upper()
        self._run(f"{club} roster", cmd_roster, self._args(team=club), self.cfg)

    def _report(self) -> None:
        from .cli import cmd_report
        self._run("Last sync report", cmd_report, self._args(), self.cfg)

    def _undo(self) -> None:
        if not messagebox.askyesno("Undo last sync",
                                   "Put the previous rosters back from the last backup?"):
            return
        from .cli import cmd_restore
        self._run("Restoring the previous rosters", cmd_restore, self._args(), self.cfg,
                  done_message="Previous rosters are back. Restart the game to see them.")

    def _change_team(self) -> None:
        from tkinter import simpledialog

        from .sources import espn
        try:
            teams = {t["abbrev"]: t["display"] for t in espn.teams()}
        except Exception as exc:
            messagebox.showerror("Could not load teams", str(exc))
            return
        current = (self.cfg.get("favorite_team") or "DET").upper()
        answer = simpledialog.askstring(
            "Change my team",
            "Team abbreviation:\n\n" + ", ".join(sorted(teams)),
            initialvalue=current, parent=self.root)
        if not answer:
            return
        answer = answer.strip().upper()
        if answer not in teams:
            messagebox.showerror("Not a team", f"{answer} is not one of the 32.")
            return
        from .cli import _save_config
        self.cfg["favorite_team"] = answer
        _save_config({"favorite_team": answer})
        self.refresh()
        self.runner.write(f"\nMy team is now {teams[answer]}.\n")


def run(cfg: dict) -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    App(root, cfg)
    root.mainloop()
    return 0
