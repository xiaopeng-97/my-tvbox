# -*- coding: utf-8 -*-
"""
TVBox 源全量测活引擎（纯标准库，可在 GitHub Actions 里直接跑）

设计要点：
1. 每个 site 条目都测，不抽样；相同 api 只发一次请求、结果复用（报告里注明映射关系）。
2. L1 列表 / L2 搜索 / L3 详情取播放地址 / L4 m3u8 首行校验，四级逐级淘汰。
3. 双通道：先按系统代理请求，失败再强制直连重试一次，避免把"本机代理坏了"误判成"源死了"。
4. L4 只流式读前 2KB 就断开，不整份下载 m3u8。
"""
import json, re, ssl, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))


def _ok_status(st):
    """HTTP 状态是否算成功。只认 2xx/3xx，0（连不上）和 4xx/5xx 都算失败。

    旧写法 `if not st or 200 > st >= 400` 里的 `200 > st >= 400` 恒为 False，
    4xx/5xx 会被当成"能连上"，判死原因从"不可达"变成"分片过小"，误导排查。
    """
    return bool(st) and 200 <= st < 400


def _headers(ua=UA, extra=None):
    h = {"User-Agent": ua, "Accept": "*/*"}
    if extra:
        h.update({k: v for k, v in extra.items() if v})
    return h


def http_get(url, timeout=8, ua=UA, stream_bytes=0, retries=1, headers=None):
    """返回 (status, body_bytes, elapsed, err)；stream_bytes>0 时只读前 N 字节。

    retries>0 时对超时/连接重置这类瞬时错误重试（默认 1 次）。
    Actions 出口抖动很常见，一次超时不该直接判源死——这是误杀的主要来源之一。
    """
    hdrs = _headers(ua, headers)
    last = (0, b"", 0.0, "no-attempt")
    for attempt in range(max(0, int(retries)) + 1):
        t = time.time()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
            body = r.read(stream_bytes) if stream_bytes else r.read()
            if stream_bytes:
                r.close()
            return r.status, body, round(time.time() - t, 2), None
        except Exception as e:
            last = (0, b"", round(time.time() - t, 2), f"{type(e).__name__}: {str(e)[:120]}")
            if attempt >= int(retries):
                break
            time.sleep(0.4)
    return last


def http_get_dual(url, timeout=8, ua=UA, stream_bytes=0, retries=1, headers=None):
    """先走系统代理，失败后强制直连再试一次。返回 (ok, status, body, elapsed, channel, err)。"""
    st, body, ms, err = http_get(url, timeout, ua, stream_bytes, retries, headers)
    if _ok_status(st):
        return True, st, body, ms, "proxy", None
    st2, body2, ms2, err2 = (0, b"", 0, "skip")
    try:
        req = urllib.request.Request(url, headers=_headers(ua, headers))
        t = time.time()
        r = _DIRECT.open(req, timeout=timeout)
        body2 = r.read(stream_bytes) if stream_bytes else r.read()
        if stream_bytes:
            r.close()
        st2, ms2, err2 = r.status, round(time.time() - t, 2), None
    except Exception as e:
        err2 = f"{type(e).__name__}: {str(e)[:120]}"
    if _ok_status(st2):
        return True, st2, body2, ms2, "direct", None
    return False, st2 or st, b"", ms2, "none", err2 or err


def key_from_api(api, fallback="src"):
    """从 api 域名推出稳定、短、纯 ASCII 的 site key（两个脚本共用，避免各写一套）。"""
    host = urllib.parse.urlparse(api or "").hostname or ""
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return re.sub(r"[^a-z0-9]", "", parts[-2])[:12] or fallback
    return re.sub(r"[^a-z0-9]", "", host)[:12] or fallback


def _json(body):
    return json.loads(body.decode("utf-8", "ignore"))


def extract_play_urls(play_url, prefer_hls=True):
    """从 vod_play_url 里抽播放地址，m3u8 优先。

    苹果CMS 的格式是 `第01集$https://a.m3u8#第02集$https://b.m3u8`，
    也常见直接 `https://x.mp4`（或 m3u8 带 query）。旧实现只认 `.m3u8` 结尾，
    带 query 的 m3u8 和 mp4 源全被判成"有详情无m3u8"，属于漏判。
    """
    raw = (play_url or "").strip()
    if not raw:
        return []
    found = re.findall(r"https?://[^\s#$]+", raw)
    if not found:
        return []
    seen, out = set(), []
    for u in found:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    if prefer_hls:
        out.sort(key=lambda u: (0 if ".m3u8" in u.lower() else 1))
    return out


