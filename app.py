#!/usr/bin/env python3
"""抖音多主播开播监控：网页添加/删除主播，独立检测和推送，支持微信服务号模板消息或 Server酱。"""
import copy
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENDKEY = os.environ.get("SENDKEY", "").strip()
WX_APPID = os.environ.get("WX_APPID", "").strip()
WX_APPSECRET = os.environ.get("WX_APPSECRET", "").strip()
WX_TEMPLATE_ID = os.environ.get("WX_TEMPLATE_ID", "").strip()
WX_OPENID = os.environ.get("WX_OPENID", "").strip()
WX_CONFIGURED = all((WX_APPID, WX_APPSECRET, WX_TEMPLATE_ID, WX_OPENID))
PUSH_CHANNEL = "wechat" if WX_CONFIGURED else ("serverchan" if SENDKEY else "")
POLL_INTERVAL = max(15, int(os.environ.get("POLL_INTERVAL", "60")))
PORT = int(os.environ.get("PORT", "8080"))
BIND = os.environ.get("BIND", "127.0.0.1")
ROOMS_FILE = os.environ.get(
    "ROOMS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json"),
)
SESSIONS_FILE = os.environ.get(
    "SESSIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.json"),
)
ALERT_AFTER_FAILS = max(2, int(os.environ.get("ALERT_AFTER_FAILS", "5")))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip()
# 粉丝数/地区变化慢，默认 6 小时刷一次；接口失败 10 分钟后再试
DETAIL_TTL = max(300, int(os.environ.get("DETAIL_TTL", "21600")))
DETAIL_RETRY = max(60, int(os.environ.get("DETAIL_RETRY", "600")))

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_web_rid(room):
    room = room.strip()
    m = re.search(r"douyin\.com/(?:live/|user/)?([^/?&#]+)", room)
    key = m.group(1) if m else room
    if "MS4wLj" in key:
        raise ValueError("主播主页链接无法解析：打开主页，复制简介里的「抖音号」再添加")
    if re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", key):
        return key
    raise ValueError("输入无效：请填直播间链接、房间号或抖音号")


def parse_viewers(text):
    """"1059" / "1.2万" / "10万+" → 整数，识别不了返回 0。"""
    m = re.search(r"([\d.]+)\s*(万|亿)?", text or "")
    if not m:
        return 0
    mult = {"万": 10000, "亿": 100000000}.get(m.group(2), 1)
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return 0


def http_request(url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.headers


_ttwid = {"value": "", "lock": threading.Lock()}


def get_ttwid(force=False):
    with _ttwid["lock"]:
        if not force and _ttwid["value"]:
            return _ttwid["value"]
        _, headers = http_request("https://live.douyin.com/", {"User-Agent": UA})
        for cookie in headers.get_all("Set-Cookie") or []:
            m = re.match(r"(ttwid=[^;]+)", cookie)
            if m:
                _ttwid["value"] = m.group(1)
                return _ttwid["value"]
        raise RuntimeError("未能从抖音首页获取 ttwid")


def avatar_url(person):
    return ((person.get("avatar_thumb") or {}).get("url_list") or [""])[0]


def fetch_room(web_rid):
    params = {
        "aid": "6383", "app_name": "douyin_web", "live_id": "1",
        "device_platform": "web", "language": "zh-CN", "browser_language": "zh-CN",
        "browser_platform": "Win32", "browser_name": "Chrome",
        "browser_version": "126.0.0.0", "web_rid": web_rid,
    }
    url = "https://live.douyin.com/webcast/room/web/enter/?" + urllib.parse.urlencode(params)
    try:
        body, _ = http_request(url, {
            "User-Agent": UA,
            "Referer": "https://live.douyin.com/",
            "Cookie": get_ttwid(),
        })
    except Exception:
        body, _ = http_request(url, {
            "User-Agent": UA,
            "Referer": "https://live.douyin.com/",
            "Cookie": get_ttwid(force=True),
        })
    if not body.strip():
        raise RuntimeError("接口返回空（可能触发风控）")
    payload = json.loads(body)
    rooms = (payload.get("data") or {}).get("data") or []
    if not rooms:
        raise RuntimeError("接口未返回房间数据：房间号无效，或接口已变更")
    room = rooms[0]
    user = (payload.get("data") or {}).get("user") or {}
    owner = room.get("owner") or {}
    # 下播时 data.user 常常是空的，头像/昵称再从房间 owner 兜一层
    return {
        "live": room.get("status") == 2,
        "room_id": room.get("id_str", ""),
        "title": room.get("title", ""),
        "nickname": user.get("nickname", "") or owner.get("nickname", ""),
        "viewers": room.get("user_count_str", ""),
        "avatar": avatar_url(user) or avatar_url(owner),
    }


def fmt_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S")


def fetch_room_detail(room_id):
    """reflow 接口里平台记录的场次时间、粉丝数、资料地区和主播头像昵称；取不到返回 None。"""
    if not room_id:
        return None
    url = ("https://webcast.amemv.com/webcast/room/reflow/info/?type_id=0&live_id=1"
           "&room_id=" + room_id + "&app_id=1128&version_code=99.99.99")
    try:
        body, _ = http_request(url, {"User-Agent": UA, "Referer": "https://live.douyin.com/"})
        room = json.loads(body)["data"]["room"]
        owner = room.get("owner") or {}
    except Exception:
        return None
    living = room.get("status") == 2
    follow = owner.get("follow_info") or {}
    try:
        followers = int(follow.get("follower_count") or 0)
    except (TypeError, ValueError):
        followers = 0
    return {
        "create": fmt_ts(room.get("create_time")),
        # 直播中时 finish_time 是预估值，只有场次结束后才可信
        "finish": "" if living else fmt_ts(room.get("finish_time")),
        "rid": str(owner.get("web_rid") or ""),
        "followers": followers,
        "area": owner.get("location_city") or owner.get("city") or "",
        # reflow 的 owner 下播时也带着头像和昵称，enter 离线时给不全，用它补
        "avatar": avatar_url(owner),
        "nickname": owner.get("nickname", ""),
    }


class SessionStore:
    """每场直播结束时追加一条记录（最新在前），用于以后做开播规律统计。"""

    KEEP = 2000

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.records = []
        try:
            with open(path, encoding="utf-8") as f:
                self.records = json.load(f).get("sessions", [])[:self.KEEP]
        except (OSError, ValueError):
            pass

    def add(self, record):
        with self.lock:
            self.records.insert(0, record)
            self.records = self.records[:self.KEEP]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sessions": self.records}, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def list(self, web_rid="", limit=100):
        with self.lock:
            rows = [r for r in self.records
                    if not web_rid or r.get("web_rid") == web_rid]
            return rows[:limit]


def serverchan_push(title, desp):
    if not SENDKEY:
        raise RuntimeError("未配置 SENDKEY 环境变量")
    data = urllib.parse.urlencode({"title": title[:32], "desp": desp}).encode()
    body, _ = http_request(
        "https://sctapi.ftqq.com/" + SENDKEY + ".send",
        headers={"User-Agent": UA},
        data=data,
    )
    result = json.loads(body)
    if result.get("code") != 0:
        raise RuntimeError("Server酱返回错误: " + body[:200])


_wx_token = {"value": "", "expires_at": 0.0}
_wx_lock = threading.Lock()


def wx_access_token(force=False):
    with _wx_lock:
        if not force and _wx_token["value"] and time.time() < _wx_token["expires_at"]:
            return _wx_token["value"]
        body, _ = http_request(
            "https://api.weixin.qq.com/cgi-bin/stable_token",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"grant_type": "client_credential",
                             "appid": WX_APPID, "secret": WX_APPSECRET}).encode(),
        )
        result = json.loads(body)
        if "access_token" not in result:
            raise RuntimeError("获取微信 access_token 失败: " + body[:200])
        _wx_token["value"] = result["access_token"]
        _wx_token["expires_at"] = time.time() + result.get("expires_in", 7200) - 300
        return _wx_token["value"]


