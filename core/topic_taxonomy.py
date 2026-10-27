"""Conservative, versioned aliases for report-level review topic aggregation.

Only exact aliases are joined. This is not semantic clustering or a second model
prediction: unknown terms retain their own bucket, and kinds stay separate in
``review_topics``. Published versions must not be edited in place.
"""

import re
import unicodedata


TAXONOMY_VERSION = "review_topics_2026_10_v1"

_ALIASES = {
    "配送时效": (
        "配送慢", "物流延迟", "物流慢", "配送延迟", "到货慢", "到货延迟", "送货慢",
        "配送速度", "送货速度", "物流速度", "到货速度", "配送及时", "送货及时",
        "slow delivery", "late delivery", "delivery delay", "delivery delays",
        "delayed delivery", "delivery speed", "fast delivery", "shipping speed",
        "slow shipping", "shipping delay", "shipping delays", "delayed shipping",
    ),
    "包装完整": (
        "包装破损", "包装完好", "包装完整性", "包装损坏", "外包装破损",
        "damaged packaging", "packaging damage", "intact packaging", "package damage",
    ),
    "包装减塑": (
        "减少塑料包装", "减少塑料", "塑料包装减量", "减塑包装",
        "reduce plastic packaging", "less plastic packaging",
    ),
    "订单进度提醒": (
        "物流进度提醒", "订单状态提醒", "发货进度提醒",
        "order status updates", "shipping status updates", "delivery updates",
    ),
    "保温效果": (
        "保温性能", "保温时长", "保温能力", "heat retention", "thermal insulation",
    ),
    "密封漏水": (
        "密封性", "杯盖漏水", "密封防漏", "漏水", "杯子漏水", "防漏效果",
        "leakproof", "leak proof", "water leakage", "leaking lid", "lid leakage",
    ),
    "客服解答": (
        "客服解释", "客服答疑", "客服讲解", "客服专业度", "客服解答清晰度",
        "customer service explanation", "support explanation",
    ),
    "客服响应速度": (
        "客服回复慢", "客服响应慢", "客服回复速度", "客服响应及时",
        "slow customer support", "support response time", "customer service response time",
    ),
    "客服态度": (
        "服务态度", "客服服务态度", "客服态度差", "客服态度好",
        "customer service attitude", "staff attitude", "rude customer service",
    ),
    "产品耐用性": (
        "耐用性", "使用寿命", "产品寿命", "product durability", "durability", "product lifespan",
    ),
    "电池续航": (
        "续航时长", "续航能力", "续航", "battery life", "battery duration",
    ),
    "退换货处理": (
        "退款处理", "退货流程", "退款速度", "退换货流程", "refund process",
        "refund processing", "return process", "returns process",
    ),
    "性价比": (
        "物有所值", "价格与价值", "value for money", "good value", "poor value",
    ),
}


def _lookup_key(topic: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", topic)).strip().casefold()


_LOOKUP = {
    _lookup_key(alias): canonical
    for canonical, aliases in _ALIASES.items()
    for alias in (canonical, *aliases)
}


def normalize_topic(topic: str) -> str:
    """Return a known canonical name, preserving unknown spellings verbatim."""
    return _LOOKUP.get(_lookup_key(topic), topic)
