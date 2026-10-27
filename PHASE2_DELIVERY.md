# 第二阶段开发交付报告

交付日期：2026-10-13（Asia/Shanghai）。已直接修改当前项目；保留原单条分析、CSV 批量分析、原作者 Y. Siva Sai Krishna、来源链接及 MIT 许可证。

## 1. 工程功能及实际验证

已完成独立 `evaluation` 包、60 条合成反馈与标注规则、冻结 v1／新增 v2、指标计算、可追溯运行记录、错误分析、四种报告文件、Streamlit 第三页签及 CLI。仍沿用 Python、Streamlit、Pandas、Pydantic 和原 OpenAI SDK，没有新增数据库或服务框架。

- 原 `analyze_feedback(text)` 保留，默认 v1；单条、CSV 与评测通过 keyword-only `prompt_version` 选择版本。未知版本在请求前拒绝。
- `.env` 的七项原始实际值在重命名时全部保留，没有使用模板覆盖。修正了宿主进程同名 Key 覆盖本地 `.env` 的问题：项目 `.env` 存在时优先使用其配置。新增的接口选择目前为 `API_MODE=chat_completions`、`CHAT_OUTPUT_MODE=json_object`。修改 `.env` 后应重启应用。
- 客户端实际传入自定义地址、模型和超时；Responses 使用 `max_output_tokens`／`reasoning.effort`，Chat 使用所选 token 参数／`reasoning_effort`。SDK 重试关闭，应用有限重试。没有静默切换接口、修补缺字段或补默认标签。
- 数据独立校验并保留 ID，拒绝空文本、非法枚举、重复 ID、冲突标注及正规化重复文本。gold、split、review_status 和 ID 不发送给模型。
- 主指标以全部有效评测样本为分母，调用／输出失败计错误。完整固定标签集合计算 Macro-F1，零分母取 0；逐类 support 和独立失败列均展示。失败没有有效预测。
- 每个版本导出完整 `.report.json`、`.metrics.json`、`.predictions.csv`、`.md`。加载时验证追溯信息、运行模式、样本 hash、复核统计及重新计算的指标；比较严格要求同数据集、划分、模型、请求设置及并发。
- 页面仅点击明确的运行按钮才调用模型，提前显示样本量和最大请求尝试数；默认 DEMO，无 Key 也能读取历史本地报告。DEMO 和 LIVE 不混用。

实际执行结果：

| 验证 | 结果与范围 |
| --- | --- |
| 第一阶段基线测试 | 原 42 项全部通过 |
| 最终完整自动化测试 | **95 项通过，0 失败、0 错误、0 跳过，退出码 0**；全部离线 |
| 配置及实际 SDK 适配 | MockTransport 运行已安装 SDK 的两个接口，核验自定义路径、序列化 token／reasoning 参数及本地校验；不产生外部请求 |
| 数据、指标、报告测试 | 覆盖 ID、gold 隔离、Prompt 透传、手算指标、缺失类别、全失败、报告比较、模式隔离、严格加载及导出 |
| Streamlit AppTest | 原功能及评测按钮、无 Key 查看、下载入口、切换选项和重渲染不重复调用均通过；LIVE 测试的模型边界使用 Mock |
| CLI | 数据校验成功：60 条、dev 40／test 20；实际运行开发集 v1／v2 DEMO，保存并重新加载两份可比较报告 |
| 应用启动 | 实际启动 Streamlit，健康接口 HTTP 200／`ok`；验收服务已停止 |
| 打包与依赖 | wheel 构建成功，含 evaluation 模块、示例 CSV、标注指南及许可证；不含 `.env`；锁文件检查通过；53 个已安装包兼容 |
| 密钥检查 | 对源代码、测试、验证记录及评测产物进行本地实际 Key 字节匹配，未发现泄露；检查只输出状态 |
| 真实浏览器下载文件落盘 | **未验证**；已验证下载入口和生成内容 |
| 独立机器／Python 3.11 环境 | **未验证**；本机实际环境为 Python 3.13.7 |

主要依赖实检：OpenAI 1.109.1、Streamlit 1.50.0、Pandas 2.3.2、Pydantic 2.11.9、python-dotenv 1.1.1。

测试证据见 `validation/test_results.json` 和 `validation/tests.log`；打包／DEMO 证据见 `validation/artifact_checks.json`；启动证据见 `validation/health_check.json`。部分沙箱启动、联网和打包操作受 Windows 权限限制，已在允许的本地执行环境完成对应检查。最终测试使用项目内临时目录，避免早期系统临时目录清理权限告警；AppTest 的 bare-mode 警告不影响测试结果。打包工具对原许可证元数据格式仍有弃用提示，MIT 许可及原版权声明保留。

## 2. 最多三次真实接口验证

严格累计 **3 次请求尝试**，仅使用独立合成反馈，应用与 SDK 重试全部为 0。达到上限后未继续探测，也没有自动运行任何完整 LIVE 数据集。

| 尝试 | 接口与凭据来源 | 实际结果 |
| --- | --- | --- |
| 1 | Responses；初始继承的宿主进程 Key | `APIConnectionError`，沙箱连接受限，未得到有效预测 |
| 2 | Responses；继承的宿主进程 Key，允许联网 | HTTP 401 `AuthenticationError`；后来发现此 Key 与项目 `.env` 不同，因此不能归因于用户填写的凭据 |
| 3 | Chat Completions、`json_object`；修正配置优先级后使用项目 `.env` Key | **成功**：收到完整 JSON，`sentiment`、`actionable_items`、`category`、`priority`、`summary` 经现有 `CustomerFeedbackAnalysis` 校验通过 |

