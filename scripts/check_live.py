#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播源深度验活 + live.txt 生成

流程：拉 sources/live_sources.json 里的全部列表 → 解析 txt/m3u → 按 URL 去重
     → 逐条 deep_hls（m3u8→子列表→拉真实分片→校验 TS/fMP4）
     → 按 policy 过滤分组/关键词 → 每频道保留前 N 条备线
     → 写 dist/live.txt 与 sources/live_verified.json

为什么必须拉分片：只看 m3u8 首行会把"返回 #EXTM3U 文本但分片为空"的占位流判成活。
实测 697 个地址浅测活 514 个，深测真活 326 个（假阳性 37%）。

退出码：0 正常；1 一个频道都没验活（CI 会标红，但不会破坏旧产物）。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sources")
DIST = os.path.join(ROOT, "dist")

GROUP_ORDER = ["央视频道", "卫视频道", "电影频道", "纪录频道",
               "少儿频道", "音乐频道", "数字频道", "春晚频道", "4K8K频道", "经典剧场"]

# 各省/地区名：出现在"分组名"里视为地方台（"安徽频道""陕西频道"）。
# 注意"安徽卫视"在"卫视频道"分组里，不会误伤。
PROV = ["安徽", "北京", "上海", "天津", "重庆", "河北", "山西", "内蒙古", "内蒙",
        "辽宁", "吉林", "黑龙江", "江苏", "浙江", "福建", "江西", "山东", "河南",
        "湖北", "湖南", "广东", "广西", "海南", "四川", "贵州", "云南", "西藏",
        "陕西", "甘肃", "青海", "宁夏", "新疆", "港澳台", "港·澳·台", "香港",
        "澳门", "台湾", "地方"]

# 源里的分组名五花八门（带 emoji、带 IPV4/IPV6 后缀、带"📺"），统一映射到标准名
GROUP_ALIAS = {
    "央视": "央视频道", "央视IPV4": "央视频道", "央视IPV6": "央视频道",
    "卫视": "卫视频道", "卫视IPV4": "卫视频道", "卫视IPV6": "卫视频道",
    "电影": "电影频道", "纪录": "纪录频道", "记录": "纪录频道",
    "少儿": "少儿频道", "儿童": "少儿频道", "卡通": "少儿频道",
    "音乐": "音乐频道", "数字": "数字频道", "春晚": "春晚频道",
    "历年春晚": "春晚频道", "4K": "4K8K频道", "8K": "4K8K频道",
}


def normalize_group(g):
    """去掉 emoji/装饰符，映射到标准分组名。"""
    raw = (g or "").strip()
    clean = "".join(ch for ch in raw if ch.isalnum() or ch in "·-_频道卫视电影纪录少儿音乐数字春晚")
    clean = clean.strip()
    for suf in ("IPV4", "IPV6", "ipv4", "ipv6"):
        clean = clean.replace(suf, "")
    clean = clean.strip()
    if clean in GROUP_ALIAS:
        return GROUP_ALIAS[clean]
    for k, v in GROUP_ALIAS.items():
        if clean == k or clean.endswith(k) or clean.startswith(k):
            return v
    return clean or "未分组"


def is_local_group(g):
    return any(p in (g or "") for p in PROV)


def norm_channel_name(n):
    """频道名规范化：CCTV-1 → CCTV1，(1080p)/[Not 24/7] 后缀剥掉，CGTN 变体合并。"""
    s = (n or "").strip()
    s = re.sub(r"\s*[\(\[（【].*?[\)\]）】]\s*", "", s)
    s = re.sub(r"\s*\|.*$", "", s)
    s = re.sub(r"^CCTV[\s\-_]*(\d{1,2})\+?$", r"CCTV\1", s, flags=re.I)
    s = re.sub(r"^CCTV[\s\-_]*(\d{1,2})", r"CCTV\1", s, flags=re.I)
    u = s.upper()
    if u in ("CGTN", "CGTN英语", "CGTN ENGLISH"):
        return "CGTN"
    if "FRENCH" in u or "法语" in s:
        return "CGTN法语"
    if "DOCUMENTARY" in u or "纪录" in s:
        return "CGTN纪录"
    if "GLOBAL BIZ" in u or "财经" in s:
        return "CGTN财经"
    return s


