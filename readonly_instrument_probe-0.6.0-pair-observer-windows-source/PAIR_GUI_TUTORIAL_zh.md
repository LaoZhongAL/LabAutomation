# 6221 / 2182A 手动 Pair 只读 GUI 上机教程

## 1. 这一版的目标

版本 `0.6.0` 允许操作者在 GUI 内自由选择一台 6221 和一台 2182A 组成软件 Pair。实验室已确认的四台仪器是：

| GUI 角色 | VISA 地址 | 型号 | 序列号 | 已观测固件 |
|---|---|---|---|---|
| 电流源 | `GPIB0::9::INSTR` | 6221 | 4533811 | D04 /700x |
| 电流源 | `GPIB0::10::INSTR` | 6221 | 4581062 | D04 /700x |
| 纳伏表 | `GPIB0::6::INSTR` | 2182A | 1340129 | C02 /A02 |
| 纳伏表 | `GPIB0::7::INSTR` | 2182A | 4510267 | C08/B01 |

四种组合都可选。GUI 只记录操作者选了哪两台，不会判断它们在物理上是否已通过标准电阻正确连接。

程序只在点击下列两个按钮时读取一次：

- **Read Pair Configuration**：读取 Pair 的完整实验配置快照。
- **Read Latest Measurement Snapshot**：读取一次简短的状态和 2182A 最后缓存电压，然后在电脑本地尝试计算 `R = V / I`。

程序没有定时 VISA 读取、没有自动刷新、没有任意命令输入框，也没有复位、清除、触发、启动采集、修改量程、设置电流或开关输出的代码。

## 2. 必须理解的实验边界

这一版支持的是：

```text
操作者手动接线和设置仪器
        ↓
操作者点击一次 GUI 按钮
        ↓
程序用固定白名单查询当前状态
        ↓
电脑本地显示并保存 JSON 证据
```

它不是自动化电阻测量程序。它不会向仪器写入一个已知安全的小电流，因为标准电阻阻值、额定功率、可允许自热和接线方式还没有被确认。任何电流、compliance 和输出操作都必须由人在仪器前面板或已批准的 LabVIEW 程序中完成。

GUI 显示的 6221 电流是 **programmed current**，不是第二台电流表独立测得的实际电流。`V/I` 只是当次手动快照的本地估算，不能直接作为标准电阻校准合格/不合格结论。

## 3. Windows 安装和版本确认

将 ZIP 解压到：

```text
C:\LabAutomation\readonly_instrument_probe-0.6.0-pair-observer
```

打开 PowerShell：

```powershell
Set-Location "C:\LabAutomation\readonly_instrument_probe-0.6.0-pair-observer"
```

先寻找 Python。新目录如果已经有 `.venv`，使用它；否则使用之前已在生产环境验证过的 0.5.1 虚拟环境：

```powershell
$ProbePython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $ProbePython)) {
    $ProbePython = "C:\LabAutomation\readonly_instrument_probe-0.5.1-english-gui-windows-source\.venv\Scripts\python.exe"
}
$ProbePython
Test-Path -LiteralPath $ProbePython
```

最后一行必须是 `True`。不要使用未定义或已失效的 `$ProbePython`。

将这个已验证环境的项目指向 0.6.0 源码：

```powershell
& $ProbePython -m pip install --no-deps --no-build-isolation -e .
```

这一步不会升级 NI-VISA、NI-488.2 或仪器固件，也不会打开 GPIB 仪器。它只更新当前 Python 虚拟环境内的项目指向。

确认版本：

```powershell
& $ProbePython -c "import instrument_probe, importlib.metadata as m; print('source:', instrument_probe.__version__); print('installed:', m.version('readonly-instrument-probe'))"
```

两行都必须是：

```text
0.6.0
```

确认 GUI 库和测试：

```powershell
& $ProbePython -c "import tkinter; print('Tk:', tkinter.TkVersion)"
& $ProbePython -m unittest discover -s tests -t . -v
```

应看到 `Ran 36 tests` 和 `OK`。在测试未全部通过时不进入 `real` 模式。

## 4. 先运行模拟模式

双击 `START_PAIR_GUI.bat`，或执行：

```powershell
& $ProbePython -m instrument_probe.pair_gui
```

GUI 每次启动都默认 `simulate`，不会记住上次的 `real`。

1. 在 6221 下拉框选 GPIB9 或 GPIB10。
2. 在 2182A 下拉框选 GPIB6 或 GPIB7。
3. 保持 `Mode = simulate`。
4. 点击 **Read Pair Configuration**。
5. 再点击 **Read Latest Measurement Snapshot**。
6. 确认下方显示完整参数，并且 `pair_runs` 中出现两个新的时间戳目录。

模拟模式不加载 VISA，也不打开任何 GPIB resource。

## 5. 第一次真实读取：先不接标准电阻

第一次现场运行建议保持 6221 输出 OFF，只验证 Pair 环境快照。

点击前确认：

- 所选 6221 和 2182A 已开机并完成自检；
- GPIB-USB-HS 和 GPIB 线保持之前已确认的连接；
- LabVIEW 控制程序已关闭；
- NI MAX 的 VISA Test Panel 已关闭，最好直接关闭 NI MAX；
- 没有另一个程序或电脑占用仪器；
- 6221 前面板 OUTPUT 保持 OFF。

在 GUI 中：

