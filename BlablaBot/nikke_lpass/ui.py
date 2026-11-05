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

import time
from dataclasses import dataclass, field
from typing import Optional

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


async def _wait_drawer(page, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if await page.get_by_text(DRAWER_HINT, exact=True).count():
            return True
        await page.wait_for_timeout(800)
    return False


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
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            await page.evaluate(
                "meta => localStorage.setItem('lip_login_meta', JSON.stringify(meta))",
                _meta(cookie))
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
                return result

            if need_friend:
                rows = await page.evaluate(ROWS_JS, DRAWER_HINT)
                rows = rows if isinstance(rows, list) else []
                result.names = [r.get("name", "") for r in rows]
                for row in rows[: max(0, count)]:
                    if dry_run:
                        result.clicked += 1
                        continue
                    await page.mouse.click(row["right"] - 24, row["y"])
                    result.clicked += 1
                    await page.wait_for_timeout(1500)
            if need_share:
                # 打开抽屉后站点就把「分享交友链接」记账，再补一次分享按钮更稳
                try:
                    await page.get_by_text("分享邀请链接", exact=False).first.click(
                        force=True, timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(4000)
        finally:
            await browser.close()

    after = await _friend_progress(api)
    if before is not None and after is not None:
        result.confirmed = max(0, after - before)
    if need_share:
        result.share_done = not await _share_pending(api)
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