def probe_cms(api, keyword="庆余年", timeout=8, deep=True):
    """标准苹果CMS(v10) 站点四级测活。"""
    rec = {"api": api, "kind": "cms"}
    base = api.split("?")[0]

    ok, st, body, ms, ch, err = http_get_dual(base, timeout)
    rec.update(l1_ok=ok, l1_status=st, l1_ms=ms, l1_channel=ch, l1_err=err)
    if not ok:
        return rec
    try:
        d = _json(body)
        lst = d.get("list") or []
        rec.update(l1_list_n=len(lst), l1_total=d.get("total"),
                   l1_first=(lst[0].get("vod_name") if lst else None),
                   l1_first_time=(lst[0].get("vod_time") if lst else None))
        if not lst:
            rec["verdict"] = "无数据"
            return rec
    except Exception as e:
        rec.update(verdict="非JSON/非CMS", note=f"{type(e).__name__}")
        return rec

    if not deep:
        rec["verdict"] = "L1通过"
        return rec

    # L2 搜索
    ok2, st2, b2, ms2, ch2, err2 = http_get_dual(
        f"{base}?ac=videolist&wd={urllib.parse.quote(keyword)}", timeout)
    rec.update(l2_ok=ok2, l2_n=0, l2_channel=ch2)
    if ok2:
        try:
            d2 = _json(b2)
            rec["l2_n"] = len(d2.get("list") or [])
        except Exception:
            rec["l2_ok"] = False

    # L3 详情 + 播放地址
    vid = None
    try:
        d2 = _json(b2)
        for it in (d2.get("list") or []):
            if it.get("vod_id"):
                vid = it["vod_id"]
                break
    except Exception:
        pass
    if vid is None:
        try:
            vid = (_json(body).get("list") or [{}])[0].get("vod_id")
        except Exception:
            pass
    if vid is None:
        rec["verdict"] = "取不到详情ID"
        return rec

    ok3, st3, b3, ms3, ch3, err3 = http_get_dual(f"{base}?ac=videolist&ids={vid}", timeout)
    if not ok3:
        rec.update(l3_ok=False, verdict="详情失败")
        return rec
    try:
        it = (_json(b3).get("list") or [{}])[0]
    except Exception:
        rec.update(l3_ok=False, verdict="详情非JSON")
        return rec
    pu = it.get("vod_play_url") or ""
    urls = extract_play_urls(pu)
    rec.update(l3_ok=bool(pu), play_lines=len(re.findall(r"\$", pu)) + (1 if pu else 0),
               m3u8_n=len(urls), detail_name=it.get("vod_name"))
    if not urls:
        rec["verdict"] = "有详情无播放地址"
        return rec

    # L4 播放地址校验
    # 旧实现只读 2KB 看有没有 #EXTM3U —— 这正是 README 里批判的"假活"判据：
    # 占位流也会回 #EXTM3U 文本，分片却是空的。这里改成与直播同一套判据：
    # m3u8 → (master)子列表 → 拉第一个分片 → 校验 TS/fMP4。
    # 只对前 L4_TRY 条播放地址试，避免单源拖慢整体。
    rec["l4_sample"] = urls[0][:100]
    l4_ok, last_note, best = False, "", None
    for u in urls[:L4_TRY]:
        d = deep_hls(u, timeout)
        if d["ok"]:
            l4_ok, best = True, d
            break
        last_note = d["note"]
    if best:
        rec.update(l4_ok=True, l4_status=200, l4_ms=best["ms"], l4_kind=best["kind"],
                   l4_kbps=best.get("kbps") or 0.0, l4_err=None,
                   l4_sample=best["url"][:100])
        rec["verdict"] = "四级通过"
    else:
        # 保留"首行能读到"的信息，方便区分"整个站点死了"和"只是这条播放地址死了"
        ok4, st4, b4, ms4, _ch4, err4 = http_get_dual(urls[0], timeout, stream_bytes=2048)
        head_ok = bool(ok4 and "#EXTM3U" in b4.decode("utf-8", "ignore"))
        rec.update(l4_ok=False, l4_status=st4, l4_ms=ms4, l4_head_ok=head_ok,
                   l4_err=err4, l4_note=last_note or "不可播")
        rec["verdict"] = ("播放地址不可播" if head_ok
                          else ("播放地址不可达" if not ok4 else "播放地址非m3u8"))
    return rec


