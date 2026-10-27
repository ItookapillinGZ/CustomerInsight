# Python 后端升级：阶段二交付

日期：2026-10-22。已在原项目完成持久异步任务、网页任务入口和真实 Redis/Celery 验收，原作者、来源及 MIT 许可证保留。下一阶段的 PostgreSQL、ORM、迁移与账户隔离尚未实施。

## 已交付的行为

上传 Excel/CSV、选择工作表及字段、明确分析后，网页通过 FastAPI 创建后台任务。用户不必保持页面连接，可重新打开任务查看进度、取消后续分组、查看已保存结果；安全可恢复的未发送部分需再次确认才能继续。队列模式允许单张工作表最多 1,000 行、5 MB，不会自动遍历其他工作表。页面仍保留“结果、Agent、明细与下载”的主要流程。

任务及原件快照、分组、初始行、请求台账、结果和 Outbox 保存于独立 SQLite 数据库。Redis 只投递任务编号并提供跨进程请求令牌，Celery Worker 分组调用既有分析逻辑并逐组保存。独立投递器处理消息未发送或丢失的恢复；重复通知先查业务状态。API、Worker 和原 Agent 共用每日请求额度，队列模式模型请求共用 Redis 并发控制。

发送前过期的租约可安全继续；发送后响应未知的请求进入 `needs_review`，保留费用占用，不自动重发。已知响应但结构失败也不会自动多调模型。旧任务迟到的报告导出有事务保护，不能覆盖同原件、同映射的新一代报告。当前是一个可信工作区、单 Worker、单执行子进程的 SQLite 部署。

## 修改范围

| 文件 | 作用 |
| --- | --- |
| `backend/job_store.py` | 持久任务、原件 BLOB、分组结果、请求台账、事务 Outbox、租约 fencing、恢复、报告代际保护、历史删除引用保护 |
| `backend/job_worker.py` | 每组一次发送、usage 检查点、心跳、取消与额度暂停、未知请求隔离、复用旧报告服务 |
| `backend/celery_app.py`、`backend/dispatch.py` | JSON 消息、迟确认、无自动模型重试、独立投递循环、Redis 所有权令牌 |
| `backend/job_service.py`、`backend/app.py`、`backend/schemas.py` | 异步 HTTP 契约、幂等提交、旧报告缓存复用、公开响应投影 |
| `backend/client.py`、`app.py` | 网页服务端 API 客户端、3 秒进度刷新、历史任务重开、取消及确认恢复、CSV 编码透传 |
| `core/csv_io.py`、`core/excel_io.py`、`ai/grouped_batch.py` | 显式且有界的队列行数上限；原直接模式默认 100 行不变 |
| `core/product_service.py` | 队列部署中的原有分析/Agent 也使用共享 Redis 请求槽位 |
| `compose.jobs.yml`、依赖及配置模板 | UI/API/Worker/投递器/Redis 部署，共用原业务数据卷，Redis 不映射宿主端口 |
| `tests/test_job_*.py` | 仓库、Worker、HTTP、网页及请求控制的离线测试 |
| `tools/verify_job_queue.py`、`tools/job_acceptance_worker.py` | 隔离容器、专用命名卷、内部网络、模拟 SDK 的真实队列与进程故障验收 |

审查中发现并修复：GB18030 CSV 上传未透传编码；删除旧历史可能留下任务原件与失效引用；Redis 槽位若在本地等待前取得可能过早到期。关联任务报告目前禁止单独删除，真正删除前持事务锁再核对。备份按钮改名并说明只覆盖历史原件、报告及对话，未声称包含所有后台数据库。

## 验证证据

完整离线回归 **535 项通过，340.986 秒，退出码 0**。审查修复后再串行复验：接口/任务仓库/Worker **60 项通过，99.523 秒**；新网页与原历史页面 **28 项通过，112.103 秒**。这些批次有重叠，不相加冒充独立测试数。最终新增任务模块共 78 个测试方法。

