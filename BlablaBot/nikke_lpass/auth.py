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


CAPTCHA_BOX_JS = r"""
() => {
  const walk = (root, out) => {
    for (const el of root.querySelectorAll('*')) { out.push(el); if (el.shadowRoot) walk(el.shadowRoot, out); }
    return out;
  };
  const all = walk(document, []);
  const sels = ['#tcaptcha_iframe', '.tcaptcha-transform', 'iframe[src*="captcha"]', '[id*="tcaptcha"]'];
  for (const sel of sels) {
    const el = all.find(e => { try { return e.matches && e.matches(sel); } catch (x) { return false; } });
    if (el) {
      const r = el.getBoundingClientRect();
      if (r.width > 150 && r.height > 80) return {x: r.x, y: r.y, width: r.width, height: r.height};
    }
  }
  const ifr = all.find(e => e.tagName === 'IFRAME' && (e.src || '').toLowerCase().includes('captcha'));
  if (ifr) {
    const r = ifr.getBoundingClientRect();
    if (r.width > 150 && r.height > 80) return {x: r.x, y: r.y, width: r.width, height: r.height};
  }
  return null;
}
"""


async def _captcha_box(page):
    """返回验证码组件在页面上的实际矩形（CSS 像素）。"""
    # 1) Playwright 定位器（可穿透 shadow DOM）
    for sel in ("#tcaptcha_iframe", ".tcaptcha-transform", "iframe[src*='captcha']"):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                box = await loc.bounding_box()
                if box and 150 < box.get("width", 0) <= 900 and 80 < box.get("height", 0) <= 900:
                    return {"x": box["x"], "y": box["y"],
                            "width": box["width"], "height": box["height"]}
        except Exception:  # noqa: BLE001
            continue
    # 2) DOM 选择器（最准确）
    try:
        box = await page.evaluate(CAPTCHA_BOX_JS)
        if box and 150 < box.get("width", 0) <= 900 and 80 < box.get("height", 0) <= 900:
            return box
    except Exception:  # noqa: BLE001
        pass
    # 再试真正的 captcha 第三方 frame（排除 pass 主站自身）
    try:
        for fr in page.frames:
            url = (fr.url or "").lower()
            if "captcha" not in url or "pass.levelinfinite.com" in url:
                continue
            try:
                el = await fr.frame_element()
                box = await el.bounding_box()
            except Exception:  # noqa: BLE001
                box = None
            if not box:
                continue
            w, h = box.get("width", 0), box.get("height", 0)
            if 150 < w <= 900 and 80 < h <= 900:
                return {"x": box["x"], "y": box["y"], "width": w, "height": h}
    except Exception:  # noqa: BLE001
        pass
    return None


async def _captcha_present(page_or_ctx) -> bool:
    page = getattr(page_or_ctx, "page", page_or_ctx)
    try:
        pages = getattr(page_or_ctx, "pages", None) or [page]
    except Exception:  # noqa: BLE001
        pages = [page]
    for p in pages:
        try:
            if await _captcha_box(p):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


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