def probe_live(url, timeout=6):
    """直播播放列表：取内容并抽验首条 HLS。"""
    ok, st, body, ms, ch, err = http_get_dual(url, timeout * 2, stream_bytes=200000)
    rec = {"url": url, "kind": "live_list", "ok": ok, "status": st, "ms": ms, "channel": ch, "err": err}
    if not ok:
        rec["verdict"] = "列表不可达"
        return rec
    txt = body.decode("utf-8", "ignore")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    chans = []
    for l in lines:
        if l.startswith("#") or "#genre#" in l or "," not in l:
            continue
        n, u = l.split(",", 1)
        if u.startswith("http"):
            chans.append((n.strip(), u.strip()))
    rec.update(lines=len(lines), channels=len(chans), uniq_ch=len(set(n for n, _ in chans)),
               groups=len([l for l in lines if l.endswith("#genre#")]))
    if chans:
        ok2, st2, b2, ms2, ch2, e2 = http_get_dual(chans[0][1], timeout, stream_bytes=1024)
        rec.update(first_ch=chans[0][0], first_ok=bool(ok2 and "#EXTM3U" in b2.decode("utf-8", "ignore")))
    rec["verdict"] = "列表可用" if rec.get("channels") else "无有效频道行"
    return rec


# ============================================================
# 深度验活（HLS）：m3u8 -> (master)子列表 -> 第一个分片 -> 校验 TS/fMP4
# 为什么需要：只看 m3u8 首行会把"返回 #EXTM3U 文本但分片为空"的占位流判成活。
# 实测 697 个直播地址里，浅测活 514 个，深测真活只有 326 个（假阳性 37%）。
# ============================================================

VARIANT_TRIES = 3      # master 里最多试几个子列表（只试第一个会漏掉"低码率挂了高码率活着"的源）
L4_TRY = 2             # 点播 L4 最多试几条播放地址（一条挂了换下一条，别整源判死）
SEG_BYTES = 60000      # 分片只读前 60KB，够判格式 + 够算瞬时速度


def _seg_probe(segurl, timeout=8, encrypted=False):
    """拉分片前 60KB，返回 (ok, kind, size, ms, kbps)。

    kbps = 本次读到的字节数 / 耗时，用来给备线排序（快的排前面，客户端秒开）。

    encrypted=True（播放列表里有 #EXT-X-KEY）时不做格式判定：
    AES-128 加密的分片在客户端解密前就是随机字节，按 0x47/ftyp 判会全部误杀。
    这种情况只要求"分片能取到、够大、不是占位 m3u8 文本"。
    """
    st, body, ms, err = http_get(segurl, timeout, stream_bytes=SEG_BYTES)
    if not _ok_status(st):
        return False, "分片不可达", 0, ms, 0.0
    if len(body) < 800:
        return False, f"分片过小({len(body)}B)", len(body), ms, 0.0
    if b"#EXTM3U" in body[:64]:
        return False, "分片仍是m3u8(占位流)", len(body), ms, 0.0
    kbps = round(len(body) / 1024.0 / ms, 1) if ms else 0.0
    if encrypted:
        return True, "HLS-ENC", len(body), ms, kbps
    if body[0] == 0x47:
        return True, "TS", len(body), ms, kbps
    if body[4:8] in (b"ftyp", b"styp", b"moof", b"moov"):
        return True, "fMP4", len(body), ms, kbps
    return False, f"未知格式({body[:4].hex()})", len(body), ms, 0.0


