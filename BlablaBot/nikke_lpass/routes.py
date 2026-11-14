"""LPass 绑定网页：/nikke/lpass/{token}

布局与 #bla登录 一致：账号密码 / 粘贴 Cookie 用 tab 切换。
登录改成「立即返回 + 轮询状态」：Playwright 登录要 30~90 秒，直接同步等待会被
Cloudflare 的 100 秒网关超时打断（返回 524 HTML，前端 JSON 解析报错）。
"""
from __future__ import annotations

import asyncio
import html
import time

from fastapi.responses import HTMLResponse, JSONResponse
from gsuid_core.logger import logger
from gsuid_core.web_app import app
from pydantic import BaseModel

from ..nikke_login.session import sessions
from .api import LPassAuthError, LPassError, parse_cookie
from .auth import LoginFlow, verify
from .store import LPassBind, encrypt_cookie

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>LI PASS 账号绑定</title>
<style>
  :root{--ink:#1f2430;--muted:rgba(255,255,255,.62);--pink:#7c4dff;--pink-dark:#6a3ff0;}
  *{box-sizing:border-box}
  html,body{min-height:100%}
  body{
    margin:0;color:#fff;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    background:#0b0d16 url('/nikke/assets/ALICEUID_Login_liberalio_.jpg') center/cover no-repeat fixed;
    display:flex;align-items:center;justify-content:flex-end;
    padding:32px 7vw;
  }
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;
    background:linear-gradient(90deg,rgba(8,9,15,.10) 0%,rgba(8,9,15,.32) 42%,rgba(8,9,15,.78) 100%);
  }
  .card{
    position:relative;z-index:1;width:min(400px,100%);
    background:rgba(12,14,24,.80);
    border:1px solid rgba(255,255,255,.22);
    border-radius:20px;
    box-shadow:0 24px 70px rgba(0,0,0,.55);
    backdrop-filter:blur(18px) saturate(120%);
    -webkit-backdrop-filter:blur(18px) saturate(120%);
    padding:28px 28px 24px;
    animation:fadeUp .55s cubic-bezier(.2,.8,.2,1);
  }
  @keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  .brand{display:flex;align-items:center;gap:14px;margin-bottom:20px}
  .brand img{
    width:56px;height:56px;border-radius:14px;object-fit:cover;
    border:1px solid rgba(255,255,255,.35);
    box-shadow:0 6px 18px rgba(0,0,0,.35);
  }
  .brand h1{margin:0;font-size:19px;font-weight:600;letter-spacing:.01em}
  .brand p{margin:5px 0 0;font-size:12px;color:var(--muted)}
  label{display:block;font-size:12px;color:#fff;font-weight:500;margin:14px 0 7px}
  input[type=email],input[type=password]{
    width:100%;height:46px;border:1px solid rgba(255,255,255,.25);
    border-radius:10px;padding:0 12px;font-size:15px;color:var(--ink);
    background:rgba(255,255,255,.92);outline:none;transition:.18s;
  }
  input[type=email]:focus,input[type=password]:focus{
    border-color:rgba(124,77,255,.85);
    box-shadow:0 0 0 3px rgba(124,77,255,.18);
    background:#fff;
  }
  textarea{
    width:100%;height:120px;resize:vertical;padding:12px;
    border:1px solid rgba(255,255,255,.25);border-radius:10px;
    font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
    color:var(--ink);background:rgba(255,255,255,.92);outline:none;
  }
  textarea:focus{border-color:rgba(124,77,255,.85);box-shadow:0 0 0 3px rgba(124,77,255,.18)}
  .btn{
    width:100%;height:46px;border:0;border-radius:10px;margin-top:20px;
    background:var(--pink);color:#fff;font-size:15px;font-weight:600;
    cursor:pointer;transition:.18s;
  }
  .btn:hover{background:var(--pink-dark);transform:translateY(-1px)}
  .btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
  .btn.ghost{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.28);color:#fff}
  .btn.ghost:hover{background:rgba(255,255,255,.14)}
  .modes{display:flex;gap:8px;margin-top:12px}
  .modes button{margin-top:0;font-size:13px}
  .modes [aria-selected=true]{background:var(--pink)}
  [hidden]{display:none!important}
  .hint{font-size:11.5px;color:rgba(255,255,255,.66);line-height:1.7;margin-top:14px}
  .status{display:none;margin-top:16px;padding:11px 13px;border-radius:10px;
          font-size:12.5px;line-height:1.6;white-space:pre-wrap}
  .status.show{display:block}
  .status.info{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.14);color:#e9ebf4}
  .status.err{background:rgba(207,75,69,.18);border:1px solid rgba(207,75,69,.38);color:#ffb8b2}
  .status.ok{background:rgba(47,158,115,.18);border:1px solid rgba(47,158,115,.38);color:#a8e6cb}
  .modes .btn.suggest{
    border-color:rgba(124,77,255,.9);background:rgba(124,77,255,.18);
    box-shadow:0 0 0 2px rgba(124,77,255,.22);animation:pulse 1.6s infinite;
  }
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.72}}
  .foot{margin-top:18px;font-size:11px;color:rgba(255,255,255,.45);line-height:1.7}
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <img src="/nikke/assets/ICON.png?v=1.0.0" alt="LI PASS">
    <div>
      <h1>LI PASS 账号绑定</h1>
      <p>pass.levelinfinite.com 奖励中心</p>
    </div>
  </div>

  <form id="form">
    <p class="hint">此链接 <b>{{TTL}} 分钟内有效</b>。绑定后 Bot 会保存登录态用于每日任务，账号密码只发给本机 Core，不写入日志。</p>
    <div class="modes" role="tablist" aria-label="登录方式">
      <button class="btn ghost" id="mode-password" type="button" role="tab" aria-selected="true">账号密码</button>
      <button class="btn ghost" id="mode-cookie" type="button" role="tab" aria-selected="false">粘贴 Cookie</button>
    </div>

    <div id="password-fields">
      <label for="email">邮箱地址</label>
      <input id="email" type="email" autocomplete="username" placeholder="you@example.com">
      <label for="password">密码</label>
      <input id="password" type="password" autocomplete="current-password" placeholder="LI PASS 密码">
      <p class="hint">登录在后台进行，页面会自动刷新进度（约 30~90 秒）。若出现人机验证，请改用「粘贴 Cookie」。</p>
    </div>

    <div id="cookie-fields" hidden>
      <label for="cookie-input">粘贴 Cookie</label>
      <textarea id="cookie-input" placeholder="lip_openid=...; lip_token=...; lip_uid=..." autocomplete="off" spellcheck="false" disabled></textarea>
      <p class="hint">已登录 pass.levelinfinite.com 时，从开发者工具复制整段 Cookie；至少包含 lip_openid 与 lip_token。</p>
    </div>

    <button class="btn" id="submit" type="submit">登录并绑定</button>
  </form>

  <div class="status" id="status"></div>

  <div class="status info" id="challenge" style="display:none">
    <div id="challenge-tip"></div>
    <div id="challenge-img-wrap" style="display:none;position:relative;margin-top:8px">
      <img id="challenge-img" alt="人机验证" style="width:100%;max-width:100%;border-radius:8px;display:block">
    </div>
    <div id="challenge-slider" style="display:none;margin-top:10px">
      <input type="range" id="challenge-dx" min="0" max="320" value="0" style="width:100%">
      <button class="btn ghost" id="challenge-send" type="button" style="margin-top:8px">提交验证</button>
    </div>
    <div id="challenge-code-box" style="display:none;margin-top:10px">
      <input id="challenge-code" placeholder="邮箱验证码" style="width:100%;height:42px;border-radius:8px;border:1px solid #bbb;padding:0 10px">
      <button class="btn ghost" id="challenge-code-send" type="button" style="margin-top:8px">提交验证码</button>
    </div>
  </div>
  <p class="foot">绑定成功后推荐发送 #LPass开启全量签到，每天自动签到。</p>
</div>

<script>
const TOKEN = "{{TOKEN}}";
const $ = (id) => document.getElementById(id);
const statusBox = $("status");
let authMode = "password";
let pollTimer = null;

function setStatus(text, kind) {
  statusBox.className = "status show " + (kind || "info");
  statusBox.textContent = text;
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
function showMode(mode) {
  authMode = mode;
  $("password-fields").hidden = mode !== "password";
  $("cookie-fields").hidden = mode !== "cookie";
  $("email").disabled = $("password").disabled = mode !== "password";
  $("cookie-input").disabled = mode !== "cookie";
  $("mode-password").setAttribute("aria-selected", String(mode === "password"));
  $("mode-cookie").setAttribute("aria-selected", String(mode === "cookie"));
  $("submit").textContent = mode === "cookie" ? "校验 Cookie 并绑定" : "登录并绑定";
  statusBox.className = "status";
  stopPoll();
}

// 网关超时时会返回 HTML，直接 r.json() 会抛 SyntaxError，这里先按文本读。
async function readJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    if (text.trim().startsWith("<")) {
      return { ok: false, msg: "服务端返回了网页而不是数据（网关超时或反代异常），请稍后重试。" };
    }
    return { ok: false, msg: "返回格式异常：" + text.slice(0, 120) };
  }
}

