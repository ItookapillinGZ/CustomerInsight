# 完整真实实验报告

日期：2026-10-15（Asia/Shanghai）。完整开发集与冻结后的单次最终测试均已完成；最终候选为 **v3**。

参考标签来自“5 条人工确认＋55 条 AI 复核的合成示例”（dev：5＋35；test：0＋20）。指标衡量与这些参考标签的一致性，不是全人工金标或真实业务准确率；单次小样本差异不宣称统计显著。

## 数据、Prompt 与实验冻结

| 对象 | SHA-256 / 说明 |
| --- | --- |
| 数据集 canonical hash | caa67f79b3ca0d27d61efb060e0e4233e9e12c30fee7d4fd0b5a1b20c7a5c89c |
| 冻结 CSV 文件 hash | f5d4c61acdffa679eae9a68ec9757519ea5ca90dfb9d317a7f2f0d7624a98fd2 |
| 复核台账文件 hash | 3da4c8f8d683fb589283e7a3827914389b8df2addc64ce11fb3553ffc33906bb |
| 标注指南 hash | 704d5831afe17297c43f94a476c6e7b563a847cf662157f9a149bd418ff02934 |
| v1 Prompt hash | ceb349897427181de8c3e9e8c622968258fd20f56c82a234b6250f38df0131c6 |
| v2 Prompt hash | bfb6a4bae5e1a2a6ad2c4627769d467fab21fe38e9c580bc63933dad9c469303 |
| v3 Prompt hash | 19ae88284b1d4634314da8846fe9da4fc8e8c804327ce7124a2c5a068bcf0a64 |
| 实验清单内容 hash | 608a97a5ded9f154e75cbb66d1500bfde2f8992bace6987318b785f1526359bd |
| 冻结时间 UTC | 2026-10-15T02:22:12.675624+00:00 |
| 复核台账来源 | 5 reviewed / 55 ai_reviewed / 0 draft；复用现有逐条历史与理由 |


数据和逐条复核证据通过一致性校验并冻结；冻结 CSV 字节 hash 与 canonical dataset hash 口径不同。dev/test 保持原 40/20 划分，测试反馈未进入 Prompt 调整。数据快照和复核历史见 [feedback_eval.csv](evaluation_reports/phase3/review_snapshot/caa67f79b3ca0d27d61efb060e0e4233e9e12c30fee7d4fd0b5a1b20c7a5c89c/feedback_eval.csv)、[feedback_eval.review.json](evaluation_reports/phase3/review_snapshot/caa67f79b3ca0d27d61efb060e0e4233e9e12c30fee7d4fd0b5a1b20c7a5c89c/feedback_eval.review.json)。

## 请求设置与选型规则

沿用项目现有 .env：请求模型标识 gpt-5.6-luna，openai_compatible 网关，Chat Completions / json_object，完整 Pydantic 本地校验；不独立证明网关内部模型身份。并发 2，应用/SDK 重试均 0，超时 300 秒，max_completion_tokens=8192，reasoning_effort=none，store=false。完整安全参数与 endpoint hash 见实验清单；不记录密钥。Responses/严格 JSON Schema 实际能力仍未验证。

预先规则：类别 Macro-F1 严格高于 v1；结构成功率、情感与优先级 Macro-F1 均不得低于 v1；主指标与 v1 同分保留 v1。合格候选按类别 F1、结构成功率、情感 F1、优先级 F1、平均耗时、版本顺序排序。v2/v3 类别 F1 相同，结构同为 100%，v3 情感 F1 更高，故冻结 v3；没有事后增加指标或耗时门槛。

## 完整开发集和最终测试指标

| 划分 | Prompt | n | 情感 Acc | 情感 Macro-F1 | 类别 Acc | 类别 Macro-F1 | 优先级 Acc | 优先级 Macro-F1 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | v1 | 40 | 0.900000 | 0.887218 | 0.950000 | 0.950000 | 0.900000 | 0.859695 | 0 |
| dev | v2 | 40 | 0.925000 | 0.914179 | 0.975000 | 0.974026 | 0.950000 | 0.954545 | 0 |
| dev | v3 | 40 | 0.950000 | 0.955840 | 0.975000 | 0.974026 | 0.975000 | 0.960234 | 0 |
| test | v1 | 20 | 0.900000 | 0.886364 | 0.900000 | 0.906173 | 0.850000 | 0.809885 | 0 |
| test | v3 | 20 | 0.900000 | 0.900000 | 0.900000 | 0.913580 | 0.850000 | 0.831169 | 0 |


