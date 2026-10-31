#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
my-tvbox 配置生成器

输入：sources/allowed.json（活源池）、sources/live.json（直播源）、sources/pc_extra.json（可选的 jar 蜘蛛源）
输出：dist/ 下四份配置 + 状态文件

设计原则
1. 只输出 status=active 的源；quarantine / retired 一律不进产物
2. key 全局唯一（重复会让 TVBox 静默覆盖站点），生成前强制断言
3. api 必须是无 query 的纯路径，避免客户端拼接 ?ac=videolist&wd= 时参数重复
4. 分类优先用 sources 里缓存的 categories，缺失的客户端自己请求
5. 分三份方言：标准 TVBox / TVBoxPC（含 jar）/ Electron（type:"cms"）
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sources")
DIST = os.path.join(ROOT, "dist")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(name, default=None):
    path = os.path.join(SRC, name)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_api(api):
    """剥掉 query 与多余尾斜杠，保证客户端拼接参数干净。"""
    api = (api or "").strip()
    if "?" in api:
        api = api.split("?")[0]
    return api.rstrip("/") + "/" if "/provide/vod" in api and not api.endswith("/") else api


def make_site(s, extra=None):
    item = {
        "key": s["key"],
        "name": s["name"],
        "type": int(s.get("type") or 3),
        "api": clean_api(s["api"]),
        "searchable": int(s.get("searchable") or 0),
        "quickSearch": int(s.get("searchable") or 0),
        "filterable": 1,
    }
    # 注意：不要写 categories。TVBox 会拿 categories 的值当 t 参数，
    # 而苹果CMS 的 t 只认数字 type_id，写中文名会让该源所有分类页返回空。
    # 不写 categories 时客户端用 ?ac=list 返回的 class（type_id 正确），分类才能出内容。
    if s.get("ext"):
        item["ext"] = s["ext"]
    if s.get("playerType"):
        item["playerType"] = s["playerType"]
    if extra:
        item.update(extra)
    return item


def build_lives(live_cfg, base=""):
    """直播条目：优先指向 check_live.py 产出的真活 live.txt（全部经深度验活）。

    live.txt 里已经按 #genre# 分好组、每个频道最多 2 条备线，
    客户端拿到的都是能拉出真实分片的地址，不带假活。
    """
    if base and os.path.exists(os.path.join(DIST, "live.txt")):
        # CI 上产物在 /dist/ 下；本地 http 服务常把 dist 当根目录，用 LIVE_REL 覆盖
        rel = os.environ.get("LIVE_REL", "dist/live.txt").lstrip("/")
        return [{"name": "精选直播·仅真活", "type": 0, "url": f"{base}/{rel}"}]
    lists = sorted((live_cfg or {}).get("lists", []), key=lambda x: x.get("priority", 99))
    return [
        {"name": L["name"], "type": int(L.get("type", 0)), "url": L["url"]}
        for L in lists
        if L.get("url")
    ]


SPIDER_PREFIX = ("csp_", "py_", "./")


def verify(sites, name, allow_spider=False):
    keys = [s["key"] for s in sites]
    assert len(keys) == len(set(keys)), f"{name}: key 重复 -> {[k for k in keys if keys.count(k) > 1][:5]}"
    for s in sites:
        api = s["api"]
        if allow_spider and api.startswith(SPIDER_PREFIX):
            continue  # csp_/py_ 是客户端内置蜘蛛名，不是 URL
        assert api.startswith("http"), f"{name}: 非法 api {api}"
        assert "?" not in api, f"{name}: api 含 query {api}"
    return True