def _abs_url(base_url, ref):
    """把播放列表里的相对地址解析成绝对地址。

    必须用 urljoin 而不是"取 base 的目录再拼"：HLS 里 `/2024/xx/index.m3u8`
    这种根相对路径（360zy 一类站点的常见写法）会被拼成
    `https://host/2024/2024/xx/index.m3u8`，整源被判"子列表无效"。
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    if ref.startswith("http"):
        return ref
    return urllib.parse.urljoin(base_url, ref)


def _first_segment_encrypted(playlist):
    """判断"第一个分片"是否加密（按 EXT-X-KEY 的出现顺序逐个生效）。

    坑：同一个播放列表里可以有多段 KEY，常见写法是开头 AES-128、后面
    `#EXT-X-KEY:METHOD=NONE` 明文。用"整份文本里有没有 METHOD=NONE"判断，
    结果会随播放列表长度/顺序变化而漂移，同一个源一会判活一会判死。
    这里严格按顺序跟踪"第一个分片生效的 key 状态"。
    """
    enc = False
    for line in (playlist or "").splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-KEY"):
            enc = "METHOD=NONE" not in s
        elif s and not s.startswith("#"):
            return enc
    return enc


def _pick_variants(txt, limit=VARIANT_TRIES):
    """master playlist 里按 BANDWIDTH 从高到低挑子列表，最多 limit 个。

    旧实现只取第一条，遇到"第一条是 4K 但已挂、后面 720p 还活着"就整源判死。
    """
    rows, cur_bw = [], 0
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            cur_bw = int(m.group(1)) if m else 0
        elif line and not line.startswith("#"):
            rows.append((cur_bw, line))
            cur_bw = 0
    rows.sort(key=lambda x: -x[0])
    return [u for _, u in rows[:limit]]


def deep_hls(url, timeout=8):
    """深度验活单个 HLS 地址。

    返回 dict(ok, kind, size, ms, kbps, resolution, note, variants_tried)。
    kbps 是分片实测下载速度，用于备线排序（慢的排后面）。
    """
    rec = {"url": url, "ok": False, "kind": "", "size": 0, "ms": 0, "kbps": 0.0,
           "resolution": "", "note": "", "variants_tried": 0}
    st, body, ms, err = http_get(url, timeout, stream_bytes=200000)
    rec["ms"] = ms
    if not _ok_status(st):
        rec["note"] = "地址不可达"
        return rec
    if body[:1] == b"\x47":                      # 直连 TS（非 HLS）
        rec.update(ok=True, kind="raw-TS", size=len(body),
                   kbps=round(len(body) / 1024.0 / ms, 1) if ms else 0.0, note="直连TS")
        return rec
    txt = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in txt:
        rec["note"] = "非m3u8"
        return rec
    m = re.search(r"RESOLUTION=(\d+x\d+)", txt)
    if m:
        rec["resolution"] = m.group(1)
    # 待测播放列表：(解析基准 URL, 播放列表正文)
    # 基准 URL 必须跟着"这份正文是从哪个地址取回来的"走，不能一律用顶层 url：
    # master 的子列表在子目录里，分片相对路径要以子列表地址为基准解析，
    # 用顶层 url 会拼错路径（原实现就是这里的隐患）。
    subs = [(url, txt)]
    if "#EXT-X-STREAM-INF" in txt:               # master playlist：按码率降序试多个
        picked = _pick_variants(txt)
        if not picked:
            rec["note"] = "master无子列表"
            return rec
        subs = []
        for su in picked:
            full = _abs_url(url, su)
            st2, b2, _, _ = http_get(full, timeout, stream_bytes=200000)
            sub_txt = b2.decode("utf-8", "ignore")
            if _ok_status(st2) and "#EXTM3U" in sub_txt:
                subs.append((full, sub_txt))
                rec["variants_tried"] += 1
            if len(subs) >= VARIANT_TRIES:
                break
        if not subs:
            rec["note"] = "子列表无效"
            return rec
    last_note = ""
    for sub_base, sub in subs:
        segs = [l.strip() for l in sub.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            last_note = "播放列表无分片"
            continue
        segurl = _abs_url(sub_base, segs[0])
        enc = _first_segment_encrypted(sub)
        ok, kind, size, ms3, kbps = _seg_probe(segurl, timeout, encrypted=enc)
        if ok:
            rec.update(ok=True, kind=kind, size=size, ms=ms3, kbps=kbps, note="")
            return rec
        last_note = kind
    rec["note"] = last_note or "不可播"
    return rec


def parse_live_text(txt):
    """解析 txt(#genre#) 或 m3u 直播列表 -> [(group, name, url)]。"""
    out = []
    if "#EXTINF" in txt:
        cur, grp = "", "未分组"
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                cur = line.split(",", 1)[1].strip() if "," in line else ""
                gm = re.search(r'group-title="([^"]*)"', line)
                if gm:
                    grp = gm.group(1).strip() or grp
            elif line.startswith("http") and cur is not None:
                out.append((grp, cur, line))
                cur = ""
    else:
        grp = "未分组"
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#genre#" in line:
                grp = line.split(",")[0].strip()
                continue
            if "," not in line:
                continue
            n, u = line.split(",", 1)
            if u.strip().startswith("http"):
                out.append((grp, n.strip(), u.strip()))
    return out


def probe_channel_list(channels, workers=16, timeout=7, per_channel_keep=2, prefilter=True):
    """对 [(group, name, url)] 逐条深度验活，按频道名归并。

    prefilter=True 时先用 1KB 头快速筛一遍（死链大多在这里就被淘汰），
    再对存活的做 deep_hls 拉分片。实测能把 600 条候选的耗时从 ~7 分钟压到 ~1.5 分钟。

    返回 (alive, stat)：
      alive = {group: {name: [url, ...]}}，组内按实测分片速度降序（快的排前面，客户端秒开）
      stat["metrics"] = {url: {"kbps": float, "kind": str, "ms": float}}，供排序与历史记录使用
    """
    items = [(g, n, u) for g, n, u in channels]

    kept = items
    if prefilter:
        def quick(it):
            g, n, u = it
            st, body, ms, err = http_get(u, 3, stream_bytes=1024)
            if not _ok_status(st):
                return None
            if body[:1] == b"\x47":
                return it
            if b"#EXTM3U" in body or b"#EXTINF" in body:
                return it
            return None
        with ThreadPoolExecutor(max_workers=workers * 3) as ex:
            kept = [x for x in ex.map(quick, items) if x]

    def one(it):
        g, n, u = it
        return (g, n, u, deep_hls(u, timeout))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, kept))

    alive, metrics = {}, {}
    stat = {"candidates": len(items), "prefilter_pass": len(kept),
            "alive": 0, "dead": len(items) - len(kept), "kinds": {}, "metrics": metrics}
    for g, n, u, r in res:
        if r["ok"]:
            stat["alive"] += 1
            stat["kinds"][r["kind"]] = stat["kinds"].get(r["kind"], 0) + 1
            metrics[u] = {"kbps": r.get("kbps") or 0.0, "kind": r.get("kind") or "",
                          "ms": r.get("ms") or 0}
            slot = alive.setdefault(g, {}).setdefault(n, [])
            if u not in slot:
                slot.append(u)
            # 按实测速度降序保留前 N 条：快线优先，慢线只在不够数时补位
            slot.sort(key=lambda x: -(metrics.get(x, {}).get("kbps") or 0))
            del slot[per_channel_keep:]
        else:
            stat["dead"] += 1
    return alive, stat


