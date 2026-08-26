# GPIB6 Keithley 2182A 单仪器诊断核心 v1.1

本目录包含 query-only 单仪器诊断核心和实时电压 GUI。v1.1 在 v1 诊断基线上将 readout 与人工干预标签完全分离：电压 CSV 只保存仪器样本，干预的类型、位置和开始/结束区间只保存到独立 `interventions.jsonl`。

## 固定目标与精确身份

- VISA 资源：`GPIB0::6::INSTR`
- 厂商：`KEITHLEY INSTRUMENTS INC.`
- 型号：`2182A`
- 序列号：`1340129`
- 固件：规范化后 `C02 /A02`
- Windows 共享 Python：`C:\LabAutomation\.venv\Scripts\python.exe`

地址不枚举、不扫描，也不从其他仪器重新推断。`*IDN?` 必须能解析成恰好四个字段，并逐字段精确匹配；序列号子串、错误固件或 GPIB7 的 2182A 都会阻止 Live。

## query-only 安全边界

配置诊断只执行源码中固定的 22 条查询；实时与 Single 阶段只允许 `*IDN?` 身份复核和 `FETCh?`。每条消息在真实 VISA 发送点再次经过精确白名单检查。

程序没有任意 SCPI 输入框、通用 write API 或仪器配置控件，不发送 `*RST`、`ABOR`、`INIT`、`READ?`、软件触发、清缓存或配置写入。故障注入只存在于 `simulate`；`real` 模式在 GUI 和函数入口都拒绝任何模拟上下文。

`FETCh?` 读取仪器当前可用读数，不重建触发模型。重复读数会按原始结果保存；空值、非数值、NaN、Inf 和绝对值不小于 `1e37` 的过量程哨兵会锁存故障。

## v1.1 Readiness 规则

Live 只有在 `can_start_live=true` 且状态为 `OBSERVE_READY`、recorder/stream 故障锁均未置位时才可启动。

| 层 | 主要规则 | 失败影响 |
|---|---|---|
| recorder | 运行目录、manifest、events/interventions JSONL 可在首查询前创建 | BLOCKED |
| communication | 22 条配置查询均有成功、非空证据 | BLOCKED |
| identity | 厂商、型号、序列号、固件精确匹配 | BLOCKED |
| acquisition | `VOLT:DC`、CH1、NPLC 5、10 mV 固定量程、CH1 两滤波关闭 | BLOCKED |
| acquisition | 触发计数近似无限、延迟 0、来源 IMM、样本数 1、连续启动 1 | BLOCKED |
| acquisition | 数据格式精确 `ASC`、元素精确 `READ` | BLOCKED |
| configuration | 线频 50 Hz | BLOCKED |
| configuration | SCPI 1991.0、上电设置 SAV0 漂移 | WARN，不单独阻止 |
| communication | 任一配置查询超过 500 ms | WARN，不单独阻止 |
| configuration | CH2 四项只取证；合法值显示 N/A，乱码显示 WARN | 不阻止 CH1 Live |

诊断表同时显示原始查询结果和规则结果。空响应不会显示绿色 PASS。

## 状态机

状态为：

`DISCONNECTED → VERIFYING_IDENTITY → CHECKING_CONFIG → OBSERVE_READY → LIVE`

运行中超出轮询时限进入 `DEGRADED`，恢复正常时回到 `LIVE`；Pause 正常收尾后回到 `OBSERVE_READY`。身份、采集、CSV、events/interventions JSONL、manifest 或会话故障进入 `FAULT_LATCHED`，必须重新运行诊断。

`RECOVERING` 为已有状态机的人工恢复状态；v1.1 不自动重连。断线后直接锁存，不会循环打开 VISA 或改变仪器。

## 文件必须整包部署

不可只复制主脚本。以下文件必须保持同目录结构：

- `gpib6_2182a_monitor.py`
- `diagnostic_core.py`
- `fault_injection.py`
- `run_evidence.py`
- `START_2182A_GPIB6_MONITOR.bat`
- `START_2182A_GPIB6_MONITOR.ps1`
- `README_zh.md`
- `tests/` 全目录

## Windows 部署与离线验证

