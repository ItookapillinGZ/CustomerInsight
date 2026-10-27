# 第一阶段：独立 HTTP API

## 交付范围

FastAPI 接口复用当前 `core/` 分析、报告、历史与 Agent 服务。现有 Streamlit 页面继续直接调用业务层；本阶段未将页面切换为 HTTP 客户端，也未实现 Celery、Redis、账户或后台持久任务。

已有结果通过同一 `APP_DATA_DIR` 的历史接口读取，不需要再次调用模型。上传预览、历史列表、报告详情、逐行结果和下载均只进行本地操作。新的分析和 Agent 追问需要明确允许模型调用并给出本次请求预算。

## 安装与启动

在项目根目录安装更新后的依赖；保留现有 `.env`：

```powershell
uv sync --frozen
```

在 `.env` 中配置一个新的随机长串 `APP_API_TOKEN`，或执行下列工具生成。工具保留已有非空 Token 和其余配置，不显示令牌值，修改前的配置保存在忽略提交的 `.runtime/` 中。也可在启动前显式设置该进程的 `APP_API_TOKEN` 环境变量；API 入口优先保留启动进程传入的非空值。不要使用模型 API Key；无需改动已有模型凭据和模型名称。未配置该项时接口兼容已有 `APP_ACCESS_PASSWORD`。两者都没有配置时，业务接口关闭并返回 503；`APP_PUBLIC_MODE=false` 也不会绕过 API 鉴权。

```powershell
.\.venv\Scripts\python.exe tools/prepare_api_access.py
```

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

打开 `http://127.0.0.1:8000/docs` 查看实际 OpenAPI 契约。`GET /healthz`、`/docs` 和 `/openapi.json` 只公开健康或协议元信息，不提供表格内容。业务请求使用：

```text
Authorization: Bearer <APP_API_TOKEN 或兼容访问码>
```

使用文档页的 Authorize 输入令牌后才能调用业务接口。不要将令牌写入 URL，也不要在共享截图中展示。模型配置加载仍以项目 `.env` 为准，可能覆盖同名终端模型环境变量；API Token 的进程优先规则只针对 API 访问凭证，不改变模型 Key 的既有优先级。修改配置后重启 API 进程。

此版本只支持单个 API Worker。业务层已有的并发槽位在进程内生效；启动多个 Uvicorn Worker 不能形成共享全局并发限制。部署 API 与旧页面也不能据此声称已实现跨进程限流。

### 在现有 Docker 部署旁启动

`compose.api.yml` 是可选叠加文件，不会在默认启动页面时自动开启 API。先在部署使用的 `.env` 配置 `APP_API_TOKEN`；这一部署入口明确要求该配置，不使用访问码回退。然后在原项目目录、沿用原 Compose 项目名执行：

```powershell
docker compose -f compose.yml -f compose.api.yml up -d --build --no-deps api
```

该命令只创建或更新 API 服务，保持现有页面容器运行。API 使用原 `usage_data` 卷，历史目录为 `/data/customerinsight`，额度台账为 `/data/product_usage.sqlite3`；对外默认只绑定 `127.0.0.1:8000`，可通过 `APP_API_PORT` 调整本机端口。已有实例使用了自定义 Compose 项目名时应继续使用同一名称，避免创建新卷后看到空历史。不要删除原数据卷。

健康检查地址为 `http://127.0.0.1:8000/healthz`，文档为 `http://127.0.0.1:8000/docs`。API 自身保留安全请求编号和路由日志，启动时关闭 Uvicorn 默认访问日志，避免原始查询参数进入该日志。

## HTTP 入口

以下业务路径统一以 `/api/v1` 开头。请求与响应字段的精确定义以当前 `/openapi.json` 为准。

