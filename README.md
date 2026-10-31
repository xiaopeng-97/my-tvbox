# my-tvbox

一套**实测驱动**的 TVBox 配置仓库：点播源四级测活、直播源深度验活（拉真实分片），
GitHub Actions 每 24 小时自动重跑，死源自动降级、淘汰、复活，产物直接给客户端用。

> 核心原则：**不放没验过的源**。宁可只有 22 个能用的，也不放 200 个点开就报错的。

---

## 一、客户端配置地址

部署后把下面任意一条填进客户端的「配置地址」（把 `xiaopeng-97` 换成你的 GitHub 用户名）：

| 客户端 | 地址 |
|---|---|
| **TVBox Desktop**（推荐） | `https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/tvbox-desktop.json` |
| TVBoxPC / TVBox 通用 | `https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/tvbox.json` |
| TVBoxPC 增强（含 jar 蜘蛛） | `https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/tvbox-pc.json` |
| zyfun / ZyPlayer | 用上面的 `tvbox.json`（其 `tbl_site` 的 `type=1` 即苹果CMS） |
| 多仓订阅 | `https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/multi.json` |
| **通用播放器**（PotPlayer / VLC / TiviMate / Kodi） | `https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/live.m3u` |

GitHub 直连慢的话，在前面加反代前缀：`https://gh-proxy.com/` + 原地址。

---

## 二、定时更新

`.github/workflows/healthcheck.yml`：

```yaml
schedule:
  - cron: '10 22 * * *'   # 每天一次：北京时间 06:10（UTC 22:10）
```

每次运行：

0. **单元测试** `python -m unittest discover -s tests`：把验活判据（地址解析、加密判定、播放地址抽取）钉住，判据改错时先在这里红，而不是产出"全绿但全死链"的配置
1. **点播测活** `check_health.py`（并发，默认 8 线程，可用 `HEALTH_WORKERS` 覆盖）
   - 连续失败 1~2 次 → `score` 打折排后（降级）
   - 连续失败 ≥3 次 → `status=retired` 移出配置
   - retired 满 30 天 → 自动复检，活了回来
   - 每轮结果追加到 `sources/history.jsonl`，供 `status.json` 算 7 日可用率
2. **直播深度验活** `check_live.py`：拉候选列表 → 解析频道 → **拉真实 TS/fMP4 分片** → 只留能播的 → 按实测速度排备线
3. **重建产物** `build_config.py`：4 份配置 + `live.txt` + `live.m3u` + `status.json` + 徽章
4. **产物校验** `validate_dist.py`：JSON 可解析、`sites` 非空、key 不重复、`api` 无 query、`live.txt`/`live.m3u` 有条目且 EXTINF 与地址数一致，任一不过标红

---

## 三、验活标准

### 点播：四级

| 级别 | 检查 | 淘汰条件 |
|---|---|---|
| L1 | `GET api` 有无 JSON、`list` 有无数据 | 无数据 / 非 JSON |
| L2 | `?ac=videolist&wd=庆余年` | 搜索无结果（标为不可搜，仍保留浏览） |
| L3 | `?ac=videolist&ids=<id>` 的 `vod_play_url` | 无播放地址 |
| L4 | **拉真实分片**：m3u8 → (master)子列表 → 第一个分片 → 校验 0x47(TS) / ftyp/styp/moof(fMP4) | 不可播 |

L4 与直播用同一套判据，而不是只看 `#EXTM3U` 首行——理由见下一节。三条实现细节：

- master 播放列表按 `BANDWIDTH` 从高到低**试多个**子列表（默认 3 个），避免"4K 挂了但 720p 还活着"被整源判死；
- 播放列表里的相对地址一律用 `urljoin` 解析，兼容 `/2024/xx/index.m3u8` 这种根相对写法；
- 带 `#EXT-X-KEY` 的 AES-128 加密流不做 TS/fMP4 格式判定（客户端解密前就是随机字节），只要求"分片能取到、够大、不是占位 m3u8 文本"——否则会把大批正常加密源误杀。

### 直播：深度验活（最关键）

```
m3u8 首行有 #EXTM3U 就算活？  ❌ 会得到大量假阳性
真实标准：m3u8 → (master)子列表 → 拉第一个分片 → 校验 0x47(TS) 或 ftyp/styp/moof(fMP4)
```

**备线排序**：同一频道保留的 2 条地址，按实测分片下载速度（`kbps`）降序排列，客户端默认取第一条 → 秒开概率更高。

**实测（2672 条候选）**：

| 判据 | 通过 |
|---|---:|
| 只看 m3u8 首行 | 大量（假活） |
| **拉到真实分片** | **403 条（15%）** |