1. 校验发布 ZIP：

   ```powershell
   Get-FileHash "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.1.zip" -Algorithm SHA256
   ```

   作用：只读计算发布包哈希，不解压、不访问仪器。预期：与交接文件给出的 v1.1 SHA-256 完全一致；不覆盖旧 v1 目录。

2. 解压到全新目录：

   ```powershell
   Expand-Archive `
     "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.1.zip" `
     "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.1"
   ```

   作用：建立独立版本目录，不覆盖旧文件。预期：目录中能看到四个 Python 运行模块和完整 `tests`。

3. 核对统一 Python：

   ```powershell
   Test-Path "C:\LabAutomation\.venv\Scripts\python.exe"
   ```

   作用：只检查共享解释器是否存在。预期：`True`。

4. 核对运行依赖：

   ```powershell
   & "C:\LabAutomation\.venv\Scripts\python.exe" -c "import sys, tkinter, pyvisa; print(sys.version); print(tkinter.TkVersion); print(pyvisa.__version__)"
   ```

   作用：只导入 Python、Tk 和 PyVISA，不打开 ResourceManager、不访问 GPIB。预期：全部导入成功并打印版本。

5. 运行离线测试：

   ```powershell
   Set-Location "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.1"
   & "C:\LabAutomation\.venv\Scripts\python.exe" -m unittest discover -s tests -t . -v
   ```

   作用：验证身份、Readiness、状态事务、manifest/events/interventions、纯样本 CSV、真实模式隔离和完整故障矩阵；不会实例化真实 VISA 会话。预期：`Ran 59 tests` 和 `OK`。

6. 启动 GUI：

   ```powershell
   & "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.1\START_2182A_GPIB6_MONITOR.ps1"
   ```

   作用：使用固定共享 Python 启动 GUI。预期：默认 `simulate`、fault=`nominal`、状态 `DISCONNECTED`。若 Python 非零退出，启动器会保留并返回原退出码。

如果 PowerShell 执行策略阻止脚本，可双击同目录的 `START_2182A_GPIB6_MONITOR.bat`。

## simulate 验证

1. 保持 `simulate` 和 `nominal`，点击 `1. Run Read-Only Diagnostics`。
2. 预期 22/22 响应、Readiness PASS、状态 `OBSERVE_READY`，Start/Single 启用；运行目录中在首查询前已有 manifest/events/interventions，随后产生 snapshot。
3. 点击 `2. Start Live Plot`。首个已提交样本后 `Mark Intervention: Start` 启用。
4. 选择 Intervention type，填写 Location，点击 `Mark Intervention: Start`；完成物理扰动后点击 `Mark Intervention: End`。预期显示带两条红色边界的浅红区间。只有 `interventions.jsonl` flush 成功后才更新图形；Start/End 不发送仪器消息。
5. 再开始一个干预区间，不手动 End，直接点击 Pause。预期先追加一条 end，再关闭 CSV/VISA 会话并回到 `OBSERVE_READY`。
6. 关闭 GUI 后再检查 manifest；只有关闭 GUI 才会使本次 diagnostic run 的 `final.closed=true`。

更换 fault 场景或 Output Folder 会立即使旧 Readiness/context 失效，必须重新点击 `1. Run Read-Only Diagnostics`。

### 故障注入场景

- 基准：`nominal`
- 配置阶段：`wrong_identity`、`malformed_identity`、`configuration_missing`、`configuration_drift`、`configuration_timeout`、`configuration_slow`
- Live 阶段：`fetch_timeout`、`fetch_slow`、`fetch_malformed`、`fetch_nan`、`fetch_inf`、`fetch_overrange`、`disconnect_after_3`
- 记录器阶段：`csv_open_failure`、`csv_write_failure`、`event_write_failure`

所有场景固定 seed=2182；每次 Live/Single 使用独立上下文。`event_write_failure` 在首条配置查询前停止。建议至少现场 smoke：

