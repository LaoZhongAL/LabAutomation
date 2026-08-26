# 实验室 Windows 生产电脑只读调试教程

实际仪器通信只在实验室原有 Windows x64 控制电脑上运行。Mac 负责生成、审查、模拟和分析代码；虚拟机不连接生产 GPIB。当天顺序是：**保存现状 → 审计生产电脑 → 零消息枚举 → 一次身份查询 → 单机 identity → 单机 core**。

开始前打印或打开 [PRODUCTION_ENVIRONMENT_CHECKLIST_zh.md](PRODUCTION_ENVIRONMENT_CHECKLIST_zh.md)。

> 0.4.1 现场热修复：0.4.0 曾把 2182A 手册命令 `SYSTem:LFRequency?` 错写为无效缩写 `SYST:LFREQ?`，导致 GPIB6 在该步骤超时并按设计安全停止。0.4.1 改用无歧义完整形式 `SYST:LFREQUENCY?`。不要继续使用 0.4.0 执行 2182A core。

## 0. 2026-08-19 NI MAX 已确认的地址

| 仪器 | VISA 地址 | 当前依据 |
|---|---|---|
| MODEL 2182A #1 | `GPIB0::6::INSTR` | NI MAX 现场列表 |
| MODEL 2182A #2 | `GPIB0::7::INSTR` | NI MAX 现场列表 |
| MODEL 6221 #1 | `GPIB0::9::INSTR` | NI MAX 现场列表 |
| MODEL 6221 #2 | `GPIB0::10::INSTR` | NI MAX 现场列表 |
| MODEL 2450 #1 | `GPIB0::25::INSTR` | NI MAX 现场列表 |
| MODEL 2450 #2 | `GPIB0::26::INSTR` | NI MAX 现场列表 |

0.4.1 已把这六个地址写成生产白名单，并拒绝旧地址 8/18、未知地址以及型号 profile 与地址不一致的组合。仍需用一次 `*IDN?` 保存每台的序列号和固件，形成当天的“物理仪器—地址—序列号”证据。

## 1. 今晚在 Mac 做完

```bash
cd "/Users/minzai/My Drive/Research/仪器学习/170 测量仪器/readonly_instrument_probe"
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

预期所有测试均为 `ok`。再运行：

```bash
python -m instrument_probe --list-lab-addresses
python -m instrument_probe --model 2182a --mode simulate --scope full
python -m instrument_probe --model 6221 --mode simulate --scope full
python -m instrument_probe --model 2450-scpi --mode simulate --scope full
python -m instrument_probe --model 2450-tsp --mode simulate --scope full
```

不要在 Mac 上连接 GPIB。即使误输入真实模式，程序也会拒绝。

## 2. 到实验室后先不要运行代码

1. 先确认没有正在运行或暂停的实验；不确定就问原操作者。
2. 拍电脑、仪器前后面板、GPIB 总线、GPIB-USB 控制器、输出灯和 2450 Command Set。
3. 记录 Windows、LabVIEW、NI-VISA、NI-488.2、NI MAX、Keithley 驱动的精确版本。
4. 记录现有 LabVIEW 工程路径、主 VI、启动顺序、VISA 地址、数据目录和外部依赖。
5. 未经许可不要更新软件、改地址、拔线或替换驱动。
6. 得到负责人同意后，正常关闭会占用仪器的 LabVIEW、VISAIC、NI MAX 测试面板。NI MAX 本体可用于查看，但不要打开会话面板后一直占用资源。

如果 Python 或依赖尚未安装，不影响完成以上盘点。第一天可以止步于此。

## 3. 独立部署，不触碰 LabVIEW 工程

把完整项目复制到版本化目录，例如：

```text
C:\LabAutomation\readonly_instrument_probe-0.4.1
```

先调查已有 Python，暂时不要安装或升级：

```powershell
python --version
py --version
py --list-paths
where.exe python
python -c "import platform, struct; print(platform.python_version(), struct.calcsize('P')*8)"
```

记录每个 Python 的完整路径、版本和 32/64 位。如果有多个版本，后续所有命令都使用同一个明确解释器，例如 `py -3.9` 或某个完整的 `python.exe` 路径。

判定方法：

- 已有 Python 3.10/3.11/3.12：建立独立虚拟环境，不覆盖原环境。
- 已有 Python 3.9：本项目支持；安装时选择 PyVISA 1.14.x，再验证 NI-VISA 位数。
- 只有 Python 3.8 或更旧：先记录它是否被其他程序依赖，不直接升级或覆盖；申请一个并行的 Python 3.9+ 环境。
- 没有 Python：仍可先用设备管理器和 NI MAX 完成 NI/GPIB 环境调查，再申请安装。

进入项目目录后再执行：

```powershell
cd C:\LabAutomation\readonly_instrument_probe-0.4.1
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[visa]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

