"""LPass 绑定持久化：一个 (user_id, bot_id) 一份 LIP 登录态，Cookie 加密落库。

另外保存「自动任务」开关、执行时间、最近一次执行结果与通知路由。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from gsuid_core.utils.database.base_models import Bind, with_session
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field

from ..nikke_api.auth import CookieCipher
from ..nikke_api.auth import mask as _mask
from ..nikke_config.config import MAIN_PATH
from .api import LPassCookie

DEFAULT_AUTO_TIME = "08:30"
FULL_SIGN_TIME = "08:45"


class LPassBind(Bind, table=True):
    """LIP 账号绑定。与 AliceBind（blablalink 游戏账号）相互独立。"""

    __table_args__: Dict[str, Any] = {"extend_existing": True}

    lip_openid: Optional[str] = Field(default=None, title="LIP openid")
    cookie_enc: str = Field(default="", title="加密后的 LIP Cookie")
    nickname: str = Field(default="", title="LIP 昵称")
    expire_at: int = Field(default=0, title="登录态过期时间")
    updated_at: int = Field(default=0, title="最近更新时间")

    auto_enabled: bool = Field(default=False, title="是否每日自动执行")
    auto_time: str = Field(default=DEFAULT_AUTO_TIME, title="自动执行时间 HH:MM")
    last_run_day: str = Field(default="", title="最近自动执行日期")
    last_result: str = Field(default="", title="最近一次执行结果")
    notify_ws: str = Field(default="", title="推送用的 WS 标识")
    notify_self: str = Field(default="", title="推送用的 bot self id")

    @classmethod
    async def get_bind(cls, user_id, bot_id) -> Optional["LPassBind"]:
        return await cls.select_data(str(user_id), str(bot_id))

    @classmethod
    async def save_bind(cls, user_id, bot_id, group_id, *, cookie_enc: str,
                        lip_openid: str, nickname: str, expire_at: int) -> int:
        data = {
            "lip_openid": lip_openid,
            "cookie_enc": cookie_enc,
            "nickname": nickname,
            "expire_at": int(expire_at or 0),
            "updated_at": int(time.time()),
        }
        exists = await cls.select_data(str(user_id), str(bot_id))
        if exists:
            return await cls.update_data(str(user_id), str(bot_id), **data)
        return await cls.insert_data(user_id=str(user_id), bot_id=str(bot_id),
                                     group_id=group_id, **data)

    @classmethod
    @with_session
    async def delete_bind(cls, session: AsyncSession, user_id, bot_id) -> int:
        # with_session 把 session 作为第二个位置参数注入（base_models._session_wrapper）。
        # 用显式谓词删除，避免 Core 的“瞬态对象再 delete”路径。
        result = await session.execute(
            delete(cls).where(cls.user_id == str(user_id), cls.bot_id == str(bot_id))
        )
        return int(result.rowcount or 0)

    # ---------- 自动任务 ----------

    @classmethod
    @with_session
    async def ensure_auto_columns(cls, session: AsyncSession) -> None:
        """老表补列（SQLite/MySQL 都能 ALTER ADD COLUMN）。"""
        ddl = (
            ("auto_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("auto_time", "VARCHAR(8) NOT NULL DEFAULT '08:30'"),
            ("last_run_day", "VARCHAR(16) NOT NULL DEFAULT ''"),
            ("last_result", "TEXT"),
            ("notify_ws", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("notify_self", "VARCHAR(64) NOT NULL DEFAULT ''"),
        )
        try:
            rows = await session.execute(text("PRAGMA table_info(lpassbind)"))
            columns = {row[1] for row in rows.fetchall()}
        except Exception:
            columns = None
        if columns is None:
            return
        for name, spec in ddl:
            if name in columns:
                continue
            try:
                await session.execute(text(f"ALTER TABLE lpassbind ADD COLUMN {name} {spec}"))
            except Exception:
                pass

    @classmethod
    @with_session
    async def set_auto(cls, session: AsyncSession, user_id, bot_id, *,
                       enabled: bool, at: Optional[str] = None,
                       ws: str = "", self_id: str = "") -> bool:
        values: Dict[str, Any] = {"auto_enabled": bool(enabled)}
        if at:
            values["auto_time"] = at
        if ws:
            values["notify_ws"] = ws
        if self_id:
            values["notify_self"] = self_id
        result = await session.execute(
            cls.__table__.update()
            .where(cls.user_id == str(user_id), cls.bot_id == str(bot_id))
            .values(**values)
        )
        return bool(result.rowcount)

    @classmethod
    @with_session
    async def due_binds(cls, session: AsyncSession, at: str, day: str) -> List["LPassBind"]:
        rows = await session.scalars(
            select(cls).where(
                cls.auto_enabled == True,  # noqa: E712
                cls.auto_time == at,
                cls.last_run_day != day,
            )
        )
        return list(rows.all())

    @classmethod
    @with_session
    async def mark_ran(cls, session: AsyncSession, user_id, bot_id, *,
                       day: str, summary: str) -> None:
        await session.execute(
            cls.__table__.update()
            .where(cls.user_id == str(user_id), cls.bot_id == str(bot_id))
            .values(last_run_day=day, last_result=summary[:1500])
        )


    @classmethod
    @with_session
    async def migrate_auto_0830(cls, session: AsyncSession) -> int:
        """把「#LPass自动任务 开启 08:30」的用户迁到全量签到（08:45）。"""
        result = await session.execute(
            cls.__table__.update()
            .where(cls.auto_enabled == True, cls.auto_time == "08:30")  # noqa: E712
            .values(auto_time=FULL_SIGN_TIME)
        )
        await session.commit()
        return int(result.rowcount or 0)

    def revision(self) -> str:
        return hashlib.sha256((self.cookie_enc or "").encode()).hexdigest()[:16]

    def summary(self) -> str:
        name = self.nickname or "LIP 账号"
        if self.expire_at:
            left = max(0, (self.expire_at - int(time.time())) // 86400)
            return f"{name}（登录态剩余 {left} 天）"
        return f"{name}（登录态有效期未知）"


_cipher: Optional[CookieCipher] = None


def get_cipher() -> CookieCipher:
    global _cipher
    if _cipher is None:
        _cipher = CookieCipher(MAIN_PATH / "secret.key")
    return _cipher


def encrypt_cookie(cookie: LPassCookie) -> str:
    return get_cipher().encrypt(cookie.header())


def decrypt_cookie(text_value: str) -> Optional[LPassCookie]:
    from .api import parse_cookie
    if not text_value:
        return None
    try:
        return parse_cookie(get_cipher().decrypt(text_value))
    except Exception:
        return None


def mask_openid(openid: str) -> str:
    return _mask(openid or "", 4)


_ready = False



async def ensure_ready() -> None:
    """老表补一次列；失败不阻断业务。"""
    global _ready
    if _ready:
        return
    try:
        await LPassBind.ensure_auto_columns()
    except Exception:
        pass
    try:
        await LPassBind.migrate_auto_0830()
    except Exception:
        pass
    _ready = True