class LoginFlow:
    """可交互的网页登录：遇到人机验证/邮箱验证码时把界面转交给绑定页。

    生命周期由 routes 持有；挑战阶段浏览器保持打开，用户提交后继续登录。
    """

    def __init__(self, email: str, password: str, *, timeout: int = 600):
        self.email, self.password = email, password
        self.status = "logging_in"       # logging_in | captcha | email_code | success | failed
        self.message = ""
        self.error = ""
        self.cookie: Optional[LPassCookie] = None
        self.captcha_png = ""            # data:image/png;base64,...
        self.captcha_width = 0
        self.captcha_attempts = 0
        self._deadline = time.time() + max(120, int(timeout))
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._box = None
        self._drag_from = None

    async def close(self):
        try:
            if self.browser is not None:
                await self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.browser = self.context = self.page = None
        self._pw = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except Exception:  # noqa: BLE001
            await self._fail("当前部署缺少 Playwright，请改用 Cookie 绑定。")
            return
        try:
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(headless=True)
            self.context = await self.browser.new_context(
                locale="zh-CN", viewport={"width": 1500, "height": 950})
            page = self.page = await self.context.new_page()
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(9000)
            for label in ("接受所有可选 cookies", "接受全部", "Accept all"):
                try:
                    await page.get_by_role("button", name=label).first.click(timeout=2500)
                    break
                except Exception:  # noqa: BLE001
                    continue
            await _open_login_modal(page)
            await page.get_by_text(MODAL_ENTRY, exact=True).first.click(timeout=15000)
            await page.wait_for_timeout(2500)
            await page.get_by_placeholder("邮箱地址").first.fill(self.email, timeout=15000)
            await page.get_by_placeholder("密码").first.fill(self.password, timeout=15000)
            if not await _submit(page):
                raise LoginError("没找到登录提交按钮，请改用 Cookie 绑定。")
            await page.wait_for_timeout(4000)
            await self._after_submit()
        except LoginError as exc:
            await self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            await self._fail("登录过程异常，请稍后重试或改用 Cookie。")
            _ = exc

    async def _fail(self, message: str):
        self.status = "failed"
        self.error = message
        await self.close()

    async def _find_code_input(self):
        if self.page is None:
            return None
        for placeholder in ("邮箱验证码", "验证码"):
            try:
                loc = self.page.get_by_placeholder(placeholder).first
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _after_submit(self):
        """提交后：优先处理验证码/邮箱码，否则等待登录态。"""
        if self.status in ("success", "failed"):
            return
        if await _captcha_present(self.context):
            await self._capture_captcha()
            return
        if await self._find_code_input() is not None:
            self.status = "email_code"
            self.message = "请输入邮箱收到的验证码"
            return
        remaining = max(10.0, min(120.0, self._deadline - time.time()))
        cookie = await _wait_lip_cookie(self.context, timeout=remaining)
        if cookie is not None:
            self.cookie = cookie
            self.status = "success"
            await self.close()
            return
        if await _captcha_present(self.context):
            await self._capture_captcha()
            return
        if await self._find_code_input() is not None:
            self.status = "email_code"
            self.message = "请输入邮箱收到的验证码"
            return
        await self._fail("登录未完成：可能需要人机验证或邮箱验证码，请重试或改用 Cookie 绑定。")

    async def _capture_captcha(self):
        page = self.page
        if page is None:
            await self._fail("验证码窗口读取失败，请重试或改用 Cookie。")
            return
        box = await _captcha_box(page)
        if not box:
            await self._fail("没有找到人机验证窗口，请重试或改用 Cookie。")
            return
        try:
            shot = await page.screenshot(clip={
                "x": box["x"], "y": box["y"],
                "width": box["width"], "height": box["height"]})
        except Exception:  # noqa: BLE001
            shot = b""
        if not shot:
            await self._fail("验证码截图失败，请重试或改用 Cookie。")
            return
        import base64
        self.captcha_png = "data:image/png;base64," + base64.b64encode(shot).decode()
        self._box = box
        self.captcha_width = box["width"]
        self.status = "captcha"
        self.message = "点选图中正确的位置（拼图类则拖动滑块），然后点「提交验证」"

    async def submit_captcha(self, dx: float, points=None):
        if self.status != "captcha" or self.page is None or not self._box:
            return
        self.captcha_attempts += 1
        box = self._box
        try:
            if points:
                # 点选类验证码：按比例点击用户选择的图片位置
                for pt in points[:6]:
                    x = box["x"] + float(pt.get("x", 0)) * box["width"]
                    y = box["y"] + float(pt.get("y", 0)) * box["height"]
                    await self.page.mouse.click(x, y)
                    await self.page.wait_for_timeout(400)
                # 点右下角「确定」
                await self.page.mouse.click(box["x"] + box["width"] * 0.88,
                                            box["y"] + box["height"] * 0.93)
            else:
                from ..nikke_login.browser import _human_drag
                sy, sx = box["height"] - 24, 40
                await _human_drag(self.page, box["x"] + sx, box["y"] + sy, float(dx))
        except Exception:  # noqa: BLE001
            await self._fail("验证码操作失败，请重试或改用 Cookie。")
            return
        await self.page.wait_for_timeout(3000)
        if await _captcha_present(self.context):
            if self.captcha_attempts >= 4:
                await self._fail("人机验证多次未通过，请改用 Cookie 绑定。")
                return
            await self._capture_captcha()
            return
        self.status = "logging_in"
        await self._after_submit()

    async def submit_email_code(self, code: str):
        if self.status != "email_code" or self.page is None:
            return
        loc = await self._find_code_input()
        if loc is None:
            await self._fail("没有找到验证码输入框，请重试或改用 Cookie。")
            return
        try:
            await loc.fill(code.strip(), timeout=8000)
            await _submit(self.page)
        except Exception:  # noqa: BLE001
            await self._fail("验证码提交失败，请重试或改用 Cookie。")
            return
        await self.page.wait_for_timeout(4000)
        self.status = "logging_in"
        await self._after_submit()


__all__ = ["LPassAuthError", "LoginError", "LoginFlow", "login_with_password", "verify"]
