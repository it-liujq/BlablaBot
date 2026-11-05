"""LPass 绑定网页：/nikke/lpass/{token} 输入邮箱密码，服务端用 Playwright 登录 pass 站点。"""
from __future__ import annotations

import html

from fastapi.responses import HTMLResponse, JSONResponse
from gsuid_core.logger import logger
from gsuid_core.web_app import app
from pydantic import BaseModel

from ..nikke_login.session import sessions
from .api import LPassAuthError, LPassError, parse_cookie
from .auth import LoginError, login_with_password, verify
from .store import LPassBind, encrypt_cookie

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LEVEL INFINITE PASS 绑定</title>
<style>
body{margin:0;background:#12101c;color:#e8e6f0;font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:420px;margin:0 auto;padding:28px 18px 48px}
h1{font-size:19px;margin:0 0 6px}
p.tip{color:#9a94b8;margin:0 0 20px}
label{display:block;margin:14px 0 6px;color:#b9b3d4;font-size:13px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:10px;border:1px solid #3a3355;
background:#1b1729;color:#fff;font-size:15px}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:10px;background:#7c4dff;
color:#fff;font-size:16px;font-weight:600}
button[disabled]{opacity:.6}
#msg{margin-top:16px;font-size:14px;white-space:pre-wrap}
.ok{color:#63d68a}.err{color:#ff7b7b}
.foot{margin-top:28px;color:#6f688c;font-size:12px}
</style></head><body><div class="wrap">
<h1>LEVEL INFINITE PASS 绑定</h1>
<p class="tip">登录 pass.levelinfinite.com 后，Bot 会保存登录态用于每日任务。账号密码只发给本机 Core，不写入日志。</p>
<form id="f">
  <label>邮箱地址</label><input id="email" type="email" autocomplete="username" required>
  <label>密码</label><input id="password" type="password" autocomplete="current-password" required>
  <button id="btn" type="submit">登录并绑定</button>
</form>
<div id="msg"></div>
<hr style="border:0;border-top:1px solid #302a49;margin:26px 0">
<h1 style="font-size:16px">方式二：粘贴 Cookie</h1>
<p class="tip">已登录 pass.levelinfinite.com 时，从开发者工具复制含 lip_openid / lip_token 的整段 Cookie。</p>
<textarea id="cookie" rows="4" style="width:100%;box-sizing:border-box;padding:10px;border-radius:10px;
border:1px solid #3a3355;background:#1b1729;color:#fff;font:13px monospace"></textarea>
<button id="btn2" type="button" style="background:#4a3f6b">用 Cookie 绑定</button>
<div id="msg2"></div>
</div>
<script>
const token = "__TOKEN__";
const msg = document.getElementById('msg');
const btn = document.getElementById('btn');
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  btn.disabled = true;
  msg.className = '';
  msg.textContent = '正在打开网页登录，约需 30~90 秒，请勿关闭…';
  try {
    const r = await fetch('/nikke/lpass/api/' + token + '/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('email').value.trim(),
        password: document.getElementById('password').value,
      }),
    });
    const data = await r.json();
    if (data.ok) {
      msg.className = 'ok';
      msg.textContent = '绑定成功\\n账号：' + (data.nickname || 'LIP 账号') + '\\n金币：' + data.points;
      btn.textContent = '已完成';
    } else {
      msg.className = 'err';
      msg.textContent = data.msg || '登录失败';
      btn.disabled = false;
    }
  } catch (err) {
    msg.className = 'err';
    msg.textContent = '请求失败：' + err;
    btn.disabled = false;
  }
});