def load_verified_live():
    """读 check_live.py 的产物（频道 → 地址 + 实测速度），给 live.m3u 用。

    读不到就返回空 dict，调用方跳过 m3u 生成——不因为一个可选产物让整份构建失败。
    """
    path = os.path.join(SRC, "live_verified.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_live_txt(path):
    """把 dist/live.txt 解析成 [(group, name, url)]。

    m3u 必须和 live.txt 用同一份频道集合：live.txt 已经过 check_live.py 的
    分组归一化、地方台剔除、跨源合并。直接读它，能保证两种方言内容完全一致，
    不会出现"m3u 里冒出 live.txt 已经剔掉的地方台"。
    """
    if not os.path.exists(path):
        return []
    out, group = [], "未分组"
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#genre#" in line:
                group = line.split(",")[0].strip() or group
                continue
            if "," not in line:
                continue
            name, url = line.split(",", 1)
            if url.strip().startswith("http"):
                out.append((group, name.strip(), url.strip()))
    return out


def write_m3u(rows, metrics=None, epg=""):
    """额外产出一份 dist/live.m3u（带分组与 tvg-name）。

    为什么加：live.txt 是 TVBox 方言（`分组,#genre#` + `频道,地址`），
    PotPlayer / VLC / TiviMate / Kodi 这些通用播放器不认；m3u 是它们的公共格式。
    同一份已验活数据换一种方言输出，等于把"只给 TVBox 用"的产物扩到所有播放器，
    不影响原有 live.txt 与 TVBox 配置的行为。
    """
    if not rows:
        return None, 0, 0
    metrics = metrics or {}
    # 频道内按实测分片速度降序（播放器默认取第一条，快线排前才能秒开），
    # 速度相同时保持 live.txt 原顺序，保证输出稳定、diff 不乱跳。
    order = {}
    for idx, (g, n, u) in enumerate(rows):
        order.setdefault((g, n), []).append((-(metrics.get(u, {}).get("kbps") or 0), idx, u))
    lines = [f'#EXTM3U x-tvg-url="{epg}"' if epg else "#EXTM3U"]
    n_ch = n_url = 0
    for (group, name), items in order.items():
        for _kbps, _idx, u in sorted(items):
            lines.append(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}')
            lines.append(u)
            n_url += 1
        n_ch += 1
    path = os.path.join(DIST, "live.m3u")
    os.makedirs(DIST, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path, n_ch, n_url


def uptime_from_history(days=7):
    """按 sources/history.jsonl 算每个源最近 N 天的可用率。

    价值：fail_streak 一旦成功就清零，"反复横跳"的源在现有数据里看不出来；
    有了可用率，status.json 与 README 才能回答"这个源到底稳不稳"。
    """
    path = os.path.join(SRC, "history.jsonl")
    if not os.path.exists(path):
        return {}
    cutoff = time.time() - days * 86400
    agg = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("ts") or 0) < cutoff:
                    continue
                a = agg.setdefault(r.get("key") or "?", [0, 0])
                a[0] += 1
                if r.get("ok"):
                    a[1] += 1
    except Exception:
        return {}
    return {k: round(v[1] * 100.0 / v[0], 1) for k, v in agg.items() if v[0]}


def write(name, obj):
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    # 回读校验：写坏了的 JSON 会让客户端整份配置加载失败
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)
    return path, os.path.getsize(path)


