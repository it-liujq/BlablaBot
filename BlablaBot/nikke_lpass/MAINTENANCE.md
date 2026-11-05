# LPass 奖励中心 · 维护说明

`nikke_lpass` 负责 LEVEL INFINITE PASS 奖励中心（<https://pass.levelinfinite.com/rewards>）的绑定与日常，
与 `nikke_login` / `AliceBind` 的 blablalink 游戏账号**完全独立**：这里是 LIP 会话
（`lip_openid` / `lip_token`），那里是 `game_token`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `Lpass绑定` | 无参数返回用法；`Lpass绑定 <Cookie>` 导入登录态；`Lpass绑定 <邮箱> <密码>` 走网页登录 |
| `Lpass日常` | 签到 → 好友请求（页面内自动点）→ 达标任务领取 → 分享链接 |
| `Lpass日常 演练` | 只读：拉金币、任务表，不提交任何写请求 |
| `Lpass好友` | 实验性：无头浏览器打开好友抽屉并点推荐好友的「+」，用接口复查次数 |
| `Lpass自动任务 开启 [HH:MM] / 关闭 / 状态` | 每天北京时间定时执行日常；结果私聊推送；到点未执行会跳过 |
| `Lpass状态` | 金币、七日进度、登录态剩余天数、openid 打码 |
| `Lpass解绑` | 删除本模块的绑定行 |

触发器统一 `prefix=False` 注册，因此不受插件级 `nk` 前缀影响。

## 接口事实（都可复现，勿凭记忆改）

- 只有 `https://api-pass.levelinfinite.com` 提供 API；`pass.levelinfinite.com` 是 COS 静态站，
  `/api/*` 返回 index.html。
- 鉴权是 Cookie 会话，且奖励代理**必须要 `lip_uid`**：只带 `lip_openid/lip_token` 会返回
  `{"code":300001,"msg":"not login lip uid"}`。请求带 `x-common-params`
  （`game_id=30004, area_id=asia, source=2`）与 `x-channel-type: 1`。
- `POST /api/ugc/login` 建立会话，body 字段名必须是长名，且 `lip_channelid` 是 **int32**、
  `lip_expire_time` 是 int64 —— 传字符串会 400 `cannot unmarshal string into ... int32`。
  成功后不额外发会话 Cookie，只是重新下发同一组 `lip_*`。
- 金币：`POST .../Points/GetUserTotalPointsNew`，body `{}`，读
  `data.active_points.total_points`（付费积分在 `data.paid_point`）。
- 任务：`GET .../Points/GetTaskListWithStatusV2?get_top=false&intl_game_id=`，
  `data.tasks[]` 含 `task_id / task_type / task_name / points / is_completed /
  cyclical_type(1 日/2 周) / sign_today / reward_infos[]{stage,points,is_completed,
  need_completed_times,completed_times}`。**task_id 一律现取，不得写死。**
- 签到：`POST .../Points/DailyCheckIn {"task_id": N}`。今日已签有两种返回：
  `code=1001009`，或 `code=1002007` + `msg` 含 `already sign in today`。
  七日阶梯是 `task_type=11`（现网 `task_id=58`，名 `7-Day Coin Bonus`，7 个 stage），
  进度看 `sign_today` 与 `reward_infos[].is_completed`；**先判 `sign_today` 再决定是否发请求**，
  否则每天第二次会撞 1002007。
- 领取：`POST .../Points/CompleteTaskAddPoint {"task_id": N}`；
  一次性任务列表 `GET .../Points/GetDisposableTask?task_type=5`（关注 X / Discord）。
- 每周分享 LI PASS（`task_type=22`，现网 `task_id=84`）：`POST .../Points/GetShareLink
  {"task_id": N}` 返回 `{"status":1,"share_link":"https://pass.levelinfinite.com/rewards?code=..."}`；
  `ClickShareAddPoint {"share_code": ...}` 是**帮别人助力**，不是完成自己的任务。
  分享奖励异步结算，代码里没有常量，只能复查 `active_points.total_points`。

## 改昵称：只能人工

`POST /api/ugc/user/modify_info` 字段名是 `username`（小写别名会 400
`ChangeInfoReq.Username ... required`），但即使字段正确也返回
`500 code=10026 SetLIPUserInfo failed, SetLIPUserInfo parse header result error`
—— 下游需要 LIP 账号服务的额外头部，网页侧由账号中心页面自己带。
因此 `修改用户昵称` 只提示人工，不在 Bot 里改用户资料。

## 好友抽屉：浏览器侧实测（2026-11-05）

纯 HTTP 打不通好友，但官方页面可以。实测结论：

1. **只注入 Cookie 不够**：必须同时写 `localStorage["lip_login_meta"]`（`openid` / `token` /
   `channel_info.channelId`）。缺了前端只会打印
   `[socialSystem] missing openid or token when get invitation list`，抽屉永远打不开。
2. 注入后点任务行的 GO → 右侧「添加好友」抽屉展开（推荐好友列表来自
   `lip_get_recommend_user_list`，同样带签名）。
3. 点一次推荐行的「+」= 一条 `lip_batch_create_invitation`，响应 `lists[].ret=0`；
   连点 3 次后任务 140「[每日]发送3次好友请求」从 0/3 变 3/3，+150 金币到账。