Accuracy 分母包含全部样本，调用/结构失败计错误。Macro-F1 使用固定完整枚举标签、零分母取 0。混淆矩阵行为参考标签，列为预测，单独保留 __failure__；所有正式报告均完整执行且无失败、无 not_executed。

## 实际 token usage、耗时和累计预算

| 划分 | Prompt | 尝试 | 输入 tokens | 输出 tokens | 总 tokens | 整轮秒 | 平均 ms | P95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | v1 | 40 | 30431 | 4036 | 34467 | 73.375 | 3191.690 | 5769.462 |
| dev | v2 | 40 | 46191 | 4293 | 50484 | 59.356 | 2716.611 | 3562.710 |
| dev | v3 | 40 | 53191 | 4051 | 57242 | 60.814 | 2739.816 | 3700.578 |
| test | v1 | 20 | 15239 | 2105 | 17344 | 33.147 | 2809.932 | 5095.531 |
| test | v3 | 20 | 26619 | 2207 | 28826 | 31.096 | 2722.403 | 3921.862 |


本阶段累计 **174/180**，剩余 **6**。构成：历史 smoke 10 次＋本轮首次连接中止 4 次＋完整开发集 120 次＋最终测试 40 次。中止后未删除预算或重置上限。正式实验 160 次全部返回 usage；本阶段所有可得 usage 合计输入 181,253、输出 18,068、总计 199,321 tokens，另 4 次连接失败 usage 不可用，费用不可用，未估算。

平均/P95 为每条请求开始至校验结束，包含失败，排除线程排队与共享客户端初始化；P95 线性插值。整轮秒来自报告 wall_duration_ms。版本顺序执行、样本小，耗时差异不宣称普遍性能提升。

## v3 的开发集依据与结果

仅追加两条通用边界澄清：dev-012/dev-024 的 neutral→positive；dev-004/dev-020 的 low→medium。前者强调客观正常事实不替代表扬，后者限定轻微展示/文案与显著影响的边界。每项有两个独立 dev 样本，未使用 few-shot、测试文本或测试预测；单例 dev-013 类别错误和 dev-038 情感错误没有被定向补规则。改动在运行前记录于 [v3_change_record.json](evaluation_reports/phase3/v3_change_record.json)。v1/v2 模板、标签集合、输出模型、标注指南和参考数据未改。

下表为相对 v1 的逐样本正确性变化；mixed 表示同一样本既有改善也有退化，unchanged 只表示正确性不变，并不保证预测标签或生成文本完全相同。

| 版本 | 改善 | 退化 | 混合 | 正确性不变 |
| --- | --- | --- | --- | --- |
| v2 | 5 | 2 | 0 | 33 |
| v3 | 6 | 1 | 1 | 32 |


v3 修正了 dev-012/dev-024 的中性事实情感和 dev-004/dev-020 的低优先级，但 dev-004/dev-020 的情感同时由正确的 negative 退为 neutral，dev-022 的优先级再次偏高，dev-013 的类别错误仍存在。以上均为观察到的变化，不能据此确认内部根因；本次保留这些代价并按原规则选型。

## 有证据的开发集差异

