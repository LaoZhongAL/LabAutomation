# 特殊版 GPIB 终端

这是一个独立诊断工具，不替换、不修改现有只读 GUI，也不使用连续的项目版本号。

终端复用实验室电脑上已有的 Python、PyVISA、NI-VISA、NI-488.2 和 NI GPIB-USB-HS 环境。每次操作只打开一个 VISA 会话、执行一次查询或写入、记录结果，然后立刻关闭会话。

## 在实验室 Windows 电脑上启动

1. 把压缩包直接解压到 `C:\LabAutomation`。
2. 关闭正在通信的 LabVIEW VI、NI MAX VISA 测试面板和其他仪器程序。测试期间只允许一个控制程序使用 GPIB 总线。
3. 第一次只读演示时，保持所有源输出为 OFF。
4. 双击 `START_GPIB_TERMINAL.bat`。
5. 如果程序没有自动找到 Python，在本文件夹中打开 PowerShell 并运行：

   ```powershell
   $env:PROBE_PYTHON = "C:\已有环境的完整路径\.venv\Scripts\python.exe"
   .\START_GPIB_TERMINAL.ps1
   ```

6. 在 `GPIB>` 提示符后输入：

   ```text
   MAP
   LIST
   QUERY GPIB0::9::INSTR *IDN?
   ```

预期通信显示：

```text
OPEN  GPIB0::9::INSTR
TX -> *IDN?
RX <- KEITHLEY INSTRUMENTS INC.,MODEL 6221,...
CLOSE GPIB0::9::INSTR
```

`GPIB0::9::INSTR` 是主机选择仪器时使用的 VISA 资源名，真正放到 GPIB 总线上的仪器消息是 `*IDN?`。这就是用户所举 `GPIB0::9::INSTR *IDN?` 例子对应的真实 PyVISA 通信形式。

## 安全模型

- `QUERY` 默认可用。`SENS:DATA:FRESH?` 等主动查询会触发或消耗一次读数，因此还要输入 `SEND ACTIVE_QUERY`。
- `WRITE` 启动时和退出后始终锁定；只有输入 `HELP` 显示的完整解锁短语才能解锁。
- 每一条写指令都要二次确认。设置源、打开输出、触发、复位以及 TSP 赋值等指令必须输入 `SEND HIGH_RISK`。
- 只接受已经现场确认的 6 个实验室 VISA 地址。
- 禁止换行和用分号拼接多条仪器指令；一行最多产生一条仪器消息。
- `LIST`、`MAP`、`STATUS`、`TIMEOUT` 和 `CALC-R` 都是主机本地操作，不向仪器发送消息。
- 程序无法替人判断物理接线、标准电阻额定功率、电压合规限值和测试电流是否安全，这些数值必须先由实验负责人确认。

每次启动都会在 `terminal_logs` 内建立一个新的只追加 JSONL 日志。请把该文件和实验记录一起保存。

英文标准命令见 [STANDARD_INPUT_COMMANDS_en.md](STANDARD_INPUT_COMMANDS_en.md)，中文标准命令见 [STANDARD_INPUT_COMMANDS_zh.md](STANDARD_INPUT_COMMANDS_zh.md)。