4. **打开好友抽屉本身就会给「[每日]分享交友链接」记账**（2026-11-06 实测：只开抽屉、
   没发任何外链，任务 141 从 0/1 变 1/1，+150 到账）。因此分享任务不需要额外的
   无痕浏览器；`send_friend_requests` 在分享任务未完成时会用「分享交友链接」那行的 GO
   打开抽屉，并补点一次「分享邀请链接」。
4. 抽屉里的「+」是图片按钮，DOM 结构不稳定；`ui.py` 改为按
   「名字元素纵坐标 + 行右侧内缩 24px」坐标点击，避开选择器脆性。
5. 页面在 wujie 的 shadow DOM 里，`document.querySelectorAll` 穿不透，查询要递归
   `shadowRoot`；`page.get_by_text(...)` 这类 Playwright 定位器可以穿透。
6. 「恭喜」弹窗挂在 teleporter 里，会挡住点击，每次执行前要先点掉。

`Lpass日常` 现在会先尝试好友抽屉（未完成时），成功则自动点满 3 次再由接口复查；
失败只降级为提示，不影响签到与领取。`Lpass好友` 可单独重试同一个动作。

## 好友系统：为什么 HTTP 侧是「需人工」

`/v2/friend/*` 在 `https://sg.intlgame.com`，鉴权靠 body 里的 `openid` + `token`，
但请求必须带 `sig`：`?channelid=131&conn=0&gameid=30004&os=3&sdk_version=1.31.0&seq=&sig=<md5>&source=32&ts=<ms>`，
`channelid` 不传 131 会直接 `1006 gameid not valid`。

`sig` 由腾讯 Chaos VM 在页面内生成（`MD5(path?query+body+apikey)` 只是表面形式，
实测按 form 复算返回 `1008 invalid sig`），纯 Python 无法稳定复现。因此：

- `[每日]发送3次好友请求`（`task_type=32`）
- `[每日]分享交友链接`（`task_type=31`）
- `添加1名/5名好友`（`task_type=33`）

一律标记为 `manual` 并给出 `https://pass.levelinfinite.com/friend`。
要真正自动化，只能在 Playwright 里加载官方 SDK 由页面自己签名（相当于 UI 自动化），
不要在 Python 里手搓 sig。

## 自动任务调度

- 复用 Core 的 `gsuid_core.aps.scheduler`，注册一个 60 秒间隔的 `blablabot-lpass-auto` 任务。
- 每个绑定在 `LPassBind` 上存 `auto_enabled / auto_time / last_run_day / last_result`，
  另有 `notify_ws / notify_self`（开启时从事件里取，用于私聊推送）。
- tick 时按 `HH:MM` 与当天日期取到期绑定；执行完写 `last_run_day`，保证一天只跑一次。
- 推送走 `gss.active_bot[ws].target_send(text, 'direct', user_id, bot_id, self_id)`；
  取不到连接就只落库，`Lpass自动任务 状态` 里能看到上次结果。
- 老库升级：`ensure_ready()` 会 `ALTER TABLE lpassbind ADD COLUMN ...` 补列（SQLite）。

## 设计约束

- 任何单步失败都不能中断其它步骤；`DailyResult.steps` 记录
  `done / already / failed / manual / skipped`，报告只写可确证事实。
- `need_completed_times` 已达标（`completed_times >= need`）的任务才自动补领取，
  否则只读不写。
- 好友类任务若将来打通，报告必须带「约10分钟结算」提示，且不得把未结算金币算进差值。
- 游玩游戏（`task_type=2`）纯服务端判定，永远 `manual`。
- Cookie 复用 `nikke_user.service.get_cipher()` 的 `secret.key` 加密落库；日志和异常
  不得出现 token。
- 模型必须在插件 `__init__.py` 里 import 一次才会建表；`Lpass*` 命令的导入必须在
  `guard_plugin_triggers` **之前**，否则绕过 BlablaBot 的用户/群名单守卫。
- Cookie / 账号密码只允许私聊提交。

## 实跑记录（2026-11-06，账号（已脱敏），第二轮）

| 步骤 | 结果 |
| --- | --- |
| `Lpass日常` 签到 | 第 2 天 +50，金币 10089→10139 |
| `Lpass好友`（自动点推荐好友 3 次） | 任务 140 从 0/3 到 3/3，金币 10139→10289（+150） |
| 任务已完成后再次执行 | 直接返回「今日好友请求任务已完成，无需再点」，不再开浏览器 |
| 同日再跑 `Lpass日常` | 显示「今日已签（2/7）」、分享链接、其余 5 项已完成 |

## 实跑记录（2026-11-05，账号（已脱敏））

| 步骤 | 结果 |
| --- | --- |
| `POST /api/ugc/login` | `code=0`（确认 body 需 int 型 `lip_channelid`） |
| `POST GetUserTotalPointsNew` | `active_points.total_points=9314` |
| `GET GetTaskListWithStatusV2` | 18 条任务；七日阶梯 `task_id=58`、`sign_today=false` |
| `POST DailyCheckIn {"task_id":"58"}` | `data.status=1`，金币 9314→9364（+50），stage1 完成 |
| 同日第二次 `Lpass日常` | 判 `sign_today=true`，不再发请求，报告「今日已签（1/7）」 |
| `POST GetShareLink {"task_id":"84"}` | 返回 `.../rewards?code=28bb...` |
| `POST /api/ugc/user/modify_info` | 500 `10026`，改昵称转为需人工 |

## 测试

```bash
python -m unittest discover -s tests -v      # 离线：httpx MockTransport
python -m ruff check BlablaBot/nikke_lpass    # 与仓库其它模块同基线
```
