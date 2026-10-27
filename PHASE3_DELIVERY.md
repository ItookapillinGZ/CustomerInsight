# 第三阶段最终交付

交付日期：2026-10-15（Asia/Shanghai）。已完成数据冻结、v1/v2/v3 全量开发集、候选 v3 冻结及 v1/v3 各 20 条单次最终测试。

参考标签来自“5 条人工确认＋55 条 AI 复核的合成示例”（dev：5＋35；test：0＋20）。指标衡量与这些参考标签的一致性，不是全人工金标或真实业务准确率；单次小样本差异不宣称统计显著。

## 结果与版本选择

| 划分 | Prompt | n | 情感 Acc | 情感 Macro-F1 | 类别 Acc | 类别 Macro-F1 | 优先级 Acc | 优先级 Macro-F1 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | v1 | 40 | 0.900000 | 0.887218 | 0.950000 | 0.950000 | 0.900000 | 0.859695 | 0 |
| dev | v2 | 40 | 0.925000 | 0.914179 | 0.975000 | 0.974026 | 0.950000 | 0.954545 | 0 |
| dev | v3 | 40 | 0.950000 | 0.955840 | 0.975000 | 0.974026 | 0.975000 | 0.960234 | 0 |
| test | v1 | 20 | 0.900000 | 0.886364 | 0.900000 | 0.906173 | 0.850000 | 0.809885 | 0 |
| test | v3 | 20 | 0.900000 | 0.900000 | 0.900000 | 0.913580 | 0.850000 | 0.831169 | 0 |


v3 与 v2 的类别 Macro-F1 持平，均严格高于 v1；按预先候选并列规则，以更高的情感 Macro-F1 选 v3。测试仅评估冻结的候选，不重新选型。v3 的两项通用规则修正及四条独立 dev 证据已在其 LIVE 运行前保存；未根据测试改 Prompt。

数据 hash：`caa67f79b3ca0d27d61efb060e0e4233e9e12c30fee7d4fd0b5a1b20c7a5c89c`。实验清单 hash：`608a97a5ded9f154e75cbb66d1500bfde2f8992bace6987318b785f1526359bd`。所有 Prompt hash、support、混淆矩阵、失败数、逐样本差异、真实错误原文和参数详见 [EXPERIMENT_REPORT.md](EXPERIMENT_REPORT.md)。

## 请求、证据与限制

| 划分 | Prompt | 尝试 | 输入 tokens | 输出 tokens | 总 tokens | 整轮秒 | 平均 ms | P95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | v1 | 40 | 30431 | 4036 | 34467 | 73.375 | 3191.690 | 5769.462 |
| dev | v2 | 40 | 46191 | 4293 | 50484 | 59.356 | 2716.611 | 3562.710 |
| dev | v3 | 40 | 53191 | 4051 | 57242 | 60.814 | 2739.816 | 3700.578 |
| test | v1 | 20 | 15239 | 2105 | 17344 | 33.147 | 2809.932 | 5095.531 |
| test | v3 | 20 | 26619 | 2207 | 28826 | 31.096 | 2722.403 | 3921.862 |


累计 174/180 次，剩余 6 次；含历史10次与首次环境连接失败4次。正式实验160次全量完成，零调用/结构失败。连接中止的4次usage不可用；所有可得阶段usage总计 199,321 tokens，未估算费用。预算未重置，未扩大上限。

数据/复核证据校验通过：5 reviewed、55 ai_reviewed、0 draft。保留逐条理由、修改前后记录及原始备份。Prompt 新增 v3；v1/v2、数据标签、标签集合、输出模型不变。另修正看板两处陈旧复核文案。

最终代码全量离线回归194项通过（98.266秒），见 validation/phase3_final_all_tests.log。旧测试把新注册的 v3 当成未知版本，已改用未注册版本并增加 v3 冻结 hash 校验；先前失败证据保留。最终5份报告重载、同split比较和15个实际下载内容验证通过，见 validation/delivery_ui_final_checks.json。AppTest 未声称真实浏览器下载。原始默认沙箱中止日志保留。最终测试两份执行标记均保留，不能删除标记重测。

## 交付文件

完整源码、报告、冻结证据、离线验证与原 MIT 许可证打包为 [CustomerInsight_phase3_final_20261015.zip](dist/CustomerInsight_phase3_final_20261015.zip)。文件 hash 与当前状态索引见 [artifact_index.json](evaluation_reports/phase3/artifact_index.json)。归档不含实际 .env 或虚拟环境；实验清单保留原机器绝对路径以保持原证据完整，迁移归档用于审阅和报告重载，不重开已使用的最终测试。

