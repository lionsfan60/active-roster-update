@echo off
REM Sync the gameday rosters, then start Axis Football.
REM
REM Two ways to use this:
REM   1. Run it directly (double-click / shortcut) - it syncs, then launches via Steam.
REM   2. Steam -> Axis Football -> Properties -> Launch Options:
REM        "C:\Path\To\ActiveRosterUpdate\launch.bat" %command%
REM      Steam substitutes the real game command, this syncs first and then runs it,
REM      so pressing Play in Steam always gets you current rosters.

setlocal
cd /d "%~dp0"

echo Syncing rosters...
python -m activerosterupdate sync --gameday --apply
if errorlevel 1 echo WARNING: sync failed - launching with the rosters already on disk.

if "%~1"=="" (
    start "" "steam://rungameid/5026790"
) else (
    %*
)
endlocal
