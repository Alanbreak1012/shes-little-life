"""内心生活（完整版）

她隔一阵自己想"现在在做什么"，并和你对话时能因你而变。

- 8 类骨架抽签（学习/做饭/画画/家务/娱乐/发呆/出门/休息）+ LLM 补细节
- 每件事有预估时长；做完了如果值得说一声，就主动发一句
- 主动发消息有时段限制（8:00-23:00）和活跃度限制（你 6 小时没说话就不打扰）
- 你说的话能改变她的状态（临时打断 / 永久取消）
- 只有你问"在干嘛"时才把状态注入上下文
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
import random
import re
import time
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

import sys as _sys
_sys.path.insert(0, '/AstrBot/data/plugins/astrbot_plugin_inner_life')
from wake_state import can_send as _can_send, state_of as _state_of

CFG = CFG_PATH
STATE = os.path.join(DATA_DIR, "inner_life.json")
DB = DB_PATH
OWNER_UMO = OWNER_UMO
OWNER_ID = OWNER_QQ

MIN_GAP = 40 * 60
MAX_GAP = 90 * 60
DAY_START, DAY_END = 7, 24
STALE = 3 * 3600
CHECK_INTERVAL = 120

CATEGORIES = {
    "学习": "学中文（背单词、写字、听中文歌、看中文视频）",
    "做饭": "做饭或煮东西（经常不太成功）",
    "画画": "素描本上画点什么",
    "家务": "收拾房间、洗衣服、刷碗、擦桌子",
    "娱乐": "看番、听歌、刷视频、看漫画",
    "发呆": "趴在窗边、躺在床上、盯着天花板",
    "出门": "去便利店、散步、买东西",
    "休息": "靠一会儿、闭眼、喝点东西",
}
CATEGORY_WEIGHTS = {
    "学习": 3, "做饭": 2, "画画": 2, "家务": 2,
    "娱乐": 3, "发呆": 2, "出门": 1, "休息": 1,
}

PROMPT = """你是她，16 岁的日本女孩，和TA同居。
现在是 {now}（{period}），TA在忙自己的事，没人跟你说话。

**这次你正在做的是「{category}」这一类的事**，具体做什么由你补：
{category_hint}

你上一段时间在做：{last}

你们刚才的聊天（最近的）：
{chat}

请写一句具体的话，说清你此刻在做什么：

要求：
1. 必须具体到能想象出画面，20 字以内
   ✅ 在背中文单词，有个字老是记不住
   ✅ 在煮汤，味道不太对
   ❌ 在做饭（太笼统）
2. 必须属于「{category}」这一类，不要跳到别的类别
3. **状态要和刚才的聊天一致**：
   刚才在聊散步 → 就得在外面或在回来的路上；在聊吃饭 → 就在吃或收拾
4. 像随口说的，不要"我正在…"这种书面开头

顺便给一个心情（两个字）

只输出 JSON：{{"activity": "在做什么", "mood": "心情", "minutes": 20, "then_say": ""}}

minutes：这件事大概持续多久（10~60）。做饭 15~30，看番 20~40，学中文 30~50，发呆 10~20，出门 40~60。
then_say：如果做完有个明确结果、你会想跟他说一声，就给一句 20 字内、**能独立看懂**的话
（❌"咸了" ✅"煮了锅汤，好像咸了"）；不值得说的留空字符串。
"""

REACT_PROMPT = """（TA是男生，指代他用「他」）
她正在：{activity}
TA刚发来：{text}

这句话需要她**放下手里的事、改变当前状态**吗？

能触发改变：
- 「我回来了」→ 去玄关等他
- 「我饿了」→ 去准备吃的
- 「陪我一会儿」→ 放下手里的事陪他
- 「我累了/不舒服」→ 停下手里的事照顾他
- 「别织了」「不要弄了」→ 停下手里的事，换别的
- 「你先去睡吧」「去洗澡」→ 照做

不能触发：
- 普通聊天、问句、闲聊、发表情
- 只是在说他自己在做什么

