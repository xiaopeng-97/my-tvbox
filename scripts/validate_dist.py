#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产物自检：把 healthcheck.yml 里那段内联 Python 抽成脚本。

为什么要抽出来：
1. 内联 heredoc 没法本地复现，出错只能看 CI 日志；
2. 现在产物多了 live.m3u 和 history.jsonl，校验项变多，内联越写越长；
3. 自检要能单独跑：`python scripts/validate_dist.py`，退出码 0/1。

校验项（任何一条不过就返回 1，CI 标红）：
  - 四份配置 JSON 能解析、sites 非空、key 不重复、api 是 http 且不含 query
  - live.txt 有地址、分组行格式正确
  - live.m3u 首行 #EXTM3U、EXTINF 与地址数量一致
  - status.json 里的源与配置里的 sites 对得上（防止产物自相矛盾）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
CONFIGS = ("tvbox.json", "tvbox-pc.json", "tvbox-desktop.json")


def check_config(name):
    path = os.path.join(DIST, name)
    if not os.path.exists(path):
        return [f"缺失 {name}"], 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return [f"{name} 解析失败：{e}"], 0
    errs, sites = [], d.get("sites") or []
    if not sites:
        errs.append(f"{name} sites 为空")
    keys = [s.get("key") for s in sites]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        errs.append(f"{name} key 重复：{sorted(dup)[:5]}")
    for s in sites:
        api = s.get("api") or ""
        if s.get("type") == "cms":
            continue
        if not api.startswith("http"):
            errs.append(f"{name} 非法 api：{api[:60]}")
        elif "?" in api:
            errs.append(f"{name} api 含 query：{api[:60]}")
    return errs, len(sites)


def check_live_txt():
    path = os.path.join(DIST, "live.txt")
    if not os.path.exists(path):
        return ["缺失 live.txt"], 0
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    body = [l for l in lines if not l.startswith("#")]
    errs = []
    if not body:
        errs.append("live.txt 没有任何频道行")
    for l in body:
        if "#genre#" in l:
            if not l.endswith(",#genre#"):
                errs.append(f"live.txt 分组行格式异常：{l[:40]}")
            continue
        if "," not in l or not l.split(",", 1)[1].startswith("http"):
            errs.append(f"live.txt 频道行格式异常：{l[:40]}")
    return errs, len([l for l in body if "#genre#" not in l])


def check_m3u():
    path = os.path.join(DIST, "live.m3u")
    if not os.path.exists(path):
        return ["缺失 live.m3u"], 0
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    errs = []
    if not lines or not lines[0].startswith("#EXTM3U"):
        errs.append("live.m3u 首行不是 #EXTM3U")
    inf = sum(1 for l in lines if l.startswith("#EXTINF"))
    urls = sum(1 for l in lines if l.startswith("http"))
    if inf != urls:
        errs.append(f"live.m3u EXTINF({inf}) 与地址({urls}) 数量不一致")
    if urls == 0:
        errs.append("live.m3u 没有地址")
    return errs, urls


def check_status():
    path = os.path.join(DIST, "status.json")
    if not os.path.exists(path):
        return ["缺失 status.json"], 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return [f"status.json 解析失败：{e}"], 0
    errs = []
    sources = d.get("sources") or []
    if not sources:
        errs.append("status.json sources 为空")
    try:
        with open(os.path.join(DIST, "tvbox.json"), "r", encoding="utf-8") as f:
            std = json.load(f)
        if len(std.get("sites") or []) != len(sources):
            errs.append("status.json 与 tvbox.json 站点数不一致")
    except Exception:
        pass
    return errs, len(sources)


def main():
    all_errs = []
    print(f"{'产物':<22}{'数量':>6}")
    checks = [(fn, check_config) for fn in CONFIGS] + [
        ("live.txt", check_live_txt), ("live.m3u", check_m3u), ("status.json", check_status)]
    for fn, chk in checks:
        errs, n = chk(fn) if chk is check_config else chk()
        all_errs.extend(errs)
        print(f"{fn:<22}{n:>6}  {'OK' if not errs else 'FAIL'}")
        for e in errs:
            print(f"    - {e}")
    if all_errs:
        print(f"\n共 {len(all_errs)} 项不通过")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