function onSuccess(data) {
  setStatus(data.message || ("绑定成功，账号：" + (data.nickname || "LI PASS 账号")), "ok");
  $("submit").textContent = "已完成";
  $("submit").disabled = true;
  $("mode-password").disabled = $("mode-cookie").disabled = true;
  stopPoll();
}

function onFailure(data) {
  setStatus(data.msg || "绑定失败", "err");
  $("submit").disabled = false;
  if (authMode === "password") $("mode-cookie").classList.add("suggest");
  stopPoll();
}

function showChallenge(data) {
  const box = $("challenge");
  box.style.display = "block";
  $("challenge-tip").textContent = data.message || "请完成验证";
  window.__captchaWidth = data.captcha_width || 0;
  if (data.status === "captcha") {
    $("challenge-img").src = data.image;
    $("challenge-img").style.display = "block";
    $("challenge-img-wrap").style.display = "block";
    $("challenge-slider").style.display = "block";
    $("challenge-code-box").style.display = "none";
  } else {
    $("challenge-img-wrap").style.display = "none";
    $("challenge-slider").style.display = "none";
    $("challenge-code-box").style.display = "block";
  }
}

async function sendChallenge(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const data = await readJson(resp);
  if (data.status === "success") { $("challenge").style.display = "none"; onSuccess(data); return; }
  if (data.status === "failed") { $("challenge").style.display = "none"; onFailure(data); return; }
  showChallenge(data);
}

