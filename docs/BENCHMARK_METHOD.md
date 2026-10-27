# 可复现的后端调度基准

此基准衡量应用的 HTTP 接口、账户认证、上传校验、PostgreSQL 持久化、Redis 分发和 Celery 调度。模型 SDK 被工具专属的离线适配器替换，**结果不能解释为真实大模型吞吐、业务准确率、线上容量或生产 SLA**。

## 运行

先在项目根目录构建当前代码，再运行：

```powershell
docker build -t customerinsight-backend:phase4 .
.\.venv\Scripts\python.exe tools/benchmark_backend.py --image customerinsight-backend:phase4
```

Linux / CI 使用 `uv run --frozen python tools/benchmark_backend.py --image customerinsight-backend:ci`。宿主脚本仅使用标准库，需要 Docker Compose。默认 5 个独立账户同时提交，每个账户 100 条不同的合成评价，共 500 行，10 行一组，共 50 次模拟 SDK 请求；每次请求固定等待 100 ms。只有 1 个 Uvicorn 进程和 1 个 Celery prefork 子进程。它没有借助多个 Worker 提高测量成绩。

可通过 `--accounts`、`--rows-per-account`、`--mock-delay-ms` 改变参数；`--timeout` 默认 300 秒。每组新的参数都必须重新运行并单独记录，不能把不同参数下的结果混为同一实验。工具不会设定与硬件无关的强制延迟或吞吐门槛，但状态、行数、请求数、报告持久化不一致会返回非零退出码。

## 隔离及模型替身

每次运行创建 `customerinsight-benchmark-<随机 ID>` Compose 项目、全新的 PostgreSQL 17 / Redis 7 / 工作区 Linux 卷，以及仅内部可达的 Docker 网络。没有公开端口，不加载项目 `.env`，不挂载真实数据库或顾客文件；唯一宿主绑定目录是只读 `tools/`，另有只读的合成实验标记。清理只针对该次随机项目自己的卷。

模型地址固定为 `http://blocked.invalid/v1`，Key 是固定的无效示例。工具进程启动前要求检查专属挂载、随机实验标记、目录、数据库地址和无效 Key。SDK 适配器再次检查每条内容都符合本次合成数据格式，且持有真实的 Redis 并发许可。生产程序没有 Mock 配置开关，也不会导入这个工具。

模型替换发生在测试工具中的 Worker SDK 工厂。分析分组、Pydantic 校验、额度记账、分组落库、原文件归档和报告生成继续使用生产代码。提交、登录、上传、轮询和读取报告都经过真实 Uvicorn TCP HTTP 接口，没有用 FastAPI TestClient 代替网络。

## 指标解释

| 指标 | 起止点与注意事项 |
| --- | --- |
| `submit_http` p50 / p95 | 客户端发送 `POST /jobs` 至完整接收 HTTP 响应；包含校验、持久化和分发，不含上传。默认只有 5 个样本，p95 是描述性统计。 |
| `poll_and_rows_http` p50 / p95 | 认证后的任务轮询及首次读取最终结果行的 HTTP 延迟；两种读接口合并统计，不能宣称代表所有查询。 |
| `job_end_to_end` p50 / p95 | 提交前计时，到轮询观察到完成且报告 ID 已存在；包含排队、模拟模型等待和最终持久化，也包含轮询观测延迟。 |
| `rows_per_second` | 总行数 / 整个负载时长；时长包含登录、上传与提交准备，属于保守的整流程数字。 |
| `max_queued_observed` | 每轮约 100 ms 的认证任务快照中，排队任务数量的最大观察值；不是 Redis 瞬时队列峰值，短暂状态可能未被看到。 |
| `max_running_observed` | 同一快照中的运行任务数。单 Worker 条件下仍需以实际结果核对。 |
| `http_error_rate` | 已测 HTTP 请求中异常或非预期状态数量 / 请求总数；不包括预热健康检查。发生失败时工具退出，日志用于定位，不以失败样本伪造成功结果。 |
| `mock_requests` / `live_llm_calls` | 实际经过 SDK 适配器的次数 / 真实模型请求数，默认应为 50 / 0。 |

分位数采用 `(n − 1) × q` 的线性插值（常见 type 7 方法），单位为毫秒。计时使用同一个负载进程的单调时钟，不比较不同容器的系统时间。默认没有正式预热负载，只有服务就绪检查，因此缓存和首次解析成本也会进入结果；一次基准不能作为稳定性或统计显著性证据。

## 环境和证据

计时负载以外另执行就绪检查：仅 API / PG / Redis 启动时 `/readyz` 返回 503，启动真实 Celery 和 Outbox dispatcher 后为 200；完成负载后强制终止**本次隔离项目**的 dispatcher，等待超过默认心跳 TTL 的 26 秒，检查为 503，再启动 dispatcher 检查恢复 200。这些阶段单列在 `readiness_checks`，不计入吞吐或端到端时长，并核对模拟 SDK 请求计数没有增加。这里没有对用户正在使用的服务做停机测试。

`summary.json` 保存应用、Python、PG 和 Redis 版本、容器内操作系统与 CPU 数、cgroup CPU / 内存限制，以及 Docker 实际资源限制。应用和负载容器各限制 2 CPU / 768 MiB；PG 和 Redis 各限制 1 CPU / 384 MiB。这里的 CPU 限额是每容器上限，宿主仍可能有其他进程竞争，并非整套服务独占 CPU。Docker Desktop 的 Linux 虚拟机规格也不等于 Windows 宿主规格。

完整输出位于 `.runtime/phase4/benchmark/<随机 ID>/`：聚合 `summary.json`、工具专属 `docker-benchmark.log`、`workers.log`。该目录默认忽略，不纳入发布镜像。Compose 配置含本次临时数据库口令，保留在本机即可；公开或 CI 归档只选择聚合结果和合成环境日志，不上传 Compose 配置或标记以外的用户文件。

## CI

`.github/workflows/ci.yml` 配置 Ubuntu / Python 3.12 / 固定 uv 版本、冻结依赖安装、编译与锁文件导出一致性、完整离线测试、真实 PG 独立 schema 套件、Redis 就绪、Docker 构建、双账户真实队列隔离、完整备份与独立恢复演练，以及此基准。备份演练使用 `tools/verify_backup_restore.py --image customerinsight-backend:ci` 创建独立合成数据源和另一套恢复环境，不加载生产 Compose 或 `.env`。临时测试服务器无需 Github Secrets 或真实模型 Key，所有输入为合成示例。

写好配置、本机执行通过与 GitHub 托管 Runner 执行通过是不同的证据。没有远程 Actions 记录时，只能声明 CI 工作流已配置并在本机核对，不能声称远程已通过。
