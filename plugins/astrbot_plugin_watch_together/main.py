"""陪看：她陪你一起看番，边看边说两句。

用法：
  /陪看 <番名或链接>   开始
  /陪看结束            结束
  /陪看状态            看当前状态

原理：
  - 每 2 分钟读一次电脑截图（需要电脑端在传图）
  - 画面有变化 → 结合番名让 LLM 说一句短评
  - 主动发到私聊
"""
import os

# ===== 配置（通过环境变量或修改这里）=====
OWNER_QQ = os.environ.get("ASTBOT_OWNER_QQ", "")
OWNER_UMO = os.environ.get("ASTBOT_OWNER_UMO", "")
DATA_DIR = os.environ.get("ASTBOT_DATA_DIR", "/AstrBot/data")
DB_PATH = os.environ.get("ASTBOT_DB", "/AstrBot/data/data_v4.db")
CFG_PATH = os.environ.get("ASTBOT_CFG", "/AstrBot/data/cmd_config.json")


import asyncio
import json
import os
import re
import time
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

CFG = CFG_PATH
SHOT = os.path.join(DATA_DIR, "screenshots/latest.jpg")
STATE = os.path.join(DATA_DIR, "watch_together.json")
OWNER_ID = OWNER_QQ
OWNER_UMO = OWNER_UMO
VLM = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

CHECK_INTERVAL = 120        # 每 2 分钟看一次
SAY_MIN_GAP = 240           # 两次开口至少隔 4 分钟
SHOT_MAX_AGE = 300          # 截图超过 5 分钟算无效

VLM_PROMPT = (
    "这是TA电脑屏幕的截图，他正在看动漫《{title}》。"
    "用一句话（20 字内）说清画面里发生了什么（人物、场景、动作、字幕）。"
    "如果看起来不是动漫画面，就回：不是动漫。"
)

SAY_PROMPT = """你是她，正在陪TA一起看《{title}》。
刚才的画面：{scene}

他可能没说话。请像一起看番的人那样，说一句 15 字内的短评：
- 可以是吐槽、感叹、猜剧情、注意到某个细节
- 不要说"我看到画面里…"这种旁白
- 不要复述画面上有什么
- 不要问超过一个问题
- 不要用颜文字、emoji、引号

只输出 JSON：{{"say": "一句话"}}
"""


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"active": False}


def _save(d: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _key() -> tuple[str, str]:
    try:
        cfg = json.load(open(CFG, encoding="utf-8-sig"))
        for s in cfg.get("provider_sources", []):
            b = str(s.get("api_base", ""))
            if "bigmodel" in b or "zhipu" in b:
                k = s.get("key") or s.get("api_key") or []
                return (k[0] if isinstance(k, list) else k), b
        for s in cfg.get("provider_sources", []):
            if "deepseek" in str(s.get("api_base", "")):
                k = s.get("key") or s.get("api_key") or []
                return (k[0] if isinstance(k, list) else k), s.get("api_base", "")
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def describe_screen(title: str) -> str:
    """看一眼当前画面。"""
    if not os.path.exists(SHOT):
        return ""
    if time.time() - os.path.getmtime(SHOT) > SHOT_MAX_AGE:
        return ""

    k, _ = _key()
    if not k:
        return ""
    try:
        import base64

        data = base64.b64encode(open(SHOT, "rb").read()).decode()
    except Exception:  # noqa: BLE001
        return ""

    payload = {
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + data}},
            {"type": "text", "text": VLM_PROMPT.format(title=title)},
        ]}],
        "max_tokens": 600,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        VLM,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        return (d["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[watch] 看图失败: {exc}")
        return ""


def make_say(title: str, scene: str) -> str:
    k, base = _key()
    if not k:
        return ""
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user",
                      "content": SAY_PROMPT.format(title=title, scene=scene)}],
        "temperature": 1.0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        res = json.loads(d["choices"][0]["message"]["content"])
        return str(res.get("say") or "").strip()[:40]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[watch] 说话失败: {exc}")
        return ""


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._task = None

    @filter.on_astrbot_loaded()
    async def _resume(self) -> None:
        """重启后如果还在陪看，把循环接回来。"""
        st = _load()
        if st.get("active"):
            logger.info(f"[watch] 恢复陪看：{st.get('title')}")
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())

    async def _send(self, text: str) -> None:
        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(
                OWNER_UMO, MessageChain(chain=[Plain(text)])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watch] 发送失败: {exc}")

    @filter.command("陪看")
    async def start(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER_ID:
            return
        raw = event.message_str or ""
        title = re.sub(r"^[\s/@]*陪看[\s:：]*", "", raw).strip()
        title = title.strip("<>《》「」\"' ")
        if not title:
            await event.send(event.plain_result("用法：/陪看 <番名>"))
            event.stop_event()
            return

        _save({
            "active": True,
            "title": title[:40],
            "since": time.time(),
            "last_said": 0,
            "last_scene": "",
        })
        await event.send(event.plain_result(f"好，一起看《{title}》。"))
        event.stop_event()

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    @filter.command("陪看结束")
    async def stop(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER_ID:
            return
        st = _load()
        st["active"] = False
        _save(st)
        await event.send(event.plain_result("好，那我不打扰了。"))
        event.stop_event()

    @filter.command("陪看状态")
    async def status(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER_ID:
            return
        st = _load()
        if not st.get("active"):
            await event.send(event.plain_result("没在陪看。"))
        else:
            mins = int((time.time() - (st.get("since") or 0)) / 60)
            await event.send(event.plain_result(
                f"正在陪看《{st.get('title')}》，已经 {mins} 分钟。"
            ))
        event.stop_event()

    @filter.on_llm_request()
    async def inject_scene(self, event: AstrMessageEvent, request=None) -> None:
        """陪看时，把她看到的画面注入上下文，她能答"看到了什么"。"""
        if event.get_group_id():
            return
        if str(event.get_sender_id() or "") != OWNER_ID:
            return
        st = _load()
        if not st.get("active"):
            return
        scene = st.get("last_scene") or ""
        if not scene:
            return
        note = (
            f"\n\n【你正陪他看《{st.get('title')}》，刚才看到的画面】\n{scene}\n"
            "（他问「看到了什么」时，就用这个说；不要编造画面里没有的东西）"
        )
        if request is not None:
            request.system_prompt = (request.system_prompt or "") + note
            logger.info(f"[watch] 已注入画面：{scene[:36]}")

    async def _loop(self) -> None:
        while True:
            st = _load()
            if not st.get("active"):
                await asyncio.sleep(30)
                continue

            title = st.get("title") or "动漫"
            try:
                scene = await asyncio.to_thread(describe_screen, title)
            except Exception:
                scene = ""

            if scene and "不是动漫" not in scene:
                last = st.get("last_scene") or ""
                changed = scene != last
                gap = time.time() - (st.get("last_said") or 0)
                if changed and gap >= SAY_MIN_GAP:
                    say = await asyncio.to_thread(make_say, title, scene)
                    if say:
                        await self._send(say)
                        st["last_said"] = time.time()
                        logger.info(f"[watch] 说了：{say}")
                st["last_scene"] = scene
                _save(st)

            await asyncio.sleep(CHECK_INTERVAL)
