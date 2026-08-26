# Keithley 2182A GPIB6 实时电压监视器

## 固定目标

- VISA：`GPIB0::6::INSTR`
- 型号：Keithley 2182A
- 预期序列号：`1340129`
- Windows共享Python：`C:\LabAutomation\.venv\Scripts\python.exe`

## 安全边界

程序只允许两类消息：

1. 固定白名单中的配置查询；
2. 实时数据查询 `FETCh?`。

程序不含任意指令输入框，不含通用写入API，也不会发送`*RST`、`ABOR`、`INIT`、`READ?`、触发指令或配置写入。

`FETCh?`读取最近可用数据，不主动重建触发模型。它可能返回重复读数；程序会按原始结果保存，不会虚构或过滤数据。

## 最快部署

1. 解压整个文件夹到：

   `C:\LabAutomation\gpib6-2182a-live-monitor`

2. 确认共享环境存在：

   ```powershell
   Test-Path "C:\LabAutomation\.venv\Scripts\python.exe"
   ```

3. 确认PyVISA：

   ```powershell
   & "C:\LabAutomation\.venv\Scripts\python.exe" -c "import pyvisa; print(pyvisa.__version__)"
   ```

4. 停止正在访问GPIB6的LabVIEW VI，关闭NI MAX测试面板。不要同时使用两个控制程序。

5. 双击`START_2182A_GPIB6_MONITOR.bat`。

6. 先保留`simulate`，依次点击：

   - `1. Read Configuration`
   - `2. Start Live Plot`
   - `Pause`

7. 离线检查正常后，将Mode改为`real`。

8. 点击`1. Read Configuration`，阅读确认框后确认。程序将读取当前仪器配置并写入JSON证据文件。

9. 配置表必须显示：

   - identity包含`MODEL 2182A`和`1340129`
   - sense_function为`"VOLT:DC"`
   - data_format为`ASC`
   - format_elements为`READ`

10. 点击`2. Start Live Plot`开始实时曲线；点击`Pause`停止并关闭VISA会话。

## 当前真实配置基线（2026-08-25）

- CH1，直流电压
- CH1固定10 mV量程
- NPLC 5
- CH1数字滤波关闭、模拟滤波关闭
- 立即触发、连续启动、每次1个样本
- ASCII格式，仅返回读数
- 三次`FETCh?`实测：

  - `+1.08673749E-07 V`
  - `+1.32680867E-07 V`
  - `+1.39338466E-07 V`

这些数值只作为通信证据，不作为以后测量的预期值。

## 数据文件

每次配置读取建立新的`monitor_runs`子目录：

- `configuration-snapshot.json`：完整配置、原始响应和通信耗时；
- `voltage-*.csv`：全部实时样本。

CSV列：

- `host_timestamp`
- `elapsed_seconds`
- `voltage_v`
- `raw_response`
- `query_elapsed_ms`

GUI绘图窗口显示最近10分钟，CSV保存本次运行的全部数据。