| Prompt | ID | 原文证据 | 观察到的差异 |
| --- | --- | --- | --- |
| v1 | dev-008 | 现在只能逐笔开票，我们每周有上百笔交易，希望支持批量开票功能。 | 情感 neutral → negative；优先级 medium → high |
| v1 | dev-012 | 建议以后把版本更新安排在周末，现有功能运行正常，方便大家提前准备。 | 情感 neutral → positive |
| v1 | dev-013 | 帮助文档的操作说明太省略了，我照着做仍然不会，建议补充完整步骤和教学视频。 | 类别 suggestions → user_experience |
| v1 | dev-018 | 系统运行稳定而且响应很快，但找报表入口要翻好几层菜单，我每天都被这个流程困扰。 | 情感 negative → positive |
| v1 | dev-019 | 页面当前使用蓝色主题，我个人更习惯绿色，能否调整配色？不影响使用。 | 类别 user_experience → suggestions |
| v1 | dev-020 | 确认与取消按钮名字太相似，我总要停下来辨认，功能都正常但文案有点别扭。 | 优先级 low → medium |
| v1 | dev-022 | 杯子收到时没有问题，正常使用两天后手柄裂开了，现在不敢再用了。 | 优先级 medium → high |
| v1 | dev-024 | 我测了一下水壶容量，确实是标注的一升，暂时没有其他评价。 | 情感 neutral → positive |
| v1 | dev-025 | 客服很热情，配送也快，但同批三个耳机有两个左耳不响，最需要解决的是产品质量。 | 优先级 medium → high |
| v2 | dev-004 | 个人资料页把日期显示成了上个月，实际保存的日期正确，只影响展示。 | 优先级 low → medium |
| v2 | dev-012 | 建议以后把版本更新安排在周末，现有功能运行正常，方便大家提前准备。 | 情感 neutral → positive |
| v2 | dev-013 | 帮助文档的操作说明太省略了，我照着做仍然不会，建议补充完整步骤和教学视频。 | 类别 suggestions → user_experience |
| v2 | dev-020 | 确认与取消按钮名字太相似，我总要停下来辨认，功能都正常但文案有点别扭。 | 优先级 low → medium |
| v2 | dev-024 | 我测了一下水壶容量，确实是标注的一升，暂时没有其他评价。 | 情感 neutral → positive |
| v2 | dev-038 | 今天心情不好，来这里写一句抱怨，和你们的产品服务无关。 | 情感 negative → neutral |
| v3 | dev-004 | 个人资料页把日期显示成了上个月，实际保存的日期正确，只影响展示。 | 情感 negative → neutral |
| v3 | dev-013 | 帮助文档的操作说明太省略了，我照着做仍然不会，建议补充完整步骤和教学视频。 | 类别 suggestions → user_experience |
| v3 | dev-020 | 确认与取消按钮名字太相似，我总要停下来辨认，功能都正常但文案有点别扭。 | 情感 negative → neutral |
| v3 | dev-022 | 杯子收到时没有问题，正常使用两天后手柄裂开了，现在不敢再用了。 | 优先级 medium → high |


这里只报告原文、参考标签与预测的差异，不能确认模型内部根因。v3 仍有错误，未据一次运行宣称普遍提升。完整分析见 [dev_comparison.json](evaluation_reports/phase3/dev_comparison.json)。

## 最终测试差异（只用于最终评估）

v1/v3 的三个 Accuracy 均持平；相对 v1，v3 有 3 条样本正确性改善、2 条退化、15 条不变。类别正确数量没有增加，类别 Macro-F1 的小幅差异来自各类错误分布。测试仅 20 条，不据此宣称显著或普遍提升。

| Prompt | ID | 原文证据 | 观察到的差异 |
| --- | --- | --- | --- |
| v1 | test-004 | 应用启动慢的问题已经解决了，现在打开很顺畅，不再觉得卡顿。 | 类别 bugs → user_experience |
| v1 | test-005 | 希望增加按项目汇总工时的功能，当前只能看到个人工时，月末统计要手工处理。 | 情感 neutral → negative |
| v1 | test-008 | 产品没坏，但售后指引写得太模糊，不知道先准备什么材料，建议把说明改详细。 | 类别 suggestions → customer_service |
| v1 | test-012 | 鞋底用了不到一周就开胶，虽然客服回复及时，最不满意的还是耐用性。 | 优先级 medium → high |
| v1 | test-014 | 客服明确说当天回电，可一周过去仍没人联系，问题一直没人处理。 | 优先级 medium → high |
| v1 | test-016 | 套餐涨价了但提供的服务没有增加，我觉得性价比下降了。 | 优先级 low → medium |
| v1 | test-017 | 运输途中包裹外盒被压坏，但里面的商品完整，主要希望承运过程更小心。 | 情感 negative → neutral |
| v3 | test-002 | 搜索框输入中文后结果排序不对，数据没有丢失，我还能用筛选找到需要的记录。 | 情感 negative → neutral；优先级 medium → low |
| v3 | test-003 | 订单详情里的小图标偶尔缺一角，点击功能正常，希望修一下显示问题。 | 情感 negative → neutral |
| v3 | test-004 | 应用启动慢的问题已经解决了，现在打开很顺畅，不再觉得卡顿。 | 类别 bugs → user_experience |
| v3 | test-008 | 产品没坏，但售后指引写得太模糊，不知道先准备什么材料，建议把说明改详细。 | 类别 suggestions → user_experience |
| v3 | test-012 | 鞋底用了不到一周就开胶，虽然客服回复及时，最不满意的还是耐用性。 | 优先级 medium → high |
| v3 | test-016 | 套餐涨价了但提供的服务没有增加，我觉得性价比下降了。 | 优先级 low → medium |


