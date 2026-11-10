import asyncio
import logging
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, r"E:\AI\aliceuid")
os.environ.setdefault("BLABLABOT_STANDALONE", "1")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

from BlablaBot.nikke_lpass.api import LPassAPI, parse_cookie  # noqa: E402
from BlablaBot.nikke_lpass.ui import send_friend_requests  # noqa: E402

cookie = parse_cookie(os.environ["LPASS_COOKIE"])
OUT = []


def log(*a):
    OUT.append(" ".join(str(x) for x in a))


async def st(api, tid):
    tasks = await api.tasks()
    t = next((x for x in tasks if str(x.get("task_id")) == tid), None)
    if not t:
        return None
    info = (t.get("reward_infos") or [{}])[0]
    return info.get("completed_times"), info.get("need_completed_times")


async def main():
    t0 = time.time()
    async with LPassAPI(cookie) as api:
        log("before 140:", await st(api, "140"), "141:", await st(api, "141"))
        res = await send_friend_requests(cookie, api, count=3, headless=True, timeout_s=120)
        log("opened:", res.opened, "clicked:", res.clicked, "confirmed:", res.confirmed)
        log("names:", res.names[:5])
        log("share_link:", res.share_link, "share_done:", res.share_done)
        log("error:", res.error)
        log("summary:", res.summary())
        for i in range(6):
            await asyncio.sleep(20)
            log(f"poll[{i}] 140:", await st(api, "140"), "141:", await st(api, "141"))
    log("elapsed", round(time.time() - t0, 1))


asyncio.run(main())
open(r"E:\AI\aliceuid\ui_run.txt", "w", encoding="utf-8").write("\n".join(OUT))
