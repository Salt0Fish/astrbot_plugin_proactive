"""主动消息：按真实聊天规律，隔一段时间自己找上来。

规律来自 146 天、656 次真实主动开场：
- 平均每天 4.5 次，集中在 10~15 点、19~1 点，4~6 点几乎不找
- 高频开场：人呢 / 嘿嘿 / 早上好 / 宝宝 / 上课啦 / 555 / 睡醒啦 / 午好呢

三个条件同时满足才发：
1. 距对方上一条消息 >= min_gap_after_user（默认 45 分钟）
2. 距上次主动 >= min_gap_between（默认 90 分钟）
3. 落在当天计划时间点附近（计划按小时权重生成）

命令：/主动 测试   立即发一条
"""

import asyncio
import datetime
import json
import random
import time
from pathlib import Path

import yaml

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

DATA_DIR = Path(__file__).parent / "data"
STATE = DATA_DIR / "state.json"

# 默认小时权重（来自真实数据，可在配置里覆盖）
DEFAULT_HOUR_WEIGHTS = {
    0: 28, 1: 35, 2: 24, 3: 20, 4: 9, 5: 7, 6: 4, 7: 16, 8: 25, 9: 26,
    10: 36, 11: 47, 12: 38, 13: 32, 14: 44, 15: 44, 16: 30, 17: 30,
    18: 26, 19: 39, 20: 32, 21: 23, 22: 21, 23: 20,
}

DEFAULT_OPENERS = {
    "morning": ["早上好", "早上好呢", "起床呢", "睡醒啦"],
    "noon": ["午好呢", "吃饭了没", "在干嘛", "人呢"],
    "afternoon": ["人呢", "嘿嘿", "在干嘛", "下课了"],
    "evening": ["人呢", "嘿嘿", "在干嘛", "回来啦"],
    "night": ["好困", "睡着了", "没电了", "555", "人呢"],
    "any": ["人呢", "嘿嘿", "哈哈哈", "你在干嘛", "听得到"],
}

CFG: dict = {}


def _bucket(hour: int) -> str:
    if 7 <= hour <= 9:
        return "morning"
    if 11 <= hour <= 13:
        return "noon"
    if 14 <= hour <= 17:
        return "afternoon"
    if 18 <= hour <= 21:
        return "evening"
    return "night"


def _openers() -> dict:
    custom = CFG.get("openers_yaml") or ""
    if custom.strip():
        try:
            data = yaml.safe_load(custom)
            if isinstance(data, dict) and data:
                return {str(k): [str(x) for x in v] for k, v in data.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[proactive] openers_yaml 解析失败: {exc}")
    return DEFAULT_OPENERS


def _hour_weights() -> dict:
    custom = CFG.get("hour_weights") or {}
    if isinstance(custom, dict) and custom:
        try:
            return {int(k): int(v) for k, v in custom.items()}
        except Exception:
            pass
    return DEFAULT_HOUR_WEIGHTS


class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        self.context = context
        CFG.clear()
        CFG.update(dict(config))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load()
        self._task = asyncio.create_task(self._loop())

    def _load(self) -> dict:
        if STATE.exists():
            try:
                return json.loads(STATE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"umo": "", "last_user": 0, "last_bot": 0,
                "last_proactive": 0, "plan": [], "plan_day": ""}

    def _save(self) -> None:
        try:
            STATE.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[proactive] 保存状态失败: {exc}")

    def _make_plan(self, day: str) -> list[int]:
        lo = int(CFG.get("min_per_day", 2))
        hi = int(CFG.get("max_per_day", 4))
        n = random.randint(max(1, lo), max(lo, hi))
        weights_map = _hour_weights()
        quiet = set(CFG.get("quiet_hours") or [4, 5, 6])
        gap = int(CFG.get("min_gap_between", 90)) * 60

        hours = list(weights_map.keys())
        weights = [max(1, weights_map[h]) for h in hours]
        picks: list[int] = []
        tries = 0
        while len(picks) < n and tries < 60:
            tries += 1
            h = random.choices(hours, weights=weights)[0]
            if h in quiet:
                continue
            ts = int(datetime.datetime.combine(
                datetime.date.fromisoformat(day),
                datetime.time(hour=h, minute=random.randint(0, 59)),
            ).timestamp())
            if ts <= int(time.time()) + 600:
                continue
            if all(abs(ts - p) >= gap for p in picks):
                picks.append(ts)
        picks.sort()
        if picks:
            logger.info("[proactive] %s 计划: %s" % (
                day, ", ".join(datetime.datetime.fromtimestamp(p).strftime("%H:%M") for p in picks)))
        return picks

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[proactive] tick 异常: {exc}")
            await asyncio.sleep(int(CFG.get("tick_seconds", 60)))

    async def _tick(self) -> None:
        now = int(time.time())
        today = datetime.date.today().isoformat()
        if self.state.get("plan_day") != today:
            self.state["plan"] = self._make_plan(today)
            self.state["plan_day"] = today
            self._save()

        if not self.state.get("umo"):
            return
        if now - self.state.get("last_user", 0) < int(CFG.get("min_gap_after_user", 45)) * 60:
            return
        if now - self.state.get("last_proactive", 0) < int(CFG.get("min_gap_between", 90)) * 60:
            return
        due = [p for p in self.state.get("plan", []) if abs(now - p) <= 300]
        if not due:
            return
        await self._send()
        self.state["plan"] = [p for p in self.state["plan"] if p != due[0]]
        self._save()

    async def _send(self, text: str | None = None) -> bool:
        umo = self.state.get("umo")
        if not umo:
            return False
        if text is None:
            pool_map = _openers()
            hour = datetime.datetime.now().hour
            pool = pool_map.get(_bucket(hour), pool_map.get("any", ["在吗"]))
            text = random.choice(pool + pool_map.get("any", []))
        try:
            ok = await self.context.send_message(umo, self._chain(text))
            if ok:
                self.state["last_proactive"] = int(time.time())
                self._save()
                logger.info(f"[proactive] 已发送: {text}")
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[proactive] 发送失败: {exc}")
            return False

    def _chain(self, text: str):
        from astrbot.api.event import MessageChain

        return MessageChain([Plain(text)])

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record(self, event: AstrMessageEvent):
        umo = getattr(event, "unified_msg_origin", "") or ""
        scope = CFG.get("scope", "FriendMessage")
        if umo and scope in umo:
            self.state["umo"] = umo
            self.state["last_user"] = int(time.time())
            self._save()

    @filter.command("主动")
    async def manual(self, event: AstrMessageEvent, arg: str = ""):
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo:
            self.state["umo"] = umo
        pool_map = _openers()
        hour = datetime.datetime.now().hour
        pool = pool_map.get(_bucket(hour), pool_map.get("any", ["在吗"]))
        text = random.choice(pool + pool_map.get("any", []))
        ok = await self._send(text)
        yield event.plain_result(f"已发送: {text}" if ok else "发送失败，检查会话是否已记录")
