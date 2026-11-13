"""绘画：她真的会画画了。

用法：
  /画 <描述>        立刻画一张
  /画               让她画一张"现在想画的"

自动：
  - 她的状态是"画画"且画完时，有概率主动发一张（每天最多 1 张）

生图：智谱 CogView-4（复用现有 GLM key）
"""
import os

# ===== 配置（通过环境变量或修改这里）=====
OWNER_QQ = os.environ.get("ASTBOT_OWNER_QQ", "")
OWNER_UMO = os.environ.get("ASTBOT_OWNER_UMO", "")
DATA_DIR = os.environ.get("ASTBOT_DATA_DIR", "/AstrBot/data")
DB_PATH = os.environ.get("ASTBOT_DB", "/AstrBot/data/data_v4.db")
CFG_PATH = os.environ.get("ASTBOT_CFG", "/AstrBot/data/cmd_config.json")


import json
import time
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain

CFG = CFG_PATH
STATE = os.path.join(DATA_DIR, "draw_state.json")
OWNER_ID = OWNER_QQ
OWNER_UMO = OWNER_UMO

API = "https://open.bigmodel.cn/api/paas/v4/images/generations"
MODEL = "cogview-4-250304"

# 她的画风
STYLE = (
    "手绘素描风格，铅笔线条，纸质感，温柔的日式插画，"
    "16 岁日本女孩的速写本，留白多，不完美的手作感"
)

DRAW_PROMPT = """你是她，16 岁的日本女孩，喜欢画画。
现在是 {now}，TA让你画点什么。

{ask}

请描述你想画的东西（用画面描述，不要用"我想画"开头）：
- 20~40 字
- 具体到画面内容（谁、在哪、在做什么、光线）
- 不要写风格（风格另加）

只输出 JSON：{{"desc": "画面描述"}}
"""


def _key() -> str:
    try:
        c = json.load(open(CFG, encoding="utf-8-sig"))
        for s in c.get("provider_sources", []):
            b = str(s.get("api_base", ""))
            if "bigmodel" in b or "zhipu" in b:
                k = s.get("key") or s.get("api_key") or []
                return k[0] if isinstance(k, list) else k
    except Exception:  # noqa: BLE001
        pass
    return ""


def _llm_key() -> tuple[str, str]:
    try:
        c = json.load(open(CFG, encoding="utf-8-sig"))
        for s in c.get("provider_sources", []):
            b = str(s.get("api_base", ""))
            if "deepseek" in b:
                k = s.get("key") or s.get("api_key") or []
                k = k[0] if isinstance(k, list) else k
                base = b.rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                return k, base
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def make_desc(ask: str) -> str:
    """让她想画什么。"""
    k, base = _llm_key()
    if not k:
        return ask or "窗边的午后"
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": DRAW_PROMPT.format(
            now=time.strftime("%H:%M"), ask=ask or "（没指定，画你现在想画的）")}],
        "temperature": 1.0,
        "max_tokens": 150,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        res = json.loads(d["choices"][0]["message"]["content"])
        return str(res.get("desc") or "").strip()[:80]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[draw] 描述失败: {exc}")
        return ask or "窗边的午后"


def generate(desc: str) -> str:
    """生成图片，返回 URL。"""
    key = _key()
    if not key:
        return ""
    payload = {
        "model": MODEL,
        "prompt": f"{desc}。{STYLE}",
        "size": "1024x1024",
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
        return (d.get("data") or [{}])[0].get("url", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[draw] 生图失败: {exc}")
        return ""


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    @filter.command("画")
    async def draw(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER_ID:
            return

        ask = (event.message_str or "").replace("/画", "").strip()
        if ask:
            desc = ask
        else:
            desc = make_desc("")

        await event.send(event.plain_result("嗯…我想想画什么。"))
        url = generate(desc)
        if not url:
            await event.send(event.plain_result("画失败了…纸被揉掉了。"))
            event.stop_event()
            return

        chain = [Plain(f"画好了。{desc[:30]}"), Image.fromURL(url)]
        await event.send(event.chain_result(chain))
        event.stop_event()
