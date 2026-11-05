"""LPass 命令：绑定 / 解绑 / 状态 / 日常。

触发器名按用户要求写成 ``Lpass绑定``、``Lpass日常``：注册时 prefix=False，
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
from .api import LPassAPI, LPassAuthError, LPassCookie, LPassError, ladder_progress, parse_cookie
from .auth import LoginError, login_with_password, verify
from .report import render, render_auto_status, render_bind, render_status
from .store import (
    DEFAULT_AUTO_TIME,
    LPassBind,
    decrypt_cookie,
    encrypt_cookie,
    ensure_ready,
    mask_openid,
)
from .tasks import DailyRunner, classify
from .ui import send_friend_requests

sv_lpass = SV("LI PASS 奖励中心", pm=6, priority=4)

_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(user_id: str, bot_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault((str(user_id), str(bot_id)), asyncio.Lock())


HELP = (
    "LEVEL INFINITE PASS 奖励中心\n"
    "Lpass绑定 —— 发一个网页登录链接（推荐，账号密码只在浏览器输入）\n"
    "Lpass绑定 cookie内容 —— 粘贴浏览器里 pass.levelinfinite.com 的 Cookie\n"
    "Lpass绑定 邮箱 密码 —— 由 Bot 打开网页登录并保存登录态\n"
    "Lpass日常 —— 签到 + 常规任务 + 一次性任务\n"
    "Lpass日常 演练 —— 只读取任务进度，不提交任何请求\n"
    "Lpass状态 / Lpass解绑\n"
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


@sv_lpass.on_command("Lpass绑定", prefix=False)
async def lpass_bind(bot: Bot, ev: Event):
    text = (ev.text or "").strip()
    if not text:
        # 与 #bla登录 一致：给一个网页登录链接，账号密码只在浏览器里输入
        active = sessions.active(ev.user_id, ev.bot_id)
        if active is not None and active.mode == "lpass":
            session = active
        else:
            session = sessions.create(ev.user_id, ev.bot_id, ev.group_id, area_id=0, ttl=900)
            session.mode = "lpass"
        url = f"{get_base_url()}/nikke/lpass/{session.token}"
        await bot.send(
            "请在浏览器打开下面的链接登录 LEVEL INFINITE PASS（15 分钟内有效）：\n"
            f"{url}\n\n"
            "登录成功后 Bot 会自动保存登录态；也可以用私聊发 "
            "「Lpass绑定 Cookie内容」或「Lpass绑定 邮箱 密码」。",
            at_sender=True)
        return
    # Cookie 与密码都是登录态，只允许私聊提交，避免在群里泄露。
    if getattr(ev, "group_id", None) and _looks_like_cookie(text):
        await bot.send("Cookie 属于登录凭据，请在与 Bot 的私聊里发送 Lpass绑定。", at_sender=True)
        return
    credentials = None if _looks_like_cookie(text) else _split_credentials(text)
    if credentials is not None and getattr(ev, "group_id", None):
        await bot.send("账号密码登录请在与 Bot 的私聊里使用 Lpass绑定 邮箱 密码。", at_sender=True)
        return
    async with _lock(ev.user_id, ev.bot_id):
        cookie: Optional[LPassCookie] = None
        if _looks_like_cookie(text):
            cookie = parse_cookie(text)
            if cookie is None:
                await bot.send(
                    "这段内容里没有 lip_openid / lip_token，请从 pass.levelinfinite.com 的"
                    "开发者工具 → Application → Cookies 复制完整一段。",
                    at_sender=True,
                )
                return
        else:
            if credentials is None:
                await bot.send(HELP, at_sender=True)
                return
            email, password = credentials
            await bot.send("正在打开网页登录，可能需要 30~60 秒，请不要重复发送。", at_sender=True)
            try:
                cookie = await login_with_password(email, password)
            except LoginError as exc:
                await bot.send(str(exc), at_sender=True)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Lpass] 网页登录失败 error_type={type(exc).__name__}")
                await bot.send("网页登录未完成，请稍后重试或改用 Cookie 绑定。", at_sender=True)
                return

        try:
            summary = await verify(cookie)
        except LPassAuthError:
            await bot.send("登录态无效或已过期，请确认复制的是当前浏览器会话的 Cookie。", at_sender=True)
            return
        except LPassError as exc:
            await bot.send(f"登录态校验失败：{exc}", at_sender=True)
            return

        nickname = str(summary.get("nickname") or cookie.user_name or "")
        await LPassBind.save_bind(
            ev.user_id, ev.bot_id, ev.group_id,
            cookie_enc=encrypt_cookie(cookie),
            lip_openid=cookie.openid,
            nickname=nickname,
            expire_at=cookie.expire_at,
        )
        await bot.send(
            render_bind(nickname, int(summary.get("points") or 0),
                        masked=mask_openid(cookie.openid),
                        expire_days=cookie.days_left),
            at_sender=True,
        )


def _valid_time(value: str) -> bool:
    return re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is not None


@sv_lpass.on_command("Lpass自动任务", prefix=False)
async def lpass_auto(bot: Bot, ev: Event):
    await ensure_ready()
    argument = (ev.text or "").strip()
    parts = [p for p in argument.split() if p]
    option = parts[0] if parts else "状态"
    bind = await LPassBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        await bot.send("还没有绑定。发送 Lpass绑定 查看绑定方式。", at_sender=True)
        return
    if option in ("开启", "开", "打开"):
        at = parts[1] if len(parts) > 1 else (bind.auto_time or DEFAULT_AUTO_TIME)
        if not _valid_time(at):
            await bot.send("时间格式应为 HH:MM（北京时间），例如 Lpass自动任务 开启 08:30。",
                           at_sender=True)
            return
        ws = str(getattr(ev, "WS_BOT_ID", "") or "")
        self_id = str(getattr(ev, "bot_self_id", "") or "")
        ok = await LPassBind.set_auto(ev.user_id, ev.bot_id, enabled=True, at=at,
                                      ws=ws, self_id=self_id)
        if not ok:
            await bot.send("自动任务开启失败，请稍后重试。", at_sender=True)
            return
        await bot.send(
            f"已开启 LPass 自动任务：每天北京时间 {at} 自动执行签到、好友请求/分享与领取，"
            "执行完会把结果推给你。\n关闭：Lpass自动任务 关闭；查看：Lpass自动任务 状态。",
            at_sender=True)
        return
    if option in ("关闭", "关", "停止"):
        ok = await LPassBind.set_auto(ev.user_id, ev.bot_id, enabled=False)
        await bot.send("已关闭 LPass 自动任务。" if ok else "关闭失败，请稍后重试。",
                       at_sender=True)
        return
    if option in ("状态", "查询", ""):
        days = max(0, (int(bind.expire_at or 0) - int(time.time())) // 86400) \
            if bind.expire_at else 0
        await bot.send(
            render_auto_status(enabled=bool(bind.auto_enabled),
                               at=bind.auto_time or DEFAULT_AUTO_TIME,
                               last_day=bind.last_run_day or "",
                               last_result=bind.last_result or "",
                               expire_days=days),
            at_sender=True)
        return
    await bot.send(
        "用法：Lpass自动任务 开启 [HH:MM] / 关闭 / 状态。\n"
        "开启后每天北京时间到点自动执行一次，结果私聊推送。",
        at_sender=True)


@sv_lpass.on_command("Lpass解绑", prefix=False)
async def lpass_unbind(bot: Bot, ev: Event):
    bind = await LPassBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        await bot.send("当前没有 LPass 绑定。", at_sender=True)
        return
    count = await LPassBind.delete_bind(ev.user_id, ev.bot_id)
    await bot.send("LPass 绑定已解除。" if count else "解绑未完成，请稍后重试。", at_sender=True)


@sv_lpass.on_command("Lpass状态", prefix=False)
async def lpass_status(bot: Bot, ev: Event):
    bind, cookie = await _load_api(ev.user_id, ev.bot_id)
    if bind is None or cookie is None:
        await bot.send("还没有绑定。发送 Lpass绑定 查看绑定方式。", at_sender=True)
        return
    try:
        async with LPassAPI(cookie) as api:
            points = await api.points()
            try:
                tasks = await api.tasks()
            except LPassError:
                tasks = []
    except LPassAuthError:
        await bot.send("登录态已失效，请重新发送 Lpass绑定。", at_sender=True)
        return
    except LPassError as exc:
        await bot.send(f"状态读取失败：{exc}", at_sender=True)
        return
    ladder = next((t for t in tasks if classify(t) == "ladder_signin"), None)
    day, total = (ladder_progress(ladder)[0:2] if ladder else (0, 0))
    await bot.send(
        render_status(bind.nickname, points.get("total", 0), points.get("earned", 0),
                      signin_day=day, ladder_total=total,
                      masked=mask_openid(cookie.openid), expire_days=cookie.days_left),
        at_sender=True,
    )


@sv_lpass.on_command("Lpass好友", prefix=False)
async def lpass_friend(bot: Bot, ev: Event):
    """实验性：用无头浏览器点任务行的 GO 与推荐好友的「+」。"""
    bind, cookie = await _load_api(ev.user_id, ev.bot_id)
    if bind is None or cookie is None:
        await bot.send("还没有绑定。发送 Lpass绑定 查看绑定方式。", at_sender=True)
        return
    if cookie.expired:
        await bot.send("登录态已过期，请重新发送 Lpass绑定。", at_sender=True)
        return
    dry = (ev.text or "").strip() in ("演练", "dry", "试运行")
    await bot.send(
        "正在打开奖励中心并尝试自动点击推荐好友，约需 1~2 分钟，请勿重复发送。",
        at_sender=True)
    async with LPassAPI(cookie) as api:
        try:
            result = await send_friend_requests(cookie, api, count=3, dry_run=dry)
        except LPassAuthError:
            await bot.send("登录态已失效，请重新发送 Lpass绑定。", at_sender=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Lpass] 好友 UI 异常 error_type={type(exc).__name__}")
            await bot.send("好友面板自动化未完成，请在网页手动点击推荐好友。", at_sender=True)
            return
    lines = ["LEVELINFINITEPASS好友请求"]
    if result.error:
        lines.append(result.error)
    else:
        lines.append(f"推荐好友：{'、'.join(result.names[:3]) or '解析失败'}")
        lines.append(f"点击 {result.clicked} 次，站点确认 {result.confirmed} 次")
    if dry:
        lines.append("演练模式：只打开面板，没有真的点「+」。")
    lines.append("该功能为实验性，失败时请在 pass 站点手动完成后回来发 Lpass日常 领取。")
    await bot.send("\n".join(lines), at_sender=True)


@sv_lpass.on_command("Lpass日常", prefix=False)
async def lpass_daily(bot: Bot, ev: Event):
    argument = (ev.text or "").strip()
    dry_run = argument in ("演练", "dry", "试运行", "测试")
    async with _lock(ev.user_id, ev.bot_id):
        bind, cookie = await _load_api(ev.user_id, ev.bot_id)
        if bind is None or cookie is None:
            await bot.send("还没有绑定。发送 Lpass绑定 查看绑定方式。", at_sender=True)
            return
        if cookie.expired:
            await bot.send("登录态已过期，请重新发送 Lpass绑定。", at_sender=True)
            return
        await bot.send(
            "开始执行日常：签到 → 好友请求/分享 → 领取，约需 1~2 分钟，请稍候。", at_sender=True)
        try:
            async with LPassAPI(cookie) as api:
                friend = None
                if not dry_run:
                    # 好友请求要在页面里由 SDK 签名，先尽力跑一次；失败不影响后面
                    try:
                        friend = await send_friend_requests(cookie, api, count=3,
                                                            headless=True, timeout_s=120)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"[Lpass] 好友自动化跳过 error_type={type(exc).__name__}")
                result = await DailyRunner(api, dry_run=dry_run).run()
                if friend is not None and friend.confirmed:
                    result.notes.append(f"好友请求：{friend.summary()}")
        except LPassAuthError:
            await bot.send("登录态已失效，请重新发送 Lpass绑定。", at_sender=True)
            return
        except LPassError as exc:
            await bot.send(f"日常执行中断：{exc}", at_sender=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Lpass] 日常执行异常 error_type={type(exc).__name__}")
            await bot.send("日常执行出现内部异常，未确认完成，请稍后重试。", at_sender=True)
            return
        await bot.send(render(result), at_sender=True)
