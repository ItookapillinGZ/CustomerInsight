# 公开顾客评价样例数据

核查日期：2026-10-15。本目录与原来 60 条合成评价的评测集相互独立；没有修改旧数据、旧参考标签、冻结记录或实验请求台账。

## 已下载并提供的英文真实评价

- 数据集：[Sentiment Labelled Sentences — UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences)。发布者：Dimitrios Kotzias，2015 年；DOI：[10.24432/C57604](https://doi.org/10.24432/C57604)。
- 官方下载：[UCI ZIP](https://archive.ics.uci.edu/static/public/331/sentiment%2Blabelled%2Bsentences.zip)。原始 ZIP 与已使用的 Amazon、Yelp 文本及作者 readme 保存在 `raw/`；原始字节和 SHA-256 保存在 `metadata.json`。
- 官方数据页面明确标为 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。本项目保留作者、来源、许可链接，并说明抽样与标签映射；这些第三方数据沿用该许可，不改为项目代码的 MIT 许可。使用和再分发时请保留本文件及归属信息。
- 数据引用：Kotzias, D. (2015). *Sentiment Labelled Sentences* [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C57604.
- 原论文引用：Dimitrios Kotzias, Misha Denil, Nando de Freitas, Padhraic Smyth (2015). *From Group to Individual Labels Using Deep Features*. KDD 2015.

原发布集包含来自 Amazon、Yelp、IMDb 的英文评价句子，每个来源各 1,000 条，正负各 500 条；作者刻意选取明确正向或负向句子，未设置中性参考标签。本项目使用 Amazon 产品评价与 Yelp 餐饮评价共 2,000 条作为原始池，不使用 IMDb 影评。这里是真实评价的**句子摘录**，不是完整评价、平台实时数据或新采集的用户数据。

### 上传样例与重现

`samples.csv` 共 100 条：Amazon 与 Yelp 各 50 条，每来源正向和负向各 25 条。固定随机种子 `20261015`，抽样时排除重复文本，按原始行号生成稳定 ID，保留原始英文文本；仅将原标签 0/1 映射为 negative/positive。CSV 使用 UTF-8 BOM，可在 Excel 中打开。

列含义：

| 列 | 含义 |
| --- | --- |
| `source_id` | 原数据源及一开始计数的原始行号 |
| `feedback` | 应上传给分析流程的评价文本 |
| `source` | amazon 或 yelp |
| `sentiment_label` | 原始数据的正负情感参考标签，仅用于本地评测，不应传给模型 |

在项目根目录离线重现（不调用模型）：

```powershell
.\.venv\Scripts\python.exe tools\prepare_public_reviews.py
```

如需重新下载**同一官方原始文件**，显式添加 `--download`。若原始 ZIP 字节或成员内容变化，脚本会因 SHA-256 不一致而停止，避免静默更换数据。`--sample-size` 支持 4 到 100 之间的 4 的倍数；默认 100 条。原始 ZIP 不应按输入文件直接上传。

### 能测什么，不能测什么

- 可测试 Excel/CSV 上传、选列、批量处理、统计和导出是否连通。
- 原始参考标签只支持**正向/负向二分类情感**检查。模型若输出 neutral，应单列统计并在完整样本分母中算作与参考不一致，不能丢弃这些样本后计算更高分。
- 原数据没有问题类别、优先级、方面、评分或日期参考值，本项目不为其编造 gold。模型可以生成这些分析，但不能据此声称相关准确率。
- 这里是等比例抽样的英语旧数据，不能推断真实店铺的差评比例、中文效果或目前客户的业务分布；餐饮、电商主题也不等于原项目的软件反馈类别。
- 数据准备脚本与 `metadata.json` 只记录离线采样步骤，`model_calls_executed=0` 指该步骤不调用模型。之后另行授权的20条真实接口验收保存在 `validation/public_reviews_pilot/`，20次调用全部通过结构校验，正负情感与原标签一致20/20；完整口径与限制见项目根目录 `PRODUCT_DELIVERY.md`。验收不修改此处的原始数据或旧180次实验台账。

## 中文候选（未捆绑，未声称可商用）

已查看发布仓库 [SophonPlus/ChineseNlpCorpus](https://github.com/SophonPlus/ChineseNlpCorpus) 及对应数据说明，找到以下更贴近中文顾客反馈的候选。检查到的页面未给出可确认的、明确覆盖底层平台评论再分发与商业使用的许可；“公开可下载”不能自动等于获得这些授权。本次不下载或随交付包再分发其评论。

| 候选 | 发布页 | 内容与限制 |
| --- | --- | --- |
| waimai_10k | [仓库目录](https://github.com/SophonPlus/ChineseNlpCorpus/tree/master/datasets/waimai_10k) | 外卖平台正负评价；发布说明未明确平台身份；需核对底层权利及使用许可 |
| online_shopping_10_cats | [说明](https://github.com/SophonPlus/ChineseNlpCorpus/blob/master/datasets/online_shopping_10_cats/intro.ipynb) | 10 类商品/服务的中文购物评价；需确认原始来源与可再分发授权 |
| ChnSentiCorp_htl_all | [说明](https://github.com/SophonPlus/ChineseNlpCorpus/blob/master/datasets/ChnSentiCorp_htl_all/intro.ipynb) | 中文酒店评价；需确认当前整理版与原始语料各自的授权范围 |

正式中文试点可优先使用店铺自己导出且有权处理的脱敏评价，另请独立人工标注抽样后的参考标签。获得明确许可后，才把相应公开中文数据加入可下载产品样例。
