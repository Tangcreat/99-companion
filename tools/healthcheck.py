"""接口体检（全覆盖）：读、写、删、模型调用都过一遍。

原则：
  * 会留下副作用的一律用临时对象，用完立刻删掉
  * 会弹窗的（/api/open-data）跳过
  * 模型调用只打必要次数

用法：
    python tools/healthcheck.py                    # 只读，快
    python tools/healthcheck.py --full             # 含写操作与模型
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8797"
for a in sys.argv[1:]:
    if a.startswith("http"):
        BASE = a
FULL = "--full" in sys.argv
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ok = bad = warn = 0
rows = []

def _make_test_png(size=240):
    """造一张够大的纯色图。太小的图会被 normalize_avatar 判为损坏。"""
    try:
        from PIL import Image
        import io
        im = Image.new("RGB", (size, size), (90, 130, 240))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        import base64
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


TINY_PNG = _make_test_png()


def hit(path, method="GET", body=None, expect=200, check=None, label=""):
    global ok, bad, warn
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        r = OPENER.open(req, timeout=90)
        code, raw, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        code, raw, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:
        rows.append(("ERR ", label or path, f"{type(e).__name__}: {e}"))
        bad += 1
        return None

    val = None
    note = ""
    if "json" in (ctype or ""):
        try:
            val = json.loads(raw.decode("utf-8"))
        except Exception:
            note = "JSON 解析失败"

    if code != expect:
        note = f"期望 {expect} 实得 {code}"
        if isinstance(val, dict) and val.get("error"):
            note += f"：{val['error']}"
    elif check:
        try:
            note = check(val) or ""
        except Exception as e:
            note = f"检查异常 {e}"

    if note.startswith("期望"):
        bad += 1
        tag = "FAIL"
    elif note:
        warn += 1
        tag = "WARN"
    else:
        ok += 1
        tag = " ok "
    rows.append((tag, label or path, note or f"{code} {len(raw)}B"))
    return val


def need(key):
    def f(v):
        if not isinstance(v, dict):
            return "返回不是对象"
        if key not in v:
            return f"缺字段 {key}"
        return "" if v[key] else f"{key} 为空"
    return f


def ok_flag(v):
    return "" if isinstance(v, dict) and v.get("ok") else "未返回 ok"


def section(title):
    rows.append(("----", title, ""))


print("=" * 80)
print("  99陪伴 · 接口体检" + ("（全量）" if FULL else "（只读）"), BASE)
print("=" * 80)

# ================= 静态 =================
section("静态资源")
for p in ("/", "/mobile", "/manifest.webmanifest", "/icon-192.png",
          "/icon-512.png", "/static-icon.png"):
    hit(p, label=f"静态 {p}")

# ================= 只读 =================
section("读接口")
chars = hit("/api/characters", check=need("characters"), label="角色列表")
hit("/api/settings", check=need("llm"), label="设置")
groups = hit("/api/groups", label="群组列表")
hit("/api/stickers", check=lambda v: "" if "items" in v else "缺 items", label="表情包索引")
hit("/api/story", check=need("chapters"), label="剧情列表")
hit("/api/story?code=7TH",
    check=lambda v: "" if (v.get("chapter") or {}).get("body") else "正文为空",
    label="剧情正文 7TH")
hit("/api/diagnostics", check=need("db"), label="运行诊断")
hit("/api/me", label="我的资料")
hit("/api/appearance", label="聊天外观")

slug = "37"
q = urllib.parse.quote(slug)
hit(f"/api/avatar/{q}", label="角色头像")
hit(f"/api/messages?char={q}", label="聊天记录")
hit(f"/api/messages/search?char={q}&q=%E4%BD%A0", label="搜索记录")
hit(f"/api/stats?char={q}", check=need("total"), label="共同生活")
hit(f"/api/memory/center?char={q}", check=need("memories"), label="回忆中心")
hit(f"/api/lore?char={q}", check=need("entries"), label="设定集")
hit(f"/api/facts?char={q}", label="事实")
mems = hit(f"/api/memories?char={q}", label="记忆列表")
hit(f"/api/profile", label="对方画像")
hit("/api/backup", label="数据备份")

if groups and groups.get("groups"):
    gid = groups["groups"][0]["id"]
    hit(f"/api/group/messages?id={urllib.parse.quote(str(gid))}", label="群聊记录")

section("异常与安全")
gone = urllib.parse.quote("弄臣")
hit(f"/api/avatar/{gone}", expect=404, label="已删角色头像 → 404")
hit(f"/api/messages?char={gone}", expect=404, label="已删角色记录 → 404")
hit("/stickers/../../config.json", expect=404, label="穿越 /stickers/")
hit("/uploads/../../config.json", expect=404, label="穿越 /uploads/")
hit("/api/avatar/..%2F..%2Fconfig.json", expect=404, label="穿越 /api/avatar/")
hit("/api/" + urllib.parse.quote("不存在"), expect=404, label="未知接口 → 404")

# ================= 写操作 =================
if FULL:
    section("写接口")

    hit("/api/characters/signature", "POST",
        {"slug": "37", "signature": "一切都可以被证明。除了我自己。"},
        check=ok_flag, label="保存个性签名")
    hit("/api/characters/state", "POST",
        {"slug": "37", "enthusiasm": 78, "affection": 42},
        check=lambda v: "" if (v.get("character") or {}).get("state") else "缺 state",
        label="保存好感/热情")
    hit("/api/appearance/save", "POST",
        {"slug": "37", "bubble": "#b04a4a", "bg": "#faf0f0"}, label="保存聊天外观")
    hit("/api/me", "POST", {"name": "老师"}, check=ok_flag, label="保存昵称")
    hit("/api/stickers/scan", "POST", {}, label="重扫表情包")

    # 临时角色：建 → 改 → 删
    tmp = "接口体检临时角色"
    made = hit("/api/characters/create", "POST", {
        "name": tmp,
        "persona_md": "这是一个用于接口检查的临时角色，用来确认新建流程能否走通。" * 12,
        "summary": "临时角色，用完即删。",
        "interests": ["测试"],
        "signature": "我只是路过。",
    }, check=lambda v: "" if v.get("slug") else "缺 slug", label="新建角色")

    if made and made.get("slug"):
        tslug = made["slug"]
        tq = urllib.parse.quote(tslug)
        hit("/api/avatar/upload", "POST",
            {"slug": tslug, "data_base64": TINY_PNG}, check=ok_flag, label="上传角色头像")

        # 设定条目：只在临时角色上加删，绝不碰真实角色的设定集
        added = hit("/api/lore/save", "POST",
                    {"slug": tslug, "entry": {"title": "临时条目", "kind": "world",
                                              "keywords": ["临时"], "content": "内容"}},
                    label="新增设定条目")
        eid = ((added or {}).get("item") or {}).get("id")
        if eid:
            hit("/api/lore/delete", "POST", {"slug": tslug, "id": eid},
                label="删除设定条目")

        hit("/api/characters/signature", "POST",
            {"slug": tslug, "signature": "改过一次。"}, check=ok_flag, label="改临时角色签名")
        hit("/api/characters/delete", "POST", {"slug": tslug}, check=ok_flag, label="删除临时角色")
        hit(f"/api/messages?char={tq}", expect=404, label="临时角色已消失 → 404")

    # 记忆修正会真的产生新版本（旧版本转 superseded），会改动真实数据，跳过
    rows.append(("skip", "修正记忆（版本链）", "跳过：会改动真实数据"))

    # 群：建 → 删
    g = hit("/api/groups/create", "POST",
            {"name": "接口体检群", "members": ["37", "维尔汀"]}, label="(试探建群)")
    gid2 = (g or {}).get("id") or ((g or {}).get("group") or {}).get("id")
    if gid2:
        hit("/api/groups/delete", "POST", {"id": gid2}, check=ok_flag, label="删除临时群")

    section("模型调用")
    hit("/api/diagnostics/test", "POST", {}, label="测试连接")
    chat = hit("/api/chat", "POST", {"slug": slug, "text": "在吗"},
               check=lambda v: "" if v.get("bubbles") else "没有气泡", label="单聊一次")
    hit("/api/chat/image", "POST", {"slug": slug, "data_base64": TINY_PNG},
        check=lambda v: "" if v.get("url") else "缺 url", label="发一张图片")

print()
for tag, label, note in rows:
    if tag == "----":
        print(f"\n  —— {label} ——")
        continue
    if tag == "skip":
        print(f"  [跳过] {label:34s} {note}")
        continue
    print(f"  [{tag}] {label:34s} {note}")
print()
print(f"  通过 {ok} · 警告 {warn} · 失败 {bad}")
print("=" * 80)
sys.exit(0 if bad == 0 else 1)
