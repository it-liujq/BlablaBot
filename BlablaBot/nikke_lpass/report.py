"""日常结果文本。按模板逐项列出进度、是否完成与奖励，只写可确证的事实。"""
from __future__ import annotations

from .tasks import DailyResult, StepResult, classify, task_need_times, task_points

TITLE = "LEVELINFINITEPASS日常执行结果"
FRIEND_SETTLE_NOTE = "（好友请求奖励约10分钟后结算，未计入上面的差值）"

DAILY_KEYS = {"signin", "ladder_signin", "play_game", "friend_requests", "share_social",
              "share_link"}
ONCE_KEYS = {"add_friend", "nickname", "recharge", "follow_social", "email",
             "appointment", "desktop_shortcut", "flexible", "shop", "share_production",
             "nikke_two_year", "lip_store_first_bind", "master_bind_lip", "play_jump_infinite",
             "jump_one_jump"}

HINTS = {
    "share_link": "需他人打开分享链接",
    "add_friend": "需好友通过申请",
    "nickname": "需在网页修改昵称",
    "recharge": "需在商店充值",
    "follow_social": "需在网页完成社交跳转",
    "email": "需在网页订阅邮件",
    "play_game": "需要进入游戏",
}


def render(result: DailyResult, *, prefix: str = "") -> str:
    lines = [f"{prefix}{TITLE}" if prefix else TITLE]
    delta = result.delta
    sign = f"+{delta}" if delta >= 0 else str(delta)
    lines.append(f"金币:{result.points_before} →{result.points_after}({sign})")
    if result.today_earned:
        lines.append(f"今日流水:+{result.today_earned}（{len(result.today_points)} 条）")
    lines.append("")

    daily_rows, once_rows, other_rows = _rows(result)

    lines.append("日常:")
    lines.extend(daily_rows or ["（无）"])
    if result.share_link:
        lines.append(f"分享链接：{result.share_link}")
    lines.append("")

    lines.append("一次性任务:")
    lines.extend(once_rows or ["（无）"])
    if other_rows:
        lines.append("")
        lines.append("其他任务:")
        lines.extend(other_rows)

    if result.notes:
        lines.append("")
        for note in result.notes:
            lines.append(f"备注：{note}")
    if result.dry_run:
        lines.append("")
        lines.append("本次为演练模式，未提交任何签到或任务请求。")
    return "\n".join(lines)


ONCE_ORDER = {"add_friend": 0, "nickname": 1, "recharge": 2, "shop": 2,
              "follow_social": 3, "email": 4}


def _rows(result: DailyResult):
    daily: list[str] = []
    once: list[tuple] = []
    other: list[str] = []

    for key in ("signin", "ladder"):
        step = next((s for s in result.steps if s.key == key), None)
        if step is not None:
            daily.append(_step_line(step, result))

    for task in result.tasks:
        key = classify(task)
        if key in ("signin", "ladder_signin"):
            continue
        line = _task_line(task, key)
        if key in DAILY_KEYS:
            daily.append(line)
        else:
            once.append((ONCE_ORDER.get(key, 9), line))

    # 一次性任务接口（GetDisposableTask）返回的条目不在任务表里，单独补进「一次性任务」
    for step in result.steps:
        if not step.key.startswith("disposable:"):
            continue
        if step.status == "done":
            text = (f"{step.label}（+{step.points}) 已完成" if step.points
                    else f"{step.label} 已完成")
            once.append((5, text))
        elif step.status == "failed":
            once.append((5, f"{step.label} 未确认（{step.detail or '接口异常'}）"))
    once_lines = [line for _, line in sorted(once, key=lambda item: item[0])]
    return daily, once_lines, other


def _step_line(step: StepResult, result: DailyResult) -> str:
    if step.key == "signin":
        return _signin_line(step, result)
    # ladder：7-Day Coin Bonus（奖励以今日流水为准）
    earned = sum(int(i.get("points", 0) or 0) for i in result.today_points
                 if str(i.get("name", "")) == step.label)
    points = f"（+{earned})" if earned else (f"（+{step.points})" if step.points else "")
    detail = f" {step.detail}" if step.detail else ""
    if step.status in ("done", "already"):
        return f"{step.label}{points} 已完成{detail}"
    if step.status == "failed":
        return f"{step.label} 未完成（{step.detail or '接口异常'}）"
    if step.status == "skipped":
        return f"{step.label} 演练跳过"
    return f"{step.label} 状态未知"