def group_by_name(n):
    """未分组频道按名字归到大类；归不上返回 None（调用方丢弃）。"""
    s = norm_channel_name(n)
    u = s.upper()
    if u.startswith("CCTV") or u.startswith("CGTN") or "央视" in n:
        return "央视频道"
    if "卫视" in n or "TV INTERNATIONAL" in u:
        return "卫视频道"
    if "电影" in n or "影院" in n or "剧场" in n:
        return "电影频道"
    if "纪录" in n or "DOCUMENTARY" in u or "记录" in n:
        return "纪录频道"
    if "少儿" in n or "卡通" in n or "儿童" in n:
        return "少儿频道"
    if "音乐" in n or "春晚" in n:
        return "春晚频道"
    return None


def load_cfg():
    path = os.path.join(SRC, "live_sources.json")
    if not os.path.exists(path):
        print("缺少 sources/live_sources.json", file=sys.stderr)
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_dropped(group, name, drop_groups, drop_kw):
    """分组/频道名命中丢弃规则即跳过。地方台用模糊匹配（源里分组名五花八门）。"""
    g = (group or "").strip()
    if g in drop_groups:
        return True
    if "地方" in g or g.upper() in ("LOCAL", "CITY"):
        return True
    for k in drop_kw:
        if k and k in (name or ""):
            return True
    return False


def fetch_lists(cfg, drop_groups=None, drop_kw=None):
    """拉所有启用的直播列表，解析成 (group, name, url, 来源名)。"""
    drop_groups = set(drop_groups or [])
    drop_kw = list(drop_kw or [])
    rows = []
    meta = []
    for s in sorted(cfg.get("sources", []), key=lambda x: x.get("priority", 99)):
        if not s.get("enabled", True):
            continue
        ok, st, body, ms, ch, err = pc.http_get_dual(s["url"], 30, stream_bytes=4_000_000)
        if not ok:
            print(f"  [拉取失败] {s['name']:<16} status={st} {err or ''}")
            meta.append({"name": s["name"], "url": s["url"], "ok": False,
                         "status": st, "channels": 0, "ms": ms})
            continue
        txt = body.decode("utf-8", "ignore")
        chans = pc.parse_live_text(txt)
        kept = 0
        for g, n, u in chans:
            if is_dropped(g, n, drop_groups, drop_kw):
                continue
            rows.append((g, n, u, s["name"]))
            kept += 1
        print(f"  [OK]       {s['name']:<16} {len(body)//1024:>5}KB  频道 {len(chans):>5} → 保留 {kept}")
        meta.append({"name": s["name"], "url": s["url"], "ok": True,
                     "status": st, "channels": len(chans), "ms": ms})
    return rows, meta


