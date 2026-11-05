"""LIP 登录态获取：Cookie 导入与账号密码登录（Playwright）。"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Optional

from .api import LPassAPI, LPassAuthError, LPassCookie, LPassError, parse_cookie

REWARDS_URL = "https://pass.levelinfinite.com/rewards?lang=zh&points=%2Fpoints%2Fsign-in"
LOGIN_ENTRY = "登录以获得"


class LoginError(Exception):
    """登录流程失败，消息可直接展示。"""


async def verify(cookie: LPassCookie, *, user_agent: Optional[str] = None) -> dict:
    """用金币接口验证登录态，成功时返回账号摘要。"""
    kwargs = {"user_agent": user_agent} if user_agent else {}
    async with LPassAPI(cookie, **kwargs) as api:
        # 前端登录后第一件事是建会话；失败不致命，奖励接口仍靠 Cookie。
        try:
            await api.open_session()
        except LPassError:
            pass
        points = await api.points()
        try:
            info = await api.user_info()
        except LPassError:
            info = {}
    nickname = (
        str(info.get("username") or info.get("user_name") or "")
        or cookie.user_name
    )
    return {
        "nickname": nickname,
        "points": points.get("total", 0),
        "openid": cookie.openid,
    }


async def login_with_password(email: str, password: str, *,
                              timeout: int = 120,
                              on_step: Optional[Callable[[str], None]] = None) -> LPassCookie:
    """用账号密码在网页端登录，回捞 lip_* Cookie。

    只在 Playwright 可用时调用；出现人机验证时抛 LoginError，提示改用 Cookie 绑定。
    """
    try:
        from playwright.async_api import async_playwright
    except Exception:
        raise LoginError("当前部署缺少 Playwright，请改用 Cookie 绑定。") from None

    def step(text: str):
        if on_step is not None:
            try:
                on_step(text)
            except Exception:
                pass

    deadline = time.time() + max(30, int(timeout))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(locale="zh-CN")
            page = await context.new_page()
            step("打开奖励中心")
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            for label in ("接受所有可选 cookies", "接受全部", "Accept all"):
                try:
                    await page.get_by_role("button", name=label).first.click(timeout=2500)
                    break
                except Exception:
                    continue
            step("展开登录框")
            try:
                await page.get_by_text(LOGIN_ENTRY).locator("..").click(timeout=15000)
            except Exception:
                await page.get_by_text("登录", exact=True).first.click(timeout=15000)
            step("切换密码登录")
            await page.get_by_text("密码登录", exact=True).first.click(timeout=15000)
            step("填写邮箱与密码")
            await page.get_by_placeholder("邮箱地址").first.fill(email, timeout=15000)
            await page.get_by_placeholder("密码").first.fill(password, timeout=15000)
            step("提交登录")
            await page.get_by_role("button").filter(
                has_text="登录", has_not_text="邮箱").first.click(timeout=15000)
            # 常见的人机验证：腾讯滑块
            try:
                await page.wait_for_selector("#tcaptcha_iframe, .tcaptcha-transform, drag_ele_global",
                                             timeout=6000)
                raise LoginError("登录触发人机验证，请在网页登录后再用 Cookie 绑定。")
            except LoginError:
                raise
            except Exception:
                pass
            step("等待登录完成")
            remaining = max(5, int(deadline - time.time()))
            await page.get_by_text(LOGIN_ENTRY).first.wait_for(state="hidden",
                                                               timeout=remaining * 1000)
            station = await context.cookies("https://pass.levelinfinite.com")
            merged = "; ".join(f"{c['name']}={c['value']}" for c in station)
            cookie = parse_cookie(merged)
            if cookie is None:
                raise LoginError("登录成功但未取到 LIP 登录态，请改用 Cookie 绑定。")
            return cookie
        finally:
            await browser.close()


__all__ = ["LPassAuthError", "LoginError", "login_with_password", "verify"]
