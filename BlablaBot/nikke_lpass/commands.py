"""LPass 命令：绑定 / 解绑 / 状态 / 日常。

触发器名按用户要求写成 ``#LPass绑定``、``#LPass日常``：注册时 prefix=False，
所以不受插件级 ``nk`` 前缀影响；带 nk 的写法不会命中这两个命令。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import Optional

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ..nikke_commands import command_prefix
from ..nikke_login.commands import get_base_url
from ..nikke_login.session import sessions
from .api import LPassAPI, LPassAuthError, LPassCookie, LPassError, parse_cookie
from .auth import LoginError, login_with_password, verify
from .report import render, render_auto_status, render_bind, render_status_detail
from .store import (
    DEFAULT_AUTO_TIME,
    LPassBind,
    decrypt_cookie,
    encrypt_cookie,
    ensure_ready,
    mask_openid,
)
from .tasks import DailyRunner
from .ui import send_friend_requests

sv_lpass = SV("LI PASS 奖励中心", pm=6, priority=4)

_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(user_id: str, bot_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault((str(user_id), str(bot_id)), asyncio.Lock())



async def _reply(bot: Bot, ev: Event, text: str, *, title: str = "LPass", **_ignored) -> None:
    """LPass 的回复统一用合并转发；失败或关闭开关时回退普通消息。"""
    from gsuid_core.segment import MessageSegment

    from ..nikke_config.config import AliceUIDConfig

    try:
        enabled = bool(AliceUIDConfig.get_config("AliceUID_LPassForward").data)
    except Exception:  # noqa: BLE001
        enabled = True
    if enabled:
        try:
            node = MessageSegment.node(
                [MessageSegment.text(str(title)), MessageSegment.text(text)])
            return await bot.send(node, at_sender=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Lpass] 合并转发失败，回退普通消息 error_type={type(exc).__name__}")
    await bot.send(text, at_sender=True)


HELP = (
    "LEVEL INFINITE PASS 奖励中心\n"
    "#LPass绑定 —— 发一个网页登录链接（推荐，账号密码只在浏览器输入）\n"
    "#LPass绑定 cookie内容 —— 粘贴浏览器里 pass.levelinfinite.com 的 Cookie\n"
    "#LPass绑定 邮箱 密码 —— 由 Bot 打开网页登录并保存登录态\n"
    "#LPass日常 —— 签到 + 常规任务 + 一次性任务\n"
    "#LPass日常 演练 —— 只读取任务进度，不提交任何请求\n"
    "#LPass状态 / #LPass解绑\n"
    f"Cookie 至少包含 lip_openid 与 lip_token；与 {command_prefix()}登录的 blablalink 账号互不影响。"
)


def _looks_like_cookie(text: str) -> bool:
    return "lip_openid" in text or "lip_token" in text or text.count("=") >= 2


def _split_credentials(text: str) -> Optional[tuple[str, str]]:
    parts = [p for p in re.split(r"[\s,，|]+", text.strip()) if p]
    if len(parts) == 2 and "@" in parts[0]:
        return parts[0], parts[1]
    return None


async def _load_api(user_id: str, bot_id: str) -> tuple[Optional[LPassBind], Optional[LPassCookie]]:
    bind = await LPassBind.get_bind(user_id, bot_id)
    if bind is None:
        return None, None
    cookie = decrypt_cookie(bind.cookie_enc)
    if cookie is not None and not cookie.expire_at and bind.expire_at:
        cookie = replace(cookie, expire_at=int(bind.expire_at))
    return bind, cookie


@sv_lpass.on_command("#LPass绑定", prefix=False)
async def lpass_bind(bot: Bot, ev: Event):
    text = (ev.text or "").strip()
    if not text:
        # 与 #bla登录 一致：给一个网页登录链接，账号密码只在浏览器里输入
        active = sessions.active(ev.user_id, ev.bot_id)
        if active is not None and active.mode == "lpass":
            session = active
        else:
            session = sessions.create(ev.user_id, ev.bot_id, ev.group_id, area_id=0, ttl=600)
            session.mode = "lpass"
        url = f"{get_base_url()}/nikke/lpass/{session.token}"
        await _reply(bot, ev, 
            "请在浏览器打开下面的链接登录 LEVEL INFINITE PASS（10 分钟内有效）：\n"
            f"{url}\n\n"
            "登录成功后 Bot 会自动保存登录态；也可以用私聊发 "
            "「#LPass绑定 Cookie内容」或「#LPass绑定 邮箱 密码」。",
            at_sender=True)
        return
    # Cookie 与密码都是登录态，只允许私聊提交，避免在群里泄露。
    if getattr(ev, "group_id", None) and _looks_like_cookie(text):
        await _reply(bot, ev, "Cookie 属于登录凭据，请在与 Bot 的私聊里发送 #LPass绑定。", at_sender=True)
        return
    credentials = None if _looks_like_cookie(text) else _split_credentials(text)
    if credentials is not None and getattr(ev, "group_id", None):
        await _reply(bot, ev, "账号密码登录请在与 Bot 的私聊里使用 #LPass绑定 邮箱 密码。", at_sender=True)
        return
    async with _lock(ev.user_id, ev.bot_id):
        cookie: Optional[LPassCookie] = None
        if _looks_like_cookie(text):
            cookie = parse_cookie(text)
            if cookie is None:
                await _reply(bot, ev, 
                    "这段内容里没有 lip_openid / lip_token，请从 pass.levelinfinite.com 的"
                    "开发者工具 → Application → Cookies 复制完整一段。",
                    at_sender=True,
                )
                return
        else:
            if credentials is None:
                await _reply(bot, ev, HELP, at_sender=True)
                return
            email, password = credentials
            await _reply(bot, ev, "正在打开网页登录，可能需要 30~60 秒，请不要重复发送。", at_sender=True)
            try:
                cookie = await login_with_password(email, password)
            except LoginError as exc:
                await _reply(bot, ev, str(exc), at_sender=True)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Lpass] 网页登录失败 error_type={type(exc).__name__}")
                await _reply(bot, ev, "网页登录未完成，请稍后重试或改用 Cookie 绑定。", at_sender=True)
                return

        try:
            summary = await verify(cookie)
        except LPassAuthError:
            await _reply(bot, ev, "登录态无效或已过期，请确认复制的是当前浏览器会话的 Cookie。", at_sender=True)
            return
        except LPassError as exc:
            await _reply(bot, ev, f"登录态校验失败：{exc}", at_sender=True)
            return

        nickname = str(summary.get("nickname") or cookie.user_name or "")
        await LPassBind.save_bind(
            ev.user_id, ev.bot_id, ev.group_id,
            cookie_enc=encrypt_cookie(cookie),
            lip_openid=cookie.openid,
            nickname=nickname,
            expire_at=cookie.expire_at,
        )
        await _reply(bot, ev, 
            render_bind(nickname, int(summary.get("points") or 0),
                        masked=mask_openid(cookie.openid),
                        expire_days=cookie.days_left),
            at_sender=True,
        )


def _valid_time(value: str) -> bool:
    return re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is not None


@sv_lpass.on_command("#LPass自动任务", prefix=False)
async def lpass_auto(bot: Bot, ev: Event):
    await ensure_ready()
    argument = (ev.text or "").strip()
    parts = [p for p in argument.split() if p]
    option = parts[0] if parts else "状态"
    bind = await LPassBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        await _reply(bot, ev, "还没有绑定。发送 #LPass绑定 查看绑定方式。", at_sender=True)
        return
    if option in ("开启", "开", "打开"):
        at = parts[1] if len(parts) > 1 else (bind.auto_time or DEFAULT_AUTO_TIME)
        if not _valid_time(at):
            await _reply(bot, ev, "时间格式应为 HH:MM（北京时间），例如 #LPass自动任务 开启 08:30。",
                           at_sender=True)
            return
        ws = str(getattr(ev, "WS_BOT_ID", "") or "")
        self_id = str(getattr(ev, "bot_self_id", "") or "")
        ok = await LPassBind.set_auto(ev.user_id, ev.bot_id, enabled=True, at=at,
                                      ws=ws, self_id=self_id)
        if not ok:
            await _reply(bot, ev, "自动任务开启失败，请稍后重试。", at_sender=True)
            return
        await _reply(bot, ev, 
            f"已开启 LPass 自动任务：每天北京时间 {at} 自动执行签到、好友请求/分享与领取，"
            "执行完会把结果推给你。\n关闭：#LPass自动任务 关闭；查看：#LPass自动任务 状态。",
            at_sender=True)
        return
    if option in ("关闭", "关", "停止"):
        ok = await LPassBind.set_auto(ev.user_id, ev.bot_id, enabled=False)
        await _reply(bot, ev, "已关闭 LPass 自动任务。" if ok else "关闭失败，请稍后重试。",
                       at_sender=True)
        return
    if option in ("状态", "查询", ""):
        days = max(0, (int(bind.expire_at or 0) - int(time.time())) // 86400) \
            if bind.expire_at else 0
        await _reply(bot, ev, 
            render_auto_status(enabled=bool(bind.auto_enabled),
                               at=bind.auto_time or DEFAULT_AUTO_TIME,
                               last_day=bind.last_run_day or "",
                               last_result=bind.last_result or "",
                               expire_days=days),
            at_sender=True)
        return
    await _reply(bot, ev, 
        "用法：#LPass自动任务 开启 [HH:MM] / 关闭 / 状态。\n"
        "开启后每天北京时间到点自动执行一次，结果私聊推送。",
        at_sender=True)


@sv_lpass.on_command("#LPass解绑", prefix=False)
async def lpass_unbind(bot: Bot, ev: Event):
    bind = await LPassBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        await _reply(bot, ev, "当前没有 LPass 绑定。", at_sender=True)
        return
    count = await LPassBind.delete_bind(ev.user_id, ev.bot_id)
    await _reply(bot, ev, "LPass 绑定已解除。" if count else "解绑未完成，请稍后重试。", at_sender=True)


@sv_lpass.on_command("#LPass状态", prefix=False)
async def lpass_status(bot: Bot, ev: Event):
    bind, cookie = await _load_api(ev.user_id, ev.bot_id)
    if bind is None or cookie is None:
        await _reply(bot, ev, "还没有绑定。发送 #LPass绑定 查看绑定方式。", at_sender=True)
        return
    try:
        async with LPassAPI(cookie) as api:
            result = await DailyRunner(api, dry_run=True).build_status()
    except LPassAuthError:
        await _reply(bot, ev, "登录态已失效，请重新发送 #LPass绑定。", at_sender=True)
        return
    except LPassError as exc:
        await _reply(bot, ev, f"状态读取失败：{exc}", at_sender=True)
        return
    await _reply(
        bot, ev,
        render_status_detail(result, nickname=bind.nickname,
                             points_earned=result.points_earned,
                             masked=mask_openid(cookie.openid),
                             expire_days=cookie.days_left),
        at_sender=True)


@sv_lpass.on_command("#LPass日常", prefix=False)
async def lpass_daily(bot: Bot, ev: Event):
    argument = (ev.text or "").strip()
    dry_run = argument in ("演练", "dry", "试运行", "测试")
    async with _lock(ev.user_id, ev.bot_id):
        bind, cookie = await _load_api(ev.user_id, ev.bot_id)
        if bind is None or cookie is None:
            await _reply(bot, ev, "还没有绑定。发送 #LPass绑定 查看绑定方式。", at_sender=True)
            return
        if cookie.expired:
            await _reply(bot, ev, "登录态已过期，请重新发送 #LPass绑定。", at_sender=True)
            return
        await _reply(bot, ev, 
            "开始执行日常：签到 → 好友请求/分享 → 领取，约需 1~2 分钟，请稍候。", at_sender=True)
        try:
            async with LPassAPI(cookie) as api:
                friend = None
                if not dry_run:
                    # 好友请求/分享要在页面里由 SDK 签名，先尽力跑一次；失败不影响后面
                    try:
                        friend = await send_friend_requests(cookie, api, count=3,
                                                            headless=True, timeout_s=90)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"[Lpass] 好友自动化跳过 error_type={type(exc).__name__}")
                runner = DailyRunner(api, dry_run=dry_run)
                result = await runner.run(settle=False)
                if friend is not None and (friend.confirmed or friend.share_done):
                    result.notes.append(f"好友/分享：{friend.summary()}")

                # 先出动作结果，别让人干等
                await _reply(bot, ev, render(result))

                if not dry_run:
                    before = result.today_earned
                    try:
                        await runner.settle(result)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[Lpass] 结算复查失败 error_type={type(exc).__name__}")
                    if result.today_earned != before:
                        await _reply(bot, ev, render(result) + "\n\n（以上为结算后更新）",
                                     at_sender=True)
        except LPassAuthError:
            await _reply(bot, ev, "登录态已失效，请重新发送 #LPass绑定。", at_sender=True)
            return
        except LPassError as exc:
            await _reply(bot, ev, f"日常执行中断：{exc}", at_sender=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Lpass] 日常执行异常 error_type={type(exc).__name__}")
            await _reply(bot, ev, "日常执行出现内部异常，未确认完成，请稍后重试。", at_sender=True)
            return
        if dry_run:
            return
        # 动作已完成，异步奖励再结算；结算后如有变化补一条
        try:
            async with LPassAPI(cookie) as api:
                pass
        except Exception:  # noqa: BLE001
            pass
        try:
            before_earned = result.today_earned
            await runner.settle(result)
            if result.today_earned != before_earned:
                await _reply(bot, ev, render(result) + "\n\n（以上为结算后更新）", at_sender=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Lpass] 结算复查失败 error_type={type(exc).__name__}")