还要判断：他是**叫你暂停一下**，还是**不要你再做这件事了**？
- 「陪我会儿」「过来一下」→ 临时打断，结束后继续（permanent=false）
- 「别织了」「以后别做这个」→ 不要做了（permanent=true）

只输出 JSON：
{{"change": true 或 false, "activity": "改变后她正在做什么（20字内）", "minutes": 整数分钟, "permanent": true 或 false}}
"""



WAKE_PROMPT = """你是她，刚睡醒。
现在是 {now}（{period}）。

请写一句你刚醒来时会发给TA的话，20 字以内：
- 可以带一点没睡醒的感觉（打哈欠、揉眼睛、声音有点哑）
- 可以提到刚醒、还困、外面天亮了
- 不要问"你醒了吗"（他可能还在睡）
- 不要问超过一个问题
- 不要用颜文字、emoji、引号

只输出 JSON：{{"text": "一句话"}}
"""

def _period(hour: int) -> str:
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "深夜"


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"activity": "", "mood": "", "since": 0}


def _save(d: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _deepseek() -> tuple[str, str]:
    try:
        cfg = json.load(open(CFG, encoding="utf-8-sig"))
        for s in cfg.get("provider_sources", []):
            if "deepseek" in str(s.get("api_base", "")):
                k = s.get("key") or s.get("api_key") or []
                if isinstance(k, list):
                    k = k[0] if k else ""
                return k, s.get("api_base", "https://api.deepseek.com/v1")
    except Exception:  # noqa: BLE001
        pass
    return "", "https://api.deepseek.com/v1"


def _strip_reminder(t: str) -> str:
    return re.sub(
        r"<system_reminder>.*?</system_reminder>", "", t, flags=re.S
    ).strip()


def recent_chat(limit: int = 8) -> str:
    """读最近几条私聊，让状态和聊天场景一致。"""
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute(
            "select content from conversations where user_id=?", (OWNER_UMO,)
        ).fetchone()
        conn.close()
        if not row:
            return ""
        msgs = json.loads(row[0])[-limit:]
        lines = []
        for m in msgs:
            c = m.get("content")
            t = c if isinstance(c, str) else "".join(
                str(p.get("text") or "") for p in (c or []) if isinstance(p, dict)
            )
            t = _strip_reminder(t)
            if not t:
                continue
            who = "TA" if m.get("role") == "user" else "我"
            lines.append(f"{who}：{t[:80]}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def pick_category(recent: list) -> str:
    """抽一个类别，避开最近两次用过的。"""
    used = set()
    for item in recent[-2:]:
        for name in CATEGORIES:
            if name in (item or ""):
                used.add(name)
    pool = [c for c in CATEGORY_WEIGHTS if c not in used] or list(CATEGORY_WEIGHTS)
    weights = [CATEGORY_WEIGHTS[c] for c in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def _llm(prompt: str, max_tokens: int = 260, temperature: float = 1.1,
         timeout: int = 60) -> dict:
    key, api_base = _deepseek()
    if not key:
        return {}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        return json.loads(raw.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[inner_life] 调用失败: {exc}")
        return {}


def think() -> dict:
    st = _load()
    recent_list = st.get("recent") or []
    category = pick_category(recent_list)
    body = PROMPT.format(
        now=time.strftime("%H:%M"),
        period=_period(time.localtime().tm_hour),
        last=st.get("activity") or "（刚开始，还没有上一件事）",
        category=category,
        category_hint=CATEGORIES[category],
        chat=recent_chat() or "（还没聊过）",
    )
    res = _llm(body, 260, 1.1)
    if res:
        res["category"] = category
    return res


def react(activity: str, text: str) -> dict:
    """判断这句话要不要改变她的状态。"""
    body = REACT_PROMPT.format(activity=activity, text=text[:200])
    return _llm(body, 140, 0.3, 45)



def make_wake_line() -> str:
    """生成一句起床话。"""
    body = WAKE_PROMPT.format(
        now=time.strftime("%H:%M"),
        period=_period(time.localtime().tm_hour),
    )
    res = _llm(body, 80, 0.9, 45)
    text = str(res.get("text") or "").strip().strip('"「」')
    if text:
        return text[:30]
    # 兜底
    hour = time.localtime().tm_hour
    if hour < 9:
        return "起床了…早"
    return "我起来啦"


class Main(star.Star):
    REACT_WORDS = (
        "回来", "到了", "到楼下", "饿了", "想吃", "吃饭", "陪我", "过来",
        "累了", "困了", "不舒服", "难受", "生病", "帮我看", "怎么办",
        "抱", "亲", "睡吧", "一起", "等你", "想见",
        "别", "不要", "不用", "算了", "停", "放下", "收起来",
        "去睡", "去洗澡", "去休息", "过来坐", "别弄了",
    )

    ASK_WORDS = (
        "在干嘛", "在做什么", "在干什么", "干什么呢", "干嘛呢",
        "在忙什么", "忙什么", "做什么呢", "在忙吗", "现在做什么",
        "在干嘛呢", "干嘛", "做什么",
    )

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._task = None

    def _ensure_loop(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
            logger.info("[inner_life] 内心生活已启动")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[inner_life] 启动失败: {exc}")

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        self._ensure_loop()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _trigger(self, event: AstrMessageEvent) -> None:
        self._ensure_loop()

    async def _notify(self, text: str) -> None:
        # 陪看中：她的状态是"和你一起看番"，不该插一句"我在做饭"
        try:
            with open(os.path.join(DATA_DIR, "watch_together.json"), encoding="utf-8") as _f:
                if (json.load(_f) or {}).get("active"):
                    logger.info(f"[inner_life] 陪看中，不发生活气泡（{text}）")
                    return
        except Exception:  # noqa: BLE001
            pass

        # 这是"要他回应"的消息（尝尝、吃不吃），只有他醒着才发
        state = _state_of()
        if not _can_send("interact"):
            logger.info(f"[inner_life] 他现在是 {state}，不发互动消息（{text}）")
            return

        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(
                OWNER_UMO, MessageChain(chain=[Plain(text)])
            )
            logger.info(f"[inner_life] 已主动告知：{text}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[inner_life] 主动告知失败: {exc}")

    async def _roll(self) -> None:
        res = await asyncio.to_thread(think)
        act = str(res.get("activity") or "").strip()
        if not act:
            return
        category = res.get("category") or ""

        old = _load()
        mood = str(res.get("mood") or "").strip()

        # 被明确叫停的事，3 小时内不要做
        blocked = [
            b for b in (old.get("blocked") or [])
            if time.time() - (b.get("ts") or 0) < 3 * 3600
        ]
        blocked_names = {b.get("activity") for b in blocked if b.get("activity")}
        if blocked_names and any(
            (name in act or act in name) for name in blocked_names
        ):
            logger.info(f"[inner_life] 跳过被叫停的事：{act}")
            return

        try:
            minutes = int(res.get("minutes") or 20)
        except Exception:  # noqa: BLE001
            minutes = 20
        minutes = max(10, min(60, minutes))
        then_say = str(res.get("then_say") or "").strip()[:40]

        now = time.time()
        recent = (old.get("recent") or [])[-2:]
        tag = f"{act}（{category}）" if category else act
        if not recent or recent[-1] != tag:
            recent.append(tag)

        _save({
            "activity": act,
            "mood": mood,
            "since": now,
            "minutes": minutes,
            "expire_at": now + minutes * 60,
            "then_say": then_say,
            "recent": recent,
            "blocked": blocked,
            "last_user_msg": old.get("last_user_msg") or 0,
        })
        logger.info(
            f"[inner_life] 现在在：{act}（{category}，{mood}，约 {minutes} 分钟"
            f"{'，做完会说：' + then_say if then_say else ''}）"
        )

    async def _react_to_message(self, said: str) -> None:
        st = _load()
        act = st.get("activity") or ""
        if not act:
            return

        if time.time() - (st.get("since") or 0) < 180:
            return
        recent = [t for t in (st.get("react_ts") or []) if time.time() - t < 600]
        if len(recent) >= 2:
            return

        res = await asyncio.to_thread(react, act, said)
        if not res.get("change"):
            return

        new_act = str(res.get("activity") or "").strip()
        if not new_act or new_act == act:
            return
        try:
            minutes = int(res.get("minutes") or 10)
        except Exception:  # noqa: BLE001
            minutes = 10
        minutes = max(2, min(480, minutes))

        now = time.time()
        hist = (st.get("recent") or [])[-2:]
        if not hist or hist[-1] != act:
            hist.append(act)

        blocked = st.get("blocked") or []
        if res.get("permanent"):
            blocked = [
                b for b in blocked if now - (b.get("ts") or 0) < 3 * 3600
            ]
            blocked.append({"activity": act, "ts": now})
            logger.info(f"[inner_life] 不再做：{act}")

        _save({
            "activity": new_act,
            "mood": st.get("mood") or "平静",
            "since": now,
            "minutes": minutes,
            "expire_at": now + minutes * 60,
            "then_say": "",
            "recent": hist,
            "react_ts": recent + [now],
            "blocked": blocked,
            "last_user_msg": now,
        })
        logger.info(
            f"[inner_life] 因他的话改变状态：{act} → {new_act}（{minutes} 分钟）"
        )

    async def _loop(self) -> None:
        await asyncio.sleep(random.randint(3 * 60, 8 * 60))
        while True:
            try:
                if DAY_START <= time.localtime().tm_hour <= DAY_END:
                    st = _load()
                    expire = st.get("expire_at") or 0
                    # 她刚睡醒？
                    last_active = st.get("last_active") or 0
                    asleep_hours = (
                        (time.time() - last_active) / 3600 if last_active else 0
                    )
                    if (
                        last_active
                        and asleep_hours >= 5
                        and 7 <= time.localtime().tm_hour <= 12
                    ):
                        line = await asyncio.to_thread(make_wake_line)
                        await self._notify(line)
                        st["last_active"] = time.time()
                        st["woke_at"] = time.time()
                        _save(st)
                        logger.info(f"[inner_life] 她刚醒：{line}")

                    if not st.get("activity"):
                        await self._roll()
                    elif expire and time.time() >= expire:
                        say = st.get("then_say") or ""
                        st["then_say"] = ""
                        st["expire_at"] = 0
                        _save(st)
                        if say:
                            await self._notify(say)
                        await self._roll()
                    elif not expire:
                        await self._roll()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[inner_life] 一轮出错: {exc}")
            # 她活动过，更新活跃时间
            try:
                _s = _load()
                _s["last_active"] = time.time()
                _save(_s)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(CHECK_INTERVAL)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, request: ProviderRequest) -> None:
        if event.get_group_id():
            return
        if str(event.get_sender_id() or "") != OWNER_ID:
            return

        said = event.message_str or ""

        # 记录他最后一次说话
        try:
            _st = _load()
            _st["last_user_msg"] = time.time()
            _save(_st)
        except Exception:  # noqa: BLE001
            pass

        # 先粗筛：只有出现这些词才让模型判断要不要改变状态
        if any(w in said for w in self.REACT_WORDS):
            try:
                await self._react_to_message(said)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[inner_life] 反应失败: {exc}")

        # 只有他问起"在做什么"时才把状态说出来
        if not any(w in said for w in self.ASK_WORDS):
            return

        st = _load()
        act = st.get("activity") or ""
        if not act:
            return
        age = time.time() - (st.get("since") or 0)
        if age > STALE:
            return

        mins = int(age // 60)
        left = max(0, int(((st.get("expire_at") or 0) - time.time()) // 60))
        tail = f"，大概还要 {left} 分钟" if left else "，快做完了"
        note = (
            f"\n\n【他问起你在做什么，你此刻正在】{act}"
            f"（{mins} 分钟前开始的，心情：{st.get('mood') or '平静'}{tail}）\n"
            "（用一两句如实说这件事就行。不要编造比这句更多的细节）"
        )
        request.system_prompt = (request.system_prompt or "") + note
        logger.info(f"[inner_life] 已注入状态：{act}（{mins} 分钟前）")
