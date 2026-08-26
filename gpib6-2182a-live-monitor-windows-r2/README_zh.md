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

10. 点击`2. Start Live Plot`开始实时曲线。收到第一个样本后，`Mark Touch`按钮会启用。

11. 每当实际触碰仪器的某个位置时，立即点击`Mark Touch`。GUI会在当前经过时间处绘制一条竖直红线，并在同一CSV中写入一条`touch`事件。

12. 点击`Pause`停止并关闭VISA会话。红线在暂停、窗口缩放和普通重绘后仍会保留；下一次开始新的Live测量或点击`Clear Plot`时，屏幕上的旧曲线和红线会清空，但已经写入的CSV不会被删除或改写。

`Mark Touch`是纯主机端记录功能：它不会打开新的VISA会话，不会查询仪器，也不会发送任何设置或控制命令。

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

每次点击`2. Start Live Plot`都会新建一个文件：

`voltage-YYYYMMDD-HHMMSS-mmm.csv`

文件名中的日期时间是本次测量开始时的Windows本地时间，也是该CSV唯一记录的日历日期时间。CSV数据区不再为每个样本保存系统日期时间；所有样本和触碰事件都使用从本次Start开始、由主机单调时钟计算的`elapsed_seconds`标量，单位为秒。

CSV列为：

- `record_type`
- `elapsed_seconds`
- `voltage_v`
- `raw_response`
- `query_elapsed_ms`

`record_type=sample`表示一次真实的`FETCh?`结果，其电压、原始响应和查询耗时均完整保存；`record_type=touch`表示点击`Mark Touch`的主机事件，只保存经过时间，电压、原始响应和查询耗时留空，不会伪造测量值。

示例：

```csv
record_type,elapsed_seconds,voltage_v,raw_response,query_elapsed_ms
sample,0.251234,1.08673749e-07,+1.08673749E-07,3.500
touch,8.417392,,,
sample,8.502117,1.32680867e-07,+1.32680867E-07,3.421
```

GUI绘图窗口显示最近10分钟，CSV保存本次运行的全部数据。

## 离线测试

在项目目录运行：

```powershell
& "C:\LabAutomation\.venv\Scripts\python.exe" -m unittest discover -s tests -t . -v
```

当前共15项测试，覆盖query-only白名单、固定身份、CSV标量时间、触碰事件记录、红色竖线重绘和模拟采集查询序列。离线测试不会访问真实仪器。
