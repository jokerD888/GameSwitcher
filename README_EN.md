# GameSwitcher — Instant Gaming & Work Mode Companion (Dual-Mode Edition)

> Free up CPU and RAM with one click before gaming; restore your entire workspace in milliseconds afterward.  
> **Innovative Dual-Mode Architecture**: Supports both "Deep Freeze (Preserve State)" and "Clean Purge (Extreme Performance)". Silent, zero popups, single portable executable.

---

## 📥 Download & Run

👉 **[Go to GitHub Releases to download the latest GameSwitcher.exe](https://github.com/jokerD888/GameSwitcher/releases)**

- **Single Portable Executable**: Standalone `GameSwitcher.exe` (~7.8 MB). Double-click to run with zero installation or bloat.
- **Zero Dependencies**: Bundles an optimized Python runtime internally; no external dependencies required.
- **Clean Persistence**: Snapshots and configurations live safely in `%APPDATA%\GameSwitcher\`.

---

## 🌟 Core Features: Dual-Mode Architecture

| Dimension | 🚀 Mode B: Deep Freeze (Preserve State · Recommended) | 🧹 Mode A: Clean Purge (Extreme Performance · Shutdown First) |
| :--- | :--- | :--- |
| **How It Works** | `NtSuspendProcess` thread freezing + `EmptyWorkingSet` memory swap to SSD | Gentle `WM_CLOSE` broadcast + fast batch cleanup |
| **CPU Usage** | **0.00%** (All threads suspended, yielding 100% CPU to the game) | **0.00%** (Processes completely terminated) |
| **Physical RAM** | **Plummets to 0.00 MB** (Forced out of physical RAM to SSD Pagefile) | **100% Fully Released** |
| **Workspace State** | **🌟 100% Lossless! Unsaved drafts, cursor positions, tabs intact** | Relaunches applications cleanly |
| **Restore Speed** | **Instantaneous revival in milliseconds** | Cold-starts applications sequentially |
| **Ideal For** | Quick gaming breaks during work, coding, browsing | Heavy 3A games, or before going to sleep |

---

## 🚀 Quick Start (Just 3 Steps)

1. **Place & Run**: Download `GameSwitcher.exe`, put it in any folder, and run it. It sits in your bottom-right system tray.
2. **Capture Baseline**: On a freshly booted clean desktop (with only your essential startup software running), right-click tray icon → **「捕获基线」(Capture Baseline)**. Only needed once.
3. **Daily Gaming**:
   - **Before Gaming**: Right-click tray icon and choose your mode:
     - **「🚀 进入游戏（保留现场 · 推荐）」(Deep Freeze · Recommended)** — Keep your entire workspace intact!
     - **「🧹 彻底清场（极度纯净 · 关机首选）」(Clean Purge)** — Release all memory and VRAM!
   - **After Gaming**: Right-click tray icon → **「✨ 恢复应用」(Restore Apps)** to resume immediately.
   - **Going to Sleep**: Right-click tray icon → **「🗑️ 放弃恢复 / 返回就绪」(Discard Restore)** to clear state.

---

## 🛡️ Smart Exclusion List

Exclude voice apps (Discord, YY, OBS, Game Accelerators) with zero typing:
- Right-click tray icon → **「排除名单（保留运行）」(Exclusion List)**.
- Running apps are automatically listed; click any app to toggle exclusion on/off.

---

## 💡 Engineering Highlights

1. **Anti-Ghost Window Technology**: Before freezing, GameSwitcher calls `ShowWindowAsync(SW_HIDE)` to unmap top-level windows from DWM, eliminating "Not Responding" prompts and gray screens.
2. **Process Tree Coverage**: Recursively scans and suspends all child processes for multi-process applications like Chrome, Edge, and VS Code.
3. **Exit Safety Recovery**: If GameSwitcher exits, all suspended processes are automatically resumed and their windows restored.
4. **Ultra Lightweight**: Single file ~7.8 MB, idle memory ~6.7 MB, 0% CPU footprint.

---

## 🛠️ Build & Smoke Test

```bash
# Install dependencies
pip install -r requirements.txt

# Run silent self-test
python main.py --self-test

# Build standalone executable
python -m PyInstaller GameSwitcher.spec --noconfirm
```