典型骗局：`http://xxx:8181/720p/cctv1.m3u8` 返回 `#EXTM3U`，但分片只有 170~198 字节，
内容是 `#EXTM3U
...` 文本，永远播不出来。只看首行必然误判。

另一层坑：很多源的分组名带 emoji 或后缀（`📺央视频道`、`央视IPV4`、`CCTV-1 (1080p)`），
直接按组名归类会把同一个台拆成好几个，`check_live.py` 里有 `normalize_group` /
`norm_channel_name` 做归一化。

---

## 四、当前实测结果

### 点播：22 个源

| 类型 | 数量 |
|---|---:|
| 可搜索 + 可播 | 15 |
| 仅可浏览 | 5 |
| 搜索异常（保留备用） | 2 |
| **置顶（用户指定）** | **5**：非凡 / 量子 / 电影天堂 / 光速 / 素博 |

分类页「总类空」的原因：苹果CMS 的 `class` 里「电影片」这类总类只做导航、不挂片，
片子全在子类（动作片/喜剧片）。**配置层改不了**；写中文 `categories` 反而会让
`t` 参数失效、整源分类全空 —— 所以本仓库**不写 `categories`**。

### 直播：只保留真活（71 个频道 / 86 条地址）

| 分组 | 频道数 | 明细 |
|---|---:|---|
| 央视频道 | 14 | CCTV1/3/5/8/9/10/12/13/15/16 + CGTN/CGTN法语/CGTN纪录/CGTN财经 |
| 卫视频道 | 18 | 湖南/浙江/东方/安徽/深圳/江西/辽宁/黑龙江/东南/山西/河北/内蒙古/西藏/延边/安多/人间/苏州4K/山东教育 |
| 电影频道 | 12 | |
| 纪录频道 | 11 | |
| 春晚频道 | 6 | |
| 少儿频道 | 3 | |
| 音乐频道 | 3 | |
| 数字频道 | 2 | |
| 4K8K / 经典剧场 | 2 | |

**地方频道全部剔除**：`live_sources.json` 的 `policy.drop_groups`，
另有 `PROV` 省名列表兜底（`☘️安徽频道`、`🌊港·澳·台` 这类按组名识别）。
要地方台时把 `"地方频道"` 从数组删掉即可。

缺口（免费源层面确实拿不到，每个都试了 9~27 个候选）：
央视 CCTV2/4/6/7/11/14/17，卫视 北京/江苏/广东/天津/重庆/四川/湖北/河南 等。

---

## 五、目录结构

```
my-tvbox/
├─ sources/
│  ├─ allowed.json        # 活源池（22 个，含 pinned/status/score/fail_streak）
│  ├─ candidates.json     # 隔离区（复检用）
│  ├─ live_sources.json   # 直播候选源 + 丢弃策略
│  ├─ live_verified.json  # 上次验活结果（频道 → 地址）
│  └─ seed_api.txt        # 手工候选接口
├─ scripts/
│  ├─ probe_core.py       # 测活引擎：四级点播 + deep_hls 直播 + 两段式预筛
│  ├─ check_health.py     # 点播降级/淘汰/复活（并发 + 历史记录）
│  ├─ check_live.py       # 直播深度验活 → live.txt（备线按速度排序）
│  ├─ build_config.py     # 生成 4 份配置 + live.m3u + status + badge
│  ├─ validate_dist.py    # 产物自检（本地可单独跑）
│  └─ discover.py         # GitHub 发现新源（四道闸）
├─ tests/                 # 判据纯函数单测（unittest，无第三方依赖）
├─ dist/                  # 产物（客户端直接读这里）
└─ .github/workflows/
   ├─ healthcheck.yml     # 每日 2 次：测活 → 重建 → 校验 → 提交
   ├─ rebuild.yml         # 改 allowed.json 自动重建
   └─ discover.yml        # 每周发现新源
```

---

## 六、本地跑

```bash
python -m unittest discover -s tests                            # 判据单测（秒级）
python scripts/validate_dist.py                                 # 产物自检
python scripts/check_health.py                                  # 点播测活（并发 8，~1 分钟）
python scripts/check_live.py                                    # 直播深度验活（~3 分钟）
python scripts/check_live.py --from-cache                       # 用上次结果只重生成 live.txt
PUBLIC_BASE=http://127.0.0.1:8899 LIVE_REL=live.txt python scripts/build_config.py
python -m http.server 8899 --bind 127.0.0.1 --directory dist
```

纯标准库，无第三方依赖，Python 3.9+。

---

## 七、免责

- 仓库只做**接口连通性与产物格式**的技术整理，不存储、不转码、不分发任何视频内容。
- 所有源来自公开网络，版权归各自权利方所有。
- 源随时可能失效，定时任务只是让它**失效得慢一点、死掉的不占位**。

---

![活源](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/xiaopeng-97/my-tvbox/main/dist/badge.json)