| 方法与路径 | 用途 | 是否可能调用模型 |
| --- | --- | --- |
| `GET /healthz` | 进程健康检查；无业务数据 | 否 |
| `POST /api/v1/uploads` | multipart 上传 `.xlsx` 或 `.csv`，取得临时上传 ID | 否 |
| `GET /api/v1/uploads/{upload_id}/preview` | 读取工作表、字段与样本；可指定工作表或 CSV 编码 | 否 |
| `POST /api/v1/analyses` | 按上传 ID 和选列配置恢复缓存或同步分析，并归档结果 | 仅明确授权新分析时 |
| `GET /api/v1/batches` | 分页历史列表 | 否 |
| `GET /api/v1/batches/{batch_id}` | 已归档报告 | 否 |
| `GET /api/v1/batches/{batch_id}/rows` | 分页逐行结果 | 否 |
| `GET /api/v1/batches/{batch_id}/exports/csv` | 下载分析 CSV | 否 |
| `GET /api/v1/batches/{batch_id}/exports/html` | 下载 HTML 报告 | 否 |
| `GET /api/v1/batches/{batch_id}/exports/original` | 下载归档原件 | 否 |
| `GET /api/v1/batches/{batch_id}/chat` | 读取该报告保存的对话 | 否 |
| `POST /api/v1/batches/{batch_id}/chat` | 结合当前报告和已保存对话继续追问 | 是，需明确允许 |

接口不允许指定任意服务器文件路径；上传与原件通过程序生成的标识定位。本阶段不提供删除接口，已有页面的删除操作保持原路径。

## 上传与同步分析

1. 调用上传接口，表单字段名为 `file`，保存返回的上传 ID。
2. 调用预览接口，确认工作表和实际字段名称。Excel 通过 `sheet_name` 选表，CSV 可指定 `encoding`。
3. 将上传 ID、工作表和选列提交至分析接口。上传暂存独立于已归档的原件，位于 `APP_DATA_DIR/api_uploads`，约一小时过期；过期或暂存目录被清理后重新上传文件。暂存最多 100 个文件、总计 100 MB，每个文件最多 5 MB；暂存文件能读取不代表已有后台任务。

上传响应包含 `upload_id`、`filename`、`size_bytes`、`sheet_names`、`expires_at`。预览响应包含 `columns`、`total_rows`、最多十行 `rows` 和 `suggested_mapping`；使用这些字段确认选列，不需要发送模型请求。

新分析的请求示例：

```json
{
  "upload_id": "上传接口返回的 ID",
  "sheet_name": "评价1",
  "encoding": "utf-8-sig",
  "text_column": "评价内容",
  "group_column": null,
  "date_column": null,
  "allow_model_call": true,
  "max_requests": 10,
  "force": false
}
```

这里的 `max_requests: 10` 只是示例的上限，不是保证只花十次或足够处理任意表格。实际分组受评价长度、每组条数和当前配置影响，并继续受全局每日额度控制；每次真正的模型尝试都计入请求预算。读取、下载和查询状态不产生模型调用。

`allow_model_call` 默认 `false`。完全相同文件字节及字段映射若已有历史，且 `force=false`，先恢复已有结果；这不需要付费授权。没有可恢复结果时，必须传入 `allow_model_call=true` 和正数 `max_requests` 才能开始调用。`force=true` 表示明确重新分析，仍需要授权和预算。

分析响应的主要字段为 `batch_id`、`cached`、`status` 和 `metadata`。`cached=true` 表示恢复历史；`status` 区分 `completed` 和 `partial`，不能只凭 HTTP 成功就认为每行都有分析结果。若分析中断，尽量将已经收到且通过校验的结果和未完成行一同归档，并带 `error_code=analysis_interrupted`；这是本次请求内的部分保存，不是进程崩溃后的分组恢复。

工作表及列名应使用实际预览值，不要将示例名称当成固定规则。沿用现有单表行数、文件大小与文本长度限制；本阶段没有放开为无限量。当前用户只对 500 条文件中选定的一张 100 条工作表执行过分析，其余四张尚未分析；读取和下载现有报告不重新分析，也不会自动处理剩余 400 条。成功覆盖数量以现有报告为准。

同步接口在分析完成后返回，不能把一次保持连接的请求理解为后台任务。客户端断开后结果状态可能不确定；进程重启也不承诺续跑。若未拿到返回，先查历史确认是否已经归档，再决定是否明确重试。下一阶段才引入任务 ID、持久进度和分组恢复。