候选在测试前已冻结；无论测试结果好坏均保留选择，不据测试新增 Prompt 或重测。逐条测试分析见 [final_test_608a97a5ded9f154.json](evaluation_reports/phase3/test/final_test_608a97a5ded9f154.json)。

## dev 每类 support

| 字段 | 标签 | support（每版相同） |
| --- | --- | --- |
| 情感 | positive | 9 |
| 情感 | negative | 19 |
| 情感 | neutral | 12 |
| 类别 | product_quality | 5 |
| 类别 | customer_service | 4 |
| 类别 | pricing | 3 |
| 类别 | delivery | 4 |
| 类别 | user_experience | 5 |
| 类别 | features | 5 |
| 类别 | bugs | 6 |
| 类别 | suggestions | 4 |
| 类别 | other | 4 |
| 优先级 | high | 7 |
| 优先级 | medium | 10 |
| 优先级 | low | 23 |


## test 每类 support

| 字段 | 标签 | support（每版相同） |
| --- | --- | --- |
| 情感 | positive | 5 |
| 情感 | negative | 11 |
| 情感 | neutral | 4 |
| 类别 | product_quality | 2 |
| 类别 | customer_service | 2 |
| 类别 | pricing | 2 |
| 类别 | delivery | 2 |
| 类别 | user_experience | 2 |
| 类别 | features | 2 |
| 类别 | bugs | 5 |
| 类别 | suggestions | 2 |
| 类别 | other | 1 |
| 优先级 | high | 3 |
| 优先级 | medium | 6 |
| 优先级 | low | 11 |


## dev v1 混淆矩阵

### 情感

| 参考 ↓ / 预测 → | positive | negative | neutral | __failure__ |
| --- | --- | --- | --- | --- |
| positive | 9 | 0 | 0 | 0 |
| negative | 1 | 18 | 0 | 0 |
| neutral | 2 | 1 | 9 | 0 |


### 类别

| 参考 ↓ / 预测 → | product_quality | customer_service | pricing | delivery | user_experience | features | bugs | suggestions | other | __failure__ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| product_quality | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_service | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pricing | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| delivery | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| user_experience | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 1 | 0 | 0 |
| features | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 |
| bugs | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| suggestions | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 3 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 |


### 优先级

| 参考 ↓ / 预测 → | high | medium | low | __failure__ |
| --- | --- | --- | --- | --- |
| high | 7 | 0 | 0 | 0 |
| medium | 3 | 7 | 0 | 0 |
| low | 0 | 1 | 22 | 0 |



## dev v2 混淆矩阵

### 情感

| 参考 ↓ / 预测 → | positive | negative | neutral | __failure__ |
| --- | --- | --- | --- | --- |
| positive | 9 | 0 | 0 | 0 |
| negative | 0 | 18 | 1 | 0 |
| neutral | 2 | 0 | 10 | 0 |


### 类别

| 参考 ↓ / 预测 → | product_quality | customer_service | pricing | delivery | user_experience | features | bugs | suggestions | other | __failure__ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| product_quality | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_service | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pricing | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| delivery | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| user_experience | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 |
| features | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 |
| bugs | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| suggestions | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 3 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 |


### 优先级

| 参考 ↓ / 预测 → | high | medium | low | __failure__ |
| --- | --- | --- | --- | --- |
| high | 7 | 0 | 0 | 0 |
| medium | 0 | 10 | 0 | 0 |
| low | 0 | 2 | 21 | 0 |



## dev v3 混淆矩阵

### 情感

