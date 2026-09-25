# GameSwitcher — Blazing Fast One-Click Gaming Mode Switcher

> Close all non-baseline background apps in one second before gaming; restore them all with one click after. Pure silence, zero interruptions, ultra-lightweight.

When you're ready to game, your PC might have dozens of apps open — browser tabs, IDEs, chat tools, music players, and office suites. They eat up precious CPU, GPU memory, and RAM bandwidth, and cause frame drops from background notifications. Reopening them one by one after gaming is tedious.  
**GameSwitcher** solves this: two clicks in your system tray to seamlessly toggle between work and play.

---

## 🌟 Key Features

- ⚡ **Ultra-Fast Parallel Termination**: Concurrent broadcast of `WM_CLOSE` graceful save signal + 1.5s smooth exit grace period + single-pass batch `TASKKILL` cleanup. **Completes in ~1 second with zero prompt interruptions.**
- 🎯 **Clear Red/Blue Visual Status**:
  - 🔵 **Tech Blue Badge**: Ready / Normal mode.
  - 🔴 **Gaming Neon Red Badge**: In Gaming Mode (tray tooltip displays the number of suspended applications in real-time).
- 🔕 **Rule of Silence**: Silent on success. Necessary balloon notifications automatically dismiss after exactly 3 seconds to keep your screen distraction-free.
- 🛡️ **Zero-Typing Exclude Menu**: The tray menu dynamically lists currently running visible apps; simply click any app to add or remove it from the exclusion list without typing filenames.
- 🔄 **Crash-Resilient Snapshot**: Pending application snapshots are persisted atomically on disk. Even if your PC loses power or crashes during a game, your suspended apps remain safe and ready to restore.
- 🗑️ **Flexible Discard Option**: Finished gaming late at night and just want to shut down? Use **「Discard Restore (Clear Snapshot)」** to clear the snapshot and return to ready state without relaunching old applications.
- 🪶 **Ultra Featherweight**: Standalone single-file executable is only **~7.58 MB**; idle memory footprint is just **~6.7 MB** with 0% CPU usage.

---

## 🚀 Quick Start (Just 3 Steps)

1. **Place & Run**: Put `GameSwitcher.exe` in a permanent folder and launch it. It resides in your bottom-right system tray area.
2. **Capture Baseline**: On a freshly booted, clean desktop (with only your essential startup software running), right-click the tray icon → **「捕获基线」(Capture Baseline)**. You only need to do this once.
3. **Auto-Start**: Right-click the tray icon → check **「开机自启」(Run at Startup)** so it's always ready.

---

## 🎮 Daily Workflow

* **Before Gaming**: Right-click tray icon → **「🎮 进入游戏模式（关闭并保存快照）」(Enter Gaming Mode)**  
  *GameSwitcher snapshots your open applications, rapidly closes them, and turns the tray icon bright red.*
* **While Gaming**: Enjoy full performance without background clutter or popups.
* **After Gaming**:
  * **Resume Work**: Right-click tray icon → **「✨ 恢复应用」(Restore Apps)** to relaunch everything.
  * **Go to Sleep**: Right-click tray icon → **「🗑️ 放弃恢复（清空挂起快照）」(Discard Restore)** to clear the snapshot and reset to blue ready state.

---

## 🛡️ Exclude List (Never Close)

To keep voice chat (Discord, YY), game launchers, or OBS streaming tools open in Game Mode:
* Right-click tray icon → **「排除名单（永不关闭）」(Exclude List)**:
  * The lower section shows currently running apps. **Click any app to immediately exclude it.**
  * Excluded apps are marked with `✓` at the top; click to un-exclude.
  * Click **「📁 打开配置与日志目录」(Open Config Folder)** to directly view or edit `config.json`.

---

## 💡 FAQ

**Q: Are there annoying popups when entering Game Mode?**  
A: **None.** Following the Rule of Silence, both closing and restoring operate completely silently. The tool gracefully asks apps to save and exit, then batch-kills remaining targets after 1.5 seconds. A warning dialog is only shown if a privileged process fails to terminate.

**Q: Will my browser tabs be restored?**  
A: Yes. Modern browsers (Chrome, Edge, Brave) and editors (VS Code, Sublime) have native crash/restart session recovery that reopens all previous tabs automatically.

**Q: What is never closed?**  
A: Your baseline apps, excluded apps, and Windows core system processes (Explorer, input methods, dwm, GPU driver overlays, Xbox GameBar, etc.).

**Q: How do I completely uninstall it?**  
A: Uncheck "Run at Startup" in tray → click "Exit" → delete the exe and `%APPDATA%\GameSwitcher\`. Clean and zero registry residue.

---

## 🛠️ Build from Source

```bash
# Install dependencies
pip install -r requirements.txt

# Run from source
python main.py

# Build portable executable
python -m PyInstaller GameSwitcher.spec --noconfirm
```
