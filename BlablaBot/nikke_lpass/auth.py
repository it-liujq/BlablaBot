"""LIP 登录态获取：Cookie 导入与账号密码登录（Playwright）。

登录入口实测（2026-11-07，pass.levelinfinite.com/rewards）：
- 页面右上角头像 → 弹窗默认是「邮箱 + 验证码」注册/登录；
- 点弹窗里的「密码登录」才切到 邮箱/密码 两个输入框，提交按钮文案是「注册/登录」；
- 提交后可能弹腾讯 TCaptcha（iframe 域名含 captcha），需要拖拼图。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Optional

from .api import LPassAPI, LPassAuthError, LPassCookie, LPassError, parse_cookie

REWARDS_URL = "https://pass.levelinfinite.com/rewards?lang=zh&points=%2Fpoints%2Fsign-in"
MODAL_ENTRY = "密码登录"
SUBMIT_TEXT = "登录"


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


SUBMIT_JS = """
() => {
  const hits = [...document.querySelectorAll('*')].filter(
    e => e.children.length === 0 && (e.textContent || '').trim() === '登录');
  if (!hits.length) return null;
  let best = null, bestW = 0;
  for (const el of hits) {
    const btn = el.closest('button') || el.parentElement || el;
    const r = btn.getBoundingClientRect();
    if (r.width > bestW) { bestW = r.width; best = btn; }
  }
  if (!best || bestW < 120) return null;
  const r = best.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width};
}
"""


async def _submit(page) -> bool:
    """密码表单的提交按钮文案是「登录」；按最宽的候选点击，避开「邮箱登录」。"""
    box = None
    try:
        box = await page.evaluate(SUBMIT_JS)
    except Exception:  # noqa: BLE001
        box = None
    if isinstance(box, dict):
        try:
            await page.mouse.click(float(box["x"]), float(box["y"]))
            return True
        except Exception:  # noqa: BLE001
            pass
    try:
        await page.get_by_text(SUBMIT_TEXT, exact=True).first.click(timeout=6000)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _open_login_modal(page) -> None:
    """点右上角头像打开登录弹窗（坐标按视口宽度推算，避开 svg 选择器）。"""
    width = page.viewport_size["width"] if page.viewport_size else 1500
    candidates = [(width - 53, 44), (width - 106, 44), (width - 70, 60)]
    for x, y in candidates:
        try:
            await page.mouse.click(x, y)
        except Exception:  # noqa: BLE001
            continue
        try:
            await page.get_by_text(MODAL_ENTRY, exact=True).first.wait_for(timeout=6000)
            return
        except Exception:  # noqa: BLE001
            continue
    raise LoginError("没能打开登录弹窗（页面结构可能变化），请改用 Cookie 绑定。")


async def _captcha_present(ctx) -> bool:
    try:
        from ..nikke_login.browser import _find_captcha
    except Exception:  # noqa: BLE001
        return False
    try:
        _, el, box = await _find_captcha(ctx)
    except Exception:  # noqa: BLE001
        return False
    return el is not None and bool(box)


async def _solve_captcha(ctx, page, *, attempts: int = 4) -> bool:
    """腾讯 TCaptcha 拼图：截图 → 定位缺口 → 拟人拖动。失败返回 False。"""
    try:
        from ..nikke_login.browser import _analyze, _find_captcha, _human_drag
    except Exception:  # noqa: BLE001
        return False

    for _ in range(attempts):
        _, el, box = await _find_captcha(ctx)
        if el is None or not box:
            return False
        try:
            shot = await el.screenshot(scale="css")
            _, target = _analyze(shot)
        except Exception:  # noqa: BLE001
            return False
        if not target:
            return False
        dx, sy, sx = target
        try:
            await _human_drag(page, box["x"] + sx, box["y"] + sy, dx)
        except Exception:  # noqa: BLE001
            return False
        await page.wait_for_timeout(3000)
        _, el2, _ = await _find_captcha(ctx)
        if el2 is None:
            return True
    return False


async def _wait_lip_cookie(context, *, timeout: float) -> Optional[LPassCookie]:
    """轮询 context.cookies，直到出现 lip_token。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        station = await context.cookies("https://pass.levelinfinite.com")
        merged = "; ".join(f"{c['name']}={c['value']}" for c in station)
        cookie = parse_cookie(merged)
        if cookie is not None:
            return cookie
        await asyncio.sleep(2)
    return None


async def login_with_password(email: str, password: str, *,
                              timeout: int = 150,
                              on_step: Optional[Callable[[str], None]] = None) -> LPassCookie:
    """用账号密码在网页端登录，回捞 lip_* Cookie。"""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        raise LoginError("当前部署缺少 Playwright，请改用 Cookie 绑定。") from None

    def step(text: str):
        if on_step is not None:
            try:
                on_step(text)
            except Exception:  # noqa: BLE001
                pass

    deadline = time.time() + max(60, int(timeout))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                locale="zh-CN", viewport={"width": 1500, "height": 950})
            page = await context.new_page()
            step("打开奖励中心")
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(9000)
            for label in ("接受所有可选 cookies", "接受全部", "Accept all"):
                try:
                    await page.get_by_role("button", name=label).first.click(timeout=2500)
                    break
                except Exception:  # noqa: BLE001
                    continue

            step("打开登录弹窗")
            await _open_login_modal(page)
            step("切换密码登录")
            await page.get_by_text(MODAL_ENTRY, exact=True).first.click(timeout=15000)
            await page.wait_for_timeout(2500)

            step("填写邮箱与密码")
            await page.get_by_placeholder("邮箱地址").first.fill(email, timeout=15000)
            await page.get_by_placeholder("密码").first.fill(password, timeout=15000)

            step("提交登录")
            if not await _submit(page):
                raise LoginError("没找到登录提交按钮，请改用 Cookie 绑定。")

            step("处理可能的人机验证")
            await page.wait_for_timeout(4000)
            if await _captcha_present(context):
                if await _solve_captcha(context, page):
                    step("人机验证已通过")
                else:
                    step("人机验证未自动通过")

            step("等待登录完成")
            remaining = max(15.0, deadline - time.time())
            cookie = await _wait_lip_cookie(context, timeout=remaining)
            if cookie is None:
                raise LoginError(
                    "登录未完成：可能需要人机验证或邮箱验证码，"
                    "请在浏览器手动登录后用「#LPass绑定 Cookie内容」。")
            return cookie
        finally:
            await browser.close()


__all__ = ["LPassAuthError", "LoginError", "login_with_password", "verify"]