如果 `py` 不存在，不要临时安装未知来源的 Python。项目最低支持 Python 3.9，但不要求覆盖实验室已有版本。Python 位数不是代码硬门槛：32 位 Python 配 32 位 NI-VISA、64 位 Python 配 64 位 NI-VISA都可能有效，必须通过资源枚举确认。如果 PowerShell 禁止激活，教程全部命令都可用 `.\.venv\Scripts\python.exe` 代替 `python`。

安装 Python 包不等于安装 GPIB 驱动。真实 GPIB 应继续使用这台电脑上已验证的 NI-VISA/NI-488.2；先盘点版本，不盲目覆盖。

如果生产电脑不能联网，先在与生产电脑使用相同 Python 大小版本和位数的受控 Windows 环境准备离线包。

Python 3.9：

```powershell
py -3.9 -m pip download --dest wheelhouse `
  "pyvisa>=1.14,<1.15" "setuptools>=68"
```

Python 3.10 或更新版本（把 `3.11` 换成实际版本）：

```powershell
py -3.11 -m pip download --dest wheelhouse `
  "pyvisa>=1.14" "setuptools>=68"
```

将 `wheelhouse` 与项目一起按实验室批准方式带入，在生产电脑运行：

```powershell
.\.venv\Scripts\python.exe -m pip install --no-index `
  --find-links .\wheelhouse pyvisa "setuptools>=68"
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
```

必须保存下载来源、文件名和 SHA-256。若实验室不允许安装任何 Python 软件，停止部署，先只做环境盘点并申请批准。

## 4. 创建当天唯一、不可覆盖的运行目录

```powershell
$RunId = Get-Date -Format "yyyyMMdd-HHmmss"
$RunDir = Join-Path "C:\LabAutomation\production_runs" $RunId
New-Item -ItemType Directory -Path $RunDir
```

确认变量：

```powershell
$RunDir
```

所有以下输出都使用同一个 `$RunDir`。若程序提示文件已存在，重新生成 Run ID；不要删除或覆盖旧结果。

## 5. 审计生产电脑（不接触仪器）

```powershell
python -m instrument_probe --audit-host `
  --output "$RunDir\host-audit.json"
```

打开 JSON，确认：

- `windows: true`
- `x86_64_machine: true`
- `python_3_9_or_newer: true`
- `host_gate_passed: true`

同时记录 `python_bitness_assessment.detected_bits`。其中 `is_hard_gate: false` 表示位数不是预先假定的安全条件。`host_gate_passed` 只表示主机和项目门槛通过，不能证明 NI-VISA 已匹配。

任一主机检查为 false 就停止真实步骤。不要在 Mac/ARM 上绕过保护。32 位或 64 位 Python 是否可用，由第 7 步能否成功加载现有 VISA backend 决定。

## 6. 检查 GPIB 物理环境

1. 确认电脑 USB 接的是实验室原有 GPIB-USB 控制器。
2. 在设备管理器确认控制器无黄色感叹号；记录驱动版本。
3. 确认只有这一台电脑作为 GPIB System Controller；不要同时接 Mac 或第二台电脑。
4. 不改变已有串接顺序、地址和线缆。每台设备必须有唯一地址。
5. 记录所有仪器可见地址、开机状态、输出状态、预热时间和线缆走向。
6. 如果 LabVIEW 正在执行或保持资源，先按实验室正常流程停止；不能直接强杀未知生产程序。

## 7. 零消息：只枚举 VISA 资源

这一步让 VISA 列出资源，不打开仪器、不发 SCPI：

```powershell
python -m instrument_probe --list-visa-resources `
  --output "$RunDir\visa-resources.json"
```

预期出现若干 `GPIB0::数字::INSTR`。同时在 NI MAX 中查看资源名，但不要点击 Reset、Clear、Self Test，也不要在测试面板发送命令。

如果列表为空：

1. 停止，不改仪器地址。
2. 检查控制器是否被 Windows 识别。
3. 检查 NI-VISA、NI-488.2 现有安装和另一程序是否占用。
4. 保存截图和版本信息，找原操作者/管理员确认。

## 8. 第一次正式消息：六个确认地址各发一次 `*IDN?`

负责人同意进行远程查询后运行：

```powershell
python -m instrument_probe --identify-lab --mode real `
  --real-device-ack QUERY_ONLY --timeout-ms 2000 `
  --output "$RunDir\lab-identity-map.json"