def wechat_send(first, keyword1, keyword2, remark, url=""):
    payload = {
        "touser": WX_OPENID,
        "template_id": WX_TEMPLATE_ID,
        "url": url,
        "data": {
            "first": {"value": first},
            "keyword1": {"value": keyword1},
            "keyword2": {"value": keyword2},
            "remark": {"value": remark},
        },
    }
    for retry in (False, True):
        token = wx_access_token(force=retry)
        body, _ = http_request(
            "https://api.weixin.qq.com/cgi-bin/message/template/send?access_token=" + token,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        result = json.loads(body)
        if result.get("errcode") == 0:
            return
        if result.get("errcode") in (40001, 42001) and not retry:
            continue
        raise RuntimeError("微信模板消息发送失败: " + body[:200])


def wechat_push(nickname, title, room_url):
    wechat_send((nickname or "主播") + " 开播了", title or "（无标题）",
                now_str(), "点击进入直播间", room_url)


def push_notice(text):
    """监控自身出问题时的告警，复用开播模板。"""
    if WX_CONFIGURED:
        wechat_send("开播监控异常", text, now_str(), "登录看板查看", DASHBOARD_URL)
    elif SENDKEY:
        desp = text + "\n\n时间：" + now_str()
        if DASHBOARD_URL:
            desp += "\n\n[打开看板](" + DASHBOARD_URL + ")"
        serverchan_push("开播监控异常", desp)
    else:
        raise RuntimeError("未配置推送，无法告警")


def push_message(nickname, title, room_url):
    if WX_CONFIGURED:
        wechat_push(nickname, title, room_url)
    elif SENDKEY:
        desp = ("**" + (title or "（无标题）") + "**\n\n"
                + "[进入直播间](" + room_url + ")\n\n"
                + "检测时间：" + now_str())
        serverchan_push((nickname or "主播") + " 开播了", desp)
    else:
        raise RuntimeError("未配置推送（需要 WX_* 或 SENDKEY 环境变量）")


def push_end(nickname, title, minutes, room_url):
    """下播通知，复用开播模板；直播间已经关了，链接指向看板。"""
    span = ("%d 分钟" % minutes) if minutes else "时长未知"
    link = DASHBOARD_URL or room_url
    if WX_CONFIGURED:
        wechat_send((nickname or "主播") + " 下播了", title or "（无标题）",
                    now_str(), "本场 " + span, link)
    elif SENDKEY:
        desp = ("**" + (title or "（无标题）") + "**\n\n本场时长：" + span +
                "\n\n检测时间：" + now_str())
        if link:
            desp += "\n\n[打开看板](" + link + ")"
        serverchan_push((nickname or "主播") + " 下播了", desp)


def minutes_between(start, end):
    try:
        a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    return max(0, round((b - a).total_seconds() / 60))


class RoomMonitor:
    def __init__(self, web_rid, sessions=None):
        self.web_rid = web_rid
        self.lock = threading.Lock()
        self.state = {
            "web_rid": web_rid,
            "room_url": "https://live.douyin.com/" + web_rid,
            "status": "unknown", "last_status": "", "nickname": "", "title": "", "viewers": "",
            "avatar": "", "room_id": "", "last_check": "", "last_error": "",
            "live_start": "", "live_end": "", "resolved_rid": "",
            "live_title": "", "peak_viewers": 0,
            "followers": 0, "area": "",
            "pushed_room_id": "", "history": [],
        }
        self.times_key = None
        self.detail_next_try = 0.0
        self.sessions = sessions
        self.fail_count = 0
        self.alerted = False

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def check_once(self):
        try:
            info = fetch_room(self.web_rid)
        except Exception as e:
            with self.lock:
                self.state["status"] = "error"
                self.state["last_error"] = str(e)
                self.state["last_check"] = now_str()
                self.fail_count += 1
                fire = self.fail_count == ALERT_AFTER_FAILS and not self.alerted
                if fire:
                    self.alerted = True
            if fire:
                self._notify_fail(str(e))
            return

        if self.fail_count:
            recovered = self.alerted
            with self.lock:
                self.fail_count = 0
                self.alerted = False
            if recovered:
                self._notify_recover()

        live = info["live"]
        with self.lock:
            # 上一轮要是风控或网络失败，status 会变成 error，那一轮的「开播→下播」边界就看不见，
            # 下播记录和整条场次记录会一起丢掉。判断边界改用最后一次成功检测到的状态。
            status = self.state["status"]
            base = status if status != "error" else (self.state.get("last_status") or "unknown")
            new_session = self.times_key != (info["room_id"], live)
            if new_session:
                self.times_key = (info["room_id"], live)
            # 场次变化时必须打一次 reflow（精确起止时间）；其余时候按 TTL 刷粉丝数
            need_detail = new_session or time.time() >= self.detail_next_try
        detail = fetch_room_detail(info["room_id"]) if need_detail else None
        if need_detail:
            self.detail_next_try = time.time() + (
                DETAIL_TTL if detail else DETAIL_RETRY)
        create = (detail or {}).get("create", "")
        finish = (detail or {}).get("finish", "")

        ts = now_str()
        session = None
        with self.lock:
            if detail:
                if detail.get("rid"):
                    self.state["resolved_rid"] = detail["rid"]
                if detail.get("followers"):
                    self.state["followers"] = detail["followers"]
                if detail.get("area"):
                    self.state["area"] = detail["area"]
                if not self.state["avatar"] and detail.get("avatar"):
                    self.state["avatar"] = detail["avatar"]
                if not self.state["nickname"] and detail.get("nickname"):
                    self.state["nickname"] = detail["nickname"]
            if live:
                cur = "live"
                self.state["live_end"] = ""
                if create:
                    self.state["live_start"] = create
                elif base != "live" or not self.state["live_start"]:
                    self.state["live_start"] = ts
                if base != "live":
                    self.state["live_title"] = info["title"]
                    self.state["peak_viewers"] = 0
                self.state["live_title"] = self.state["live_title"] or info["title"]
                self.state["peak_viewers"] = max(self.state["peak_viewers"],
                                                 parse_viewers(info["viewers"]))
            else:
                if base == "live":
                    self.state["live_end"] = finish or ts
                    session = {
                        "web_rid": self.web_rid,
                        "anchor_rid": self.state["resolved_rid"],
                        "nickname": info["nickname"] or self.state["nickname"],
                        "room_id": self.state["room_id"],
                        "title": self.state["live_title"] or info["title"],
                        "start": self.state["live_start"],
                        "end": self.state["live_end"],
                        "minutes": minutes_between(self.state["live_start"],
                                                   self.state["live_end"]),
                        "peak_viewers": self.state["peak_viewers"],
                    }
                elif not self.state["live_start"] and create:
                    # 新添加且已下播：回补上一场的起止时间
                    self.state["live_start"] = create
                    self.state["live_end"] = finish
                cur = "ended" if self.state["live_start"] else "offline"
            self.state.update({
                "status": cur,
                "last_status": cur,
                # 空响应（下播/风控）不该把已知的头像昵称抹掉，保留上一次的值
                "nickname": info["nickname"] or self.state["nickname"],
                "title": info["title"] or self.state["title"],
                "viewers": info["viewers"],
                "avatar": info["avatar"] or self.state["avatar"],
                "room_id": info["room_id"] or self.state["room_id"],
                "last_check": ts,
                "last_error": "",
            })
            if base in ("live", "offline", "ended") and cur != base:
                self.state["history"].insert(0, {
                    "time": ts, "event": cur, "title": info["title"],
                })
                self.state["history"] = self.state["history"][:50]
            need_push = (bool(PUSH_CHANNEL) and cur == "live" and info["room_id"]
                         and info["room_id"] != self.state["pushed_room_id"])
        if session and self.sessions:
            self.sessions.add(session)
        if need_push:
            self._push(info)
        if session and PUSH_CHANNEL:
            self._push_end(session)

    def _notify_fail(self, detail):
        text = self.web_rid + " 连续 " + str(ALERT_AFTER_FAILS) + " 次检测失败：" + detail[:80]
        try:
            push_notice(text)
        except Exception:
            pass

    def _notify_recover(self):
        try:
            push_notice(self.web_rid + " 检测已恢复正常")
        except Exception:
            pass

    def _push(self, info):
        try:
            push_message(info["nickname"], info["title"],
                         "https://live.douyin.com/" + self.web_rid)
            with self.lock:
                self.state["pushed_room_id"] = info["room_id"]
        except Exception as e:
            with self.lock:
                self.state["last_error"] = "推送失败（下轮自动重试）: " + str(e)

    def _push_end(self, s):
        try:
            push_end(s["nickname"], s["title"], s["minutes"],
                     "https://live.douyin.com/" + self.web_rid)
        except Exception as e:
            with self.lock:
                self.state["last_error"] = "下播推送失败: " + str(e)

    def to_dict(self):
        with self.lock:
            return {
                "web_rid": self.web_rid,
                "pushed_room_id": self.state["pushed_room_id"],
                "status": self.state["status"],
                "last_status": self.state.get("last_status", ""),
                "nickname": self.state["nickname"],
                "live_start": self.state["live_start"],
                "live_end": self.state["live_end"],
                "resolved_rid": self.state["resolved_rid"],
                "live_title": self.state["live_title"],
                "peak_viewers": self.state["peak_viewers"],
                "followers": self.state["followers"],
                "area": self.state["area"],
                "history": self.state["history"],
            }

    def load_dict(self, d):
        with self.lock:
            self.state["pushed_room_id"] = d.get("pushed_room_id", "")
            self.state["history"] = d.get("history", [])[-50:]
            self.state["live_start"] = d.get("live_start", "")
            self.state["live_end"] = d.get("live_end", "")
            self.state["resolved_rid"] = d.get("resolved_rid", "")
            self.state["live_title"] = d.get("live_title", "")
            self.state["peak_viewers"] = d.get("peak_viewers", 0)
            self.state["followers"] = d.get("followers") or 0
            self.state["area"] = d.get("area", "")
            self.state["last_status"] = d.get("last_status", "")
            status = d.get("status", "")
            if status in ("live", "offline", "ended"):
                self.state["status"] = status
                self.state["nickname"] = d.get("nickname", "")
            elif self.state["last_status"] in ("live", "offline", "ended"):
                # 存盘那一刻正好在报错：拿最后一次成功检测到的状态接着判边界，
                # 不然重启后这一场的下播会整条丢掉
                self.state["status"] = self.state["last_status"]


class MonitorManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.monitors = {}
        self.wake = threading.Event()
        self.sessions = SessionStore(SESSIONS_FILE)
        self._load()
        initial_room = os.environ.get("ROOM", "").strip()
        if initial_room:
            try:
                web_rid = parse_web_rid(initial_room)
                if web_rid not in self.monitors:
                    self.monitors[web_rid] = RoomMonitor(web_rid, self.sessions)
            except ValueError:
                pass

    def _load(self):
        try:
            with open(ROOMS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("rooms", []):
                web_rid = d.get("web_rid")
                if web_rid:
                    m = RoomMonitor(web_rid, self.sessions)
                    m.load_dict(d)
                    self.monitors[web_rid] = m
        except (OSError, ValueError):
            pass

    def _save(self):
        # 抓快照和写盘必须在同一把锁里：否则周期保存可能抓到旧顺序、
        # 却比拖拽排序后写盘，把用户刚排好的顺序覆盖回去。
        with self.save_lock:
            with self.lock:
                data = {"rooms": [m.to_dict() for m in self.monitors.values()]}
            tmp = ROOMS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, ROOMS_FILE)

    def add_room(self, room_input):
        web_rid = parse_web_rid(room_input)
        with self.lock:
            if web_rid in self.monitors:
                return web_rid, False
            self.monitors[web_rid] = RoomMonitor(web_rid, self.sessions)
        self._save()
        self.wake.set()
        return web_rid, True

    def remove_room(self, web_rid):
        with self.lock:
            if web_rid not in self.monitors:
                return False
            del self.monitors[web_rid]
        self._save()
        return True

    def reorder(self, web_rids):
        """看板拖拽排序：列表顺序就是 rooms.json 里的存储顺序。"""
        with self.lock:
            picked = [(r, self.monitors[r]) for r in web_rids if r in self.monitors]
            seen = {r for r, _ in picked}
            picked += [(r, m) for r, m in self.monitors.items() if r not in seen]
            self.monitors = dict(picked)
        self._save()
        return True

    def list_rooms(self):
        with self.lock:
            return [m.snapshot() for m in self.monitors.values()]

    def check_room(self, web_rid):
        with self.lock:
            m = self.monitors.get(web_rid)
        if m:
            m.check_once()
            self._save()

    def run(self):
        while True:
            with self.lock:
                monitors = list(self.monitors.values())
            for m in monitors:
                m.check_once()
            self._save()
            self.wake.wait(POLL_INTERVAL)
            self.wake.clear()


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音开播监控</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #121417; color: #e6e8eb; min-height: 100vh; padding: 24px 16px; }
  main { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 16px; }
  .add-form { display: flex; gap: 8px; margin-bottom: 16px; }
  .add-form input { flex: 1; background: #2a2e35; border: 1px solid #3a3f47;
                    border-radius: 8px; padding: 10px 14px; color: #e6e8eb; font-size: 14px; }
  .add-form input::placeholder { color: #666; }
  button { background: #2a2e35; color: #e6e8eb; border: 1px solid #3a3f47;
           border-radius: 8px; padding: 10px 16px; cursor: pointer; font-size: 13px; }
  button:hover { background: #343941; }
  button:disabled { opacity: .5; cursor: default; }
  button.primary { background: #1a4d2e; border-color: #22c55e; }
  button.primary:hover { background: #1f5a35; }
  button.danger { background: #4d1a1a; border-color: #ef4444; }
  button.danger:hover { background: #5a1f1f; }
  #banner { background: #3a2a1a; border: 1px solid #6b4a2a; color: #f0c080;
            border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; font-size: 13px; }
  #msg { font-size: 13px; color: #9aa0a8; margin-bottom: 12px; min-height: 18px; }
  #rooms { display: grid; grid-template-columns: 1fr; gap: 12px; }
  .room { background: #1c1f24; border: 1px solid #2a2e35; border-radius: 12px;
          padding: 16px; }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
             flex-wrap: wrap; font-size: 12px; color: #666; }
  .toolbar select { background: #2a2e35; color: #e6e8eb; border: 1px solid #3a3f47;
                    border-radius: 8px; padding: 6px 8px; font-size: 12px; }
  .handle { color: #5a6068; font-size: 15px; line-height: 1; cursor: grab;
            padding: 4px 6px; margin-left: -6px; touch-action: none;
            -webkit-user-select: none; user-select: none; }
  .handle:active { cursor: grabbing; }
  .room.hole { border-style: dashed; border-color: #3f4a58;
               background: rgba(79, 140, 255, .07); }
  .room.hole > * { visibility: hidden; }
  .room.drop-before { box-shadow: inset 0 3px 0 #4f8cff, inset 3px 0 0 #4f8cff; }
  .room.drop-after { box-shadow: inset 0 -3px 0 #4f8cff, inset -3px 0 0 #4f8cff; }
  .drag-ghost { position: fixed; z-index: 50; margin: 0; cursor: grabbing;
                border-color: #4f8cff; transform: scale(1.02); transform-origin: 0 0;
                box-shadow: 0 14px 30px rgba(0, 0, 0, .55); pointer-events: none; }
  .room-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #666; flex-shrink: 0; }
  .dot.live { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
  .dot.ended { background: #e0a030; }
  .dot.error { background: #ef4444; }
  .status-text { font-weight: 600; font-size: 15px; }
  .room-actions { margin-left: auto; display: flex; gap: 6px; }
  .room-actions button { padding: 6px 10px; font-size: 12px; }
  .anchor { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .anchor img { width: 40px; height: 40px; border-radius: 50%; }
  .nickname { font-weight: 600; font-size: 14px; }
  .title, .viewers, .meta { color: #9aa0a8; font-size: 12px; margin-top: 2px; }
  .room-link { color: #4f8cff; text-decoration: none; font-size: 13px; }
  .error-msg { color: #f0c080; font-size: 12px; margin-top: 8px; }
  .history { margin-top: 12px; padding-top: 12px; border-top: 1px solid #2a2e35; }
  .history-title { font-size: 12px; color: #666; margin-bottom: 6px; }
  .history-list { list-style: none; font-size: 12px; }
  .history-list li { padding: 4px 0; color: #9aa0a8; }
  .tag { display: inline-block; padding: 1px 6px; border-radius: 4px; margin-right: 6px;
         font-size: 11px; }
  .tag.live { background: #14301f; color: #4ade80; }
  .tag.offline { background: #2a2e35; color: #9aa0a8; }
  #empty { text-align: center; color: #666; padding: 40px 20px; }
  .config { font-size: 12px; color: #666; margin-top: 16px; text-align: center; }
  #sessions { background: #1c1f24; border: 1px solid #2a2e35; border-radius: 12px;
              padding: 14px 16px; margin-top: 16px; }
  .sess-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .sess-head h2 { font-size: 14px; }
  #sessSummary { color: #9aa0a8; font-size: 12px; margin-left: auto; }
  .sess-head button { padding: 6px 10px; font-size: 12px; }
  .table-scroll { max-height: 520px; overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: #666; font-weight: 500; padding: 6px 5px;
       position: sticky; top: 0; background: #1c1f24; }
  td { padding: 6px 5px; border-top: 1px solid #23272e; color: #c8ccd2; white-space: nowrap; }
  td.name { color: #e6e8eb; }
  .sess-empty { color: #666; font-size: 12px; }
  /* 展开/收起分两段，免得卡片列数在动画中途整排跳一下：第一段只让面板淡入淡出
     （右栏那一格宽度不变，卡片纹丝不动），第二段布局才真的腾挪，同时用弹簧把每张
     卡片从旧格子补到新格子。宽屏靠栏宽把面板裁掉，窄屏靠 max-height 收放。 */
  .layout { --sess-col: 0px; }
  .layout:not(.sess-collapsed) { --sess-col: 506px; }
  .sess-col { max-height: 0; overflow: hidden; visibility: hidden;
              transition: max-height 240ms cubic-bezier(.22,.61,.36,1),
                          visibility 0s linear 240ms; }
  .layout.sess-show .sess-col { max-height: 780px; visibility: visible;
              transition: max-height 240ms cubic-bezier(.22,.61,.36,1), visibility 0s; }
  /* clip 不会生成滚动容器，#sessions 的 sticky 才不会被顶掉 */
  @supports (overflow: clip) { .sess-col { overflow: clip; } }
  #sessions { opacity: 0; transform: translateX(14px);
              transition: opacity 180ms ease-out,
                          transform 220ms cubic-bezier(.22,.61,.36,1); }
  .layout.sess-show #sessions { opacity: 1; transform: none; }
  @media (min-width: 960px) {
    main { max-width: 1640px; }
    #rooms { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) var(--sess-col);
              gap: 16px 0; align-items: start; }
    /* 490px 是场次表两列完整时间戳不出现横向滚动条的下限，16px 留白跟着栏宽一起收；
       撑满整行是为了 sticky 还有移动空间，宽屏用不上 max-height，这条要和窄屏那条
       同特异度并放在后面，否则 780px 会把 sticky 的行程压短 */
    .layout .sess-col, .layout.sess-show .sess-col { max-height: none; align-self: stretch; }
    #sessions { width: 490px; margin: 0 0 0 16px; position: sticky; top: 24px; }
  }
  @media (min-width: 1700px) {
    .layout:not(.sess-collapsed) { --sess-col: 536px; }
    #sessions { width: 520px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .sess-col, #sessions { transition: none; }
  }
</style>
</head>
<body>
<main>
  <h1>抖音开播监控</h1>
  <div id="banner" hidden></div>
  <div class="add-form">
    <input type="text" id="roomInput" placeholder="直播间链接 / 房间号 / 抖音号，如 https://live.douyin.com/712345678901">
    <button class="primary" id="addBtn">添加主播</button>
  </div>
  <div id="msg"></div>
  <div class="layout sess-collapsed" id="layout">
    <div>
      <div class="toolbar" id="toolbar" hidden>
        <select id="sortSel">
          <option value="manual">手动排序</option>
          <option value="live">开播优先</option>
        </select>
        <span id="sortHint"></span>
        <button id="sessToggle" style="margin-left:auto">展开场次记录</button>
      </div>
      <div id="rooms"></div>
      <div id="empty" hidden>还没有监控任何主播，在上方添加一个吧</div>
    </div>
    <div class="sess-col" id="sessCol">
      <section id="sessions">
      <div class="sess-head">
        <h2>场次记录</h2>
        <span id="sessSummary"></span>
        <button id="exportBtn" style="margin-left:auto">导出 CSV</button>
        <button id="sessCollapse">收起</button>
      </div>
      <div class="table-scroll" id="sessionsTable" hidden>
        <table>
          <thead>
            <tr><th>主播</th><th>开播</th><th>结束</th><th>时长</th><th>峰值</th></tr>
          </thead>
          <tbody id="sessBody"></tbody>
        </table>
      </div>
      <div class="sess-empty" id="sessEmpty">还没有完整场次记录：抓到一场「开播 → 下播」才会落一条</div>
      </section>
    </div>
  </div>
  <div class="config">
    检测间隔 <span id="interval">-</span> 秒 · 推送渠道 <span id="channel">-</span>
    · <button id="testPushBtn" style="padding:4px 8px;font-size:11px">测试推送</button>
  </div>
</main>
<script>
const STATUS_TEXT = { live: '直播中', ended: '直播结束', offline: '未开播', error: '检测失败', unknown: '等待检测' };
const $ = id => document.getElementById(id);
let rooms = [];
let sessions = [];
let dragEl = null, dragGhost = null, dragX = 0, dragY = 0, grabX = 0, grabY = 0;
let dragW = 0, dragH = 0, landing = false, scrollBoost = 1;
let dragTimer = null, settleTimer = null, dragStartOrder = null;
let dropTarget = null, dropBefore = false;
let sortMode = 'manual';
try { sortMode = localStorage.getItem('sortMode') || 'manual'; } catch (e) {}
const STATUS_RANK = { live: 0, ended: 1, offline: 2, unknown: 3, error: 4 };

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtCount(n) {
  const v = Number(n) || 0;
  if (!v) return '';
  if (v >= 1e8) return Math.round(v / 1e7) / 10 + '亿';
  if (v >= 1e4) return Math.round(v / 1e3) / 10 + '万';
  return String(v);
}

async function loadRooms() {
  try {
    const data = await (await fetch('/api/rooms')).json();
    rooms = data.rooms;
    $('interval').textContent = data.poll_interval;
    $('channel').textContent = data.push_channel === 'wechat' ? '微信服务号' :
                               data.push_channel === 'serverchan' ? 'Server酱' : '未配置';
    $('banner').hidden = data.push_channel !== '';
    $('banner').textContent = '未配置推送，开播时不会发送微信通知。';
    render();
    loadSessions();
  } catch (e) {
    $('msg').textContent = '无法连接服务: ' + e;
  }
}

function statusRank(s) {
  const v = STATUS_RANK[s];
  return v === undefined ? 9 : v;
}

function orderedRooms() {
  if (sortMode !== 'live') return rooms;
  return rooms.slice().sort((a, b) =>
    statusRank(a.status) - statusRank(b.status) ||
    String(b.live_start || '').localeCompare(String(a.live_start || '')));
}

function render() {
  if (dragEl || sessBusy) return;
  const list = orderedRooms();
  $('empty').hidden = list.length > 0;
  $('toolbar').hidden = list.length < 2;
  $('sortHint').textContent = sortMode === 'live' ? '按状态排序中，拖拽已停用'
                                                      : '按住 ⠿ 拖动卡片可排顺序';
  const container = $('rooms');
  container.innerHTML = '';
  for (const r of list) {
    const stats = [r.followers ? '粉丝 ' + fmtCount(r.followers) : '',
                   r.area ? '地区 ' + esc(r.area) : ''].filter(Boolean).join(' · ');
    const div = document.createElement('div');
    div.className = 'room';
    div.dataset.rid = r.web_rid;
    div.innerHTML = `
      <div class="room-header">
        ${sortMode === 'manual' && list.length > 1 ? '<span class="handle" title="拖动排序">⠿</span>' : ''}
        <span class="dot ${r.status}"></span>
        <span class="status-text">${STATUS_TEXT[r.status] || esc(r.status)}</span>
        <div class="room-actions">
          <button data-act="check" data-rid="${esc(r.web_rid)}">立即检测</button>
          <button class="danger" data-act="remove" data-rid="${esc(r.web_rid)}">删除</button>
        </div>
      </div>
      <div class="anchor">
        ${r.avatar ? `<img src="${esc(r.avatar)}" referrerpolicy="no-referrer" alt="">` : ''}
        <div>
          <div class="nickname">${esc(r.nickname) || '（未知主播）'}</div>
          ${r.title ? `<div class="title">${esc(r.title)}</div>` : ''}
        </div>
      </div>
      ${stats ? `<div class="meta">${stats}</div>` : ''}
      ${r.status === 'live' && r.viewers ? `<div class="viewers">在线观众：${esc(r.viewers)}</div>` : ''}
      ${r.live_start ? `<div class="meta">${r.status === 'live' ? '本场开播' : '上次开播'}：${esc(r.live_start)}</div>` : ''}
      ${r.live_end ? `<div class="meta">结束时间：${esc(r.live_end)}</div>` : ''}
      <a class="room-link" href="${esc(r.room_url)}" target="_blank" rel="noopener">进入直播间 →</a>
      <div class="meta">房间号 ${esc(r.resolved_rid || r.web_rid)} · 上次检测 ${esc(r.last_check) || '-'}</div>
      ${r.last_error ? `<div class="error-msg">${esc(r.last_error)}</div>` : ''}
      ${r.history && r.history.length ? `
        <div class="history">
          <div class="history-title">最近状态</div>
          <ul class="history-list">
            ${r.history.slice(0, 5).map(h => `
              <li><span class="tag ${h.event === 'live' ? 'live' : 'offline'}">${h.event === 'live' ? '开播' : '下播'}</span>${esc(h.time)}${h.title ? ' · ' + esc(h.title) : ''}</li>
            `).join('')}
          </ul>
        </div>
      ` : ''}
    `;
    container.appendChild(div);
  }
}

$('sortSel').value = sortMode;
$('sortSel').onchange = () => {
  sortMode = $('sortSel').value;
  localStorage.setItem('sortMode', sortMode);
  render();
};

// 场次记录默认收起，别占着卡片的宽度。展开/收起分两段：面板先动，格子后动，
// 卡片列数变化那一下用弹簧补位盖掉，不再整排瞬移。
const SESS_FADE = 200, SESS_FLIP = 320;
let sessOpen = false, sessTimer = null, sessBusy = false;
try { sessOpen = localStorage.getItem('sessOpen') === '1'; } catch (e) {}

function sessMs(ms) { return REDUCED_MOTION && REDUCED_MOTION.matches ? 0 : ms; }

function roomRects() {
  return Array.prototype.slice.call($('rooms').children).map(el => el.getBoundingClientRect());
}

// 布局已经变了：把每张卡片从旧格子的位置和宽度弹到新格子，列数跳变就藏在这段补位里
function flipRooms(first) {
  const ms = sessMs(SESS_FLIP);
  if (!ms) return;
  Array.prototype.slice.call($('rooms').children).forEach((el, i) => {
    const f = first[i];
    if (!f) return;
    const l = el.getBoundingClientRect();
    const dx = f.left - l.left, dy = f.top - l.top;
    if (!dx && !dy && Math.abs(f.width - l.width) < 1) return;
    el.style.transition = 'none';
    el.style.width = f.width + 'px';
    el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
    requestAnimationFrame(() => {
      el.style.transition = 'transform ' + ms + 'ms ' + FLIP_EASE +
                            ', width ' + ms + 'ms ' + FLIP_EASE;
      el.style.transform = '';
      el.style.width = l.width + 'px';
      setTimeout(() => { el.style.transition = ''; el.style.width = ''; }, ms + 40);
    });
  });
}

function sessText(open) {
  $('sessToggle').textContent = open ? '收起场次记录' : '展开场次记录';
}

function setSess(open) {
  clearTimeout(sessTimer);
  sessTimer = null;
  if (open === sessOpen) {   // 首次进页面：直接落到该在的状态，不播动画
    $('layout').classList.toggle('sess-collapsed', !open);
    $('layout').classList.toggle('sess-show', open);
    sessText(open);
    return;
  }
  sessOpen = open;
  try { localStorage.setItem('sessOpen', open ? '1' : '0'); } catch (e) {}
  sessBusy = true;
  sessText(open);
  if (open) {
    // 先腾出右栏并让卡片补位，面板等它们就位再淡进来
    const first = roomRects();
    $('layout').classList.remove('sess-collapsed');
    flipRooms(first);
    sessTimer = setTimeout(() => {
      sessTimer = null;
      $('layout').classList.add('sess-show');
      setTimeout(() => { sessBusy = false; }, sessMs(240));
    }, sessMs(140));
  } else {
    // 面板先淡出去（右栏还占着），收完这一步才腾格子、卡片补位
    $('layout').classList.remove('sess-show');
    sessTimer = setTimeout(() => {
      sessTimer = null;
      const first = roomRects();
      $('layout').classList.add('sess-collapsed');
      flipRooms(first);
      setTimeout(() => { sessBusy = false; }, sessMs(SESS_FLIP));
    }, sessMs(SESS_FADE));
  }
}
$('sessToggle').onclick = () => setSess(!sessOpen);
$('sessCollapse').onclick = () => setSess(false);
setSess(sessOpen);

function stopTicker() { if (dragTimer) { clearInterval(dragTimer); dragTimer = null; } }

function dragClean() {
  stopTicker();
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
  if (dragGhost) dragGhost.remove();
  if (dragEl) dragEl.classList.remove('hole');
  Array.prototype.slice.call($('rooms').children).forEach(el => {
    el.classList.remove('drop-before', 'drop-after');
  });
  dragEl = null; dragGhost = null; dropTarget = null; dropBefore = false;
  dragStartOrder = null; landing = false; scrollBoost = 1;
}

// 拖拽全程不动 DOM：原卡片只把自己变成虚线空槽，跟随指针的是它的克隆体。
// 布局一步不重排，命中判断就不会被动画中间态来回拉扯。
$('rooms').addEventListener('pointerdown', e => {
  const handle = e.target.closest('.handle');
  if (!handle || sortMode !== 'manual') return;
  e.preventDefault();
  dragClean();
  const card = handle.closest('.room');
  const rect = card.getBoundingClientRect();
  dragX = e.clientX; dragY = e.clientY;
  grabX = dragX - rect.left; grabY = dragY - rect.top;
  dragEl = card;
  dragW = rect.width; dragH = rect.height;
  dragStartOrder = Array.prototype.slice.call(card.parentNode.children).map(el => el.dataset.rid);
  card.classList.add('hole');
  dragGhost = card.cloneNode(true);
  dragGhost.classList.remove('hole');
  dragGhost.classList.add('drag-ghost');
  // 上一张卡片可能还在补位动画里，克隆体不能把那段位移/过渡一起带过来
  dragGhost.style.transform = '';
  dragGhost.style.transition = 'none';
  dragGhost.style.width = rect.width + 'px';
  dragGhost.style.height = rect.height + 'px';
  dragGhost.style.left = rect.left + 'px';
  dragGhost.style.top = rect.top + 'px';
  document.body.appendChild(dragGhost);
  try { handle.setPointerCapture(e.pointerId); } catch (err) {}
  dragTimer = setInterval(tick, 55);
});

// 命中判断用「克隆体盖住哪些卡片」，不是指针那一个点：
// 把手在卡片左上角，指针永远落在被压住的卡片左上区域，
// 只看指针就会出现「卡片已经拖过去了，顺序却纹丝不动」。
const GHOST_SCALE = 1.02;
// 盖过目标卡片四分之一才算拖到它，蹭个边不算
const DROP_FRACTION = 0.25;

function ghostBox() {
  const w = dragW * GHOST_SCALE, h = dragH * GHOST_SCALE;
  const left = dragX - grabX, top = dragY - grabY;
  return {left, top, w, h, right: left + w, bottom: top + h, cx: left + w / 2, cy: top + h / 2};
}

// 插前还是插后：看克隆体相对「原槽位」偏哪边来定方向（原槽位拖拽全程冻结，
// 所以 dragEl 的 rect 就是出发点），再看在主轴上探进目标有多深。探过四分之一
// 就翻到另一边。用相对原槽位的方向、而不是相对目标的方向，来回拖才会在目标
// 中心附近抖。
function quarterSide(g, q) {
  const home = dragEl.getBoundingClientRect();
  const dx = g.cx - (home.left + home.width / 2);
  const dy = g.cy - (home.top + home.height / 2);
  if (Math.abs(dy) >= Math.abs(dx)) {        // 竖着拖为主
    if (dy >= 0) return (g.bottom - q.top) / q.height <= DROP_FRACTION;   // 往下：底边探进
    return (q.bottom - g.top) / q.height > DROP_FRACTION;                 // 往上：顶边探进
  }
  if (dx >= 0) return (g.right - q.left) / q.width <= DROP_FRACTION;      // 往右：右边探进
  return (q.right - g.left) / q.width > DROP_FRACTION;                    // 往左：左边探进
}

function updateDrop() {
  const kids = Array.prototype.slice.call(dragEl.parentNode.children);
  const slots = [];
  for (const el of kids) {
    if (el === dragEl) continue;
    slots.push([el, el.getBoundingClientRect()]);
  }
  if (!slots.length) return;
  const g = ghostBox();
  let hit = null, hitRect = null, ratio = 0;
  for (const [el, q] of slots) {
    const ox = Math.min(g.right, q.right) - Math.max(g.left, q.left);
    const oy = Math.min(g.bottom, q.bottom) - Math.max(g.top, q.top);
    if (ox <= 0 || oy <= 0) continue;
    // 盖过这张卡片的四分之一才算拖到它，蹭个边不算
    const r = ox * oy / (q.width * q.height);
    if (r > DROP_FRACTION && r > ratio) { ratio = r; hit = el; hitRect = q; }
  }
  let fallback = false;
  if (!hit) {   // 空白处（列表下方、卡片之间）也要落到最近的一格，否则永远排不到最后
    let min = Infinity;
    for (const [el, q] of slots) {
      const dx = Math.max(q.left - g.cx, 0, g.cx - q.right);
      const dy = Math.max(q.top - g.cy, 0, g.cy - q.bottom);
      const d = dx * dx + dy * dy;
      if (d < min) { min = d; hit = el; hitRect = q; }
    }
    fallback = true;
  }
  const before = fallback
    ? (g.cx - (hitRect.left + hitRect.width / 2)) / hitRect.width +
      (g.cy - (hitRect.top + hitRect.height / 2)) / hitRect.height < 0
    : quarterSide(g, hitRect);
  if (hit === dropTarget && before === dropBefore) return;
  if (dropTarget) dropTarget.classList.remove('drop-before', 'drop-after');
  dropTarget = hit; dropBefore = before;
  hit.classList.add(before ? 'drop-before' : 'drop-after');
}

function tick() {
  if (!dragEl || landing) return;
  // 列表比屏幕长时，拖到上下边缘自动滚动；越贴边越快，贴久了再加一档
  const edge = 80;
  const k = f => Math.min(1, Math.max(0, f));
  let v = 0;
  if (dragY < edge) v = -(10 + 22 * k(1 - dragY / edge));
  else if (dragY > window.innerHeight - edge) v = 10 + 22 * k(1 - (window.innerHeight - dragY) / edge);
  if (v) {
    scrollBoost = Math.min(2.2, scrollBoost + 0.06);
    window.scrollBy(0, v * scrollBoost);
  } else {
    scrollBoost = 1;
  }
  updateDrop();
}

document.addEventListener('pointermove', e => {
  // 松手后卡片正在补位，这时再读位置会被动画中间态骗到，所以落位阶段一概不再判断
  if (!dragEl || landing) return;
  e.preventDefault();
  dragX = e.clientX; dragY = e.clientY;
  dragGhost.style.left = (dragX - grabX) + 'px';
  dragGhost.style.top = (dragY - grabY) + 'px';
  updateDrop();
});

// 落位用弹簧：欠阻尼解析解采样成 CSS linear() 曲线（ζ=0.7 约等于 iOS 的 bounce 0.3，
// 会冲过目标一点点再收回来）。老浏览器不支持 linear() 就退回带轻微过冲的贝塞尔。
const LINEAR_EASE_OK = typeof CSS !== 'undefined' && !!CSS.supports &&
                       CSS.supports('animation-timing-function', 'linear(0, 1)');
const REDUCED_MOTION = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const LAND_MS = 400, FLIP_MS = 260;

function springEase(zeta, period, ms) {
  const w = 2 * Math.PI / period, wd = w * Math.sqrt(1 - zeta * zeta);
  const pts = [];
  for (let i = 0; i <= 40; i++) {
    const t = ms / 1000 * i / 40;
    const x = 1 - Math.exp(-zeta * w * t) * (Math.cos(wd * t) + zeta * w / wd * Math.sin(wd * t));
    pts.push(Math.round(x * 1e4) / 1e4 + ' ' + Math.round(i / 40 * 1e4) / 100 + '%');
  }
  // 采样末尾还差零点几个百分点才到静止，补成整数 1，落位不会留一条缝
  pts[pts.length - 1] = '1 100%';
  return 'linear(' + pts.join(', ') + ')';
}

// 被拖的那张给足回弹，旁边补位的卡片只留 0.6% 过冲，免得互相压边
const LAND_EASE = LINEAR_EASE_OK ? springEase(0.7, 0.27, LAND_MS) : 'cubic-bezier(.34,1.24,.64,1)';
const FLIP_EASE = LINEAR_EASE_OK ? springEase(0.85, 0.19, FLIP_MS) : 'cubic-bezier(.22,.9,.3,1)';

// 克隆体飞进最终格子
function land(rect, after) {
  const g = dragGhost;
  const ms = REDUCED_MOTION && REDUCED_MOTION.matches ? 0 : LAND_MS;
  g.style.transition = ms
    ? `left ${ms}ms ${LAND_EASE}, top ${ms}ms ${LAND_EASE}, transform ${ms}ms ${LAND_EASE}`
    : 'none';
  g.style.left = rect.left + 'px';
  g.style.top = rect.top + 'px';
  g.style.transform = 'scale(1)';
  settleTimer = setTimeout(() => { settleTimer = null; after(); }, ms + 30);
}

// 其余卡片从旧位置平滑补到新位置
function flipOthers(kids, first) {
  const ms = REDUCED_MOTION && REDUCED_MOTION.matches ? 0 : FLIP_MS;
  kids.forEach((el, i) => {
    if (el === dragEl) return;
    const f = first[i], l = el.getBoundingClientRect();
    const dx = f.left - l.left, dy = f.top - l.top;
    if (!dx && !dy) return;
    if (!ms) return;
    el.style.transition = 'none';
    el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
    requestAnimationFrame(() => {
      el.style.transition = `transform ${ms}ms ${FLIP_EASE}`;
      el.style.transform = '';
      setTimeout(() => { el.style.transition = ''; }, ms + 30);
    });
  });
}

function endDrag() {
  if (!dragEl) return;
  stopTicker();
  const container = dragEl.parentNode;
  const kids = Array.prototype.slice.call(container.children);
  const first = kids.map(el => el.getBoundingClientRect());
  const rid = dragEl.dataset.rid;
  const order = kids.map(el => el.dataset.rid).filter(x => x !== rid);
  if (!dropTarget) order.splice(kids.indexOf(dragEl), 0, rid);
  else {
    const i = order.indexOf(dropTarget.dataset.rid);
    order.splice(dropBefore ? i : i + 1, 0, rid);
  }
  kids.forEach(el => el.classList.remove('drop-before', 'drop-after'));
  order.forEach(x => {
    const el = kids.filter(k => k.dataset.rid === x)[0];
    if (el) container.appendChild(el);
  });
  flipOthers(kids, first);
  const slot = kids.filter(k => k.dataset.rid === rid)[0].getBoundingClientRect();
  landing = true;
  land(slot, () => {
    const el = dragEl;
    dragClean();
    rooms = order.map(x => rooms.filter(r => r.web_rid === x)[0]).filter(r => r);
    fetch('/api/order', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order: order})
    }).then(() => {
      $('msg').textContent = '顺序已保存';
      setTimeout(() => { $('msg').textContent = ''; }, 3000);
    });
  });
}

// 系统打断时格子从没动过，把克隆体飞回原处，再按服务器顺序重画一次
// （拖拽期间轮询会跳过重绘，不补这一下画面可能停在旧顺序上）
function abortDrag() {
  if (!dragEl) { dragClean(); return; }
  stopTicker();
  landing = true;
  land(dragEl.getBoundingClientRect(), () => { dragClean(); render(); });
}

document.addEventListener('pointerup', endDrag);
document.addEventListener('pointercancel', abortDrag);
window.addEventListener('blur', endDrag);

async function loadSessions() {
  try {
    const data = await (await fetch('/api/sessions?limit=200')).json();
    sessions = data.sessions || [];
  } catch (e) {
    sessions = [];
  }
  renderSessions();
}

function fmtMinutes(m) {
  const min = Math.round(Number(m) || 0);
  const h = Math.floor(min / 60);
  return h ? h + ' 小时 ' + (min % 60) + ' 分' : min + ' 分';
}

function renderSessions() {
  $('sessEmpty').hidden = sessions.length > 0;
  $('sessionsTable').hidden = sessions.length === 0;
  // 没有场次时导出只会得到一个表头，不如把按钮一起收起来
  $('exportBtn').hidden = sessions.length === 0;
  const body = $('sessBody');
  body.innerHTML = '';
  for (const s of sessions) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="name">${esc(s.nickname) || esc(s.web_rid)}</td>
      <td>${esc(s.start)}</td>
      <td>${esc(s.end) || '-'}</td>
      <td>${s.minutes == null ? '-' : fmtMinutes(s.minutes)}</td>
      <td>${s.peak_viewers ? Number(s.peak_viewers).toLocaleString() : '-'}</td>`;
    body.appendChild(tr);
  }
  const total = sessions.length;
  const mins = sessions.reduce((a, s) => a + (Number(s.minutes) || 0), 0);
  $('sessSummary').textContent = total ? total + ' 场 · 平均 ' + fmtMinutes(mins / total) : '';
}

function exportCsv() {
  const head = ['主播', '抖音号', '房间号', '直播标题', '开播', '结束', '时长(分钟)', '峰值观众'];
  const rows = sessions.map(s => [s.nickname || s.web_rid, s.web_rid, s.anchor_rid || s.room_id,
                                  s.title, s.start, s.end, s.minutes, s.peak_viewers]);
  const csv = [head].concat(rows)
    .map(r => r.map(v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"').join(','))
    .join('\\r\\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['\\ufeff' + csv], {type: 'text/csv;charset=utf-8'}));
  a.download = 'douyin-sessions-' + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

$('exportBtn').onclick = exportCsv;

$('rooms').onclick = e => {
  const btn = e.target.closest('button[data-rid]');
  if (!btn) return;
  if (btn.dataset.act === 'check') checkRoom(btn.dataset.rid);
  else if (btn.dataset.act === 'remove') removeRoom(btn.dataset.rid);
};

$('addBtn').onclick = async () => {
  const input = $('roomInput').value.trim();
  if (!input) { $('msg').textContent = '请输入房间号或链接'; return; }
  $('addBtn').disabled = true;
  $('msg').textContent = '添加中…';
  try {
    const r = await (await fetch('/api/rooms', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room: input})
    })).json();
    if (r.ok) {
      $('roomInput').value = '';
      $('msg').textContent = r.added ? '添加成功，正在检测…' : '该主播已在监控列表中';
      await loadRooms();
    } else {
      $('msg').textContent = '添加失败：' + r.error;
    }
  } catch (e) {
    $('msg').textContent = '请求失败：' + e;
  }
  $('addBtn').disabled = false;
};

$('roomInput').onkeydown = e => { if (e.key === 'Enter') $('addBtn').click(); };

async function checkRoom(webRid) {
  $('msg').textContent = '检测中…';
  await fetch('/api/rooms/' + encodeURIComponent(webRid) + '/check', {method: 'POST'});
  setTimeout(async () => { await loadRooms(); $('msg').textContent = ''; }, 2000);
}

async function removeRoom(webRid) {
  if (!confirm('确定删除该主播？')) return;
  await fetch('/api/rooms/' + encodeURIComponent(webRid), {method: 'DELETE'});
  await loadRooms();
}

$('testPushBtn').onclick = async () => {
  $('testPushBtn').disabled = true;
  $('msg').textContent = '发送测试推送…';
  try {
    const r = await (await fetch('/api/test-push', {method: 'POST'})).json();
    $('msg').textContent = r.ok ? '推送成功，请查看微信' : '推送失败：' + r.error;
  } catch (e) {
    $('msg').textContent = '请求失败：' + e;
  }
  $('testPushBtn').disabled = false;
};

loadRooms();
setInterval(loadRooms, 10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    manager = None

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/rooms":
            self._send(200, json.dumps({
                "rooms": self.manager.list_rooms(),
                "poll_interval": POLL_INTERVAL,
                "push_channel": PUSH_CHANNEL,
            }, ensure_ascii=False))
        elif path == "/api/sessions":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, json.dumps({
                "sessions": self.manager.sessions.list(
                    query.get("room", [""])[0],
                    int(query.get("limit", ["50"])[0])),
            }, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/rooms":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode())
                web_rid, added = self.manager.add_room(body.get("room", ""))
                self._send(200, json.dumps({"ok": True, "added": added, "web_rid": web_rid}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        elif path == "/api/order":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode())
                self.manager.reorder(body.get("order", []))
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        elif path.startswith("/api/rooms/") and path.endswith("/check"):
            web_rid = path.split("/")[3]
            threading.Thread(target=self.manager.check_room, args=(web_rid,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        elif path == "/api/test-push":
            try:
                push_message("推送测试", "收到说明配置成功", "https://live.douyin.com/")
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/rooms/"):
            web_rid = path.split("/")[3]
            removed = self.manager.remove_room(web_rid)
            self._send(200, json.dumps({"ok": removed}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    manager = MonitorManager()
    Handler.manager = manager
    threading.Thread(target=manager.run, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print("看板: http://%s:%d  初始主播数: %d  间隔: %ds  推送: %s"
          % (BIND, PORT, len(manager.monitors), POLL_INTERVAL, PUSH_CHANNEL or "未配置"), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