1. 选择物理上计划组合的 Pair。
2. 把 Mode 改为 `real`。
3. 点击一次 **Read Pair Configuration**。
4. 等待状态栏显示完成。运行期间不打开 LabVIEW/NI MAX，不拔插 GPIB。

预期读取数：

| 仪器 | Configuration 查询数 | 内容 |
|---|---:|---|
| 6221 | 21 | 已验证 core + Delta 配置/是否 armed + trigger source |
| 2182A | 18 | 已在两台真机通过的 core |

每台仪器的第一条命令都是 `*IDN?`。型号不匹配时，该仪器立即停止。某条 VISA 查询超时时，该仪器在第一个 I/O 错误处停止，另一台仍会尝试完成快照，并保存部分 JSON 证据。

## 6. 接入标准电阻后的手动流程

只在实验负责人确认标准电阻的接线、额定功率、允许电流和 compliance 后继续。

1. 保持 6221 OUTPUT OFF。
2. 按实验室四线法或被批准的回路连接 6221、标准电阻和 2182A。
3. 核对 GUI 中选择的 Pair 与实际线缆一致。GUI 不能替你发现接错线。
4. 用前面板或已批准的 LabVIEW 程序设置 6221 电流、量程和 compliance。
5. 用前面板确认 2182A 通道、量程、NPLC 和滤波。
6. 在 OUTPUT 仍为 OFF 时，点击一次 **Read Pair Configuration**，确认 GUI 所读参数与前面板一致。
7. 由操作者在安全条件下手动开启 6221 输出。程序不会执行这一步。
8. 等待 2182A 前面板读数稳定。
9. 点击一次 **Read Latest Measurement Snapshot**。
10. 读取完成后，由操作者按现场流程手动关闭输出。GUI 不会自动关闭。

Measurement 快照只发送：

| 仪器 | Measurement 查询数 | 关键内容 |
|---|---:|---|
| 6221 | 10 | 输出、互锁、编程电流、量程、compliance、Delta 状态 |
| 2182A | 9 | 量程/NPLC/通道 + `SENS:DATA:LATEST?` 最后缓存读数 |

`SENS:DATA:LATEST?` 是 2182A 手册定义的 query-only 命令，返回最后读数，不会触发一次新测量。因此点击前必须先在前面板看到读数已稳定。

## 7. `V/I` 何时才会显示

只有同时满足以下条件时，GUI 才在电脑内存中计算：

- 6221 和 2182A 的本次查询都完成；
- 6221 输出状态为 ON；
- 编程电流不是 0；
- 2182A 返回可解析的缓存电压；
- 6221 Delta 模式没有 armed。

如果未知标准电阻阻值，`Nominal resistance` 留空即可。GUI 仍可显示 `V/I` 估算，但不显示与标称值的误差。如果以后知道阻值，该值只保存在电脑的 JSON 中，不会发送给仪器。

## 8. 与 6221 Delta 模式的区别

这一版的 `V/I` 专门对应“人工设置一个恒定电流，2182A 显示电压”的简单快照。

真正的 6221 Delta 模式会在正负电流之间切换，6221 通常通过 RS-232 管理 2182A，并对多次电压测量进行 Delta 计算。当 GUI 读到 `SOUR:DELTA:ARM? = 1` 时，它不会用某一个缓存电压除以一个电流，因为无法保证该电压对应哪一个电流极性。

如果 Delta 模式正在占用 2182A，2182A 的 GPIB 直接查询还可能超时。程序会停在首个 I/O 错误并保存部分证据，不会发送 abort 或触发命令。真正 Delta 读数将属于后续单独审计的版本。

## 9. 证据和无自动刷新的证明

每次点击都新建唯一目录：

```text
pair_runs\YYYYMMDD-HHMMSS-mmm-real-pair-configuration\pair-observer.json
pair_runs\YYYYMMDD-HHMMSS-mmm-real-pair-measurement\pair-observer.json
```

已有证据不会被覆盖。JSON 包含：选择的 Pair、本机环境、每条查询/回复/耗时/错误、本地标准电阻元数据、快照总结和安全声明。

GUI 内部的 100 ms Tk timer 只把已完成的后台结果交给界面线程；它永远不调用 VISA。唯一能打开 VISA 的路径是操作者点击两个 Read 按钮之一。

## 10. 2450 互锁显示修正

0.6.0 也修正了六台仪器主 GUI 中 2450 TSP 互锁的显示方向。按 2450 参考手册：

- `smu.OFF`：互锁信号未 asserted，高电压量程不可用；
- `smu.ON`：互锁信号 asserted，所有电压量程可用。

因此之前 0.5.1 截图中 GPIB25/GPIB26 的 Warning 方向需要反过来解读。0.6.0 的主 GUI 已经按手册修正；Pair GUI 本身不操作 2450。

## 11. 第一次上机异常处理

- 出现 `VISA session-open failure`：关闭 LabVIEW 和 NI MAX Test Panel，确认地址未被占用，不要重置仪器。
- 6221 在某个 Delta 查询超时：保存当次 JSON，记录失败命令和固件；程序不会继续猜命令。
- 2182A 在 `SENS:DATA:LATEST?` 超时：先确认前面板是否在正常测量，以及是否正被 6221 Delta/RS-232 控制。
- GUI 显示 Output ON：只记录警告。程序不会帮你关断；由操作者按实验流程处理。
- 任何结果与前面板不一致：停止实验，保留 JSON 和现场照片，不要通过添加写命令尝试“修复”。