- [EXPERIMENT_REPORT.md](EXPERIMENT_REPORT.md)
- [dev_comparison.json](evaluation_reports/phase3/dev_comparison.json)
- [v3_change_record.json](evaluation_reports/phase3/v3_change_record.json)
- [experiment_manifest.json](evaluation_reports/phase3/experiment_manifest.json)
- [experiment_manifest.execution.json](evaluation_reports/phase3/experiment_manifest.execution.json)
- [final_test_608a97a5ded9f154.json](evaluation_reports/phase3/test/final_test_608a97a5ded9f154.json)
- [phase3_final_report_paths.json](validation/phase3_final_report_paths.json)
- [phase3_final_preflight.json](validation/phase3_final_preflight.json)
- [phase3_final_test_preflight.json](validation/phase3_final_test_preflight.json)
- [phase3_request_budget.json](validation/phase3_request_budget.json)

冻结数据与复核证据在 evaluation_reports/phase3/frozen/ 和 review_snapshot/；首次中止和旧 smoke 留在各自目录，正式报告以索引为准。旧文档已归档在 evaluation_reports/phase3/history_before_final_20261015/，不是当前实验结论。

## 三分钟演示

```powershell
Set-Location 'C:\path\to\CustomerInsight'
.\.venv\Scripts\python.exe -m streamlit run main.py --server.port 8503
```

打开 http://localhost:8503 。整个演示只读已保存报告，无需模型请求。

1. **0:00–0:35**：选择“自动化评测”，说明固定 dev 40 / test 20、5 条人工确认＋55 条 AI 复核及参考一致性口径。
2. **0:35–1:20**：在“选择本地报告（最多两个）”选下方指定的完整 dev v1/v3 两份报告，点“读取选定报告”。展示类别主指标、情感和优先级护栏，再切换详情看 support、混淆矩阵及 dev-004/012/020/024。
3. **1:20–2:15**：改选指定的最终 test v1/v3 两份报告并读取，说明候选已在测试前冻结。展示 test 20 条的实际结果和错误案例，不继续改 Prompt。
4. **2:15–3:00**：在详情分别点击“下载评测 Markdown 报告”“下载评测指标 JSON”“下载逐样本预测 CSV”；打开实验清单、累计预算与本报告，说明首次连接中止已保存，最终测试执行记录禁止重复运行。

演示所选报告（不要选 smoke 或 incomplete）：

- [live_dev_v1_5b943480875d45fa8300ea09e2b78fbf.report.json](evaluation_reports/phase3/dev/live_dev_v1_5b943480875d45fa8300ea09e2b78fbf.report.json)
- [live_dev_v3_f31ba93954e34e9c9203d2eca0cbfa62.report.json](evaluation_reports/phase3/dev/live_dev_v3_f31ba93954e34e9c9203d2eca0cbfa62.report.json)
- [live_test_v1_0b51d5b7cdd7424fb1fedc315c22e5dc.report.json](evaluation_reports/phase3/test/live_test_v1_0b51d5b7cdd7424fb1fedc315c22e5dc.report.json)
- [live_test_v3_4bc641f9c7bf431bad80b3899836e381.report.json](evaluation_reports/phase3/test/live_test_v3_4bc641f9c7bf431bad80b3899836e381.report.json)


## 可据实使用的三条简历描述

1. 基于 Y. Siva Sai Krishna 的 MIT 开源项目二次开发 CustomerInsight，使用 Python、Streamlit、Pandas、Pydantic 与 OpenAI SDK 实现单条/CSV 反馈分析、严格结构校验、行级失败隔离和可追溯报告。
2. 构建固定 dev 40/test 20 的可复现合成反馈评测，保留 5 条人工确认＋55 条 AI 复核来源，完成 3 版 Prompt 共 120 次完整开发集预测及基线/候选共 40 次最终测试；按预注册类别 Macro-F1 与多指标护栏冻结 v3。
3. 实现 180 次累计请求硬预算、逐条持久日志、Prompt/数据 hash 冻结与单次测试防重；完整离线回归 194 项通过。最终候选在 20 条合成测试参考上，类别 Accuracy=90.0%、Macro-F1=0.9136，指标限于混合来源参考标签一致性。

请按本人实际承担内容使用；不写全人工金标、真实客户准确率、统计显著、普遍提速或业务收益。

## 来源与许可证

基于 [AI Customer Feedback Analyzer](https://github.com/ysskrishna/ai-customer-feedback-analyzer)，原作者 **Y. Siva Sai Krishna（ysskrishna）**。保留原 [MIT License](LICENSE)，Copyright (c) 2025 Y. Siva Sai Krishna。