def score(rec):
    """0-100 分。权重：可播 40 / 数据量 15 / 搜索 15 / 速度 15 / 新鲜度 15。"""
    s = 0.0
    if rec.get("l4_ok"):
        s += 40
    elif rec.get("l3_ok"):
        s += 20
    elif rec.get("l1_ok"):
        s += 8
    n = rec.get("l1_total") or 0
    s += 15 if n > 50000 else 10 if n > 10000 else 5 if n > 1000 else 0
    s += 15 if (rec.get("l2_n") or 0) > 0 else 0
    ms = rec.get("l1_ms") or 9
    s += 15 if ms < 1 else 10 if ms < 2.5 else 5 if ms < 5 else 0
    ft = rec.get("l1_first_time") or ""
    if ft[:10] == time.strftime("%Y-%m-%d"):
        s += 15
    elif ft[:7] == time.strftime("%Y-%m"):
        s += 10
    return round(s, 1)


def run_batch(items, kind="cms", workers=32, keyword="庆余年"):
    """items: [(name, api)] → list[dict]"""
    def one(it):
        name, api = it
        try:
            rec = probe_cms(api, keyword=keyword) if kind == "cms" else probe_live(api)
        except Exception as e:
            rec = {"api": api, "kind": kind, "verdict": f"异常 {type(e).__name__}"}
        rec["name"] = name
        rec["score"] = score(rec) if kind == "cms" else 0
        return rec
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, items))
