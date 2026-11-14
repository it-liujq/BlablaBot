# 本会话修改记录

本文件记录本次会话中针对 BlablaBot（原 AliceUID）所做的全部修改：问题、原因、处理与验证。

## 1. 帮助命令崩溃 `KeyError: 'plugins.BlablaBot.BlablaBot'`

- **问题**：`#bla帮助` 执行时报 `KeyError: 'plugins.BlablaBot.BlablaBot'`。
- **原因**：`BlablaBot/nikke_community`、`BlablaBot/nikke_permissions` 缺少 `__init__.py`，被 Python 当作命名空间包；帮助在运行时延迟导入 `..nikke_community.runtime`，热重载后父包不在 `sys.modules`，命名空间路径重算触发 KeyError。
- **处理**：补齐两个 `__init__.py`；`nikke_help/draw.py` 中对 `configured_sign_time` 的导入加 try/except 兜底（默认 `08:33`）。
- **验证**：本地 `python -m unittest discover -s tests` 通过；新增守卫测试检查每个含 .py 的目录都有 `__init__.py`。

## 2. 插件整体导入失败导致大量命令消失

- **问题**：部署后 `#bla帮助` 之外的多数命令消失，SV 服务配置少了一大片。
- **原因**：合并上游时品牌替换脚本把 `AliceUIDConfig` 误改成 `BlablaBotConfig`，`nikke_character/commands.py` 导入失败，`__init__.py` 顺序导入在第 60 行中断，后续所有模块未注册。
- **处理**：改回 `AliceUIDConfig`；`nikke_config/config.py` 增加 `BlablaBotConfig = AliceUIDConfig` 别名兼容；新增守卫测试禁止其他文件出现 `BlablaBotConfig`。
- **验证**：`ruff` F821 全包通过；`unittest` 31 项通过。

## 3. `#LPass解绑` 装饰器被拼成 `a@b`

- **问题**：`TypeError: unsupported operand type(s) for @: 'function' and 'function'`，LPass 及之后模块未注册。
- **原因**：补丁脚本把两行装饰器合并成一行，Python 解析成矩阵乘法。
- **处理**：恢复单行装饰器；`__init__.py` 中 LPass 导入块单独 try/except，避免一个模块失败连带全部命令。
- **验证**：新增守卫测试扫描 AST，装饰器中不允许出现 BinOp；桩环境完整导入 LPass 各模块成功。

## 4. LPass 日常金币统计与好友请求显示错误

- **问题**：报告金币增量与流水不符；好友请求已 3/3 却显示 0/3。
- **原因**：金币用余额差计算；报告用的是动作前的任务快照（好友请求在浏览器里完成）。
- **处理**：金币改用记录页流水；动作后调用 `refresh_tasks()` 再渲染；结算只等本轮新完成项，无新完成项立即返回。
- **验证**：实跑账号，报告显示 `金币:+500（今日流水 6 条）`、好友请求 3/3。

## 5. `#LPass日常` 连发两条相同结果

- **问题**：一次命令收到两条完整报告。
- **原因**：上一版补丁残留了第二段结算逻辑。
- **处理**：删除重复结算块，只保留一处结算与一次补充消息。

## 6. 好友请求点不到「+」

- **问题**：`#LPass日常` 好友请求长期 0/3，日志显示点击 0 次。
- **原因**：抽屉内容在 `pass.levelinfinite.com/friend` 子 frame 内；真正按钮是名字右侧 `span.anticon`，名字块内的空 div 只是占位；旧代码按坐标点击主文档。
- **处理**：在 `/friend` frame 内枚举行，按「名字 → 同级 anticon」取坐标后 `mouse.click`；抽屉可用性以能否枚举出加号为准。
- **验证**：实测点击 3 次，站点返回 `lip_batch_create_invitation ret:0`，任务 0/3 → 3/3。

## 7. 分享交友链接任务无法完成

- **问题**：`[每日]分享交友链接到社交媒体1次` 一直 0/1。
- **原因**：复制按钮坐标算错（超出视口）；链接兜底正则匹配到了字体文件。
- **处理**：点邀请文案同级的 `span.anticon` 复制图标，链接直接从文案 `[https://...searchUser&searchUserName=xx]` 提取，再用无痕窗口打开；`render_compact` 始终输出交友邀请链接。
- **验证**：点击复制后任务 0/1 → 1/1。