def main():
    allowed = load("allowed.json", {"sources": []})
    live_cfg = load("live.json", {"lists": []})
    pc_extra = load("pc_extra.json", {"sources": []})

    # 发布基址：CI 用 PUBLIC_BASE 环境变量（GitHub raw），本地默认 127.0.0.1:8899
    base = (os.environ.get("PUBLIC_BASE") or allowed.get("public_base") or
            "http://127.0.0.1:8899").rstrip("/")

    sources = [s for s in allowed.get("sources", []) if s.get("status", "active") == "active"]
    # 用户指定置顶的源绝对优先（pinned 是 1..n 的序号），其余按可搜索 + 得分
    sources.sort(key=lambda s: (s.get("pinned") or 99,
                                -(s.get("searchable") or 0),
                                -(s.get("score") or 0)))
    if not sources:
        print("没有可用源，中止", file=sys.stderr)
        return 1

    lives = build_lives(live_cfg, base)
    parses = allowed.get("parses") or []
    flags = allowed.get("flags") or []

    # 1) 标准版：所有 Windows / Android 端通用，纯 CMS + 直播
    sites = [make_site(s) for s in sources]
    verify(sites, "standard")
    payload = {
        "wallpaper": allowed.get("wallpaper", ""),
        "logo": allowed.get("logo", ""),
        "sites": sites,
        "lives": lives,
        "parses": parses,
        "flags": flags,
        "doh": [],
        "rules": [],
    }
    p1, n1 = write("tvbox.json", payload)

    # 2) TVBoxPC 增强版：额外并入 jar 蜘蛛源（只有 TVBoxPC 这类带 JVM 的客户端能跑）
    pc_sites = list(sites)
    used = {s["key"] for s in pc_sites}
    for s in pc_extra.get("sources", []):
        if s.get("status", "active") != "active":
            continue
        key = s.get("key") or ""
        if not key or key in used:
            continue
        pc_sites.append(make_site(s))
        used.add(key)
    verify(pc_sites, "pc", allow_spider=True)
    pc_payload = dict(payload)
    pc_payload["sites"] = pc_sites
    if pc_extra.get("spider"):
        pc_payload["spider"] = pc_extra["spider"]
    p2, n2 = write("tvbox-pc.json", pc_payload)

    # 3) Electron 版方言：type 用 "cms"，扩展字段放 tvboxDesktop，标准端不认这些
    desk_sites = []
    for s in sources:
        if int(s.get("type") or 3) not in (3, 4):
            continue
        desk_sites.append({
            "key": s["key"],
            "name": s["name"],
            "type": "cms",
            "api": clean_api(s["api"]),
            "searchable": int(s.get("searchable") or 0),
        })
    home = allowed.get("desktop_home") or [
        {"title": "电影 · 最近更新", "site": desk_sites[0]["key"]} if desk_sites else {}
    ]
    desk_payload = {
        "format": "tvbox-desktop/1",
        "name": "my-tvbox 活源",
        "note": "自动生成：全部源经过四级测活（列表/搜索/详情/播放地址拉真实分片）",
        "sites": desk_sites,
        "lives": [{"name": L["name"], "type": 0, "url": L["url"]} for L in lives],
        "tvboxDesktop": {"home": {"sections": home}},
    }
    p3, n3 = write("tvbox-desktop.json", desk_payload)

    # 4) 多仓索引
    if base:
        multi = {
            "urls": [
                {"url": f"{base}/dist/tvbox.json", "name": "通用版"},
                {"url": f"{base}/dist/tvbox-pc.json", "name": "TVBoxPC 增强版"},
            ]
        }
        p4, n4 = write("multi.json", multi)
    else:
        p4 = n4 = 0

    # 5) 状态文件（给徽章和给自己看）
    status = {
        "updated": NOW,
        "counts": {"active": len(sources), "searchable": sum(1 for s in sources if s.get("searchable")),
                   "sites_standard": len(sites), "sites_pc": len(pc_sites), "lives": len(lives)},
        "sources": [
            {"key": s["key"], "name": s["name"], "api": s["api"], "score": s.get("score"),
             "searchable": s.get("searchable"), "total": s.get("total"),
             "last_ok": s.get("last_ok"), "fail_streak": s.get("fail_streak", 0)}
            for s in sources
        ],
    }
    # 5.5) 可用率（近 7 天，来自 sources/history.jsonl）
    uptime = uptime_from_history(7)
    for row in status["sources"]:
        row["uptime_7d"] = uptime.get(row["key"])
    p5, n5 = write("status.json", status)

    # 6) 徽章数据（shields.io endpoint 格式，README 直接引用）
    badge = {
        "schemaVersion": 1,
        "label": "活源",
        "message": f"{len(sources)} 个",
        "color": "brightgreen" if len(sources) >= 15 else ("yellow" if len(sources) >= 8 else "red"),
    }
    write("badge.json", badge)

    print(f"标准版   {os.path.basename(p1):<22} {len(sites):>3} 站点  {n1:>6} 字节")
    print(f"PC 增强  {os.path.basename(p2):<22} {len(pc_sites):>3} 站点  {n2:>6} 字节")
    print(f"Electron {os.path.basename(p3):<22} {len(desk_sites):>3} 站点  {n3:>6} 字节")
    if p4:
        print(f"多仓索引 {os.path.basename(p4):<22}   2 项   {n4:>6} 字节")
    print(f"状态     {os.path.basename(p5):<22}         {n5:>6} 字节")
    print(f"直播源 {len(lives)} 个 / 可搜索站点 {status['counts']['searchable']} 个")

    # 7) 通用播放器方言：live.m3u（同一批真活数据，给 VLC/TiviMate/Kodi 用）
    epg = (allowed.get("epg") or "")
    verified = load_verified_live()
    m3u_rows = parse_live_txt(os.path.join(DIST, "live.txt"))
    m3u_path, m3u_ch, m3u_url = write_m3u(m3u_rows, verified.get("metrics") or {}, epg)
    if m3u_path:
        print(f"m3u     {os.path.basename(m3u_path):<22} {m3u_ch:>3} 频道  {m3u_url:>3} 地址  "
              f"{os.path.getsize(m3u_path):>6} 字节")
    else:
        print("m3u     跳过（没有 live_verified.json 或缓存为空）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