| 参考 ↓ / 预测 → | positive | negative | neutral | __failure__ |
| --- | --- | --- | --- | --- |
| positive | 9 | 0 | 0 | 0 |
| negative | 0 | 17 | 2 | 0 |
| neutral | 0 | 0 | 12 | 0 |


### 类别

| 参考 ↓ / 预测 → | product_quality | customer_service | pricing | delivery | user_experience | features | bugs | suggestions | other | __failure__ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| product_quality | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_service | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pricing | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| delivery | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| user_experience | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 |
| features | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 |
| bugs | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| suggestions | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 3 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 |


### 优先级

| 参考 ↓ / 预测 → | high | medium | low | __failure__ |
| --- | --- | --- | --- | --- |
| high | 7 | 0 | 0 | 0 |
| medium | 1 | 9 | 0 | 0 |
| low | 0 | 0 | 23 | 0 |



## test v1 混淆矩阵

### 情感

| 参考 ↓ / 预测 → | positive | negative | neutral | __failure__ |
| --- | --- | --- | --- | --- |
| positive | 5 | 0 | 0 | 0 |
| negative | 0 | 10 | 1 | 0 |
| neutral | 0 | 1 | 3 | 0 |


### 类别

| 参考 ↓ / 预测 → | product_quality | customer_service | pricing | delivery | user_experience | features | bugs | suggestions | other | __failure__ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| product_quality | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_service | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pricing | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| delivery | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| user_experience | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 |
| features | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| bugs | 0 | 0 | 0 | 0 | 1 | 0 | 4 | 0 | 0 | 0 |
| suggestions | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |


### 优先级

| 参考 ↓ / 预测 → | high | medium | low | __failure__ |
| --- | --- | --- | --- | --- |
| high | 3 | 0 | 0 | 0 |
| medium | 2 | 4 | 0 | 0 |
| low | 0 | 1 | 10 | 0 |



## test v3 混淆矩阵

### 情感

| 参考 ↓ / 预测 → | positive | negative | neutral | __failure__ |
| --- | --- | --- | --- | --- |
| positive | 5 | 0 | 0 | 0 |
| negative | 0 | 9 | 2 | 0 |
| neutral | 0 | 0 | 4 | 0 |


### 类别

| 参考 ↓ / 预测 → | product_quality | customer_service | pricing | delivery | user_experience | features | bugs | suggestions | other | __failure__ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| product_quality | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_service | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pricing | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| delivery | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| user_experience | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 |
| features | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| bugs | 0 | 0 | 0 | 0 | 1 | 0 | 4 | 0 | 0 | 0 |
| suggestions | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |


### 优先级

| 参考 ↓ / 预测 → | high | medium | low | __failure__ |
| --- | --- | --- | --- | --- |
| high | 3 | 0 | 0 | 0 |
| medium | 1 | 4 | 1 | 0 |
| low | 0 | 1 | 10 | 0 |



## 完整逐样本标签及版本差异

每格顺序为情感 / 类别 / 优先级。原文、复核来源、结构化行动建议与逐次 usage 同时保留在各报告 JSON/CSV。

### dev 40 条

