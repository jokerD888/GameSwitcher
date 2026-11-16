# GameSwitcher — 游戏与办公应用切换工具

Windows 托盘工具，提供两种模式：

| 模式 | 行为 | 恢复方式 |
| --- | --- | --- |
| A：正常关闭 | 保存启动记录，发送正常关闭请求，超时保留应用 | 按原参数和工作目录重新启动 |
| B：挂起保留现场 | 最小化目标窗口，暂停经过校验的应用进程及子进程 | 解除本次挂起并还原窗口位置，不重新启动应用 |

## 基线与保护规则

“捕获完整进程基线”采集当前用户、当前登录会话中所有可读取的进程路径，包括托盘和无窗口后台，不再只采集可见窗口。

请在刚开机、没有打开临时工作软件的环境下捕获。旧版可见窗口基线、空基线仍可供模式 A 使用，但不能直接进入模式 B，需重新捕获一次。

模式 B 处理非基线可见应用及其经过校验的子进程；每个实际 PID 都重新检查：

- 基线和排除名单；
- 用户、登录会话、创建时间和完整 exe 路径；
- 系统进程、自身及祖先进程；
- 远控和输入程序，例如 GameViewer、ToDesk、AnyDesk、TeamViewer、AutoHotkey，以及相关依赖进程。

如果应用树包含应保留的后代，保留整棵候选树。基线程序后来产生的新辅助进程也保留。Explorer 等启动器不会把所有后来打开的软件都变成基线应用。

新菜单采用完整路径排除，旧文件名规则继续有效。可以刷新运行列表；“预览模式 B 的挂起目标”会列出拟处理和保留原因。

## 模式 B 的恢复保障

- 挂起前落盘全部目标身份和事务引用；按 PID 与创建时间去重，父子窗口不会导致重复挂起。
- 独立守护持有经过创建时间和路径校验的进程句柄，负责本次暂停和恢复。
- 暂挂起后需在 **12 秒内确认键鼠、桌面正常**；未确认、取消或超时会自动回滚。
- 游戏中可按 **Ctrl+Alt+F12** 紧急恢复。热键被占用时拒绝执行挂起。
- 托盘响应中断约 5 秒、托盘进程崩溃或正常退出时，守护恢复应用。
- 保留原窗口位置与最小化状态；恢复不擅自解除第三方调试器额外添加的挂起。
- 恢复失败时保留身份记录，支持重试；放弃记录先恢复本工具拥有的挂起，不强杀应用。

模式 B **不调用 EmptyWorkingSet，也不修改页文件设置**。虚拟内存换页交给 Windows，不承诺物理内存降到 0 或毫秒恢复。程序暂停期间内存状态保留，但关机、重启或程序退出会让未保存内存消失；JSON 记录不是内存备份。

守护不能覆盖整机断电、内核故障、同时强杀托盘与守护等情况。未适配软件仍可能不适合挂起，请先预览，并在试运行确认阶段核对输入是否正常。

## 使用

1. 先处理上次待恢复记录，退出旧版，运行新版。
2. 干净环境下选择“捕获完整进程基线”。
3. 添加需要保留的程序，可先预览模式 B 的目标。
4. 选择 A 或 B；B 暂挂起后，12 秒内确认输入正常。
5. 结束游戏后点击恢复。需要紧急恢复时按 Ctrl+Alt+F12。

旧版 PID-only 挂起记录缺少子进程身份，不会被盲目恢复或终止。重启后可按启动信息恢复；Windows 快速启动等使启动时间不明确时，可选择“确认已重启：恢复旧启动记录”。转换前备份原文件，并确认原进程已经不存在。

配置、基线、恢复记录、守护事务位于 %APPDATA%\GameSwitcher。日志会轮转。编辑配置前先安全退出；新版快照不要交给旧版写入。

## 开发与验证

Windows / Python 3.13，使用固定依赖：

~~~powershell
python -m pip install -r requirements-build.txt
python main.py
python -m unittest discover -s tests -v
python main.py --self-test
python tests/window_integration.py

# 专用离屏测试程序，仅用于集成测试
python -m PyInstaller tests/window_fixture.py --onefile --noconsole --name SuspensionFixture --distpath dist-fixed/fixtures --workpath build-fixed/fixture --specpath build-fixed/fixture --noconfirm
python tests/suspension_integration.py

python -m PyInstaller GameSwitcher.spec --noconfirm
python tests/packaged_smoke.py dist/GameSwitcher.exe
python tests/packaged_suspension_smoke.py dist/GameSwitcher.exe
~~~

隔离回归不操作真实应用；挂起集成测试只处理测试自身创建的专用窗口。Windows CI 包含正常恢复、确认超时、心跳超时、紧急热键、托盘崩溃和 EXE 自检。

[Releases](https://github.com/jokerD888/GameSwitcher/releases) 的历史制品可能不包含当前修复。详见 [模式 B 修复说明](review/MODE_B_FIXES.md)。