$("challenge-img-wrap").addEventListener("click", (e) => {
  const img = $("challenge-img");
  const r = img.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return;
  const x = (e.clientX - r.left) / r.width;
  const y = (e.clientY - r.top) / r.height;
  window.__points = window.__points || [];
  window.__points.push({ x: x, y: y });
  const dot = document.createElement("div");
  dot.className = "cap-dot";
  dot.style.cssText = "position:absolute;width:16px;height:16px;margin:-8px;border-radius:50%;background:#ff3b30;border:2px solid #fff;pointer-events:none;left:" + (e.clientX - r.left) + "px;top:" + (e.clientY - r.top) + "px";
  $("challenge-img-wrap").appendChild(dot);
});

$("challenge-send").addEventListener("click", () => {
  const img = $("challenge-img");
  const shown = img.clientWidth || 1;
  const scale = window.__captchaWidth ? window.__captchaWidth / shown : 1;
  const dx = Number($("challenge-dx").value) * scale;
  sendChallenge("/nikke/lpass/api/" + TOKEN + "/captcha", { dx: dx, points: window.__points || [] });
});
$("challenge-code-send").addEventListener("click", () => {
  sendChallenge("/nikke/lpass/api/" + TOKEN + "/email-code", { code: $("challenge-code").value });
});

