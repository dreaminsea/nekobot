"""旧图回看工具：当前图片已直接进入主对话，旧图按需复用同一 Flash 模型。"""

from core import images
from core.runtime import get_event
from core.tools import tool


@tool(
    name="look_image",
    description=(
        "回看本轮未附原图的历史图片；已经随消息附上的图片直接理解，无需调用此工具。"
        "图片标成 [图片#xxxx]，传它的 id 即可回看；不传 id 则看当前消息的图。"
        "可选 question 针对性提问（如『图里有什么字』『这是什么动物』）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_id": {
                "type": "string",
                "description": "要看的图片 id，即 [图片#xxxx] 里的 xxxx；省略则看当前消息的图",
            },
            "question": {
                "type": "string",
                "description": "针对这张图想问的具体问题；省略则给出通用描述",
            },
        },
    },
)
async def look_image(image_id: str = "", question: str = "") -> str:
    image_id = (image_id or "").strip()

    if image_id:
        return await images.describe_id(image_id, question)
    else:
        ev = get_event()
        imgs = ev.images if ev is not None else []
        if not imgs:
            return "[当前消息里没有图片；要看某张图请传它的 id（[图片#xxxx] 里的 xxxx）]"
        img = imgs[-1]  # 当前消息最新的一张

    return await images.describe(img, question)
