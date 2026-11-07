"""LPass 模块离线测试：不打真实网络，只验证协议解析与报告口径。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 让 BlablaBot/__init__.py 跳过 GsCore 相关注册，只做纯逻辑测试
os.environ.setdefault("BLABLABOT_STANDALONE", "1")
os.environ.setdefault("LPASS_NO_BROWSER", "1")

from BlablaBot.nikke_lpass.api import (  # noqa: E402
    LPassAPI,
    LPassAuthError,
    LPassCookie,
    LPassError,
    ladder_progress,
    parse_cookie,
)
from BlablaBot.nikke_lpass.report import render  # noqa: E402
from BlablaBot.nikke_lpass.tasks import DailyRunner, classify  # noqa: E402

COOKIE_TEXT = (
    "lip_user_name=Player_abc; lip_openid=1234567890; lip_token=tok-xyz; "
    "lip_channelid=131; lip_adult_status=1; game_token=should_be_ignored"
)


def cookie() -> LPassCookie:
    parsed = parse_cookie(COOKIE_TEXT)
    assert parsed is not None
    return parsed


async def _fast_sleep(_seconds):
    return None


def api_with(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LPassAPI(cookie(), http=client)


POINTS_OK = {"code": 0, "data": {"active_points": {"total_points": 5735,
                                                   "total_points_earned": 9000,
                                                   "total_points_consumed": 100},
                                 "paid_point": {"total_points": 300}}}


def tasks_payload(*tasks):
    return {"code": 0, "data": {"tasks": list(tasks)}}


SIGNIN_TASK = {"task_id": "15", "task_type": 11, "task_name": "7-Day Sign-in",
               "is_completed": False, "sign_today": False,
               "reward_infos": [{"stage": 1, "points": 20, "is_completed": True},
                                {"stage": 2, "points": 20, "is_completed": True},
                                {"stage": 3, "points": 20, "is_completed": False}]}
FRIEND_TASK = {"task_id": "32", "task_type": 32, "task_name": "发送3次好友请求",
               "cyclical_type": 1, "is_completed": False,
               "reward_infos": [{"points": 150, "need_completed_times": 3,
                                 "completed_times": 0}]}
GAME_TASK = {"task_id": "2", "task_type": 2, "task_name": "游玩游戏",
             "is_completed": False, "reward_infos": [{"points": 100}]}
SHARE_TASK = {"task_id": "22", "task_type": 22, "task_name": "分享 LI PASS",
              "is_completed": False, "reward_infos": [{"points": 100}]}


class CookieTest(unittest.TestCase):
    def test_parse_picks_lip_keys_only(self):
        parsed = parse_cookie(COOKIE_TEXT)
        self.assertEqual(parsed.openid, "1234567890")
        self.assertEqual(parsed.token, "tok-xyz")
        self.assertEqual(parsed.channel_id, "131")
        self.assertNotIn("game_token", parsed.header())

    def test_login_body_uses_long_names(self):
        body = cookie().login_body()
        self.assertEqual(body["lip_openid"], "1234567890")
        self.assertEqual(body["lip_token"], "tok-xyz")
        self.assertNotIn("openid", body)

    def test_parse_requires_openid_and_token(self):
        self.assertIsNone(parse_cookie("lip_openid=1"))
        self.assertIsNone(parse_cookie(""))

    def test_expire_seconds_normalised(self):
        parsed = parse_cookie("lip_openid=1; lip_token=t; lip_expire_time=1893456000000")
        self.assertEqual(parsed.expire_at, 1893456000)


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_points_reads_active_points(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json=POINTS_OK)

        async with api_with(handler) as api:
            points = await api.points()
        self.assertEqual(points["total"], 5735)
        self.assertEqual(points["paid"], 300)
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["path"].endswith("GetUserTotalPointsNew"))

    async def test_auth_error_on_300001(self):
        def handler(request):
            return httpx.Response(200, json={"code": 300001,
                                             "msg": "not login lip openid", "data": None})

        async with api_with(handler) as api:
            with self.assertRaises(LPassAuthError):
                await api.points()

    async def test_business_error_is_safe(self):
        def handler(request):
            return httpx.Response(200, json={"code": 20021, "msg": "User does not exist",
                                             "data": None})

        async with api_with(handler) as api:
            with self.assertRaises(LPassError) as ctx:
                await api.points()
        self.assertIn("20021", str(ctx.exception))
        self.assertNotIn("tok-xyz", str(ctx.exception))

    async def test_tasks_uses_v2_params(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=tasks_payload(SIGNIN_TASK))

        async with api_with(handler) as api:
            tasks = await api.tasks()
        self.assertEqual(tasks[0]["task_id"], "15")
        self.assertEqual(seen["params"].get("get_top"), "false")

    async def test_already_checked_in_is_not_an_error(self):
        def handler(request):
            return httpx.Response(200, json={"code": 1001009, "msg": "already", "data": {}})

        async with api_with(handler) as api:
            payload = await api.daily_check_in("15")
        self.assertTrue(payload["already"])

    async def test_share_link(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0,
                                             "data": {"status": 1,
                                                      "share_link": "https://x/?code=abc"}})

        async with api_with(handler) as api:
            data = await api.share_link("22")
        self.assertEqual(data["status"], 1)
        self.assertTrue(data["link"].endswith("code=abc"))

    def test_ladder_progress(self):
        done, total, today = ladder_progress(SIGNIN_TASK)
        self.assertEqual((done, total, today), (2, 3, False))


class AlreadySigninTest(unittest.IsolatedAsyncioTestCase):
    async def test_ladder_signed_today_makes_no_request(self):
        ladder = dict(SIGNIN_TASK, sign_today=True,
                      reward_infos=[{"stage": 1, "points": 50, "is_completed": True},
                                    {"stage": 2, "points": 50, "is_completed": False}])
        calls = []

        def handler(request):
            path = request.url.path
            calls.append(path)
            if path.endswith("GetUserTotalPointsNew"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "active_points": {"total_points": 9314}}})
            if path.endswith("GetTaskListWithStatusV2"):
                return httpx.Response(200, json=tasks_payload(ladder))
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})

        async with api_with(handler) as api:
            result = await DailyRunner(api, sleep=_fast_sleep).run()
        self.assertFalse(any(p.endswith("DailyCheckIn") for p in calls))
        self.assertEqual({s.key: s.status for s in result.steps}["ladder"], "already")
        self.assertIn("7-Day", render(result))

    async def test_new_already_code_is_not_a_failure(self):
        def handler(request):
            return httpx.Response(200, json={
                "code": 1002007,
                "msg": "DailyCheckIn UserCompleteTaskAndAddPoints error, "
                       "err=stageTaskAllComplete already sign in today",
                "data": None})

        async with api_with(handler) as api:
            payload = await api.daily_check_in("58")
        self.assertTrue(payload["already"])


class RunnerTest(unittest.IsolatedAsyncioTestCase):
    def _handler(self, *, signin_done=False, extra_tasks=()):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content or b"{}")
            calls.append((path, body))
            if path.endswith("GetUserTotalPointsNew"):
                signed = signin_done or any(p.endswith("DailyCheckIn") for p, _ in calls)
                total = 5735 if signed else 5485
                return httpx.Response(200, json={"code": 0, "data": {
                    "active_points": {"total_points": total, "total_points_earned": 9000}}})
            if path.endswith("GetTaskListWithStatusV2"):
                tasks = [dict(SIGNIN_TASK, is_completed=signin_done), FRIEND_TASK,
                         GAME_TASK, SHARE_TASK, *extra_tasks]
                return httpx.Response(200, json=tasks_payload(*tasks))
            if path.endswith("GetDisposableTask"):
                return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                    {"task_id": 901, "task_type": 5, "points": 25,
                     "task_name": "Follow LI Pass X", "is_completed": False}]}})
            if path.endswith("DailyCheckIn"):
                return httpx.Response(200, json={"code": 0, "data": {}})
            if path.endswith("GetShareLink"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "status": 1, "share_link": "https://pass.levelinfinite.com/points?code=zz"}})
            return httpx.Response(200, json={"code": 0, "data": {}})

        return handler, calls

    async def test_daily_full_run(self):
        handler, calls = self._handler()
        async with api_with(handler) as api:
            result = await DailyRunner(api, sleep=_fast_sleep).run()

        text = render(result)
        self.assertEqual(result.delta, 250)
        self.assertEqual(result.signin_day, 3)
        self.assertIn("金币:5485 →5735(+250)", text)
        self.assertIn("7-Day", text)
        self.assertIn("分享链接：https://pass.levelinfinite.com/points?code=zz", text)
        self.assertIn("日常:", text)
        self.assertIn("一次性任务:", text)
        self.assertIn("Follow LI Pass X", text)
        self.assertNotIn("（好友请求奖励约10分钟后结算，未计入上面的差值）", text)
        self.assertTrue(any(p.endswith("DailyCheckIn") for p, _ in calls))

    async def test_dry_run_sends_nothing(self):
        handler, calls = self._handler()
        async with api_with(handler) as api:
            result = await DailyRunner(api, dry_run=True, sleep=_fast_sleep).run()
        self.assertFalse(any(p.endswith("DailyCheckIn") for p, _ in calls))
        self.assertFalse(any(p.endswith("CompleteTaskAddPoint") for p, _ in calls))
        self.assertFalse(any(p.endswith("GetShareLink") for p, _ in calls))
        self.assertIn("演练模式", render(result))

    async def test_signin_failure_does_not_block_share(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("GetUserTotalPointsNew"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "active_points": {"total_points": 100}}})
            if path.endswith("GetTaskListWithStatusV2"):
                return httpx.Response(200, json=tasks_payload(
                    dict(SIGNIN_TASK, is_completed=False), SHARE_TASK))
            if path.endswith("DailyCheckIn"):
                return httpx.Response(200, json={"code": 40051, "msg": "oper limit",
                                                 "data": None})
            if path.endswith("GetShareLink"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "status": 1, "share_link": "https://pass.levelinfinite.com/points?code=qq"}})
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})

        async with api_with(handler) as api:
            result = await DailyRunner(api, sleep=_fast_sleep).run()
        statuses = {step.key: step.status for step in result.steps}
        self.assertEqual(statuses["ladder"], "failed")
        self.assertTrue(result.share_link.endswith("code=qq"))

    async def test_walkin_task_claimed_when_quota_met(self):
        task = {"task_id": "77", "task_type": 12, "task_name": "灵活任务",
                "is_completed": False,
                "reward_infos": [{"points": 50, "need_completed_times": 1,
                                  "completed_times": 1}]}
        handler, calls = self._handler(extra_tasks=[task])
        async with api_with(handler) as api:
            result = await DailyRunner(api, sleep=_fast_sleep).run()
        self.assertTrue(any(p.endswith("CompleteTaskAddPoint") and b.get("task_id") == "77"
                            for p, b in calls))
        self.assertEqual({s.key: s.status for s in result.steps}["task:77"], "done")


class ClassifyTest(unittest.TestCase):
    def test_type_wins_over_name(self):
        self.assertEqual(classify({"task_type": 32, "task_name": "whatever"}), "friend_requests")

    def test_name_fallback(self):
        self.assertEqual(classify({"task_name": "【每日】游玩游戏"}), "play_game")
        self.assertEqual(classify({"task_name": "Change user nickname"}), "nickname")

    def test_ladder(self):
        self.assertEqual(classify({"task_type": 11}), "ladder_signin")

    def test_unknown(self):
        self.assertEqual(classify({"task_name": "神秘任务"}), "")


class FriendProgressTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_counts_as_full(self):
        from BlablaBot.nikke_lpass.ui import _friend_progress

        class FakeAPI:
            async def tasks(self):
                return [{"task_id": "140", "task_type": 32,
                         "task_name": "[每日]发送3次好友请求", "is_completed": True,
                         "reward_infos": [{"completed_times": 3,
                                           "need_completed_times": 3,
                                           "is_completed": True}]}]

        self.assertEqual(await _friend_progress(FakeAPI()), 3)

    async def test_pending_task_returns_times(self):
        from BlablaBot.nikke_lpass.ui import _friend_progress

        class FakeAPI:
            async def tasks(self):
                return [{"task_id": "140", "task_type": 32,
                         "task_name": "[每日]发送3次好友请求", "is_completed": False,
                         "reward_infos": [{"completed_times": 1,
                                           "need_completed_times": 3}]}]

        self.assertEqual(await _friend_progress(FakeAPI()), 1)

    async def test_missing_task_returns_none(self):
        from BlablaBot.nikke_lpass.ui import _friend_progress

        class FakeAPI:
            async def tasks(self):
                return []

        self.assertIsNone(await _friend_progress(FakeAPI()))


class AutoStatusTest(unittest.TestCase):
    def test_render_auto_status(self):
        from BlablaBot.nikke_lpass.report import render_auto_status

        text = render_auto_status(enabled=True, at="08:30", last_day="2026-11-06",
                                  last_result="LEVELINFINITEPASS日常执行结果", expire_days=28)
        self.assertIn("已开启", text)
        self.assertIn("08:30", text)
        self.assertIn("2026-11-06", text)
        self.assertIn("约 28 天", text)

    def test_render_auto_status_off(self):
        from BlablaBot.nikke_lpass.report import render_auto_status

        text = render_auto_status(enabled=False, at="09:00")
        self.assertIn("已关闭", text)
        self.assertIn("还没有执行记录", text)


class TemplateTest(unittest.TestCase):
    def test_daily_template_sections(self):
        from BlablaBot.nikke_lpass.api import task_completed  # noqa: F401
        from BlablaBot.nikke_lpass.report import render
        from BlablaBot.nikke_lpass.tasks import DailyResult

        result = DailyResult(points_before=10089, points_after=10289)
        result.add("signin", "每日签到", "already", "七日进度 2/7", 0)
        result.signin_day, result.ladder_total = 2, 7
        result.tasks = [
            {"task_id": "14", "task_type": 2, "task_name": "【每日】游玩游戏",
             "is_completed": True, "reward_infos": [{"points": 100, "completed_times": 1,
                                                     "need_completed_times": 1,
                                                     "is_completed": True}]},
            {"task_id": "84", "task_type": 22, "task_name": "【每周】分享LI PASS",
             "is_completed": False, "reward_infos": [{"points": 100, "completed_times": 0,
                                                      "need_completed_times": 1}]},
            {"task_id": "148", "task_type": 30, "task_name": "修改用户昵称",
             "is_completed": False, "reward_infos": [{"points": 100, "completed_times": 0,
                                                      "need_completed_times": 1}]},
        ]
        text = render(result)
        self.assertIn("金币:10089 →10289(+200)", text)
        self.assertIn("日常:", text)
        self.assertIn("【每日】游玩游戏 1/1 已完成（+100)", text)
        self.assertIn("【每周】分享LI PASS 0/1 未完成（+100)", text)
        self.assertIn("一次性任务:", text)
        self.assertIn("修改用户昵称 0/1 未完成（+100)", text)


if __name__ == "__main__":
    unittest.main()