async function pollStatus() {
  try {
    const resp = await fetch("/nikke/lpass/api/" + TOKEN + "/status");
    const data = await readJson(resp);
    if (data.status === "success") { onSuccess(data); return; }
    if (data.status === "failed") { onFailure(data); return; }
    if (data.status === "captcha" || data.status === "email_code") {
      setStatus("需要你完成验证", "info");
      showChallenge(data);
      return;
    }
    if (data.status === "logging_in") {
      setStatus("正在登录中…（约 30~90 秒，请勿关闭页面）", "info");
      return;
    }
    onFailure(data);
  } catch (err) {
    setStatus("查询进度失败：" + err, "err");
  }
}

$("mode-password").addEventListener("click", () => showMode("password"));
$("mode-cookie").addEventListener("click", () => showMode("cookie"));

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = $("submit");
  submit.disabled = true;
  try {
    if (authMode === "cookie") {
      setStatus("正在校验 Cookie…", "info");
      const resp = await fetch("/nikke/lpass/api/" + TOKEN + "/cookie", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookie: $("cookie-input").value })
      });
      const data = await readJson(resp);
      if (data.ok) onSuccess(data); else onFailure(data);
      return;
    }

    setStatus("已提交登录，正在后台打开网页…", "info");
    const resp = await fetch("/nikke/lpass/api/" + TOKEN + "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("email").value.trim(),
        password: $("password").value
      })
    });
    const data = await readJson(resp);
    if (!data.ok && data.status !== "logging_in") { onFailure(data); return; }
    setStatus("正在登录中…（约 30~90 秒，请勿关闭页面）", "info");
    stopPoll();
    pollTimer = setInterval(pollStatus, 2500);
  } catch (err) {
    setStatus("请求失败：" + err, "err");
    submit.disabled = false;
  }
});
</script>
</body>
</html>"""


# token -> 成功载荷（含金币与剩余天数）；页面轮询直接读这里
_RESULTS: dict[str, dict] = {}


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


class CookieBody(BaseModel):
    cookie: str = ""


class CaptchaPoint(BaseModel):
    x: float = 0
    y: float = 0


class CaptchaBody(BaseModel):
    dx: float = 0
    points: list[CaptchaPoint] = []


class EmailCodeBody(BaseModel):
    code: str = ""


def _bad(msg: str, status: int = 410) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8">'
        '<body style="background:#12101c;color:#e8e6f0;font:15px system-ui;padding:28px">'
        f"{html.escape(msg)}</body>", status_code=status)


async def _notify_session(session, text: str) -> None:
    """把绑定结果发回聊天（与 #bla登录 一致：群里发群、私聊发私聊）。"""
    ws = str(getattr(session, "notify_ws", "") or "")
    if not ws:
        return
    try:
        from gsuid_core.gss import gss

        transport = gss.active_bot.get(ws)
        if transport is None:
            return
        group = str(session.group_id or "")
        if group:
            await transport.target_send(text, "group", group, session.bot_id,
                                        str(getattr(session, "notify_self", "") or ""),
                                        group_id=group, at_sender=False, wait_recall=True)
        else:
            await transport.target_send(text, "direct", str(session.user_id),
                                        session.bot_id,
                                        str(getattr(session, "notify_self", "") or ""),
                                        at_sender=True, wait_recall=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[Lpass] 绑定结果推送失败 error_type={type(exc).__name__}")


def _mask(openid: str) -> str:
    if not openid:
        return ""
    return openid[:4] + "*" * 8 + openid[-4:]


def _success_payload(nickname: str, points: int, days: int, openid: str) -> dict:
    """与 #bla登录 一致：明确告知已绑定，并给出账号、金币与剩余天数。"""
    lines = ["绑定成功", f"账号：{nickname or 'LI PASS 账号'}", f"金币：{points}"]
    lines.append(f"登录态剩余约 {days} 天" if days else "登录态有效期未知，失效后需重新绑定")
    lines.append("推荐发送 #LPass开启全量签到，每天自动签到")
    return {"ok": True, "status": "success", "nickname": nickname, "points": points,
            "days": days, "masked": _mask(openid), "message": "\n".join(lines)}


_FLOWS: dict[str, LoginFlow] = {}


async def _complete_binding(token: str, session, cookie):
    """登录成功后统一落库、更新会话并推送聊天。"""
    try:
        summary = await verify(cookie)
    except LPassAuthError:
        session.status = "failed"
        session.error = "登录完成但未拿到有效 LIP 登录态，请改用 Cookie 绑定。"
        await _notify_session(session, "LEVELINFINITEPASS绑定失败：未拿到有效登录态，请改用 Cookie 绑定。")
        return
    except LPassError as exc:
        session.status = "failed"
        session.error = f"登录态校验失败：{exc}"
        await _notify_session(session, f"LEVELINFINITEPASS绑定失败：{exc}")
        return
    nickname = str(summary.get("nickname") or cookie.user_name or "")
    try:
        await LPassBind.save_bind(
            session.user_id, session.bot_id, session.group_id,
            cookie_enc=encrypt_cookie(cookie),
            lip_openid=cookie.openid,
            nickname=nickname,
            expire_at=cookie.expire_at,
        )
    except Exception as exc:  # noqa: BLE001
        session.status = "failed"
        session.error = "登录成功但保存绑定失败，请稍后重试。"
        logger.error(f"[Lpass] 绑定保存异常 error_type={type(exc).__name__}")
        await _notify_session(session, "LEVELINFINITEPASS绑定失败：保存绑定失败，请稍后重试。")
        return
    session.nickname = nickname
    session.status = "success"
    payload = _success_payload(nickname, int(summary.get("points") or 0),
                               cookie.days_left, cookie.openid)
    _RESULTS[token] = payload
    _FLOWS.pop(token, None)
    logger.info("[Lpass] 网页登录绑定成功")
    await _notify_session(session, "LEVELINFINITEPASS" + payload["message"])


async def _finish_login(token: str, email: str, password: str) -> None:
    """后台任务：启动可交互登录；遇到验证码/邮箱码时把控制权交给绑定页。"""
    session = sessions.get(token)
    if session is None:
        return
    flow = LoginFlow(email, password)
    _FLOWS[token] = flow
    session.login_flow = flow
    try:
        await flow.start()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[Lpass] 交互登录启动异常 error_type={type(exc).__name__}")
        await flow._fail("登录启动异常，请稍后重试或改用 Cookie。")
    if flow.status == "success" and flow.cookie is not None:
        await _complete_binding(token, session, flow.cookie)
        return
    if flow.status in ("captcha", "email_code"):
        session.status = flow.status
        session.error = ""
        return
    session.status = "failed"
    session.error = flow.error or "登录未完成，请稍后重试或改用 Cookie。"
    _FLOWS.pop(token, None)
    await _notify_session(session, "LEVELINFINITEPASS绑定失败：" + session.error[:200])


@app.get("/nikke/lpass/{token}", response_class=HTMLResponse)
async def lpass_login_page(token: str):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return _bad("链接已失效，请在聊天里重新发送「#LPass绑定」。")
    ttl = max(1, int((session.created_at + session.ttl - time.time()) // 60))
    return HTMLResponse(PAGE.replace("{{TOKEN}}", html.escape(token))
                        .replace("{{TTL}}", str(ttl)))


@app.post("/nikke/lpass/api/{token}/login")
async def lpass_login_submit(token: str, payload: LoginBody):
    """立即返回并启动后台登录，避免被网关 100 秒超时打断。"""
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "链接已失效，请重新发送 #LPass绑定。"}, status_code=410)
    if session.status == "success":
        cached = _RESULTS.get(token)
        if cached:
            return JSONResponse(cached)
        return JSONResponse({"ok": True, "status": "success",
                             "nickname": session.nickname,
                             "message": f"绑定成功\n账号：{session.nickname}"})
    email = payload.email.strip()
    password = payload.password
    if not email or not password:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "请填写邮箱和密码。"}, status_code=400)
    if session.status == "logging_in":
        return JSONResponse({"ok": True, "status": "logging_in"})
    session.status = "logging_in"
    session.error = ""
    asyncio.create_task(_finish_login(token, email, password))
    return JSONResponse({"ok": True, "status": "logging_in"})


