"""任务编排：七日金币激励、常规任务、一次性任务。

口径（见 MAINTENANCE.md）：只做"能用接口确证"的事。
- 签到：``DailyCheckIn``，七日进度看 ``task_type=11`` 的 ``sign_today`` / ``reward_infos``。
- 好友请求 / 分享到社媒：动作在 ``sg.intlgame.com``，需要 SDK 签名，标记为需人工。
- 游玩游戏：纯服务端判定，无法代跑。
- 分享 LI PASS：取到分享链接后仍需他人点击，只能算"已生成"。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from .api import (
    _int,
    LPassAPI,
    LPassAuthError,
    LPassError,
    ladder_progress,
    task_block_completed,
    task_completed,
    task_id,
    task_name,
    task_need_times,
    task_points,
)

# 前端 TaskType 枚举（源码可确证）
TASK_TYPE = {
    1: "signin",
    2: "play_game",
    3: "shop",
    4: "share_production",
    5: "follow_social",
    6: "jump_one_jump",
    8: "nikke_two_year",
    9: "lip_store_first_bind",
    10: "master_bind_lip",
    11: "ladder_signin",
    12: "flexible",
    16: "email",
    17: "appointment",
    19: "desktop_shortcut",
    22: "share_link",
    23: "play_jump_infinite",
    26: "recharge_first",
    27: "recharge_after",
    28: "share_quartz",
    30: "nickname",
    31: "share_social",
    32: "friend_requests",
    33: "add_friend",
}

# 名称兜底：接口按语言返回，中英文都覆盖
NAME_HINTS = (
    ("signin", ("签到", "簽到", "sign-in", "sign in")),
    ("ladder_signin", ("7-day", "七日", "七天", "ladder")),
    ("friend_requests", ("好友请求", "好友申請", "好友申请", "friend request")),
    ("add_friend", ("添加", "add friend", "add a friend")),
    ("share_social", ("分享交友", "分享好友", "分享邀請", "share friend")),
    ("play_game", ("游玩游戏", "遊玩遊戲", "play game", "play games")),
    ("nickname", ("昵称", "暱稱", "nickname")),
    ("share_link", ("分享 li pass", "分享lipass", "share li pass")),
    ("follow_social", ("关注", "關注", "follow", "discord", "twitter")),
    ("recharge", ("充值", "首购", "recharge", "purchase")),
)

SIGNIN_KEYS = ("signin", "ladder_signin")
FRIEND_KEYS = ("friend_requests", "add_friend", "share_social")
PLAY_KEYS = ("play_game",)
ONCE_KEYS = ("nickname", "follow_social", "email")
MANUAL_KEYS = ("recharge", "shop", "share_production", "appointment", "desktop_shortcut",
               "email", "flexible", "nikke_two_year", "lip_store_first_bind")

try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 tzdata 时退回固定 +08:00
    from datetime import timedelta, timezone

    TZ = timezone(timedelta(hours=8))
FRIEND_PAGE = "https://pass.levelinfinite.com/friend"
FRIEND_SETTLE_NOTE = "（好友请求奖励约10分钟后结算，未计入上面的差值）"


def classify(task: dict) -> str:
    """把任务归到可执行类别；无法归类返回空串。"""
    raw_type = task.get("task_type", task.get("type"))
    if raw_type is not None:
        try:
            key = TASK_TYPE.get(int(raw_type), "")
            if key:
                return key
        except (TypeError, ValueError):
            pass
    name = task_name(task).lower()
    if not name:
        return ""
    for key, hints in NAME_HINTS:
        if any(h in name for h in hints):
            return key
    return ""


@dataclass
class StepResult:
    key: str
    label: str
    status: str  # done | already | failed | manual | skipped
    detail: str = ""
    points: int = 0


@dataclass
class DailyResult:
    points_before: int = 0
    points_after: int = 0
    steps: list[StepResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    share_link: str = ""
    tasks: list[dict] = field(default_factory=list)
    today_points: list[dict] = field(default_factory=list)
    today_earned: int = 0
    points_earned: int = 0
    signin_day: Optional[int] = None
    ladder_total: int = 0
    dry_run: bool = False

    @property
    def delta(self) -> int:
        return self.points_after - self.points_before

    def add(self, key: str, label: str, status: str, detail: str = "",
            points: int = 0) -> StepResult:
        step = StepResult(key, label, status, detail, points)
        self.steps.append(step)
        return step


class DailyRunner:
    """一次日常的全部动作。任何单步失败都不影响后续步骤。"""

    def __init__(self, api: LPassAPI, *, dry_run: bool = False, clock=time.time,
                 sleep=asyncio.sleep):
        self.api = api
        self.dry_run = dry_run
        self.clock = clock
        self.sleep = sleep

    async def run(self, *, settle: bool = True) -> DailyResult:
        result = DailyResult(dry_run=self.dry_run)
        await self._read_points(result, first=True)
        tasks = await self._load_tasks(result)
        result.tasks = tasks

        await self._signin(tasks, result)
        await self._collect_walkin(tasks, result)
        await self._share_link(tasks, result)
        await self._manual_tasks(tasks, result)
        await self._one_time(tasks, result)

        await self._read_points(result, first=False)
        # 首份报告也要有流水，否则「每日签到」等只能显示未完成
        await self._refresh_points(result)
        if settle and not self.dry_run:
            await self.settle(result)
        return result

    # ---------- 基础数据 ----------

    async def _read_points(self, result: DailyResult, *, first: bool):
        try:
            points = await self.api.points()
        except LPassAuthError:
            raise
        except LPassError as exc:
            result.notes.append(f"金币{'读取' if first else '结算读取'}失败：{exc}")
            return
        total = int(points.get("total", 0))
        result.points_earned = int(points.get("earned", 0) or 0)
        if first:
            result.points_before = result.points_after = total
        else:
            result.points_after = total

    async def build_status(self) -> DailyResult:
        """只读快照：金币、今日流水、签到与各任务进度（不写任何数据）。"""
        result = DailyResult()
        await self._read_points(result, first=True)
        tasks = await self._load_tasks(result)
        result.tasks = tasks
        result.today_points = await self._today_points()
        result.today_earned = sum(_int(i.get("points")) for i in result.today_points)

        ladder = next((x for x in tasks if classify(x) == "ladder_signin"), None)
        if ladder is not None:
            done, total, sign_today = ladder_progress(ladder)
            result.ladder_total = total
            result.signin_day = min(done + (0 if sign_today else 1), total)
            status = "already" if (sign_today or (total and done >= total)) else "pending"
            result.add("ladder", task_name(ladder) or "7-Day Coin Bonus", status,
                       f"第{done}天/{total}", task_points(ladder))

        daily = next((x for x in tasks if classify(x) == "signin"), None)
        signed = any(str(i.get("name", "")) == "每日签到" for i in result.today_points)
        if daily is not None and task_completed(daily) is True:
            result.add("signin", "每日签到", "already", "", task_points(daily))
        elif daily is None and signed:
            result.add("signin", "每日签到", "already", "", 100)
        else:
            result.add("signin", "每日签到", "pending", "")
        return result

    async def _today_points(self) -> list[dict]:
        try:
            items = await self.api.points_list(size=50)
        except LPassError:
            return []
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        out = []
        for item in items:
            stamp = _int(item.get("create_time"))
            if not stamp:
                continue
            if datetime.fromtimestamp(stamp, TZ).strftime("%Y-%m-%d") != today:
                continue
            out.append(item)
        return out

    async def settle(self, result: DailyResult):
        """只等本轮真正新完成的任务到账；历史已完成项不参与等待。

        最多 5 分钟、每 30 秒复查一次积分流水；命中即停。
        """
        expected = {s.label for s in result.steps
                    if s.status == "done" and s.points > 0 and s.label}
        if not expected:
            # 本轮没有新完成项，刷新一次流水就返回，不等待
            await self._refresh_points(result)
            return
        for attempt in range(10):
            await self._refresh_points(result)
            names = {str(i.get("name", "")) for i in result.today_points}
            if expected <= names:
                return
            if attempt == 9:
                return
            await self.sleep(30)

    async def _signin_in_points(self) -> bool:
        """任务表没有每日签到项时，用记录页流水判断今天是否已签。"""
        try:
            items = await self._today_points()
        except LPassError:
            return False
        return any(str(i.get("name", "")) == "每日签到" for i in items)

    async def _refresh_points(self, result: DailyResult):
        items = await self._today_points()
        result.today_points = items
        result.today_earned = sum(_int(i.get("points")) for i in items)

    async def refresh_tasks(self, result: DailyResult) -> list[dict]:
        """动作执行完后重新拉一次任务表，避免报告用旧快照（如好友请求 0/3）。"""
        tasks = await self._load_tasks(result)
        if tasks:
            result.tasks = tasks
        return tasks

    async def _load_tasks(self, result: DailyResult) -> list[dict]:
        try:
            return await self.api.tasks()
        except LPassAuthError:
            raise
        except LPassError as exc:
            result.notes.append(f"任务列表读取失败：{exc}")
            return []

    # ---------- 七日金币激励 / 每日签到 ----------

    async def _signin(self, tasks: list[dict], result: DailyResult):
        """每日签到与 7-Day Coin Bonus 是两个任务，分别处理与展示。"""
        daily = next((t for t in tasks if classify(t) == "signin"), None)
        ladder = next((t for t in tasks if classify(t) == "ladder_signin"), None)

        if ladder is not None:
            done, total, sign_today = ladder_progress(ladder)
            result.ladder_total = total
            result.signin_day = min(done + (0 if sign_today else 1), total)
            name = task_name(ladder) or "7-Day Coin Bonus"
            if sign_today or (total and done >= total):
                result.add("ladder", name, "already", f"第{done}天/{total}")
            elif not task_id(ladder):
                result.add("ladder", name, "failed", "缺少 task_id")
            elif self.dry_run:
                result.add("ladder", name, "skipped", "演练模式未提交", task_points(ladder))
            else:
                try:
                    payload = await self.api.daily_check_in(task_id(ladder))
                except LPassError as exc:
                    result.add("ladder", name, "failed", str(exc))
                else:
                    if payload.get("already"):
                        result.add("ladder", name, "already", "站点提示今日已签")
                    else:
                        result.add("ladder", name, "done",
                                   f"第{result.signin_day}天签到成功", task_points(ladder))

        if daily is None:
            if await self._signin_in_points():
                result.add("signin", "每日签到", "already", "", 100)
            else:
                result.add("signin", "每日签到", "pending", "")
        elif task_completed(daily) is True:
            result.add("signin", "每日签到", "already", "", task_points(daily))
        elif not task_id(daily):
            result.add("signin", "每日签到", "failed", "缺少 task_id")
        elif self.dry_run:
            result.add("signin", "每日签到", "skipped", "演练模式未提交", task_points(daily))
        else:
            try:
                payload = await self.api.daily_check_in(task_id(daily))
            except LPassError as exc:
                result.add("signin", "每日签到", "failed", str(exc))
            else:
                result.add("signin", "每日签到",
                           "already" if payload.get("already") else "done", "",
                           task_points(daily))

    # ---------- 常规任务：已完成次数达标即可领取 ----------

    async def _collect_walkin(self, tasks: list[dict], result: DailyResult):
        """``need_completed_times`` 已达标的任务，直接补一次领取。"""
        for task in tasks:
            key = classify(task)
            if key in FRIEND_KEYS or key in PLAY_KEYS or key in SIGNIN_KEYS:
                continue
            if task_completed(task) is True or not task_id(task):
                continue
            infos = task.get("reward_infos")
            if not isinstance(infos, list) or not infos:
                continue
            first = infos[0] if isinstance(infos[0], dict) else {}
            need = task_need_times(task)
            done = int(first.get("completed_times", 0) or 0)
            if done < need or need == 0:
                continue
            name = task_name(task) or key
            if self.dry_run:
                result.add(f"task:{task_id(task)}", name, "skipped", "演练模式未提交",
                           task_points(task))
                continue
            try:
                await self.api.complete_task(task_id(task))
            except LPassError as exc:
                result.add(f"task:{task_id(task)}", name, "failed", str(exc))
                continue
            result.add(f"task:{task_id(task)}", name, "done", f"已领取（{done}/{need}）",
                       task_points(task))

    # ---------- 每周分享 LI PASS ----------

    async def _share_link(self, tasks: list[dict], result: DailyResult):
        task = next((t for t in tasks if classify(t) == "share_link"), None)
        if task is None:
            return
        if task_completed(task) is True:
            result.add("share_link", "分享 LI PASS", "already")
            return
        if self.dry_run:
            result.add("share_link", "分享 LI PASS", "skipped", "演练模式未取链")
            return
        try:
            data = await self.api.share_link(task_id(task))
        except LPassAuthError:
            raise
        except LPassError as exc:
            result.add("share_link", "分享 LI PASS", "failed", str(exc))
            return
        link = data.get("link") or ""
        result.share_link = link
        if not link:
            result.add("share_link", "分享 LI PASS", "failed", "站点未返回分享链接")
            return
        visited = False
        try:
            from .ui import open_link_anonymous
            visited = await open_link_anonymous(link, wait_ms=8000)
        except Exception:  # noqa: BLE001
            visited = False
        result.add("share_link", "分享 LI PASS", "manual",
                   "链接已生成" + ("并已无痕访问，等待结算" if visited else "，需他人打开"),
                   task_points(task))

    # ---------- 必须人工/签名的常规任务 ----------

    async def _manual_tasks(self, tasks: list[dict], result: DailyResult):
        """无法代跑的任务：按类型聚合，一条一行。"""
        buckets: dict[str, dict] = {}
        for task in tasks:
            key = classify(task)
            if key in SIGNIN_KEYS or not key:
                continue
            if task_completed(task) is True:
                result.add(f"task:{task_id(task)}", task_name(task) or key, "already")
                continue
            if key in ONCE_KEYS:
                continue  # 一次性任务由 _one_time 统一处理
            if key not in FRIEND_KEYS + PLAY_KEYS + MANUAL_KEYS:
                continue
            need = task_need_times(task)
            item = buckets.setdefault(key, {"labels": [], "need": 0, "points": 0})
            item["labels"].append(task_name(task) or key)
            item["need"] = max(item["need"], need)
            item["points"] += task_points(task)

        hints = {
            "friend_requests": f"需在 {FRIEND_PAGE} 发送好友请求（站点要 SDK 签名）",
            "share_social": f"需在 {FRIEND_PAGE} 分享交友链接",
            "add_friend": f"需在 {FRIEND_PAGE} 加好友并通过申请",
            "play_game": "需要实际进入游戏，无法代跑",
            "share_link": "链接已生成，需他人打开后才会累计",
            "nickname": "需在 pass 账号管理处修改昵称",
            "recharge": "需在商店充值",
            "follow_social": "需在网页完成社交跳转（X / Discord）",
            "email": "需在网页订阅邮件",
        }
        for key, item in buckets.items():
            labels = item["labels"]
            label = _short_label(key, labels)
            detail = hints.get(key, "站点未提供可调用接口")
            if key == "add_friend" and len(labels) > 1:
                detail = f"{detail}（共 {len(labels)} 档）"
            result.add(f"manual:{key}", label, "manual", detail, item["points"])

    # ---------- 一次性任务 ----------

    async def _one_time(self, tasks: list[dict], result: DailyResult):
        await self._disposable(result)
        for task in tasks:
            key = classify(task)
            if key not in ONCE_KEYS or task_completed(task) is True:
                continue
            name = task_name(task) or key
            if key == "nickname":
                await self._nickname(task, name, result)

    async def _disposable(self, result: DailyResult):
        """关注 X / 加入 Discord 等一次性任务：站点按"点过入口"上报。"""
        try:
            items = await self.api.disposable_tasks()
        except LPassAuthError:
            raise
        except LPassError as exc:
            result.notes.append(f"一次性任务读取失败：{exc}")
            return
        for task in items:
            if task_completed(task) is True or not task_id(task):
                continue
            name = task_name(task) or "一次性任务"
            if self.dry_run:
                result.add(f"disposable:{task_id(task)}", name, "skipped", "演练模式未提交")
                continue
            try:
                await self.api.complete_task(task_id(task))
            except LPassError as exc:
                result.add(f"disposable:{task_id(task)}", name, "failed", str(exc))
                continue
            result.add(f"disposable:{task_id(task)}", name, "done",
                       "已上报，站点审核后到账", task_points(task))

    async def _nickname(self, task: dict, name: str, result: DailyResult):
        """改昵称：UGC ``modify_info`` 实测 500（下游 SetLIPUserInfo 要额外头部），
        不替用户改资料，只提示人工完成后再领取。"""
        result.add(f"task:{task_id(task)}", name, "manual",
                   "需在 pass 账号管理处修改昵称", task_points(task))


def _short_label(key: str, labels: list[str]) -> str:
    """同类任务取最短的名字做代表，避免一长串重复。"""
    if key == "add_friend":
        return "添加好友"
    if key == "friend_requests":
        return "发送好友请求"
    pool = sorted(set(labels), key=len)
    return pool[0] if pool else key


def task_progress(task: dict) -> tuple[int, int]:
    infos = task.get("reward_infos")
    if not isinstance(infos, list) or not infos:
        return 0, task_need_times(task)
    first = infos[0] if isinstance(infos[0], dict) else {}
    return int(first.get("completed_times", 0) or 0), task_need_times(task)


__all__ = ["DailyResult", "DailyRunner", "StepResult", "classify", "task_block_completed"]
