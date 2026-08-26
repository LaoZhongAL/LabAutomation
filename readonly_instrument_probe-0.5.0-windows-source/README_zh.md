# Keithley 三仪器只读环境探针 0.5.0

目标仪器：Keithley 6221 电流源、Keithley 2182A 纳伏表、Keithley 2450 SourceMeter。此版本只用于盘点仪器身份、固件、通信环境和当前配置，不执行测量，不改变输出，不触发采集。

## Windows 一键 GUI

0.5.0 增加固定六台生产仪器的一键只读 core 扫描界面。启动：

```powershell
& .\.venv\Scripts\python.exe -m instrument_probe.gui
```

也可双击项目根目录的 `START_GUI.bat`。所有电脑都默认离线模拟；真实扫描必须在界面中明确选择 `real`。详细安装、扫描条件、状态解释和证据目录见 [GUI_TUTORIAL_zh.md](GUI_TUTORIAL_zh.md)。

GUI 复用与命令行相同的精确查询白名单和安全门禁，不提供任意命令输入、设置、复位、清除、触发、采集或输出控制。

## 电脑角色已经固定

- **Mac**：生成和审查代码、运行模拟/安全测试、分析从实验室带回的 JSON。Mac 不连接真实 GPIB。
- **实验室原有 Windows x64 控制电脑**：唯一允许运行真实 VISA/GPIB 模式的主机。先原样盘点现有 LabVIEW/NI 环境，不能为了 Python 先升级或替换驱动。
- **Windows 虚拟机**：可作普通 Python 或回放测试，但不作为真实仪器入口。

程序本身也执行这一规则：`--mode real`、`--identify-lab` 和 `--list-visa-resources` 在 macOS 或非 x64 Windows 上会被拒绝。

明天现场的完整勾选清单见 [PRODUCTION_ENVIRONMENT_CHECKLIST_zh.md](PRODUCTION_ENVIRONMENT_CHECKLIST_zh.md)，命令教程见 [DEBUG_TUTORIAL_zh.md](DEBUG_TUTORIAL_zh.md)。

## 安全边界

- 默认 `simulate`，不会打开 VISA、网络、串口或 GPIB 资源。
- 真实模式只能发送代码中逐条列出的白名单查询；没有通用 `write()` 方法。
- 禁止复位、清状态、输出开关、源值、校准、保存/调用配置、触发和清缓冲。
- 禁止可能启动测量的 `READ?`、`MEAS?`、`FETCh?` 和 `...DATA:FRESh?`。
- 禁止会清除事件/错误状态的 `SYST:ERR?`、`...EVENT?` 和 `*ESR?`。
- 每条消息禁止换行和分号，防止在查询后拼接第二条指令。
- 身份与 profile 不符或第一次 I/O 错误后立即停止，不尝试自动修复。
- 真实操作必须写入新的 JSON 文件，并禁止 `--overwrite-output`，以保留审计证据。

任何远程查询都可能让面板短暂显示 REMOTE 或占用通信会话。它不等于修改参数，但仍是可见的接口状态变化；若现场不允许，停在枚举资源阶段。

## NI MAX 已确认的生产地址

| 设备 | 固定 VISA 地址 |
|---|---|
| MODEL 2182A #1 | `GPIB0::6::INSTR` |
| MODEL 2182A #2 | `GPIB0::7::INSTR` |
| MODEL 6221 #1 | `GPIB0::9::INSTR` |
| MODEL 6221 #2 | `GPIB0::10::INSTR` |
| MODEL 2450 #1 | `GPIB0::25::INSTR` |
| MODEL 2450 #2 | `GPIB0::26::INSTR` |

离线查看：

```bash
python -m instrument_probe --list-lab-addresses
```

这些地址已由 2026-08-19 的 NI MAX 现场截图确认。0.5.0 真实模式只接受这六个地址，并验证所选 profile 与地址预期型号一致。`--identify-lab` 仍会各发送一次 `*IDN?`，用序列号/固件回复建立可审计的当天身份基线；它不会修改地址。

## Mac：只做离线验证

