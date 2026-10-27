# 顾客评价分析：自己部署与使用

需要账户隔离、后台任务和历史持久化时，当前推荐使用 [阶段四部署说明](docs/API_PHASE4.md) 的 `compose.production.yml`，同时包含完整备份恢复与运行检查。以下单容器方式保留为本地简易模式。

部署者配置一次自己的模型服务，使用者之后通过网页上传 Excel/CSV 即可。新用户入口是 `app.py`；原 `main.py` 和历史实验保留用于开发复核。

## Windows 本机使用

1. 安装 Python 3.12 或 3.13，解压完整自部署包。
2. 复制 `.env.example` 为 `.env`，填写自己的 `OPENAI_API_KEY`、`OPENAI_MODEL`；使用兼容服务时还要填写 `OPENAI_BASE_URL`（通常包含 `/v1`）。不要把示例占位值当成实际配置。
3. 双击 `start-local.cmd`，或运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`。首次安装依赖，之后启动网页。该参数仅用于本次脚本进程，不修改全局执行策略。
4. 打开 http://localhost:8501 。也可在已安装环境中直接运行：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501 --server.address 127.0.0.1
```

兼容服务模板使用 Chat Completions / JSON object；模型必须支持该接口和 JSON 输出。应用仍会严格校验字段，输出不完整的记录单独显示，不补默认标签。

## Docker 部署

先安装并启动 Docker Engine / Docker Desktop。在项目根目录填写 `.env` 后执行：

```sh
docker compose up -d --build
```

访问 http://localhost:8501 。`docker compose logs -f customerinsight` 查看启动状态，`docker compose down` 停止；不要附加删除数据卷的参数，否则历史原表、报告、对话及每日使用量记录会丢失。升级源码后再次 `docker compose up -d --build`。1.7 工作台的数据范围和备份恢复见 [WORKSPACE_DELIVERY.md](WORKSPACE_DELIVERY.md)。

默认只绑定本机。局域网试用时，在 `.env` 设置 `APP_BIND_HOST=0.0.0.0`、`APP_PUBLIC_MODE=true` 和自己的长访问码 `APP_ACCESS_PASSWORD`，再重建服务；使用者访问服务器的局域网地址。访问码为空时，远程模式停止服务。访问码是小范围共享门禁，不是完整用户账号系统。

## 自己的域名与 HTTPS

适用于已经拥有服务器和域名的部署者：将域名 DNS 指向服务器，放行 TCP 80/443，在 `.env` 设置真实 `APP_DOMAIN` 和长访问码，保持应用端口仅绑定 localhost，然后执行：

```sh
docker compose -f compose.yml -f compose.public.yml up -d --build
```

Caddy 自动申请证书并代理 WebSocket，使用者访问 `https://你的域名`。需要服务器可以正常连接模型服务和证书服务；本次没有代替用户注册域名、租赁服务器或公开发布服务。

## 上传与报告

