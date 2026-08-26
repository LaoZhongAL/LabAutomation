# Diagnostic Core v1.5.1 Release Notes

发布日期：2026-08-26（Asia/Tokyo）

## Modified

- 2182A operation-condition B0 的单次诊断时序证据。
- B0 汇总、GUI 顶部摘要、configuration snapshot 与证据验证的一致性。
- 此前仅存在于活动源码、但尚未作为新版本发布的运行错误日志和 2182A 规则修正。
- 应用、profile、schema、README、Release Notes、HANDOFF 与部署包的 v1.5.1 身份。

## Cause

- v1.5 只在顺序 snapshot 的开始和末端各读取一次 `STAT:OPER:COND?`。当两次都看到 B0 置位时，只能证明两个时点为 calibrating，不能区分短暂变化、持续置位或证据不完整，也不能推断 FULL ACAL、Autozero 或其他根因。
- 活动源码已加入 `errors.jsonl`/证据校验、B9 非归因描述、`SYST:POSETUP?` 的 `PRES` 合法返回和修正后的规则失败文案，但发布身份仍停留在 v1.5。

## Change

- 只对 2182A profile 增加 13 条精确 `STAT:OPER:COND?`；它们在同一 VISA session 的末端按固定非均匀时点覆盖约 3 秒。连同开始和末端读数形成 15 个带主机单调时间的 B0 证据样本。
- 汇总分类固定为 `clear_for_entire_window`、`changed_during_window`、`set_for_entire_window` 或 `insufficient_evidence`。只有完整且全程清除时 PASS；任一样本 B0 置位为 BLOCKED；缺样本、查询失败、非法 condition word 或时间证据无效为 UNKNOWN 并阻断 Live。
- `configuration-snapshot.json` 保存完整观察协议、样本和汇总；`evidence_verifier.py` 从原始 transcript 独立重算，拒绝被改写的汇总。
- 2182A nominal 计数由 `38/38` 更新为 `51/51`。6221 保持 `21/21`，2450 保持 `41/43`。
- 正式纳入 `errors.jsonl` 及其与 events/manifest 的一致性校验；B9 只报告 ACAL questionable condition，不推断原因；`SYST:POSETUP?` 接受手册规定的 `PRES`；规则失败文案不再把已完成的前面板 FULL ACAL 错报为当前根因。
- `APP_RELEASE_TAG` 更新为 `v1.5.1`，`APP_VERSION` 更新为 `1.5.1-query-only-b0-observation`，诊断 schema 更新为 4。

## Verification

在 macOS 开发环境运行完整离线测试：175 项，171 通过，4 项需要 Tk 显示的 smoke 测试跳过，0 failure/error。另有直接回归覆盖 B0 全程清除、窗口内变化、全程置位、观察查询失败和汇总篡改。

## Result

v1.5.1 能把 2182A B0 限定为本次短观察窗内的可复核状态证据，并继续 fail-closed；不再用两个相邻时点或规则文案推断具体校准操作。版本身份与本次新增功能已同步。

## Unchanged

- 不重新推断或改变任何 VISA 地址、四字段身份映射或 GPIB6 Live allowlist。
- 不增加仪器写入、配置、触发、clear、reset、event/error queue 消费、retry 或任意 SCPI/TSP 输入。
- 6221/2450 的查询、Readiness 和 nominal 计数不变；2182A B0 规则没有套用到其他型号。
- CSV、`interventions.jsonl`、单 worker 和同一 GPIB 总线顺序访问保持不变。
- 未操作 Git；v1.5 ZIP 与其 SHA 文件不删除、不覆盖。

## Remaining blocker

Windows Tk GUI、NI-VISA/PyVISA 与真实 2182A 的 v1.5.1 query-only 观察窗尚未现场执行；在此之前不能把离线通过称为真实仪器问题已经解决。