```bash
cd "/Users/minzai/My Drive/Research/仪器学习/170 测量仪器/readonly_instrument_probe"
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

模拟所有配置：

```bash
python -m instrument_probe --model 2182a --mode simulate --scope full
python -m instrument_probe --model 6221 --mode simulate --scope full
python -m instrument_probe --model 2450-scpi --mode simulate --scope full
python -m instrument_probe --model 2450-tsp --mode simulate --scope full
```

预览白名单但不打开仪器：

```bash
python -m instrument_probe --model 6221 --scope full --show-plan
python -m instrument_probe --model 2182a --mode dry-run --scope core
```

在 Mac 运行主机审计会得到 `host_gate_passed: false` 和退出码 2，这是预期保护，不是安装故障。

## 实验室 Windows 电脑：部署原则

先记录 Windows、LabVIEW、NI MAX、NI-VISA、NI-488.2、仪器驱动和 GPIB 控制器的现有版本。除非负责人批准，不执行升级、卸载或驱动替换。

先调查现有 Python，不预设必须安装 3.10：

```powershell
python --version
py --version
py --list-paths
where.exe python
python -c "import platform, struct; print(platform.python_version(), struct.calcsize('P')*8)"
```

项目 0.5.0 的兼容下限是 Python 3.9。Python 3.9 本身不会改变仪器安全性；它只影响代码和 PyVISA 包的可安装版本。Python 位数也不再作为硬性门槛，必须由后续 VISA 枚举实际验证它能否加载现有 NI-VISA。

建议把项目独立放在：

```text
C:\LabAutomation\readonly_instrument_probe-0.5.0
```

不要放入现有 LabVIEW 工程目录。使用独立虚拟环境安装 Python 包，不改变 LabVIEW 的 VI 搜索路径。

```powershell
cd C:\LabAutomation\readonly_instrument_probe-0.5.0
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[visa]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

这里的 `visa` extra 只安装 PyVISA，并优先沿用生产电脑现有的 NI-VISA（IVI backend）。Python 3.9 自动限制在 PyVISA 1.14.x；Python 3.10+ 可选择与其兼容的较新版本。若实验室电脑不能访问 Python 包仓库，应使用与生产电脑**相同 Python 大小版本和位数**的 Windows 环境准备离线 wheel。不要通过覆盖 NI-VISA 来解决 Python 包问题。

`--audit-host` 只判断主机和项目是否合格，不宣称 VISA 位数已经匹配。只有以下命令成功，才证明当前 Python 可以加载现有 VISA backend 并枚举资源：

```powershell
python -m instrument_probe --list-visa-resources --output "$RunDir\visa-resources.json"
```

每次创建唯一证据目录：

```powershell
$RunId = Get-Date -Format "yyyyMMdd-HHmmss"
$RunDir = Join-Path "C:\LabAutomation\production_runs" $RunId
New-Item -ItemType Directory -Path $RunDir
```

依次运行：

```powershell
python -m instrument_probe --audit-host --output "$RunDir\host-audit.json"
python -m instrument_probe --list-visa-resources --output "$RunDir\visa-resources.json"
python -m instrument_probe --identify-lab --mode real `
  --real-device-ack QUERY_ONLY --timeout-ms 2000 `
  --output "$RunDir\lab-identity-map.json"
```

真实单机查询必须使用现场确认的地址。第一次只用 `identity`：

```powershell
python -m instrument_probe --model 6221 --mode real --scope identity `
  --resource "GPIB0::9::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\6221-identity.json"
```

确认身份和每台 2450 的 Command Set 后，才进入 `core`。2450 core/full 还必须提供与 profile 一致的 `--2450-command-set-ack SCPI` 或 `TSP`；程序不会切换仪器模式。

## 输出和防覆盖

JSON 包含 UTC 时间、操作系统/Python/VISA 包版本、所选 profile、VISA 资源、每个回复、查询耗时和安全停止原因。默认不保存主机名、用户名、环境变量或凭据。

已存在的输出文件不会被覆盖。`--overwrite-output` 只允许离线模拟或 dry-run；真实操作使用它会被拒绝。生产现场应创建新 Run ID，而不是删除旧证据。

环境温湿度不是仪器内部配置。应使用独立、可校准的记录仪，另行保存数值、来源、序列号和采集时间。

## 资料边界

- 2182A 查询依据目录中的英文用户手册。
- 2450 结合用户手册与 Tektronix 2450 Reference Manual 核对。
- 6221 结合 Tektronix 6220/6221 Reference Manual 和实验室 Keithley 622x LabVIEW 驱动核对。
- 现有 LabVIEW 主 VI 含 Set Source、Enable Output 等写入节点，因此第一版不调用它。
- `full` 是“手册中适合无副作用查询的主要配置”，不是读取全部内存；校准常数、密码、事件/错误队列、测量缓冲等故意排除。
