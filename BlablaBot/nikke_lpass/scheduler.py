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
try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 tzdata 时退回固定 +08:00
    from datetime import timedelta, timezone

    TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


class LPassAutoScheduler:
    def __init__(self):
        self.scheduler = None
        self._lock = asyncio.Lock()
        self.tz = TZ

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
            groups = {}
            for bind in due:
                key = (str(bind.bot_id), str(bind.group_id or ""))
                groups.setdefault(key, []).append(bind)
            for (bot_id, group_id), binds in groups.items():
                try:
                    if group_id:
                        await self.run_group(bot_id, group_id, binds, day)
                    else:
                        for bind in binds:
                            await self.run_one(bind, day)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LPass auto run failed error_type=%s", type(exc).__name__)

    async def run_group(self, bot_id: str, group_id: str, binds, day: str):
        """群内全量签到：按账号依次执行，全部跑完后发一条群汇总。"""
        successes, failures = [], []
        for bind in binds:
            try:
                text = await self._execute(bind, day, notify=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LPass group member failed error_type=%s", type(exc).__name__)
                failures.append((bind.nickname or bind.user_id, "执行异常"))
                continue
            if text is None:
                failures.append((bind.nickname or bind.user_id, "登录态已过期"))
                continue
            if "每日签到" in text and "已完成" in text:
                successes.append(bind.nickname or bind.user_id)
            else:
                failures.append((bind.nickname or bind.user_id, "未确认签到成功"))
        summary = self.group_message(successes, failures, len(binds))
        logger.info("LPass group summary group=%s success=%s fail=%s",
                    group_id, len(successes), len(failures))
        await self.notify_group(bot_id, group_id, binds[0], summary)

    def group_message(self, successes, failures, total: int) -> str:
        complete = not failures
        title = "今日自动签到已完成！" if complete else "自动签到处理结束。"
        text = ("✅" if complete else "⚠️") + " [BlablaBot] LIPASS" + title
        text += f"\n📝 本群共签到成功{len(successes)}人，共签到失败{len(failures)}人。"
        if total == 0:
            text += "\n本群还没有开启全量签到的用户。"
        if failures:
            text += "\n" + "、".join(f"{name}（{reason}）" for name, reason in failures[:10])
        return text

    async def notify_group(self, bot_id: str, group_id: str, bind, text: str):
        ws = str(getattr(bind, "notify_ws", "") or "")
        if not ws:
            return
        try:
            from gsuid_core.gss import gss
            transport = gss.active_bot.get(ws)
            if transport is None:
                return
            await transport.target_send(
                text, "group", str(group_id), str(bot_id),
                str(getattr(bind, "notify_self", "") or ""),
                group_id=str(group_id), at_sender=False, wait_recall=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass group notify failed error_type=%s", type(exc).__name__)

    async def _execute(self, bind, day: str, *, notify: bool = True):
        cookie = decrypt_cookie(bind.cookie_enc)
        if cookie is None or cookie.expired:
            text = "LPass 全量签到：登录态已过期，请重新发送 #LPass绑定。"
            await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day, summary=text)
            if notify:
                await self.notify(bind, text)
            return None
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
                if friend is not None:
                    if friend.share_link:
                        result.friend_link = friend.share_link
                    prefix = "⚠ " if friend.error else ""
                    result.notes.append(f"{prefix}好友/分享：{friend.summary()}")
            text = render(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass auto error_type=%s", type(exc).__name__)
            text = "LPass 全量签到执行异常，未确认完成，请稍后手动发送 #LPass签到。"
        await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day, summary=text)
        if notify:
            await self.notify(bind, text)
        return text

    async def run_one(self, bind, day: str):
        await self._execute(bind, day, notify=True)

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


def get_scheduler():
    return _scheduler


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