@app.get("/nikke/lpass/api/{token}/status")
async def lpass_login_status(token: str):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "链接已失效，请重新发送 #LPass绑定。"}, status_code=410)
    if session.status == "success":
        cached = _RESULTS.get(token)
        if cached:
            return JSONResponse(cached)
        return JSONResponse({"ok": True, "status": "success",
                             "nickname": session.nickname,
                             "message": f"绑定成功\n账号：{session.nickname}"})
    if session.status == "failed":
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": session.error or "登录失败，请改用 Cookie 绑定。"})
    if session.status in ("captcha", "email_code"):
        flow = getattr(session, "login_flow", None)
        if flow is None:
            return JSONResponse({"ok": False, "status": "failed",
                                 "msg": "验证会话已失效，请重新发起登录或改用 Cookie。"})
        return JSONResponse({"ok": True, "status": session.status,
                             "message": flow.message,
                             "image": flow.captcha_png if session.status == "captcha" else "",
                             "captcha_width": getattr(flow, "captcha_width", 0)})
    return JSONResponse({"ok": True, "status": "logging_in"})


async def _challenge_result(token: str, session, flow):
    if flow.status == "success" and flow.cookie is not None:
        await _complete_binding(token, session, flow.cookie)
        cached = _RESULTS.get(token)
        if cached:
            return JSONResponse(cached)
        return JSONResponse({"ok": True, "status": "success",
                             "nickname": session.nickname,
                             "message": f"绑定成功\n账号：{session.nickname}"})
    if flow.status == "failed":
        session.status = "failed"
        session.error = flow.error or "验证失败，请重试或改用 Cookie。"
        return JSONResponse({"ok": False, "status": "failed", "msg": session.error})
    session.status = flow.status
    return JSONResponse({"ok": True, "status": flow.status,
                         "message": flow.message,
                         "image": flow.captcha_png if flow.status == "captcha" else "",
                         "captcha_width": getattr(flow, "captcha_width", 0)})