## 8. 全量签到与命令精简（参考 #bla）

- **需求**：新增 `#LPass开启全量签到`、`#LPass签到` 别名、签到完成通知、群内按账号依次执行、迁移 08:30 用户、删除 `#LPass日常`/`#LPass自动任务`、大小写不敏感。
- **处理**：
  - `#LPass开启全量签到 [HH:MM]`（默认 08:45，与 #bla 08:33 错峰，开启后立即执行一次，支持 关闭/状态）；
  - `#LPass签到` 为唯一日常入口，默认简要结果，`完整日志` 输出原完整报告；
  - 群内按账号串行执行，完成后发 `✅ [BlablaBot] LIPASS今日自动签到已完成！` 汇总；
  - 启动时把 `auto_enabled=1 且 auto_time='08:30'` 的绑定迁移到 08:45；
  - 每个命令逐个注册 原样/小写/大写 三种触发器。
- **验证**：31 项单元测试通过；帮助页与维护文档同步更新。

## 9. 帮助图仍显示已删除命令

- **原因**：帮助图片有 30 天磁盘缓存，`CACHE_VERSION` 未变。
- **处理**：提升 `CACHE_VERSION`，并删除重复的 `#LPass开启全量签到` 帮助条目。

## 10. 账号密码登录：验证码与网关超时

- **问题**：登录接口被 Cloudflare 100 秒网关超时打断返回 524 HTML；验证码无法在页面完成；出现「验证码截图失败」「点提交无反应」。
- **处理**：
  - 登录改为「立即返回 + 轮询状态」，避免网关超时；
  - 新增 `LoginFlow` / `BrowserLoginFlow`：后台真实 Chromium 打开 LIP 页面，页面内嵌远程视图（截图 + 输入转发）；
  - 只截取登录弹窗区域（含 LEVEL INFINITE logo），2x DPI + JPEG 85，1:1 显示；
  - 去掉外层标题栏/边框/多余按钮，仅保留登录框自带关闭；
  - Ctrl+V 通过 paste 事件转发剪贴板文本，Ctrl+A/C/X 转发按键；
  - 每秒轮询，检测到 `lip_*` Cookie 立即判定成功、隐藏窗口并保存绑定；
  - `/screen` 截图异常改为 503 + 日志；补回 `fastapi.Response` 导入修复 500。
- **验证**：本地演示登录后抓到 Cookie（openid `1148****9457`，剩余 29 天），用该 Cookie 调用 LPass 接口成功：`points=15689`、`tasks=18`。

## 11. 反向代理方案（已废弃）

- **尝试**：把 `pass.levelinfinite.com` 反代到同域，用 iframe 直接登录。
- **结论**：LIP 为 wujie 微前端 + 多子域 + Service Worker，子应用 iframe 使用根路径、SW scope 限制、多个外部接口 CORS，代理无法完整还原；相关提交与代码已删除。

## 12. 合并上游 1.1.37 → 1.2.07

- **内容**：Dildoro 赛季/区服识别、风控策略与刷新速度、妮姬公开/隐藏隐私命令（`nikke_privacy`）、阵容查询临时解锁、信息卡重发修复、帮助图样式、日志与用户服务调整。
- **处理**：新增文件整包复制并做品牌替换；冲突文件（`nikke_help/cache.py`、`nikke_help/draw.py`、`nikke_login/commands.py`、`version.py`、`README.md`）取上游内容后重新套用 `#bla` 前缀与 BlablaBot 品牌；修正上游新代码里被误替换的 `BlablaBotConfig`（`nikke_help/cache.py`、`nikke_login/commands.py`、`nikke_user/pacing.py`）。
- **验证**：`compileall` 全包通过；`ruff` F821 全包通过；31 项单元测试通过；确认 `#LPass*` 五个命令、`#bla` 前缀、`nikke_privacy` 注册均存在；触发器重复检查 0 冲突。

## 验证方式汇总

```bash
python -m compileall -q BlablaBot
python -m ruff check BlablaBot --select F821,F822,F823,E9
python -m unittest discover -s tests
```

## 遗留与注意

- `#LPass` 绑定链接有效期 10 分钟，过期需重新发送命令。
- 帮助图片缓存 30 天，改动帮助内容后需提升 `CACHE_VERSION`。
- 真实腾讯验证码需在远程视图中人工完成，无法离线自动化。
