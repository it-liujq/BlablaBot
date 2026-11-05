"""LPass 自动任务调度：每分钟检查一次，到点在北京时间执行当天日常。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .api import LPassAPI
from .report import render
from .store import LPassBind, decrypt_cookie, ensure_ready
from .tasks import DailyRunner
from .ui import send_friend_requests

JOB_ID = "blablabot-lpass-auto"
TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


class LPassAutoScheduler:
    def __init__(self):
        self.scheduler = None
        self._lock = asyncio.Lock()

    def register(self, scheduler=None):
        if scheduler is None:
            from gsuid_core.aps import scheduler
        self.scheduler = scheduler
        return scheduler.add_job(
            self.tick, "interval", seconds=60, id=JOB_ID,
            name="BlablaBot LPass 自动任务", replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            await ensure_ready()
            now = datetime.now(TZ)
            at = now.strftime("%H:%M")
            day = now.strftime("%Y-%m-%d")
            try:
                due = await LPassBind.due_binds(at, day)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LPass auto due query failed error_type=%s", type(exc).__name__)
                return
            for bind in due:
                try:
                    await self.run_one(bind, day)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LPass auto run failed error_type=%s", type(exc).__name__)

    async def run_one(self, bind, day: str):
        cookie = decrypt_cookie(bind.cookie_enc)
        if cookie is None or cookie.expired:
            text = "LPass 自动任务：登录态已过期，请重新发送 Lpass绑定。"
            await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day, summary=text)
            await self.notify(bind, text)
            return
        try:
            async with LPassAPI(cookie) as api:
                friend = None
                try:
                    friend = await send_friend_requests(cookie, api, count=3,
                                                        headless=True, timeout_s=150)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LPass auto friend ui skipped error_type=%s",
                                   type(exc).__name__)
                result = await DailyRunner(api).run()
                if friend is not None and (friend.confirmed or friend.share_done):
                    result.notes.append(f"好友/分享：{friend.summary()}")
            text = render(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass auto error_type=%s", type(exc).__name__)
            text = "LPass 自动任务执行异常，未确认完成，请稍后手动发送 Lpass日常。"
        await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day, summary=text)
        await self.notify(bind, text)

    async def notify(self, bind, text: str):
        ws = getattr(bind, "notify_ws", "")
        if not ws:
            return
        try:
            from gsuid_core.gss import gss
            transport = gss.active_bot.get(ws)
            if transport is None:
                return
            await transport.target_send(
                text, "direct", str(bind.user_id), bind.bot_id,
                getattr(bind, "notify_self", ""), at_sender=False, wait_recall=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass auto notify failed error_type=%s", type(exc).__name__)


_scheduler = None


def setup_auto(scheduler=None):
    """在插件入口调用一次；失败不影响手动命令。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = LPassAutoScheduler()
        try:
            _scheduler.register(scheduler)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass auto scheduler not registered error_type=%s",
                           type(exc).__name__)
    return _scheduler