第三次请求传入原 `.env` 的模型、地址、超时、输出 token 上限及 reasoning 配置。确认的是该设置下接口受理请求且一次输出完整；没有证明网关内部一定执行所有参数语义，也没有证明它只支持 Chat。当前已显式选择这条验证成功的路径，原 Responses 路径继续保留。

**Responses 的实际支持性：未验证。严格 JSON Schema 的实际支持性：未验证。** `json_object` 不等同于服务端强制完整 Schema，所以所有返回仍必须本地 Pydantic 校验；截断、拒绝、非法枚举、缺字段或非 JSON 均失败。记录保存于 `validation/interface_verification.json`，没有保存密钥或服务端原始错误体。

## 3. 标注与模型效果评测状态

示例集是独立编写的 **60 条合成反馈**，固定 40 条开发集、20 条测试集。**60 条全部 `draft`，已人工复核 0 条。** 不是实际业务数据，也不是已人工标注的数据。

v2 按类别边界、混合情感、主问题及优先级规则编写，没有 few-shot，也没有把测试集或近似改写放进 Prompt。标注指南明确开发集可用于 Prompt 调整，测试集只用于冻结版本后的评测；正规化去重不能自动识别语义近似改写，仍需人工检查。

已完成工程流程的确定性 DEMO 评测和两个版本比较，**未完成真实模型效果评测**。单条真实接口验证不构成 Accuracy／F1 评测。DEMO 的标签和耗时由哈希生成，故意包含错误、调用失败及结构失败，只验证流程；其数值不能作为模型效果、Prompt 提升或真实性能结论。即使之后运行 LIVE，在这些待复核标签上的结果也只是临时结果。

## 修改文件

| 范围 | 文件 |
| --- | --- |
| 配置及接口 | `core/config.py`、`ai/client.py`、`ai/analyzer.py`、`.env`（保留实际值）、`.env.example` |
| Prompt 与批量 | `ai/prompts.py`、`ai/batch.py` |
| 评测数据 | `evaluation/data.py`、`evaluation/data/feedback_eval.csv`、`evaluation/ANNOTATION_GUIDE.md` |
| 评测引擎 | `evaluation/metrics.py`、`evaluation/runner.py`、`evaluation/reports.py`、`evaluation/cli.py`、`evaluation/__init__.py`、`evaluation/__main__.py` |
| 页面 | `main.py`、`evaluation/ui.py` |
| 测试 | `tests/test_config_and_transport.py`、`tests/test_evaluation_data.py`、`tests/test_evaluation_metrics.py`、`tests/test_evaluation_runner.py`、`tests/test_evaluation_reports.py`、`tests/test_evaluation_ui.py`；同步更新 `test_analyzer.py`、`test_batch.py`、`test_streamlit.py` 的配置隔离／第三页签期待 |
| 打包及文档 | `pyproject.toml`、`uv.lock`、`.gitignore`、`README.md`、本报告 |
| 本地验证记录 | `validation/`；运行结果位于 `evaluation_reports/`，该结果目录已忽略版本管理 |

`LICENSE`、原作者元数据、项目来源及页面作者信息均保留。

## 运行方法（Windows PowerShell）

在项目根目录执行；以下三个命令均不调用真实模型：

```powershell
.\.venv\Scripts\python.exe -m evaluation validate
.\.venv\Scripts\python.exe -m evaluation compare --split dev --workers 2
.\.venv\Scripts\python.exe -m streamlit run main.py
```

完整自动化测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

单版本离线演示可使用 `python -m evaluation demo --split test --prompt-version v2`。真实评测需要用户明确执行带 `--live` 的命令，或点击页面 LIVE 按钮；本轮未自动执行下面的命令：

```powershell
.\.venv\Scripts\python.exe -m evaluation run --split test --prompt-version v1 --workers 2 --live
.\.venv\Scripts\python.exe -m evaluation compare --split test --workers 2 --live
```

在默认两次应用重试设置下，测试集单版本最多 60 次尝试，双版本最多 120 次；具体按配置计算。CLI 和页面显示规模，默认运行仍为 DEMO。安装、配置模板及读取历史报告的详细命令见 README。已有 `.env` 不应再次复制模板。

## 已知限制及简历描述

一次接口成功不保证后续可用性或模型质量；真实 Responses／strict schema 未验证。重试只应对暂时连接及部分 HTTP 错误，不修补不完整结构。平均与 P95 覆盖成功及失败，单条计时含调用、应用重试、退避和校验，不含线程排队及共享客户端初始化；Mock 耗时不代表真实性能。报告为本地 JSON／CSV／Markdown，没有数据库、签名或业务级人工复核系统；对比按记录的配置和 hash 检查一致性，不能替代独立审计。

可据实使用的简历描述：

> 基于开源 AI Customer Feedback Analyzer 二次开发 CustomerInsight，使用 Python、Streamlit、Pandas、Pydantic 与 OpenAI SDK 实现单条／CSV 批量反馈分析及 Responses／Chat Completions 接口适配；构建固定开发／测试划分的合成评测集、双 Prompt 版本追溯、失败计入分母的 Accuracy／Macro-F1、混淆矩阵、错误明细和本地报告看板，通过 95 项离线自动化测试及一次合成反馈真实接口校验。评测标签待人工复核，尚未完成真实模型效果评测。

不应增加未经测量的准确率、F1 提升、耗时优化、业务收益或“真实业务人工标注”等表述。