document.getElementById('btn2').addEventListener('click', async () => {
  const box = document.getElementById('msg2');
  box.className = '';
  box.textContent = '正在校验…';
  const r = await fetch('/nikke/lpass/api/' + token + '/cookie', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cookie: document.getElementById('cookie').value}),
  });
  const data = await r.json();
  box.className = data.ok ? 'ok' : 'err';
  box.textContent = data.ok
    ? '绑定成功\n账号：' + (data.nickname || 'LIP 账号') + '\n金币：' + data.points
    : (data.msg || '绑定失败');
});
</script></body></html>"""


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


def _bad(msg: str, status: int = 410) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8">'
        '<body style="background:#12101c;color:#e8e6f0;font:15px system-ui;padding:28px">'
        f"{html.escape(msg)}</body>", status_code=status)


@app.get("/nikke/lpass/{token}", response_class=HTMLResponse)
async def lpass_login_page(token: str):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return _bad("链接已失效，请在聊天里重新发送「Lpass绑定」。")
    return HTMLResponse(PAGE.replace("__TOKEN__", html.escape(token)))


@app.post("/nikke/lpass/api/{token}/login")
async def lpass_login_submit(token: str, payload: LoginBody):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return JSONResponse({"ok": False, "msg": "链接已失效，请重新发送 Lpass绑定。"},
                            status_code=410)
    email = payload.email.strip()
    password = payload.password
    if not email or not password:
        return JSONResponse({"ok": False, "msg": "请填写邮箱和密码。"}, status_code=400)
    if session.status in ("logging_in", "sending_code"):
        return JSONResponse({"ok": False, "msg": "登录正在进行，请稍候。"}, status_code=429)
    session.status = "logging_in"
    try:
        cookie = await login_with_password(email, password, timeout=150)
        summary = await verify(cookie)
    except LoginError as exc:
        session.status = "failed"
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    except LPassAuthError:
        session.status = "failed"
        return JSONResponse({"ok": False,
                             "msg": "登录完成但未拿到有效 LIP 登录态，请改用 Cookie 绑定。"},
                            status_code=400)
    except LPassError as exc:
        session.status = "failed"
        return JSONResponse({"ok": False, "msg": f"登录态校验失败：{exc}"}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        session.status = "failed"
        logger.error(f"[Lpass] 网页登录异常 error_type={type(exc).__name__}")
        return JSONResponse({"ok": False, "msg": "登录过程异常，请稍后重试或改用 Cookie。"},
                            status_code=500)

    nickname = str(summary.get("nickname") or cookie.user_name or "")
    await LPassBind.save_bind(
        session.user_id, session.bot_id, session.group_id,
        cookie_enc=encrypt_cookie(cookie),
        lip_openid=cookie.openid,
        nickname=nickname,
        expire_at=cookie.expire_at,
    )
    session.status = "success"
    logger.info("[Lpass] 网页登录绑定成功")
    return JSONResponse({"ok": True, "nickname": nickname,
                         "points": int(summary.get("points") or 0)})


class CookieBody(BaseModel):
    cookie: str = ""


@app.post("/nikke/lpass/api/{token}/cookie")
async def lpass_login_cookie(token: str, payload: CookieBody):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return JSONResponse({"ok": False, "msg": "链接已失效，请重新发送 Lpass绑定。"},
                            status_code=410)
    cookie = parse_cookie(payload.cookie)
    if cookie is None:
        return JSONResponse({"ok": False,
                             "msg": "这段内容里没有 lip_openid / lip_token，请复制完整 Cookie。"},
                            status_code=400)
    try:
        summary = await verify(cookie)
    except LPassAuthError:
        return JSONResponse({"ok": False, "msg": "Cookie 无效或已过期。"}, status_code=400)
    except LPassError as exc:
        return JSONResponse({"ok": False, "msg": f"校验失败：{exc}"}, status_code=400)
    nickname = str(summary.get("nickname") or cookie.user_name or "")
    await LPassBind.save_bind(
        session.user_id, session.bot_id, session.group_id,
        cookie_enc=encrypt_cookie(cookie), lip_openid=cookie.openid,
        nickname=nickname, expire_at=cookie.expire_at)
    session.status = "success"
    return JSONResponse({"ok": True, "nickname": nickname,
                         "points": int(summary.get("points") or 0)})
