# GameSwitcher — Desktop and gaming mode switcher

A Windows tray utility with two modes:

- **A: Normal closure.** Save launch information, request normal closure, and reopen later. Surviving applications are not forcibly terminated.
- **B: Suspend and preserve context.** Minimize validated application windows, pause their validated processes, and resume the same processes later.

## Complete process baseline

Capture in a clean desktop environment after startup. The baseline includes every accessible process path in the current user/session, including tray and windowless applications. Legacy window-only or empty baselines must be recaptured before Mode B becomes available.

Mode B starts from non-baseline visible applications and checks every descendant against baseline, exclusions, user/session, process identity and system protection. A tree containing a protected member is preserved. Helpers of protected applications remain protected; Explorer and other launchers do not implicitly protect every later application.

Remote-control/input applications such as GameViewer, ToDesk, AnyDesk, TeamViewer and AutoHotkey, and their dependency families, are retained. A preview lists candidate applications, process counts and reasons for keeping applications running.

## Suspension recovery

- Durable identity/transaction records precede native suspension. Overlapping process trees are deduplicated.
- A separate guardian holds verified process handles and owns each suspend/resume operation.
- Confirm normal keyboard/mouse/desktop behavior within **12 seconds** after trial suspension; otherwise the guardian rolls back.
- **Ctrl+Alt+F12** triggers emergency recovery. A conflicting hotkey prevents suspension.
- Tray responsiveness loss for approximately five seconds, tray process death or normal exit causes recovery.
- Window placement is retained. Failed recovery keeps its journal. Discard first releases suspension owned by this utility rather than terminating applications.

Mode B does not call EmptyWorkingSet or change page-file settings. Windows manages paging. There is no guarantee of 0 MB RAM or instantaneous recovery. Unsaved memory cannot survive process exit, reboot or power loss; the JSON is not a memory image.

The guardian cannot cover kernel failure, power loss or killing both the tray and guardian. Some applications remain unsuitable for suspension. Preview targets and verify input during the confirmation window.

Legacy PID-only suspension records are not blindly resumed or killed. After restarting Windows, recover them as launch information. Explicit restart confirmation is available for ambiguous Fast Startup cases; it verifies that original processes are gone and backs up the journal.

Data lives in %APPDATA%\GameSwitcher. Exit safely before editing it. Do not run an old version against upgraded snapshots.

## Development

Windows / Python 3.13:

~~~powershell
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -v
python main.py --self-test
python tests/window_integration.py

python -m PyInstaller tests/window_fixture.py --onefile --noconsole --name SuspensionFixture --distpath dist-fixed/fixtures --workpath build-fixed/fixture --specpath build-fixed/fixture --noconfirm
python tests/suspension_integration.py

python -m PyInstaller GameSwitcher.spec --noconfirm
python tests/packaged_smoke.py dist/GameSwitcher.exe
python tests/packaged_suspension_smoke.py dist/GameSwitcher.exe
~~~

Regression tests mock application operations. Native suspension tests use only their own offscreen fixture executable. Windows CI tests normal restoration, confirmation/heartbeat timeouts, the rescue hotkey, parent process death and the packaged executable.

[Historical releases](https://github.com/jokerD888/GameSwitcher/releases) may predate these fixes.