- 支持 `.xlsx` 和 `.csv`；旧 `.xls` 请用 Excel/WPS 另存为 `.xlsx`。
- 首行是唯一、非空列名，每行一条评价；每次最多100条、5 MB。选择工作表和评价列，可选商品/门店列分组。
- 基础分析只发送选中的评价文本；Agent 还可能发送当前报告、对话和查询所需的原表字段。选择历史对比后，也可能发送该批的统计和原文。上传前去除无关个人信息。
- 点击分析前显示有效待分析条数和预计合并请求次数。默认每组最多10条、合计12,000字符；100条短评通常为10次请求，长评会分成更多组。默认每日50次是请求额度，不是50条评价；服务内最多2个并发、零重试，失败的已发送请求也消耗一次尝试。
- 部署者可配置 `APP_LLM_BATCH_SIZE`（1至10，默认10）和 `APP_LLM_BATCH_MAX_CHARACTERS`（8,000至12,000，默认12,000），保存 `.env` 后重启。字符目标只计算评价正文，不等于token或价格估算。新模板输出上限为4,096 token；保留旧配置时，如分组输出被截断，可调小每组条数或在服务支持范围内调整输出上限。
- 每组一次请求，逐条核对返回编号并校验标签。编号缺失、重复或错配时整组不采用；单条标签错误只影响该条。不会自动拆组补发或重试。401/403后停止后续派发，在途结果收尾保存。空白、公式和无效记录跳过，同文重复只安排首条分析。
- 工作台保留“分析结果、问 Agent、明细与下载”三个入口，增加具体主题与历史批次比较；图表可悬停，明细可搜索和筛选，查看或下载不会重新调用模型。
- 下载明细 CSV 可用 Excel 打开；静态 HTML 报告含离线SVG图表、原文证据跳转与打印样式，可在浏览器打印为PDF。分类与摘要由模型生成，统计与文字洞察由本地代码计算，不额外调用模型。
- 统计按原始评价条数计权：同文重复评价可复用首条成功结果，并按各自的商品或门店分别计数；页面区分“实际分析”和“重复复用”。仅与首条文本完全一致、关联正确且首条分析成功时才复用，首条失败不会变成有效结果。明细保留原始记录、重复来源和跳过原因；相同文字在不同情境下的含义仍需业务人员核对。
- 分析后的原表、报告及对话归档到部署机器。`APP_DATA_DIR` 本地默认 `.runtime/history`，Docker 为持久化卷内的 `/data/customerinsight`，其中 SQLite 保存结构化结果，`originals/` 保存校验过的原件。清除当前页面不删除历史；历史管理提供备份、原件下载及确认删除。一个部署实例内属于同一可信工作区，没有账号级私有隔离。
- 同一会话中发生处理异常时保留已返回结果，未派发的评价仍显示待处理；这不提供跨刷新、断开或服务重启后的后台任务恢复。逐条耗时是其所在组的共享耗时，不是每条独立请求时间。
- 额度 SQLite 仍只记录随机运行 ID、日期与请求计数，和历史数据库分开保存。数据卷保留两者；额度控制是单实例限制，不是跨多副本的计费系统。

## 生成经营建议

基础分析完成后，在“问 Agent”输入问题并单独同意数据发送，再点击提问。支持连续追问、下载对话及重新打开历史；仅填入问题或点击主题预填不会发送请求。

每轮实际使用 1 至 3 次模型请求，最多 6 次本地只读查询（包括初始概览与字段清单）。模型结合当前报告和对话直接撰写正文，信息不足时可查询原文、字段、具体主题及用户明确选定的一份历史快照。原文与统计引用按本批、上批分别校验；不提供任意文件、SQL、网络或自动执行动作。程序不能验证自由回答的全部语义和因果。

Agent 与合并分类共用 `APP_DAILY_REQUEST_LIMIT`，开始前预留最多 3 次，按实际发送次数扣减，零 SDK 重试。失败明确提示本轮回答未完成，不用模板总结代替，保留原报告及下载。选择历史对比或改变报告时会校验对话范围。

示例报告不会运行 Agent，历史打开、切换页签、筛选和下载也不自动请求。真实合成小样本验收及已知限制见工作台交付说明；这些结果不能证明真实业务准确率。对外部署仍需按自身数据和使用场景复核。

## 用公开数据试用

`datasets/public_reviews/customer_reviews_sample.xlsx` 有20条真实英文评价摘录；选择 `公开顾客评价` 工作表、`feedback` 评价列、`source` 分组列。另提供100条固定CSV样本、原始文件、CC BY 4.0来源和抽样记录，见 `datasets/public_reviews/DATA_SOURCES.md`。

没有接口配置也能点击“查看示例报告”：只读取20条历史公开结果，不发新请求、不消耗额度。样例历史采用20次逐条调用；当前合并模式的验证与功能说明见 `BATCH_UI_DELIVERY.md`，两者不会混作同一次实验。

公开数据只有原始正负情感标签；类别、优先级和中性没有对应金标。英文样本不能证明中文业务准确率。`examples/customer_reviews_template.xlsx` 是含手工示例行的填写模板，使用前替换示例。

离线检查不会调用模型：

```powershell
.\.venv\Scripts\python.exe tools/test_public_reviews_live.py
.\.venv\Scripts\python.exe tools/test_grouped_reviews.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

开发机原20条逐条试测和新20条合并试测分别使用独立台账，新合并试测仅授权2次请求；原阶段174/180台账保持不变。不把开发验收上限当成部署者API余额。上述两个脚本默认不联网；各自首次真实验收完成后不会重复发起调用。

## 来源

本应用基于 Y. Siva Sai Krishna 的 AI Customer Feedback Analyzer 二次开发，原 MIT 许可与版权见 `LICENSE`。公开数据是独立的 CC BY 4.0 数据，不因代码MIT许可证而改换数据许可。