| ID | 参考：情感 / 类别 / 优先级 | v1 | v2 | v3 |
| --- | --- | --- | --- | --- |
| dev-001 | negative / bugs / high | negative / bugs / high | negative / bugs / high | negative / bugs / high |
| dev-002 | negative / bugs / high | negative / bugs / high | negative / bugs / high | negative / bugs / high |
| dev-003 | negative / bugs / medium | negative / bugs / medium | negative / bugs / medium | negative / bugs / medium |
| dev-004 | negative / bugs / low | negative / bugs / low | negative / bugs / medium | neutral / bugs / low |
| dev-005 | positive / bugs / low | positive / bugs / low | positive / bugs / low | positive / bugs / low |
| dev-006 | negative / bugs / high | negative / bugs / high | negative / bugs / high | negative / bugs / high |
| dev-007 | neutral / features / low | neutral / features / low | neutral / features / low | neutral / features / low |
| dev-008 | neutral / features / medium | negative / features / high | neutral / features / medium | neutral / features / medium |
| dev-009 | positive / features / low | positive / features / low | positive / features / low | positive / features / low |
| dev-010 | negative / features / high | negative / features / high | negative / features / high | negative / features / high |
| dev-011 | neutral / features / low | neutral / features / low | neutral / features / low | neutral / features / low |
| dev-012 | neutral / suggestions / low | positive / suggestions / low | positive / suggestions / low | neutral / suggestions / low |
| dev-013 | negative / suggestions / medium | negative / user_experience / medium | negative / user_experience / medium | negative / user_experience / medium |
| dev-014 | neutral / suggestions / low | neutral / suggestions / low | neutral / suggestions / low | neutral / suggestions / low |
| dev-015 | positive / suggestions / low | positive / suggestions / low | positive / suggestions / low | positive / suggestions / low |
| dev-016 | negative / user_experience / medium | negative / user_experience / medium | negative / user_experience / medium | negative / user_experience / medium |
| dev-017 | positive / user_experience / low | positive / user_experience / low | positive / user_experience / low | positive / user_experience / low |
| dev-018 | negative / user_experience / medium | positive / user_experience / medium | negative / user_experience / medium | negative / user_experience / medium |
| dev-019 | neutral / user_experience / low | neutral / suggestions / low | neutral / user_experience / low | neutral / user_experience / low |
| dev-020 | negative / user_experience / low | negative / user_experience / medium | negative / user_experience / medium | neutral / user_experience / low |
| dev-021 | negative / product_quality / high | negative / product_quality / high | negative / product_quality / high | negative / product_quality / high |
| dev-022 | negative / product_quality / medium | negative / product_quality / high | negative / product_quality / medium | negative / product_quality / high |
| dev-023 | positive / product_quality / low | positive / product_quality / low | positive / product_quality / low | positive / product_quality / low |
| dev-024 | neutral / product_quality / low | positive / product_quality / low | positive / product_quality / low | neutral / product_quality / low |
| dev-025 | negative / product_quality / medium | negative / product_quality / high | negative / product_quality / medium | negative / product_quality / medium |
| dev-026 | negative / customer_service / high | negative / customer_service / high | negative / customer_service / high | negative / customer_service / high |
| dev-027 | negative / customer_service / medium | negative / customer_service / medium | negative / customer_service / medium | negative / customer_service / medium |
| dev-028 | positive / customer_service / low | positive / customer_service / low | positive / customer_service / low | positive / customer_service / low |
| dev-029 | neutral / customer_service / low | neutral / customer_service / low | neutral / customer_service / low | neutral / customer_service / low |
| dev-030 | negative / pricing / medium | negative / pricing / medium | negative / pricing / medium | negative / pricing / medium |
| dev-031 | positive / pricing / low | positive / pricing / low | positive / pricing / low | positive / pricing / low |
| dev-032 | neutral / pricing / low | neutral / pricing / low | neutral / pricing / low | neutral / pricing / low |
| dev-033 | negative / delivery / high | negative / delivery / high | negative / delivery / high | negative / delivery / high |
| dev-034 | negative / delivery / medium | negative / delivery / medium | negative / delivery / medium | negative / delivery / medium |
| dev-035 | positive / delivery / low | positive / delivery / low | positive / delivery / low | positive / delivery / low |
| dev-036 | neutral / delivery / low | neutral / delivery / low | neutral / delivery / low | neutral / delivery / low |
| dev-037 | neutral / other / low | neutral / other / low | neutral / other / low | neutral / other / low |
| dev-038 | negative / other / low | negative / other / low | neutral / other / low | negative / other / low |
| dev-039 | positive / other / low | positive / other / low | positive / other / low | positive / other / low |
| dev-040 | neutral / other / low | neutral / other / low | neutral / other / low | neutral / other / low |


### test 20 条

