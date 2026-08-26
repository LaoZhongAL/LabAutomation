# 六台仪器只读扫描 GUI 教程

## GUI 做什么

这个 GUI 固定扫描已经在 NI MAX 和生产 core 基线中确认的六台仪器：

| VISA 地址 | profile | 命令集 |
|---|---|---|
| `GPIB0::6::INSTR` | 2182A | SCPI |
| `GPIB0::7::INSTR` | 2182A | SCPI |
| `GPIB0::9::INSTR` | 6221 | SCPI |
| `GPIB0::10::INSTR` | 6221 | SCPI |
| `GPIB0::25::INSTR` | 2450 | TSP |
| `GPIB0::26::INSTR` | 2450 | TSP |

点击一次“扫描六台仪器”后，程序按地址顺序逐台运行已经通过现场验证的 `core` 白名单。扫描不是零消息枚举：它会向每台仪器发送只读查询，但不会发送设置命令。

GUI 显示：

- 通信是否成功；
- VISA 地址、型号、序列号和固件；
- source/output 状态；
- 互锁状态；
- 50 Hz 等 core 环境参数；
- 每条 core 查询的名称和值；
- 每台成功/总查询数；
- 明确的安全或通信警告。

GUI 不读取实时测量值，因为读取实时值可能启动或影响采集流程。GUI 也不提供复位、清除、触发、输出开关、量程设置或任意 VISA 命令输入框。

## 第一次安装 0.5.0

在 Windows PowerShell 中进入新版本项目目录。下面假设目录为：

```powershell
Set-Location "C:\LabAutomation\readonly_instrument_probe-0.5.0"
$ProbePython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
Test-Path -LiteralPath $ProbePython
```

最后一行必须返回 `True`。如果新版本目录没有 `.venv`，不要把旧路径继续保存在 `$ProbePython`；应先按实验室现有 Python 环境建立或复制经过验证的虚拟环境。

安装当前目录代码：

```powershell
& $ProbePython -m pip install --no-deps --no-build-isolation -e .
```

确认版本：

```powershell
& $ProbePython -c "import instrument_probe, importlib.metadata as m; print('source:', instrument_probe.__version__); print('installed:', m.version('readonly-instrument-probe'))"
```

两行都应为 `0.5.0`。

确认这个 Python 带有 GUI 运行库：

```powershell
& $ProbePython -c "import tkinter; print('Tk:', tkinter.TkVersion)"
```

应打印 Tk 版本。如果出现 `No module named 'tkinter'`，GUI 尚不能启动；这不是 GPIB 或仪器故障。先记录结果，不要为了 GUI 擅自升级实验室 Python、NI-VISA 或 NI-488.2，应在确认现有 Miniforge/conda 环境后单独补充兼容的 Tk 组件。

运行测试：

```powershell
& $ProbePython -m unittest discover -s tests -t . -v
```

必须看到所有测试通过后才能打开真实扫描。

## 先用离线模拟检查界面

启动 GUI：

```powershell
& $ProbePython -m instrument_probe.gui
```

也可以在资源管理器中双击项目根目录的 `START_GUI.bat`。

打开后：

1. 将“运行模式”选择为 `simulate`。
2. 点击“扫描六台仪器”。
3. 六行都应完成，表格显示模拟型号和参数。
4. 点击任意一行，在下方检查完整 core 参数。
5. 确认保存目录中出现一个名称含 `simulation-gui-core` 的新文件夹。

模拟模式不加载 VISA、不打开 GPIB，也不向真实仪器发送消息。

为了防止误操作，Windows 和 macOS 每次启动都默认 `simulate`；程序不会记住上次的 `real` 选择。

## 真实只读扫描前的现场条件

真实扫描前确认：

- 六台仪器已经正常开机并完成自检；
- GPIB-USB-HS 已连接；
- NI MAX 能看到 GPIB0 和六台已确认仪器；
- 没有地址冲突；
- 原 LabVIEW 控制程序没有在运行或占用这些 VISA session；
- NI MAX 的 VISA Test Panel 没有保持连接；
- 没有人正在通过前面板或其他电脑改变仪器状态；
- 不把 GPIB26 的互锁警告当成软件故障，也不为消除警告而改线或短接。

GUI 会在打开任何 VISA resource 之前执行 Windows/AMD64/Python 主机门禁。如果主机不符合项目条件，扫描不会开始。

## 一次真实扫描

1. 启动 GUI。
2. 确认运行模式为 `real`。
3. 确认保存位置正确；默认是程序启动目录下的 `gui_runs`。
4. 点击一次“扫描六台仪器”。
5. 等待六行依次从“等待”变为“扫描中”，再变为“通过”“警告”或“错误”。
6. 不要在扫描过程中启动 LabVIEW、打开 VISA Test Panel 或拔插 GPIB。

颜色和文字含义：

- 绿色“通过”：该仪器完成全部 core 查询，没有已定义警告；
- 黄色“警告”：通信可以成功，但存在需要人工理解的状态；
- 红色“错误”：身份不匹配、VISA/I/O 错误或该仪器未完成 core；
- GPIB26 若仍返回 `smu.interlock.tripped = smu.ON`，预计显示黄色“已跳闸”；程序不会修复或清除它。

即使某台仪器显示输出 ON，GUI 也只会显示警告，不会自动把输出关掉。任何输出状态处理必须属于未来经过单独审批和限值设计的控制程序。

## 自动保存的证据

每次点击真实扫描都会新建目录，例如：

```text
gui_runs\20260819-183000-123-real-gui-core\
```

其中包含六台的 `*-core.json` 和总表 `gui-scan-summary.json`。如果某台在打开 VISA 前失败，会保存对应的 `*-error.json`。程序使用新时间戳目录和“只创建、不覆盖”写法，不会覆盖以前的生产记录。

## 当前边界

- GUI 只运行 core，不运行 full。
- GUI 固定六个已确认地址，不会自动控制发现到的未知仪器。
- core 是某一时刻的配置快照，不保证扫描后状态不会被 LabVIEW 或人员改变。
- JSON 内没有完整记录 NI-VISA、NI-488.2 和 GPIB-USB-HS 驱动/固件版本；这些仍属于独立环境审计。
- 下一阶段若增加 full，必须先按固件和查询组重新审计，不能直接把按钮改成读取全部命令。
