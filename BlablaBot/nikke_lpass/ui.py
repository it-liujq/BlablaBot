"""好友抽屉的浏览器侧操作（Playwright）。

为什么必须走浏览器：``/v2/friend/*`` 需要 ``sig``，签名在页面里的腾讯 Chaos VM 内生成，
纯 HTTP 复算会被判 1008。这里复用官方页面：注入 Cookie + ``lip_login_meta``，
点任务行的 GO 打开抽屉，再按行坐标点右侧「+」按钮。

实测要点（2026-11-05）：
- 只塞 Cookie 不够：``localStorage.lip_login_meta`` 里没有 openid/token 时，
  前端直接 ``[socialSystem] missing openid or token``，抽屉永远打不开。
- 抽屉里的「+」是图片按钮，DOM 结构不稳定；按「名字元素 → 行右侧内缩 24px」点更稳。
- 点一次 = 一条 ``lip_batch_create_invitation``，响应 ``lists[].ret``：0 成功，
  40001 已是好友，40003 已申请过，40011 次数用尽。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    from gsuid_core.logger import logger
except Exception:  # 离线测试环境
    import logging
    logger = logging.getLogger("BlablaBot.lpass")

from .api import LPassAPI, LPassCookie, LPassError, task_completed, task_name

REWARDS_URL = ("https://pass.levelinfinite.com/rewards?lang=zh"
               "&points=%2Fpoints%2Fsign-in")
FRIEND_TASK_HINT = "好友请求"
DRAWER_HINT = "推荐好友"


@dataclass
class FriendUIResult:
    opened: bool = False
    clicked: int = 0
    confirmed: int = 0
    names: list[str] = field(default_factory=list)
    share_done: bool = False
    share_link: str = ""
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        parts = []
        if not self.opened:
            parts.append("好友抽屉未打开")
        else:
            parts.append(f"点击 {self.clicked} 次，站点确认 {self.confirmed} 次")
        if self.share_done:
            parts.append("分享交友链接已触发")
        return "；".join(parts)


def _jar(cookie: LPassCookie) -> list[dict]:
    items = [
        ("lip_openid", cookie.openid), ("lip_token", cookie.token),
        ("lip_uid", cookie.uid), ("lip_channelid", cookie.channel_id),
        ("lip_adult_status", cookie.adult_status), ("lip_user_name", cookie.user_name),
        ("lip_expire_time", str(cookie.expire_at or "")),
    ]
    return [{"name": k, "value": v, "domain": ".levelinfinite.com", "path": "/"}
            for k, v in items if v]


def _meta(cookie: LPassCookie) -> dict:
    """前端 ``saveLocalStorage`` 存的那份登录态；缺了社交系统不可用。"""
    return {
        "openid": cookie.openid,
        "token": cookie.token,
        "user_name": cookie.user_name,
        "email": "",
        "token_expire_time": cookie.expire_at,
        "channel_info": {"channelId": int(cookie.channel_id or 131), "account": ""},
        "channelId": int(cookie.channel_id or 131),
        "ret": 0,
        "extra_json": {"get_status_rsp": {"adult_check_status": 1, "region": "sg"}},
    }


# 奖励中心跑在 wujie 的 shadow DOM 里，document.querySelectorAll 穿不透，
# 所有查询都先递归收集节点（含 shadowRoot）。
COLLECT_JS = """
const collectAll = (root, out) => {
  for (const el of root.querySelectorAll('*')) {
    out.push(el);
    if (el.shadowRoot) collectAll(el.shadowRoot, out);
  }
  return out;
};
"""

CLICK_LABEL_JS = "label => { " + COLLECT_JS + """
  const all = collectAll(document, []);
  const hits = all.filter(e => (e.textContent || '').trim() === label);
  if (!hits.length) return 'none';
  hits.sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width);
  const target = hits[0].closest('button') || hits[0];
  target.click();
  return 'ok';
}
"""

CLICK_GO_JS = "keyword => { " + COLLECT_JS + """
  const all = collectAll(document, []);
  const leaves = all.filter(
    e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.includes(keyword)));
  let node = leaves[0];
  for (let i = 0; i < 10 && node; i++) {
    const go = [...node.querySelectorAll('*')].find(
      x => x.textContent.trim() === 'GO' && x.children.length === 0);
    if (go) { go.click(); return 'ok'; }
    node = node.parentElement;
  }
  return 'no-go';
}
"""

ROWS_JS = "hint => { " + COLLECT_JS + """
  const all = collectAll(document, []);
  const anchor = all.find(e => e.children.length === 0
                                && (e.textContent || '').trim() === hint);
  if (!anchor) return [];
  let panel = anchor;
  for (let i = 0; i < 6 && panel.parentElement; i++) panel = panel.parentElement;
  const box = panel.getBoundingClientRect();
  const rows = [];
  for (const el of panel.querySelectorAll('*')) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (!t || t.length > 24 || t === hint) continue;
    if (!/^[A-Za-z0-9_.-]+$/.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 8) continue;
    if (r.right > box.right + 4) continue;
    rows.push({name: t, y: r.y + r.height / 2, right: box.right});
  }
  return rows.slice(0, 12);
}
"""

XPATH_GO = ("xpath=ancestor::div[.//*[normalize-space()='GO']][1]"
            "//*[normalize-space()='GO']")


async def _click_confirm(page) -> bool:
    """关掉「恭喜」弹窗；它挂在 teleporter 里，会挡住后续点击。"""
    loc = page.get_by_text("确认", exact=True)
    try:
        count = await loc.count()
        if not count:
            return False
        await loc.nth(count - 1).click(force=True, timeout=3000)
    except Exception:
        await page.evaluate(CLICK_LABEL_JS, "确认")
    await page.wait_for_timeout(2000)
    return True


async def _click_go(page, keyword: str = FRIEND_TASK_HINT) -> bool:
    """点任务行的 GO：先用定位器，被遮挡时退回坐标点击。"""
    row = page.get_by_text(keyword, exact=False).first
    if not await row.count():
        return False
    button = row.locator(XPATH_GO).last
    try:
        await button.click(force=True, timeout=6000)
        return True
    except Exception:
        pass
    try:
        box = await button.bounding_box()
    except Exception:
        box = None
    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return True
    return False


CAPTURE_COPY_JS = r"""
() => {
  if (window.__copiedHooked) return;
  window.__copiedHooked = true;
  window.__copied = [];
  const push = (t) => { if (t) window.__copied.push(String(t)); };
  try {
    const clip = navigator.clipboard;
    if (clip && clip.writeText) {
      const orig = clip.writeText.bind(clip);
      clip.writeText = (t) => { push(t); return orig(t); };
    }
  } catch (e) {}
  document.addEventListener('copy', () => {
    try { push(window.getSelection().toString()); } catch (e) {}
  }, true);
}
"""


COPY_INVITE_JS = r"""
() => {
  const collect = (root, out) => {
    for (const el of root.querySelectorAll('*')) { out.push(el); if (el.shadowRoot) collect(el.shadowRoot, out); }
    return out;
  };
  const all = collect(document, []);
  const anchor = all.find(e => (e.textContent || '').includes('添加我为'));
  if (!anchor) return null;
  let row = anchor;
  for (let i = 0; i < 4 && row.parentElement; i++) {
    const r = row.getBoundingClientRect();
    if (r.width > 220 && r.height > 30) break;
    row = row.parentElement;
  }
  const r = row.getBoundingClientRect();
  return {x: r.right - 16, y: r.y + r.height / 2};
}
"""


INVITE_URL_JS = r"""
() => {
  const collect = (root, out) => {
    for (const el of root.querySelectorAll('*')) { out.push(el); if (el.shadowRoot) collect(el.shadowRoot, out); }
    return out;
  };
  const all = collect(document, []);
  const anchor = all.find(e => (e.textContent || '').includes('添加我为'));
  const scope = anchor ? (anchor.closest('.ant-drawer') || anchor.parentElement) : document.body;
  const text = scope ? (scope.textContent || '') : '';
  const m = text.match(/https?:\/\/[^\s"'\]\[<>)]*searchUser[^\s"'\]\[<>)]*/);
  if (m) return m[0];
  return '';
}
"""


async def _copy_invite_link(page) -> str:
    """点邀请文本右侧的复制图标，并从文本里取出链接；剪贴板/DOM 兜底。

    实测结构（2026-11-07）：
        <div class="_1p2ts9tj">
          <div class="_1p2ts9tl">添加我为…[https://pass.levelinfinite.com?action=searchUser&searchUserName=xx]</div>
          <span class="anticon _1p2ts9tm"><svg .../></span>   ← 复制按钮
        </div>
    """
    url = ""
    anchor = None
    for scope in [page] + list(_friend_frames(page)):
        try:
            cand = scope.get_by_text("添加我为", exact=False).first
            if await cand.count():
                anchor = cand
                break
        except Exception:  # noqa: BLE001
            continue
    try:
        if anchor is None:
            raise LookupError("no anchor")
        text = await anchor.inner_text()
        match = re.search(r"https?://[^\s\]]*searchUser[^\s\]]*", text)
        if match:
            url = match.group(0)
    except Exception:  # noqa: BLE001
        anchor = None
    if anchor is None:
        return ""

    if anchor is not None:
        for sel in ("xpath=following-sibling::*[contains(@class,'anticon')]",
                    "xpath=..//*[contains(@class,'anticon')]"):
            try:
                await anchor.locator(sel).first.click(timeout=5000)
                break
            except Exception:  # noqa: BLE001
                continue

    if url:
        return url

    for _ in range(4):
        try:
            dom = await page.evaluate(INVITE_URL_JS)
        except Exception:  # noqa: BLE001
            dom = ""
        if dom and "http" in str(dom):
            return str(dom).strip()
        try:
            copied = await page.evaluate("window.__copied || []")
        except Exception:  # noqa: BLE001
            copied = []
        for item in copied:
            if item and "http" in str(item):
                return str(item).strip()
        try:
            clip = await page.evaluate("navigator.clipboard.readText()")
        except Exception:  # noqa: BLE001
            clip = ""
        if clip and "http" in str(clip):
            return str(clip).strip()
        await page.wait_for_timeout(800)
    return ""


def _friend_frames(page):
    """好友抽屉实际渲染在 /friend 子 frame 里，优先用它。"""
    try:
        frames = [f for f in page.frames if "/friend" in (f.url or "")]
    except Exception:  # noqa: BLE001
        frames = []
    return frames or [page.main_frame]


ADD_BTN_JS = r"""
() => {
  const walk = (root, out) => {
    for (const el of root.querySelectorAll('*')) {
      out.push(el);
      if (el.shadowRoot) walk(el.shadowRoot, out);
    }
    return out;
  };
  const all = walk(document, []);
  const out = [];
  const seen = new Set();
  for (const el of all) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (t.length < 3 || t.length > 24 || /\s/.test(t)) continue;
    if (!/^[A-Za-z0-9_.-]+$/.test(t)) continue;
    let p = el.parentElement, icon = null;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const found = p.querySelectorAll('span.anticon');
      if (found.length === 1) { icon = found[0]; break; }
      if (found.length > 1) break;
    }
    if (!icon || seen.has(t)) continue;
    const r = icon.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    seen.add(t);
    out.push({name: t, x: r.x + r.width / 2, y: r.y + r.height / 2});
  }
  return out;
}
"""


async def _add_buttons(page) -> list[dict]:
    """返回推荐好友每行的 {name, x, y}（x/y 为加号图标的页面坐标）。"""
    for frame in _friend_frames(page):
        try:
            items = await frame.evaluate(ADD_BTN_JS)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(items, list) and items:
            return [i for i in items if isinstance(i, dict)]
    return []


async def _wait_drawer(page, timeout_s: float) -> bool:
    """抽屉真正可用的唯一标志：能列出带「+」的推荐好友。

    注意：页面上的「添加好友/推荐好友」文字在抽屉打开前就存在，
    不能作为判定依据，否则会过早返回、随后按钮数为 0。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if await _add_buttons(page):
            return True
        await page.wait_for_timeout(1000)
    return False


async def _await_buttons(page, timeout_s: float = 12.0) -> list[dict]:
    """在超时内轮询，直到能枚举出加号按钮。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        buttons = await _add_buttons(page)
        if buttons:
            return buttons
        await page.wait_for_timeout(800)
    return []


MARK_ICON_JS = r"""
(name) => {
  const walk = (root, out) => {
    for (const el of root.querySelectorAll('*')) { out.push(el); if (el.shadowRoot) walk(el.shadowRoot, out); }
    return out;
  };
  const all = walk(document, []);
  const target = all.find(e => e.children.length === 0 && (e.textContent || '').trim() === name);
  if (!target) return false;
  let p = target.parentElement, icon = null;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
    const found = p.querySelectorAll('span.anticon');
    if (found.length === 1) { icon = found[0]; break; }
    if (found.length > 1) break;
  }
  if (!icon) return false;
  document.querySelectorAll('[data-lpass-icon]').forEach(e => e.removeAttribute('data-lpass-icon'));
  icon.setAttribute('data-lpass-icon', '1');
  return true;
}
"""


async def _click_add_buttons(page, count: int) -> list[str]:
    """点推荐好友每行右侧的加号图标（span.anticon）。

    先用坐标点（实测有效），再补一次 locator 点击兜底；
    每次点完重新枚举，避免列表刷新后索引错位。
    """
    clicked: list[str] = []
    if count <= 0:
        return clicked
    for _ in range(count):
        items = await _await_buttons(page, 12)
        pending = [i for i in items if i.get("name") not in clicked]
        if not pending:
            break
        item = pending[0]
        name = str(item["name"])
        ok = False
        try:
            await page.mouse.click(float(item["x"]), float(item["y"]))
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            for frame in _friend_frames(page):
                try:
                    marked = await frame.evaluate(MARK_ICON_JS, name)
                    if marked:
                        await frame.locator("[data-lpass-icon='1']").click(timeout=4000)
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    continue
        clicked.append(name)
        await page.wait_for_timeout(2200)
    return clicked


async def _list_add_buttons(page) -> list[str]:
    return [str(i.get("name")) for i in await _add_buttons(page)]


async def send_friend_requests(cookie: LPassCookie, api: LPassAPI, *, count: int = 3,
                               headless: bool = True, timeout_s: int = 120,
                               dry_run: bool = False) -> FriendUIResult:
    """登录态 → 打开好友抽屉 → 点 count 次「+」→ 必要时触发分享 → 接口复查。"""
    result = FriendUIResult()
    try:
        from playwright.async_api import async_playwright
    except Exception:
        result.error = "当前部署缺少 Playwright，好友任务需人工"
        return result

    state = await _friend_state(api)
    need_friend = state is not None and not state["completed"]
    need_share = await _share_pending(api)
    if not need_friend and not need_share:
        result.error = "今日好友请求与分享任务都已完成，无需再打开页面"
        return result
    before = None if state is None else state["times"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(locale="zh-CN",
                                                viewport={"width": 1600, "height": 1000})
            await context.add_cookies(_jar(cookie))
            page = await context.new_page()
            await page.add_init_script(script=f"({CAPTURE_COPY_JS})()")
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            await page.evaluate(
                "meta => localStorage.setItem('lip_login_meta', JSON.stringify(meta))",
                _meta(cookie))
            try:
                await page.evaluate(CAPTURE_COPY_JS)
            except Exception:  # noqa: BLE001
                pass
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(10000)
            try:
                await page.get_by_role("button", name="接受所有可选 cookies").first.click(
                    timeout=2000)
            except Exception:
                pass
            for _ in range(4):
                if not await page.get_by_text("确认", exact=True).count():
                    break
                await page.evaluate(CLICK_LABEL_JS, "确认")
                await page.wait_for_timeout(1200)

            # 任务列表与「恭喜」弹窗都是延迟渲染的，重试几轮再判定失败
            keyword = FRIEND_TASK_HINT if need_friend else "分享交友链接"
            clicked_go = False
            for _ in range(10):
                if await page.get_by_text("确认", exact=True).count():
                    await _click_confirm(page)
                    continue
                if await _click_go(page, keyword):
                    clicked_go = True
                    break
                await page.wait_for_timeout(2500)
            if not clicked_go:
                result.error = "没有找到好友/分享任务的 GO 按钮"
                return result
            result.opened = await _wait_drawer(page, min(30, timeout_s))
            if not result.opened:
                result.error = "好友抽屉未打开（可能是登录态或页面版本变化）"
                logger.warning("[Lpass] 好友抽屉未打开 keyword=%s", keyword)
                return result
            logger.info("[Lpass] 好友抽屉已打开 keyword=%s need_friend=%s need_share=%s",
                        keyword, need_friend, need_share)

            if need_friend:
                if dry_run:
                    result.names = (await _list_add_buttons(page))[:count]
                    result.clicked = len(result.names)
                else:
                    result.names = await _click_add_buttons(page, count)
                    result.clicked = len(result.names)
                logger.info("[Lpass] 好友点击 names=%s clicked=%s",
                            result.names[:5], result.clicked)
                if not dry_run and result.clicked == 0:
                    buttons = await _add_buttons(page)
                    result.error = ("没找到可点的加号按钮（抽屉可能未加载完）"
                                    f"，当前可见按钮数={len(buttons)}")
                    logger.warning("[Lpass] 未找到加号按钮 buttons=%s", len(buttons))
            if need_share:
                for _ in range(3):
                    try:
                        await page.get_by_text("分享邀请链接", exact=False).first.click(
                            force=True, timeout=6000)
                        break
                    except Exception:  # noqa: BLE001
                        await page.wait_for_timeout(1500)
                await page.wait_for_timeout(2000)
                share_url = await _copy_invite_link(page)
                if not share_url:
                    try:
                        share_url = await page.evaluate(INVITE_URL_JS)
                    except Exception:  # noqa: BLE001
                        share_url = ""
                if not share_url:
                    share_url = ("https://pass.levelinfinite.com?action=searchUser"
                                 f"&searchUserName={cookie.user_name}")
                result.share_link = str(share_url or "")[:300]
                logger.info("[Lpass] 分享链接 captured=%s", bool(result.share_link))
        finally:
            await browser.close()

    if result.share_link:
        await open_link_anonymous(result.share_link, headless=headless)
    after = await _friend_progress(api)
    if before is not None and after is not None:
        result.confirmed = max(0, after - before)
    if need_share:
        result.share_done = not await _share_pending(api)
    # 站点计数有延迟：最多等 60 秒确认，避免报告里显示旧进度
    if need_friend and result.clicked and (before is not None) and result.confirmed == 0:
        for _ in range(6):
            state = await _friend_state(api)
            if state is not None and state["completed"]:
                result.confirmed = max(0, state["times"] - before)
                break
            await asyncio.sleep(10)
    logger.info("[Lpass] 好友复查 before=%s after=%s confirmed=%s share_done=%s",
                before, after, result.confirmed, result.share_done)
    if not need_share:
        result.share_done = True
    return result


async def _share_pending(api: LPassAPI) -> bool:
    """「分享交友链接到社交媒体」是否还没完成。"""
    try:
        tasks = await api.tasks()
    except LPassError:
        return False
    task = next((t for t in tasks
                 if "分享交友" in task_name(t) or "share" in task_name(t).lower()), None)
    if task is None:
        return False
    return task_completed(task) is not True


async def open_link_anonymous(url: str, *, wait_ms: int = 12000,
                               headless: bool = True, incognito: bool = True) -> bool:
    """用全新的无痕窗口（--incognito，无任何 Cookie）打开外链。"""
    if not url or not url.startswith("http"):
        return False
    if os.environ.get("LPASS_NO_BROWSER") == "1":
        return False
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001
        return False
    try:
        async with async_playwright() as p:
            args = ["--incognito"] if incognito else []
            browser = await p.chromium.launch(headless=headless, args=args)
            try:
                ctx = await browser.new_context(locale="zh-CN")
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(wait_ms)
            finally:
                await browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _friend_state(api: LPassAPI) -> Optional[dict]:
    """好友请求任务的 (completed, times, need)；找不到任务返回 None。"""
    try:
        tasks = await api.tasks()
    except LPassError:
        return None
    task = next((t for t in tasks
                 if "好友请求" in task_name(t) or "friend request" in task_name(t).lower()),
                None)
    if task is None:
        return None
    infos = task.get("reward_infos") or [{}]
    first = infos[0] if isinstance(infos[0], dict) else {}
    try:
        times = int(first.get("completed_times", 0) or 0)
        need = int(first.get("need_completed_times", 0) or 0)
    except (TypeError, ValueError):
        times, need = 0, 0
    completed = task_completed(task) is True or (need and times >= need)
    return {"completed": completed, "times": max(times, need) if completed else times,
            "need": need}


async def _friend_progress(api: LPassAPI) -> Optional[int]:
    state = await _friend_state(api)
    return None if state is None else state["times"]


__all__ = ["FriendUIResult", "send_friend_requests"]