历史列表和逐行结果支持 `limit`、`offset`，默认每页 20 条、最多 100 条，`offset` 从 0 开始。响应均包含 `items`、`total`、`limit`、`offset`；逐行接口另提供 `columns`。报告详情响应包含 `metadata` 和 `report`，完整逐行明细通过 `/rows` 分页读取，避免把原件内部存储格式暴露为公共契约。

## 多轮追问

对已有 `batch_id` 提交问题，服务端读取这个报告的已保存对话，复用当前报告的可验证统计及只读原文工具。请求示例：

```json
{
  "question": "这些评价中最值得优先处理的具体问题是什么？请引用原文。",
  "allow_model_call": true,
  "max_requests": 3,
  "comparison_id": null
}
```

后续对同一批次再次提问，即使用同一份已保存历史继续对话。若需要跨批比较，只能通过 `comparison_id` 指定一份已有历史报告；不能提交任意磁盘路径、SQL 或联网目标。模型回复经过结构和引用检查，但这些检查不证明建议的全部语义正确。

读取对话时也可通过查询参数 `comparison_id` 选择相应比较范围。返回 `items`、`total` 和 `comparison_id`；新一轮回复返回 `result` 与 `turn_count`。当前范围和跨批范围的对话不会直接混作同一轮查询依据。

每轮仍受既有模型请求和本地工具调用上限控制；服务端不会因为客户端给出更大的 `max_requests` 就无限调用。共享工作区中的 Token 持有者可以访问同一批次的对话，这不是私人账户对话。并发聊天的冲突处理以实际接口返回为准，失败时先重新读取历史再决定重试，避免重复计费。

## 数据兼容与边界

- `APP_DATA_DIR` 指向已有历史数据库和 `originals/` 目录。模型调用额度数据库由 `APP_USAGE_DB_PATH` 单独配置。
- 本机 API 与 Docker 页面默认工作目录不同。读取 Docker 内已分析的历史，应使用实际持久卷挂载；本机新建的空目录不等于 Docker 旧历史。启动前核对配置，保留旧数据卷。
- 原 SQLite 历史结构沿用，不做破坏性迁移，不覆盖原件，不主动重新分析历史。不能仅凭目录存在就声称所有旧会话已迁移。
- 此时是部署实例内可信共享工作区。API Token 是工作区访问凭证，没有账户隔离、用户配额、独立所有者或租户权限；不要把此版本描述成完整多用户系统。
- 响应错误不应回显模型凭据、本机路径或上传原文；可用请求 ID 关联排查记录。实际 HTTP 状态及错误结构以测试和当前 OpenAPI 契约为准。

错误响应格式为 `{"error":{"code":"错误标识","message":"安全提示"},"request_id":"请求编号"}`，响应头同时提供 `X-Request-ID`。鉴权失败使用 401 并返回 `WWW-Authenticate: Bearer`；缺少服务端配置则使用 503，不能依靠省略令牌获得访问。

常见状态码包括：401（凭证无效）、503（尚未配置工作区凭证或存储不可用）、404（记录不存在）、410（上传过期）、413（文件过大）、422（字段或预算无效）、428（缺少模型调用授权或请求预算）、429（暂存容量或每日额度不足）、409（当前 API 已有分析或聊天操作，或记录校验冲突）。错误消息是当前操作结果，不是允许客户端自动重发付费请求的信号。

## 验证方式

新增接口测试应使用临时历史目录、临时额度台账与 Mock 模型，不依赖私人 Excel 或真实 API。重点验证鉴权、文件与字段边界、无授权不调用模型、缓存复用、预算控制、历史与下载、多轮对话及缺失记录。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_backend_api -v
```

本阶段已完成 31 项 HTTP 测试和包含这些测试在内的 464 项完整离线回归，全部通过。实际 Docker 接口也已读取原有报告、验证下载与缓存复用，数据哈希保持一致。证据与本阶段限制见 [实际交付结果](API_PHASE1_DELIVERY.md)。本阶段真实模型请求为 0，已分析的一张工作表没有重复调用，其余四张也未因接口验收产生调用。