实际 Docker 隔离验收使用真正 Redis 和两个独立 Celery Worker 进程，SDK 响应为模拟数据。数据及用量位于独立 Linux 命名卷，不读取实际 `.env`，不挂载用户原表；网络设为内部网络，测试密钥和地址均为无效固定值。生产没有 Mock 环境开关。两个测试 Worker 用来验证竞争和租约，不表示已经完成多 Worker 生产扩容验收。

| 验收场景 | 实际结果 |
| --- | --- |
| 500 条固定模拟评价 | 50 个合并请求，500 条完成，保存模拟 usage 7,000 tokens |
| 再次投递同一任务两次 | 新增模型模拟请求 0 次，已有结果不重写 |
| 第一组执行时取消 | 保留已完成 10 条，不领取后续组；明确恢复后累计 3 次请求完成 30 条 |
| 两个测试 Worker 竞争 | 实际最大同时请求为 2；Redis 容量、令牌所有权和 TTL 检查通过 |
| 真实 SIGKILL 并重启：已领取尚未发送 | 等真实租约过期后继续，50 条共 5 次请求完成 |
| 真实 SIGKILL 并重启：已标记发送 | `needs_review`，保留 1 次占用，自动重试 0，恢复请求被拒绝 |
| 实际 LLM 调用 | **0 次** |

最终聚合证据见 [queue_acceptance.json](../validation/backend_phase2/queue_acceptance.json)，实际运行和失败迭代日志保留在本机 `.runtime/phase2/acceptance/`。早期工具分别因未传队列上传用途、Windows 挂载 SQLite WAL 兼容性和并发时序假设失败；最终改为正确用途、Linux 命名卷和显式屏障后完整验收通过。隔离项目的容器、网络及两个数据卷均已清理，主服务未参与模拟分析。

最终代码编译通过，Python wheel 构建通过并包含全部后台任务模块；wheel 不含 `.env` 或数据库。Compose 配置校验通过，依赖锁与 `requirements.txt` 已同步。已逐项比较部署镜像与最终工作区的 17 个业务源码和依赖文件摘要，全部相同。浏览器实际打开队列版首页，可见 1,000 行限制和后台任务入口，没有提交付费分析。

## 部署与旧数据保留

当前 <http://localhost:8501> 已切换到队列模式，API 为 <http://localhost:8000/docs>。原数据卷保留；API/UI/Redis 健康检查通过，Worker 日志确认 ready，独立投递器已运行。现有 HTTP 探针不是完整队列就绪或模型可用性检查。

升级前导出 74,622 字节的历史备份，保存于本机 `.runtime/phase2/history_before.zip`。升级后通过实际 HTTP 接口读取两份报告、逐行明细、两份对话、四个 CSV/HTML 导出和两份原件；通过新任务入口在 `allow_model_call=false` 下复用两份已有报告，均命中缓存、不创建任务。

现有 **2 份报告、105 条记录、98 条有效分析**的原件、字段映射、报告与对话摘要逐项相同，每日额度台账摘要也与升级前一致。本轮未重跑已分析的那张 100 条工作表，原文件其余 400 条仍未分析。HTTP 聚合证据见 [existing_history.json](../validation/backend_phase2/existing_history.json)。

## 使用及下一步

已部署用户直接刷新 <http://localhost:8501> 使用。新环境启动及 HTTP 契约见 [后台任务说明](API_PHASE2.md)。不要覆盖已填好的 `.env`，不要删除原数据卷。

可用于简历的描述：

> 基于 MIT 开源项目二次开发顾客评价分析平台，以 FastAPI、Celery、Redis 和 SQLite 实现异步分组分析、事务 Outbox、持久检查点、租约 fencing、幂等提交及调用预算；以 500 条模拟输入和两次真实 Worker 强制重启验证恢复与重复消费，隔离处理模型响应未知时的重复计费风险。

下一步应实现 PostgreSQL、SQLAlchemy、Alembic 和账户/工作区授权，包含旧 SQLite 数据迁移核对、跨用户下载及 Agent 查询的权限测试。完整工作区备份恢复、任务保留/清理策略、健康就绪指标与 CI 也仍需补齐。当前实现不宣称多租户隔离、上游绝对仅收费一次、真实业务模型准确率或生产吞吐。