- `wrong_identity`：只出现一次 `*IDN?`，状态锁存，余下配置查询不执行；
- `configuration_slow`：Readiness WARN 但仍为 `OBSERVE_READY`；
- `fetch_slow`：events 出现 `POLL_DEADLINE_MISSED` 后出现 `POLL_TIMING_RECOVERED`；
- `fetch_overrange`、`fetch_timeout`、`disconnect_after_3`、`csv_write_failure`：保留此前已提交样本，随后 `FAULT_LATCHED`；
- `csv_open_failure`：live 查询历史为空；
- `event_write_failure`：配置查询历史为空，partial manifest 保留最后成功提交状态。

每次必须先选场景，再运行诊断。stream manifest 会保存实际 query history 与 consumed rule IDs。

## real 现场验证

只有 simulate、离线测试和故障 smoke 均通过后才进入 real：

1. 停止访问 GPIB6 的 LabVIEW VI，并关闭 NI MAX 测试面板；确保不存在第二控制器。
2. 切换 Mode=`real`。预期 fault 自动变为 `nominal` 且下拉禁用。
3. 点击 `1. Run Read-Only Diagnostics` 并确认 exclusive query-only 访问。
4. 预期地址仍为 `GPIB0::6::INSTR`，身份四字段精确匹配，Readiness PASS 或仅有可解释 WARN。
5. 若 BLOCKED/UNKNOWN：不要启动 Live，不要改仪器；关闭 GUI 并保留整个运行目录复核。
6. 若为 `OBSERVE_READY`：Live 采集 30–60 s，选择干预类型、填写位置，用 `Mark Intervention: Start/End` 标记实际扰动区间，随后 Pause 并关闭 GUI。
7. 上传整个最新运行目录，而不是只上传 CSV。

## 证据、readout CSV 与人工干预

每次诊断建立：

`monitor_runs/YYYYMMDD-HHMMSS-mmm-<mode>-gpib6-diagnostic-v1.1/`

其中可能包含：

- `run-manifest.json`：目标、白名单哈希、模式、故障场景、Readiness、状态、stream 统计和最终关闭状态；
- `events.jsonl`：稀疏、序号单调、使用单调经过时间的生命周期/故障事件；
- `interventions.jsonl`：人工干预的唯一事实源，保存类型、位置和成对的 start/end 标量时间；
- `configuration-snapshot.json`：原子写入的 22 条查询、原始响应、耗时、完整规则结果；
- `configuration-failure.json`：主证据记录器异常时的尽力失败说明；
- `voltage-*.csv`：每次 Live 的完整仪器 readout 样本，不包含人工干预行。

CSV 文件名 `voltage-YYYYMMDD-HHMMSS-mmm.csv` 只记录一次测量开始时的 Windows 本地日期时间。CSV 数据区没有逐行系统时间，字段固定为：

- `elapsed_seconds`
- `voltage_v`
- `raw_response`
- `query_elapsed_ms`

CSV 的每一行都是真实 `FETCh?` 样本；GUI 显示最近 10 分钟，CSV 保存本次 stream 全部样本。

`interventions.jsonl` 在每个 diagnostic run 创建一次，与多个 stream CSV 通过 `stream_id` 关联。每行只有：

- `schema_version`、`run_id`、`seq`、`stream_id`、`intervention_id`
- `phase`（`start` 或 `end`）
- `elapsed_seconds`
- `intervention_type`
- `location`

可选类型为 `cable_disturbance`、`connector_disturbance`、`interface_mechanical_stress`、`other`；Location 必填。同一区间的 start/end 共用同一 `intervention_id`，两个时间的差即区间持续时间。每行不写系统时间，不包含阈值、异常判断或自动流程字段。若记录异常中断，未配对的 start 原样保留，程序不猜测结束时间。

## v1.1 能证明与不能证明的范围

v1.1 能证明：固定资源上的精确身份、批准查询的通信完整性、当前 CH1/触发/格式基线、记录器可用性、Live 返回值可解析性、主机轮询时序，以及与 readout 隔离的人工干预区间。

v1.1 不能证明：样品接线正确、低温系统热平衡、接地/屏蔽质量、真实噪声谱性能、量程或触发设置在物理上最优。v1.1 不根据人工标签自动判断或控制后续流程，不读取仪器错误队列，不自动修正配置，不触发、不 INIT、不复位。多仪器协同应在本核心现场通过后再设计。