| ID | 参考：情感 / 类别 / 优先级 | v1 | v3 |
| --- | --- | --- | --- |
| test-001 | negative / bugs / high | negative / bugs / high | negative / bugs / high |
| test-002 | negative / bugs / medium | negative / bugs / medium | neutral / bugs / low |
| test-003 | negative / bugs / low | negative / bugs / low | neutral / bugs / low |
| test-004 | positive / bugs / low | positive / user_experience / low | positive / user_experience / low |
| test-005 | neutral / features / medium | negative / features / medium | neutral / features / medium |
| test-006 | positive / features / low | positive / features / low | positive / features / low |
| test-007 | neutral / suggestions / low | neutral / suggestions / low | neutral / suggestions / low |
| test-008 | negative / suggestions / medium | negative / customer_service / medium | negative / user_experience / medium |
| test-009 | negative / user_experience / medium | negative / user_experience / medium | negative / user_experience / medium |
| test-010 | positive / user_experience / low | positive / user_experience / low | positive / user_experience / low |
| test-011 | negative / product_quality / high | negative / product_quality / high | negative / product_quality / high |
| test-012 | negative / product_quality / medium | negative / product_quality / high | negative / product_quality / high |
| test-013 | positive / customer_service / low | positive / customer_service / low | positive / customer_service / low |
| test-014 | negative / customer_service / medium | negative / customer_service / high | negative / customer_service / medium |
| test-015 | neutral / pricing / low | neutral / pricing / low | neutral / pricing / low |
| test-016 | negative / pricing / low | negative / pricing / medium | negative / pricing / medium |
| test-017 | negative / delivery / low | neutral / delivery / low | negative / delivery / low |
| test-018 | positive / delivery / low | positive / delivery / low | positive / delivery / low |
| test-019 | neutral / other / low | neutral / other / low | neutral / other / low |
| test-020 | negative / bugs / high | negative / bugs / high | negative / bugs / high |


## 中止、复验与适用范围

首次默认受限运行环境无法解析服务地址，连续连接错误后自动停止：v1 已尝试 4 条、36 条未执行；v2 未请求、40 条未执行。两份 incomplete 报告与日志保留，未纳入选型、正式指标或混淆矩阵。正常本地权限 TCP 诊断成功后，完整重新运行 dev，4 次失败仍计预算。旧 hash 的 smoke 与 DEMO 只作历史证据。

最终代码的全量离线回归 194 项通过（98.266 秒，正常本地权限），日志为 validation/phase3_final_all_tests.log。v3 注册后的首次相关回归发现旧测试把 v3 固定当作未知版本；已改为确实未注册的版本，并增加 v3 冻结 hash 校验。该失败日志保留在 validation/phase3_v3_regression_before_fixture_update.log；最终全量回归包含相关测试且全部通过。默认沙箱的 Windows 临时目录 PermissionError 也另留日志。最终 5 份报告均可严格重新加载、重算指标、同 split 比较并导出；Streamlit AppTest 实际控件核对了全部 15 个下载内容，新增模型请求为 0。证据见 validation/delivery_ui_final_checks.json；没有宣称真实浏览器下载落盘。

局限：60 条人工设计合成数据，测试仅 20 条且全部 AI 复核，未采用独立全人工金标、业务抽样或统计显著性检验。标签边界可能仍有分歧，同一模型和网关单次请求有随机性。结果只描述本次设置下的参考标签一致性。

## 本次正式报告索引

- [live_dev_v1_5b943480875d45fa8300ea09e2b78fbf.report.json](evaluation_reports/phase3/dev/live_dev_v1_5b943480875d45fa8300ea09e2b78fbf.report.json)
- [live_dev_v2_311a6b7edfc14d6baa463efc94d3834d.report.json](evaluation_reports/phase3/dev/live_dev_v2_311a6b7edfc14d6baa463efc94d3834d.report.json)
- [live_dev_v3_f31ba93954e34e9c9203d2eca0cbfa62.report.json](evaluation_reports/phase3/dev/live_dev_v3_f31ba93954e34e9c9203d2eca0cbfa62.report.json)
- [live_test_v1_0b51d5b7cdd7424fb1fedc315c22e5dc.report.json](evaluation_reports/phase3/test/live_test_v1_0b51d5b7cdd7424fb1fedc315c22e5dc.report.json)
- [live_test_v3_4bc641f9c7bf431bad80b3899836e381.report.json](evaluation_reports/phase3/test/live_test_v3_4bc641f9c7bf431bad80b3899836e381.report.json)

每个 .report.json 同目录附带 .metrics.json、.predictions.csv、.md 与 journals/。实验清单与执行记录不可覆盖或删除；请勿再次运行最终测试。原作者 Y. Siva Sai Krishna、来源 ysskrishna/ai-customer-feedback-analyzer 与 MIT 许可证保留。
