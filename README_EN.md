# GameSwitcher — Switch between gaming and desktop applications

A Windows tray utility that records application launch information, requests normal closure of visible non-baseline applications, and attempts to reopen them later.

## Reliability behavior

- Recovery records are atomically committed before any close request.
- Applications are given 5 seconds by default to close normally. Save prompts and cancellation are respected by leaving surviving applications alone. No forced termination or process-tree termination is used.
- Targets are limited to accessible visible windows in the current user/session. System applications, this process and its ancestors are protected. Executable matching uses full paths.
- Separate window-owning processes keep separate launch arguments and working directories.
- A relaunch is confirmed by a new visible window remaining present briefly. Unconfirmed records are retained. Clicking Restore again does not silently repeat an uncertain launch; explicit retry is available after checking the application.
- Exit cancels subsequent work and waits for active work to finish.
- The exclusion menu provides Refresh and lists every available path. New exclusions are path-specific; legacy filename rules remain supported.
- Invalid or unreadable configuration stops the operation instead of removing protection rules.
- Logs rotate at approximately 1 MB with three backups.

## Quick start

1. Start the utility and capture a baseline in a clean desktop environment. Game mode requires an explicitly captured baseline.
2. Refresh the exclusion list and select applications to keep running.
3. Enter game mode. Handle any save prompt shown by the target application. Applications that remain open are not forcibly terminated.
4. Restore after gaming. Check uncertain applications manually before using the explicit retry action.
5. Discard recovery records / return to ready when recovery is no longer needed.

Blue means ready. Red means game mode or pending work; the tooltip distinguishes partial or interrupted operations. Run at Startup uses the current executable path, so reconfigure it after moving the application.

## Recovery limits

The snapshot stores launch information, not process memory or document content. Unsaved work, every browser tab, and multiple workspaces within a single process cannot be guaranteed; session recovery depends on the target application's settings.

Invisible applications, slow startup, additional application prompts, internal helper arguments and unavailable working directories can prevent confirmation. Records remain available. Closing a window normally can also leave the application's own background processes running.

Legacy list snapshots can be read. New writes use version 2; do not let an old executable write to upgraded snapshots. Data lives in %APPDATA%\GameSwitcher. Exit safely before manually editing configuration.

~~~json
{
  "exclude_list": ["C:/Apps/Voice/voice.exe"],
  "grace_timeout_sec": 5,
  "restore_timeout_sec": 8
}
~~~

Close timeout: 0.1–60 seconds. Restore confirmation timeout: 1–30 seconds.

## Development and build

Windows / Python 3.13 with the pinned dependencies:

~~~powershell
python -m pip install -r requirements.txt
python main.py
python -m unittest discover -s tests -v
python main.py --self-test

python -m pip install -r requirements-build.txt
python -m PyInstaller GameSwitcher.spec --noconfirm
.\dist\GameSwitcher.exe --self-test packaged-smoke.json
~~~

Regression tests mock all application operations. The native self-test uses temporary data and an invisible tray; it never closes applications or changes the registry. Windows CI runs regressions, the source self-test, a build and the packaged self-test.

[Historical releases](https://github.com/jokerD888/GameSwitcher/releases) may predate these changes; a local fix does not publish a new release. See the Chinese review and fix notes in the review directory.