def rebuild_from_cache():
    """用上次的 live_verified.json 重新生成 live.txt（改分组规则后不用重测 2 分钟）。"""
    path = os.path.join(SRC, "live_verified.json")
    if not os.path.exists(path):
        print("没有缓存", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_cfg()
    if not cfg:
        return 1
    policy = cfg.get("policy", {})
    drop_groups = set(policy.get("drop_groups", []))
    drop_kw = policy.get("drop_name_keywords", [])
    keep_n = int(policy.get("per_channel_keep", 2))
    workers = int(policy.get("workers", 16))
    timeout = int(policy.get("timeout", 8))

    use_cache = "--from-cache" in sys.argv
    alive, stat, meta = {}, {"candidates": 0, "prefilter_pass": 0, "alive": 0,
                             "dead": 0, "kinds": {}}, []
    metrics = {}

    if use_cache:
        cache = rebuild_from_cache()
        if not cache:
            return 1
        alive, stat = cache["channels"], cache["stat"]
        meta = cache.get("list_sources", [])
        metrics = cache.get("metrics") or {}
        print(f"== 缓存模式（{cache.get('generated','?')}）：{stat['alive']} 条真活，跳过拉取与验活 ==")
    else:
        print("== 1/3 拉取直播列表 ==")
        rows, meta = fetch_lists(cfg, drop_groups, drop_kw)
        if not rows:
            print("没有任何频道候选，中止（保留旧产物）", file=sys.stderr)
            return 1

        # 去重（同 URL 只测一次），并过滤明显无效项
        seen, uniq = set(), []
        for g, n, u, src in rows:
            if u in seen:
                continue
            if any(k in (n or "") for k in drop_kw):
                continue
            seen.add(u)
            uniq.append((g, n, u, src))
        print(f"  候选频道行 {len(rows)} 条 → 去重后 {len(uniq)} 条待验活")

        print(f"\n== 2/3 深度验活（拉真实分片，并发 {workers}）==")
        B = 240
        for i in range(0, len(uniq), B):
            part = [(g, n, u) for g, n, u, _ in uniq[i:i + B]]
            a2, s2 = pc.probe_channel_list(part, workers=workers, timeout=timeout,
                                           per_channel_keep=keep_n)
            metrics.update(s2.get("metrics") or {})
            for g, d in a2.items():
                for n, us in d.items():
                    tgt = alive.setdefault(g, {}).setdefault(n, [])
                    for u in us:
                        if u not in tgt and len(tgt) < keep_n:
                            tgt.append(u)
            for k in ("candidates", "prefilter_pass", "alive", "dead"):
                stat[k] += s2.get(k, 0)
            for k, v in s2.get("kinds", {}).items():
                stat["kinds"][k] = stat["kinds"].get(k, 0) + v
            done = min(i + B, len(uniq))
            print(f"    进度 {done}/{len(uniq)}  预筛通过 {stat['prefilter_pass']}  "
                  f"真活 {stat['alive']}", flush=True)

        print(f"  地址级：真活 {stat['alive']} / 死 {stat['dead']} "
              f"（存活率 {stat['alive']*100//max(1,stat['candidates'])}%）")
        print(f"  格式分布：{stat.get('kinds')}")

    print("\n== 3/3 归一化分组并生成 live.txt ==")
    # 归一化分组 + 二次过滤（源里 emoji/省名分组在拉取阶段没拦住，这里兜底）
    norm = {}
    for g, d in alive.items():
        if is_local_group(g):
            print(f"  [跳过地方台分组] {g}（{len(d)} 个频道）")
            continue
        ng = normalize_group(g)
        for n, us in d.items():
            target = ng
            if ng == "未分组":
                target = group_by_name(n)      # 未分组的靠频道名归类，归不上就丢
                if not target:
                    continue
            nn = norm_channel_name(n)          # CCTV-1 与 CCTV1 合并成同一个频道
            slot = norm.setdefault(target, {}).setdefault(nn, [])
            for u in us:
                if u not in slot and len(slot) < keep_n:
                    slot.append(u)

    # 全局收敛：同一频道名可能被归到两个大类（如 CGTN纪录 同时出现在"央视频道"
    # 和"纪录频道"两个源分组里），合并到先出现的那个；URL 全局唯一。
    merged, seen_url = {}, set()
    name_home = {}
    for g in list(norm):
        for n, us in list(norm[g].items()):
            home = name_home.get(n, g)
            slot = merged.setdefault(home, {}).setdefault(n, [])
            for u in us:
                if u in seen_url or len(slot) >= keep_n:
                    continue
                slot.append(u)
                seen_url.add(u)
            # 同一频道可能来自不同源、速度差很多：组内按实测分片速度降序
            slot.sort(key=lambda x: -(metrics.get(x, {}).get("kbps") or 0))
            name_home[n] = home
    norm = {g: d for g, d in merged.items() if d}

    ch_total = sum(len(v) for v in norm.values())
    print(f"  频道级：{ch_total} 个唯一频道（已排除地方台）")

    lines = ["# my-tvbox 真活直播源",
             "# 全部频道经深度验活：m3u8 → 子列表 → 拉真实分片 → 校验 TS/fMP4",
             f"# 候选 {stat['candidates']} 条地址，真活 {stat['alive']} 条",
             ""]
    total_lines = 0
    ordered = [g for g in GROUP_ORDER if g in norm]
    ordered += [g for g in sorted(norm) if g not in GROUP_ORDER]
    for g in ordered:
        items = sorted(norm[g].items())
        if not items:
            continue
        lines.append(f"{g},#genre#")
        for n, us in items:
            for u in us:
                lines.append(f"{n},{u}")
                total_lines += 1
        lines.append("")

    txt = "\n".join(lines)
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "live.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt)
    size = os.path.getsize(out)
    print(f"  dist/live.txt  {total_lines} 条地址 / {ch_total} 频道  {size} 字节")
    for g in ordered:
        print(f"    {g:<10} {len(norm[g]):>3} 个频道")

    if not use_cache:
        import time as _t
        verified = {
            "generated": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
            "policy": policy,
            "stat": stat,
            "list_sources": meta,
            "channels": {g: {n: us for n, us in v.items()} for g, v in alive.items()},
            "metrics": metrics,
        }
        with open(os.path.join(SRC, "live_verified.json"), "w", encoding="utf-8") as f:
            json.dump(verified, f, ensure_ascii=False, indent=1)

    if total_lines == 0:
        print("警告：没有任何频道通过验活", file=sys.stderr)
        return 1
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
