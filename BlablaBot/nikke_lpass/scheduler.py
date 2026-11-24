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
        all_binds = await LPassBind.group_all(bot_id, group_id)
        enabled_ids = {str(b.user_id) for b in binds}
        not_enabled = [b for b in all_binds if str(b.user_id) not in enabled_ids]
        successes, failures = [], []
        for bind in binds:
            display = bind.nickname or str(bind.user_id)
            try:
                _text, signed, reason = await self._execute(bind, day, notify=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LPass group member failed error_type=%s", type(exc).__name__)
                signed, reason = False, "执行异常，请稍后检查"
            if signed:
                successes.append(display)
            else:
                streak = int(getattr(bind, "fail_streak", 0) or 0) + 1
                failures.append({"name": display, "qq": str(bind.user_id),
                                 "reason": reason, "streak": streak,
                                 "cookie": reason.startswith("登录态")})
        summary = self.group_message(successes, failures, not_enabled)
        logger.info("LPass group summary group=%s success=%s fail=%s",
                    group_id, len(successes), len(failures))
        await self.notify_group(bot_id, group_id, binds[0] if binds else all_binds[0], summary,
                                mentions=[f["qq"] for f in failures])

    def group_message(self, successes, failures, not_enabled) -> str:
        complete = not failures
        head = ("✅" if complete else "⚠️") + " [BlablaBot] LIPASS"
        head += "今日自动签到已完成！" if complete else "自动签到处理结束。"
        lines = [head]
        tail = f"📝 本群共签到成功{len(successes)}人，共签到失败{len(failures)}人，"
        tail += (f"{len(not_enabled)}人尚未开启全量签到。" if not_enabled else "全员均已开启全量签到。")
        lines.append(tail)
        lines.append("统计范围：本群已绑定 LPass 的账号。")
        if failures:
            lines.extend(["", "异常详情（1/1）："])
            for item in failures[:20]:
                lines.append(f"{item['name']} （QQ：{item['qq']}）")
                lines.append(f"原因：{item['reason']}；共尝试1轮。")
                lines.append(f"处理：连续失败{item['streak']}/3天，全量签到保持开启，下个周期继续。")
                if item.get("cookie"):
                    lines.append("COOKIE 可能已过期，请发送 #LPass绑定；如为网络或服务异常，请稍后检查。")
        return "\n".join(lines)

    async def notify_group(self, bot_id: str, group_id: str, bind, text: str, mentions=None):
        ws = str(getattr(bind, "notify_ws", "") or "")
        if not ws:
            return
        try:
            from gsuid_core.gss import gss
            transport = gss.active_bot.get(ws)
            if transport is None:
                return
            content = text
            if mentions:
                from gsuid_core.segment import MessageSegment
                content = [MessageSegment.text(text), MessageSegment.text("\n")]
                for idx, qq in enumerate(dict.fromkeys(str(m) for m in mentions if str(m).isdigit())):
                    if idx:
                        content.append(MessageSegment.text(" "))
                    content.append(MessageSegment.at(qq))
            await transport.target_send(
                content, "group", str(group_id), str(bot_id),
                str(getattr(bind, "notify_self", "") or ""),
                group_id=str(group_id), at_sender=False, wait_recall=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass group notify failed error_type=%s", type(exc).__name__)

    async def _execute(self, bind, day: str, *, notify: bool = True):
        """执行单个账号，返回 (文本, 是否签到成功, 失败原因)。"""
        cookie = decrypt_cookie(bind.cookie_enc)
        if cookie is None or cookie.expired:
            text = "LPass 全量签到：登录态已过期，请重新发送 #LPass绑定。"
            await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day,
                                     summary=text, success=False)
            if notify:
                await self.notify(bind, text)
            return text, False, "登录态已过期，请发送 #LPass绑定 重新绑定"
        signed = False
        reason = "未确认签到成功"
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
            signed = any(getattr(st, "key", "") == "signin"
                         and getattr(st, "status", "") in ("done", "already")
                         for st in getattr(result, "steps", []))
            if not signed:
                signed = any(str(i.get("name", "")) == "每日签到"
                             for i in getattr(result, "today_points", []))
            if not signed:
                signed = "每日签到" in text and "已完成" in text
            if signed:
                reason = "签到已确认"
        except Exception as exc:  # noqa: BLE001
            logger.warning("LPass auto error_type=%s", type(exc).__name__)
            text = "LPass 全量签到执行异常，未确认完成，请稍后手动发送 #LPass签到。"
            reason = "执行异常，请稍后检查"
        await LPassBind.mark_ran(bind.user_id, bind.bot_id, day=day,
                                 summary=text, success=signed)
        if notify:
            await self.notify(bind, text)
        return text, signed, reason

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