def _signin_line(step: StepResult, result: DailyResult) -> str:
    # 每日签到独立结算：优先看今日流水里是否已有「每日签到」
    earned = 0
    for item in result.today_points:
        if str(item.get("name", "")) == "每日签到":
            earned += int(item.get("points", 0) or 0)
    if earned:
        return f"每日签到（+{earned}) 已完成"
    if step.status == "done":
        return f"每日签到（+{step.points}) 已完成"
    if step.status == "already":
        return f"每日签到（+{step.points}) 已完成" if step.points else "每日签到 已完成"
    if step.status == "failed":
        return f"每日签到 未完成（{step.detail or '接口异常'}）"
    if step.status == "skipped":
        return "每日签到 演练跳过"
    return "每日签到 未完成（+100)"


def _task_line(task: dict, key: str) -> str:
    name = _display_name(task)
    points = task_points(task)
    reward = f"（+{points})" if points else ""
    need = task_need_times(task)
    infos = task.get("reward_infos") or [{}]
    first = infos[0] if isinstance(infos[0], dict) else {}
    try:
        times = int(first.get("completed_times", 0) or 0)
    except (TypeError, ValueError):
        times = 0
    completed = _completed(task) or (need and times >= need)
    if completed:
        return f"{name} {min(times, need) if need else 1}/{need or 1} 已完成{reward}"
    progress = f"{times}/{need}" if need else "未完成"
    hint = HINTS.get(key)
    tail = f" — {hint}" if hint else ""
    return f"{name} {progress} 未完成{reward}{tail}"


def _display_name(task: dict) -> str:
    from .api import task_name
    return task_name(task) or "未命名任务"


def _completed(task: dict) -> bool:
    from .api import task_completed
    return task_completed(task) is True


def render_bind(nickname: str, points: int, *, masked: str = "", expire_days: int = 0) -> str:
    lines = [
        "LEVELINFINITEPASS绑定成功",
        f"账号：{nickname or 'LIP 账号'}",
        f"金币：{points}",
    ]
    if masked:
        lines.append(f"openid：{masked}")
    lines.append(f"登录态剩余约 {expire_days} 天" if expire_days
                 else "登录态有效期未知，失效后需重新绑定")
    lines.append("下次执行：#LPass日常")
    return "\n".join(lines)


def render_status(nickname: str, points: int, points_earned: int = 0, *,
                  signin_day: int = 0, ladder_total: int = 0, masked: str = "",
                  expire_days: int = 0) -> str:
    lines = [
        "LEVELINFINITEPASS绑定状态",
        f"账号：{nickname or 'LIP 账号'}",
        f"金币：{points}" + (f"（累计获得 {points_earned}）" if points_earned else ""),
    ]
    if signin_day:
        total = f"/{ladder_total}" if ladder_total else ""
        lines.append(f"七日金币激励：第{signin_day}天{total}")
    if masked:
        lines.append(f"openid：{masked}")
    lines.append(f"登录态剩余：约 {expire_days} 天" if expire_days else "登录态有效期：未知")
    return "\n".join(lines)


def render_auto_status(*, enabled: bool, at: str, last_day: str = "",
                       last_result: str = "", expire_days: int = 0) -> str:
    lines = [
        "LPass 自动任务",
        f"状态：{'已开启' if enabled else '已关闭'}",
        f"时间：每天北京时间 {at}",
    ]
    if expire_days:
        lines.append(f"登录态剩余：约 {expire_days} 天")
    if last_day:
        lines.append(f"上次执行：{last_day}")
    if last_result:
        lines.append("")
        lines.append(last_result)
    else:
        lines.append("还没有执行记录。")
    return "\n".join(lines)


__all__ = ["render", "render_auto_status", "render_bind", "render_status"]
