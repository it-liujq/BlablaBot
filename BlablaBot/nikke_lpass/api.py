"""LEVEL INFINITE PASS 奖励中心接口客户端（仅 httpx，不依赖 GsCore）。

实测结论（2026-11，来源：前端 bundle + 抓包，详见 MAINTENANCE.md）：

- 只有 ``api-pass.levelinfinite.com`` 提供 API；``pass.levelinfinite.com`` 是纯静态站，
  ``/api/*`` 一律返回 index.html。
- 鉴权是 Cookie 会话（前端 axios ``withCredentials``），未登录统一
  ``{"code":300001,"msg":"not login lip openid"}``；请求需带 ``x-common-params`` 与
  ``x-channel-type: 1``。
- 金币在 ``data.active_points``；任务 ID **只能**从 ``GetTaskListWithStatusV2`` 现取。
- 好友系统在 ``sg.intlgame.com``，鉴权靠 body 里的 openid/token，但请求必须带
  ``sig``（腾讯 Chaos VM 内生成），纯 HTTP 无法伪造 —— 因此好友写操作不在本层实现。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

API_HOST = "https://api-pass.levelinfinite.com"
POINTS = "/api/rewards/proxy/lipass/Points"
UGC = "/api/ugc"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)

CODE_OK = 0
CODE_ALREADY = (1001009, 1002007)  # 今日已签到（老/新两种业务码）
CODE_AUTH = (300001, 10005, 10010, 401, 403)

COOKIE_KEYS = (
    "lip_openid",
    "lip_uid",
    "lip_token",
    "lip_channelid",
    "lip_adult_status",
    "lip_user_name",
    "lip_region",
    "lip_email",
    "lip_expire_time",
)

NICKNAME_KEYS = ("username", "nick_name", "nickname")
_NICKNAME_KEY = ""


class LPassError(Exception):
    """可安全展示给用户的错误；不携带 Cookie 与响应原文。"""

    def __init__(self, reason: str, *, category: str = "business",
                 retryable: bool = False, not_sent: bool = False):
        super().__init__(reason)
        self.category = category
        self.retryable = retryable
        self.not_sent = not_sent


class LPassAuthError(LPassError):
    def __init__(self, reason: str = "LIP 登录态已失效，请重新绑定"):
        super().__init__(reason, category="auth")


@dataclass(frozen=True)
class LPassCookie:
    """一份可用的 LIP 登录态。token 不参与 repr/日志。"""

    openid: str
    token: str = field(repr=False)
    uid: str = ""
    channel_id: str = "131"
    adult_status: str = "1"
    user_name: str = ""
    region: str = ""
    expire_at: int = 0

    @property
    def expired(self) -> bool:
        return bool(self.expire_at and self.expire_at <= int(time.time()))

    @property
    def days_left(self) -> int:
        if not self.expire_at:
            return 0
        return max(0, (self.expire_at - int(time.time())) // 86400)

    def header(self) -> str:
        parts = [
            f"lip_openid={self.openid}",
            f"lip_token={self.token}",
        ]
        # 奖励代理按 lip_uid 认人；缺了会直接 300001 not login lip uid。
        if self.uid:
            parts.append(f"lip_uid={self.uid}")
        parts += [
            f"lip_channelid={self.channel_id or '131'}",
            f"lip_adult_status={self.adult_status or '1'}",
        ]
        if self.user_name:
            parts.append(f"lip_user_name={self.user_name}")
        if self.region:
            parts.append(f"lip_region={self.region}")
        return "; ".join(parts)

    def login_body(self) -> dict:
        """``POST /api/ugc/login`` 只认这套字段名（短名会全量报 required）。"""
        body = {
            "lip_openid": self.openid,
            "lip_token": self.token,
            # Go 侧是 int32/int64，传字符串会 400 Parameter error。
            "lip_channelid": _int(self.channel_id, 131),
            "lip_adult_status": self.adult_status or "1",
            "lip_user_name": self.user_name,
        }
        if self.uid:
            body["lip_uid"] = _int(self.uid, 0)
        if self.expire_at:
            body["lip_expire_time"] = int(self.expire_at)
        if self.region:
            body["lip_region"] = self.region
        return body


def parse_cookie(text: str, *, expire_at: int = 0) -> Optional[LPassCookie]:
    """从浏览器 Cookie / document.cookie 文本里挑出 LIP 凭证。"""
    data: dict[str, str] = {}
    for seg in (text or "").replace("\n", ";").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        key, value = seg.split("=", 1)
        key = key.strip()
        if key in COOKIE_KEYS:
            data[key] = value.strip()
    openid = data.get("lip_openid", "")
    token = data.get("lip_token", "")
    if not openid or not token:
        return None
    expire = _int(data.get("lip_expire_time", "0"))
    if expire > 4102444800:  # 毫秒时间戳兜底
        expire //= 1000
    return LPassCookie(
        openid=openid,
        token=token,
        uid=data.get("lip_uid", ""),
        channel_id=data.get("lip_channelid", "131") or "131",
        adult_status=data.get("lip_adult_status", "1") or "1",
        user_name=data.get("lip_user_name", ""),
        region=data.get("lip_region", ""),
        expire_at=min([x for x in (expire_at, expire) if x > 0], default=0),
    )


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class LPassAPI:
    """奖励中心 / UGC 的协议层（好友写操作需要签名，不在此层）。"""

    def __init__(self, cookie: LPassCookie, *, user_agent: str = DEFAULT_UA,
                 language: str = "zh", http: Optional[httpx.AsyncClient] = None):
        self.cookie = cookie
        self.user_agent = user_agent
        self.language = language
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self):
        if self._own_http:
            await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ---------- 基础 ----------

    def headers(self) -> dict[str, str]:
        # 前端 onRequest$1：x-common-params 里的 game_id/area_id 有默认值，
        # 奖励中心不是具体游戏，取 30004(li pass) / asia。
        common = {
            "game_id": "30004",
            "area_id": "asia",
            "source": "2",
            "language": self.language,
            "intl_game_id": "",
            "lip_region": self.cookie.region or "",
        }
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": "https://pass.levelinfinite.com",
            "referer": "https://pass.levelinfinite.com/",
            "user-agent": self.user_agent,
            "x-language": self.language,
            "x-channel-type": "1",
            "x-common-params": json.dumps(common, ensure_ascii=False, separators=(",", ":")),
            "cookie": self.cookie.header(),
        }

    async def request(self, method: str, path: str, *, body: Any = None,
                      params: Optional[dict] = None, host: str = API_HOST) -> dict:
        try:
            resp = await self._http.request(
                method, host + path, headers=self.headers(), params=params,
                json=body, follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise LPassError("连接未建立，请求未发出", category="network",
                             retryable=True, not_sent=True) from None
        except Exception:
            raise LPassError("网络异常，结果未确认", category="network", retryable=True) from None
        if resp.status_code in (401, 403):
            raise LPassAuthError()
        if resp.status_code == 429:
            raise LPassError("平台限流（HTTP 429），本轮已停止", category="rate_limit")
        if resp.status_code != 200:
            raise LPassError(f"接口 HTTP 异常（{resp.status_code}）", category="server",
                             retryable=resp.status_code >= 500)
        try:
            payload = resp.json()
        except Exception:
            raise LPassError("接口返回非 JSON，可能是登录跳转", category="schema") from None
        if not isinstance(payload, dict) or "code" not in payload:
            raise LPassError("接口返回格式变化", category="schema")
        code = _int(payload.get("code"), -1)
        if code in CODE_AUTH:
            raise LPassAuthError()
        msg_text = str(payload.get("msg") or "")
        if code in CODE_ALREADY or "already sign in" in msg_text.lower():
            payload["already"] = True
            payload.setdefault("data", {})
            return payload
        if code != CODE_OK:
            msg = str(payload.get("msg") or "")
            if msg.startswith("not login"):
                raise LPassAuthError()
            raise LPassError(f"接口业务异常（code={code}{'，' + msg if msg else ''}）",
                             category="business")
        data = payload.get("data")
        if data is None:
            payload["data"] = {}
        elif not isinstance(data, (dict, list)):
            raise LPassError("接口 data 结构变化", category="schema")
        return payload

    # ---------- 会话 / 用户 ----------

    async def open_session(self) -> dict:
        """用 lip_* 建立服务端会话（前端登录后第一件事）。"""
        return await self.request("POST", f"{UGC}/login", body=self.cookie.login_body())

    async def check_login(self) -> bool:
        payload = await self.request("GET", f"{UGC}/user/check_login")
        data = payload.get("data") or {}
        if isinstance(data, dict) and "is_login" in data:
            return bool(data["is_login"])
        return True

    async def user_info(self) -> dict:
        payload = await self.request("GET", f"{UGC}/user/get_info")
        return payload.get("data") or {}

    async def modify_nickname(self, nickname: str) -> dict:
        """改昵称。UGC 字段名随版本变化，按候选顺序探测并记住可用键。"""
        global _NICKNAME_KEY
        keys = ([_NICKNAME_KEY] if _NICKNAME_KEY else []) + list(NICKNAME_KEYS)
        last: Optional[LPassError] = None
        for key in keys:
            try:
                payload = await self.request("POST", f"{UGC}/user/modify_info",
                                             body={key: nickname})
            except LPassAuthError:
                raise
            except LPassError as exc:
                last = exc
                continue
            _NICKNAME_KEY = key
            return payload.get("data") or {}
        raise last or LPassError("改昵称接口未确认", category="schema")

    # ---------- 金币 ----------

    async def points(self) -> dict:
        """金币总览。任务积分在 active_points，付费积分在 paid_point。"""
        payload = await self.request("POST", f"{POINTS}/GetUserTotalPointsNew", body={})
        data = payload.get("data") or {}
        active = data.get("active_points")
        if not isinstance(active, dict):
            active = data if "total_points" in data else {}
        total = active.get("total_points", data.get("total_points"))
        if total is None:
            raise LPassError("金币接口未返回 total_points", category="schema")
        return {
            "total": _int(total),
            "earned": _int(active.get("total_points_earned", 0)),
            "consumed": _int(active.get("total_points_consumed", 0)),
            "paid": _int((data.get("paid_point") or {}).get("total_points", 0))
            if isinstance(data.get("paid_point"), dict) else 0,
        }

    # ---------- 任务 ----------

    async def tasks(self) -> list[dict]:
        """任务列表：task_id 的唯一权威来源。"""
        payload = await self.request(
            "GET", f"{POINTS}/GetTaskListWithStatusV2",
            params={"get_top": "false", "intl_game_id": ""},
        )
        tasks = _extract_tasks(payload.get("data"))
        if tasks is None:
            raise LPassError("任务列表结构变化", category="schema")
        return tasks

    async def disposable_tasks(self, task_type: Optional[int] = None) -> list[dict]:
        params = {"task_type": task_type} if task_type is not None else None
        payload = await self.request("GET", f"{POINTS}/GetDisposableTask", params=params)
        return _extract_tasks(payload.get("data")) or []

    async def daily_check_in(self, task_id: str) -> dict:
        payload = await self.request("POST", f"{POINTS}/DailyCheckIn",
                                     body={"task_id": task_id})
        return {"confirmed": not payload.get("already"),
                "already": bool(payload.get("already")),
                "raw": payload.get("data") or {}}

    async def complete_task(self, task_id: str) -> dict:
        payload = await self.request("POST", f"{POINTS}/CompleteTaskAddPoint",
                                     body={"task_id": task_id})
        return {"ok": True, "already": bool(payload.get("already")),
                "raw": payload.get("data") or {}}

    async def share_link(self, task_id: str) -> dict:
        """取本人的分享链接（``task_type=22`` 每周分享 LI PASS）。"""
        payload = await self.request("POST", f"{POINTS}/GetShareLink",
                                     body={"task_id": _int(task_id, task_id)})
        data = payload.get("data") or {}
        return {"status": _int(data.get("status")), "link": str(data.get("share_link") or "")}

    async def help_share(self, share_code: str) -> dict:
        """用别人的 share_code 助力（给分享者加金币，不是完成自己的任务）。"""
        payload = await self.request("POST", f"{POINTS}/ClickShareAddPoint",
                                     body={"share_code": share_code})
        data = payload.get("data") or {}
        return {"status": _int(data.get("status"))}


def _extract_tasks(data: Any) -> Optional[list[dict]]:
    """兼容 tasks / task_list / 分组结构。"""
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if not isinstance(data, dict):
        return None
    for key in ("tasks", "task_list", "taskList"):
        value = data.get(key)
        if isinstance(value, list):
            return [t for t in value if isinstance(t, dict)]
    groups = data.get("task_group") or data.get("groups")
    if isinstance(groups, list):
        merged: list[dict] = []
        for group in groups:
            if isinstance(group, dict):
                for key in ("tasks", "task_list"):
                    if isinstance(group.get(key), list):
                        merged.extend(t for t in group[key] if isinstance(t, dict))
        if merged:
            return merged
    return None


def task_completed(task: dict) -> Optional[bool]:
    """任务是否已完成；无法判断时返回 None（绝不猜测）。"""
    for key in ("is_completed", "completed", "is_finish"):
        value = task.get(key)
        if type(value) is bool:
            return value
        if type(value) is int and value in (0, 1):
            return bool(value)
    infos = task.get("reward_infos")
    if isinstance(infos, list) and infos and all(
        isinstance(i, dict) and task_block_completed(i) is not None for i in infos
    ):
        states = [task_block_completed(i) for i in infos]
        if states:
            return all(bool(s) for s in states)
    return None


def task_block_completed(block: dict) -> Optional[bool]:
    value = block.get("is_completed")
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def task_id(task: dict) -> str:
    for key in ("task_id", "id", "taskId"):
        if task.get(key) is not None:
            return str(task[key])
    return ""


def task_name(task: dict) -> str:
    for key in ("task_name", "name", "title"):
        if isinstance(task.get(key), str):
            return task[key]
    return ""


def task_points(task: dict) -> int:
    for key in ("points", "point", "reward_points"):
        if task.get(key) is not None:
            return _int(task[key])
    infos = task.get("reward_infos")
    if isinstance(infos, list) and infos and isinstance(infos[0], dict):
        return _int(infos[0].get("points", 0))
    return 0


def task_need_times(task: dict) -> int:
    """``reward_infos[0].need_completed_times``：区分"加1个/加5个好友"。"""
    infos = task.get("reward_infos")
    if isinstance(infos, list) and infos and isinstance(infos[0], dict):
        return max(1, _int(infos[0].get("need_completed_times", 1), 1))
    return 1


def ladder_progress(task: dict) -> tuple[int, int, bool]:
    """七日阶梯：(已签天数, 总格数, 今天是否已签)。"""
    infos = task.get("reward_infos")
    blocks = [i for i in infos if isinstance(i, dict)] if isinstance(infos, list) else []
    done = sum(1 for i in blocks if task_block_completed(i) is True)
    sign_today = task.get("sign_today")
    return done, len(blocks) or 7, bool(sign_today) if type(sign_today) is bool else False
