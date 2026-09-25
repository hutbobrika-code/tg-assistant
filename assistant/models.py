from dataclasses import dataclass, field

from .config import settings


@dataclass
class Model:
    id: str
    name: str
    api: str  # responses или chat (chat/completions)
    vision: bool = False
    pdf: bool = False
    efforts: list = field(default_factory=list)


# бесплатные модели OpenCode Zen
MODELS = [
    Model("muse-spark-1.3-contributor-free", "Muse Spark 1.3", "responses", vision=True, pdf=True,
          efforts=["minimal", "low", "medium", "high", "xhigh"]),
    Model("space-bunny-free", "Space Bunny", "chat", vision=True,
          efforts=["low", "medium", "high", "xhigh", "max"]),
    Model("mimo-v2.6-flash-free", "MiMo V2.6 Flash", "chat", vision=True),
    Model("ling-3.0-flash-fin-free", "Ling 3.0 Flash", "chat", efforts=["low", "medium", "high"]),
    Model("nemotron-3-ultra-free", "Nemotron 3 Ultra", "chat"),
    Model("big-pickle", "Big Pickle", "chat"),
]
BY_ID = {m.id: m for m in MODELS}

EFFORTS = {
    "minimal": "⚡ Мгновенно",
    "low": "🟢 Быстро",
    "medium": "🟡 Обычно",
    "high": "🟠 Вдумчиво",
    "xhigh": "🔴 Максимум",
    "max": "🟣 Предел",
}


def get(model_id):
    return BY_ID.get(model_id) or BY_ID.get(settings.default_model) or MODELS[0]


def effort_for(model, wanted):
    # если у модели нет выбранного режима, берём ближайший из её режимов
    if not model.efforts:
        return None
    if wanted in model.efforts:
        return wanted
    order = list(EFFORTS)
    pos = order.index(wanted) if wanted in order else order.index("high")
    return min(model.efforts, key=lambda e: abs(order.index(e) - pos))