```

程序按 6、7、9、10、25、26 的顺序分别最多发送一次 `*IDN?`。任何一台超时或型号不符，整体退出码为 2，不进入 core。检查：

- `ok`
- `identity`
- `detected_model`
- `matches_expected_model`

只有顶层 `all_six_identities_match` 为 `true` 才通过。发现未知型号、重复地址或接线与照片不符时停止，不修改地址。

## 9. 单台 identity 门槛

以下全部是已确认的固定地址。程序还会在打开 VISA 前检查 profile 与地址是否匹配。

2182A：

```powershell
python -m instrument_probe --model 2182a --mode real --scope identity `
  --resource "GPIB0::6::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2182a-gpib6-identity.json"

python -m instrument_probe --model 2182a --mode real --scope identity `
  --resource "GPIB0::7::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2182a-gpib7-identity.json"
```

6221：

```powershell
python -m instrument_probe --model 6221 --mode real --scope identity `
  --resource "GPIB0::9::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\6221-gpib9-identity.json"

python -m instrument_probe --model 6221 --mode real --scope identity `
  --resource "GPIB0::10::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\6221-gpib10-identity.json"
```

2450 身份阶段两种 profile 都只发 `*IDN?`，可暂用 TSP profile；进入 core 前必须从面板或原 VI 确认 Command Set：

```powershell
python -m instrument_probe --model 2450-tsp --mode real --scope identity `
  --resource "GPIB0::25::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2450-gpib25-identity.json"

python -m instrument_probe --model 2450-tsp --mode real --scope identity `
  --resource "GPIB0::26::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2450-gpib26-identity.json"
```

身份不匹配时程序只保留第一条查询并停止。

## 10. identity 全部通过后才读 core

```powershell
python -m instrument_probe --model 2182a --mode real --scope core `
  --resource "GPIB0::6::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2182a-gpib6-core.json"

python -m instrument_probe --model 2182a --mode real --scope core `
  --resource "GPIB0::7::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\2182a-gpib7-core.json"

python -m instrument_probe --model 6221 --mode real --scope core `
  --resource "GPIB0::9::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\6221-gpib9-core.json"

python -m instrument_probe --model 6221 --mode real --scope core `
  --resource "GPIB0::10::INSTR" --real-device-ack QUERY_ONLY `
  --output "$RunDir\6221-gpib10-core.json"
```

2450 面板/原 VI 已确认 TSP 时：

```powershell
python -m instrument_probe --model 2450-tsp --mode real --scope core `
  --resource "GPIB0::25::INSTR" --real-device-ack QUERY_ONLY `
  --2450-command-set-ack TSP `
  --output "$RunDir\2450-gpib25-tsp-core.json"
```

已确认 SCPI 时：

```powershell
python -m instrument_probe --model 2450-scpi --mode real --scope core `
  --resource "GPIB0::25::INSTR" --real-device-ack QUERY_ONLY `
  --2450-command-set-ack SCPI `
  --output "$RunDir\2450-gpib25-scpi-core.json"
```

对 GPIB26 使用完全相同的命令，只把地址和输出文件名中的 `25` 改成 `26`。两台 2450 可能采用不同 Command Set，必须分别查看面板；`--2450-command-set-ack` 必须与所选 profile 一致，程序不会替你切换。每运行一台先审查 JSON，再运行下一台。

## 11. 立刻停止的情况

- 实验状态或操作授权不明确。
- 主机审计/安全测试未通过。
- 有两个控制端，或 LabVIEW 仍占用资源。
- 型号不匹配、重复地址、未知仪器。
- 第一条后续查询超时/错误。
- 2450 Command Set 无法确认。
- 输出/源状态与现场预期不同，或仪器出现报警。
- JSON 中 `stopped_after_first_io_error` 或 `stopped_after_identity_mismatch` 为 `true`。

不要发送 LOCAL、RESET、CLEAR 或错误队列查询来恢复。保存结果、关闭 Python 会话、记录时间并联系负责人。

## 12. 当天收尾和发回资料

关闭 Python、VISAIC 和 NI MAX 测试面板；保持仪器、线缆和开关机状态符合实验室 SOP。将整个 `$RunDir` 复制到批准位置，计算哈希并保留原始副本。

需要带回分析的资料：

- `host-audit.json`
- `visa-resources.json`
- `lab-identity-map.json`
- 每台仪器的 identity/core JSON
- 软件版本、设备管理器、前后面板、GPIB 拓扑、2450 Command Set 照片
- 原操作者流程与尚未确认事项
- 独立温湿度计的读数、型号和时间（若有）

不要带回密码、许可证密钥、用户名、内部网络凭据或未经许可的实验数据。

第一天不要求运行 `full`。只有 core 结果稳定、与手册/面板/原 LabVIEW 一致并经过复核后，才在新的 Run ID 中对一台仪器试运行 full。