@app.post("/nikke/lpass/api/{token}/captcha")
async def lpass_login_captcha(token: str, payload: CaptchaBody):
    session = sessions.get(token)
    flow = getattr(session, "login_flow", None) if session else None
    if session is None or flow is None:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "验证会话已失效，请重新发起登录或改用 Cookie。"}, status_code=410)
    await flow.submit_captcha(payload.dx, [p.model_dump() for p in payload.points])
    return await _challenge_result(token, session, flow)


@app.post("/nikke/lpass/api/{token}/email-code")
async def lpass_login_email_code(token: str, payload: EmailCodeBody):
    session = sessions.get(token)
    flow = getattr(session, "login_flow", None) if session else None
    if session is None or flow is None:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "验证会话已失效，请重新发起登录或改用 Cookie。"}, status_code=410)
    await flow.submit_email_code(payload.code)
    return await _challenge_result(token, session, flow)


@app.post("/nikke/lpass/api/{token}/cookie")
async def lpass_login_cookie(token: str, payload: CookieBody):
    session = sessions.get(token)
    if session is None or session.mode != "lpass":
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "链接已失效，请重新发送 #LPass绑定。"}, status_code=410)
    cookie = parse_cookie(payload.cookie)
    if cookie is None:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "这段内容里没有 lip_openid / lip_token，请复制完整 Cookie。"},
                            status_code=400)
    try:
        summary = await verify(cookie)
    except LPassAuthError:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "Cookie 无效或已过期。"}, status_code=400)
    except LPassError as exc:
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": f"校验失败：{exc}"}, status_code=400)
    nickname = str(summary.get("nickname") or cookie.user_name or "")
    try:
        await LPassBind.save_bind(
            session.user_id, session.bot_id, session.group_id,
            cookie_enc=encrypt_cookie(cookie), lip_openid=cookie.openid,
            nickname=nickname, expire_at=cookie.expire_at)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[Lpass] Cookie 绑定保存异常 error_type={type(exc).__name__}")
        return JSONResponse({"ok": False, "status": "failed",
                             "msg": "校验通过但保存绑定失败，请稍后重试。"}, status_code=500)
    session.nickname = nickname
    session.status = "success"
    result = _success_payload(nickname, int(summary.get("points") or 0),
                              cookie.days_left, cookie.openid)
    _RESULTS[token] = result
    await _notify_session(session, "LEVELINFINITEPASS" + result["message"])
    return JSONResponse(result)
