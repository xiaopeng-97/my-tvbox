# -*- coding: utf-8 -*-
"""probe_core 纯函数单元测试（只用标准库 unittest，CI 里 `python -m unittest discover -s tests`）。

为什么要有：验活引擎的判据全在几个纯函数里（地址解析、加密判定、分组归一化），
这些地方一旦改错，产出的是一份"看起来正常但全是死链"的配置，而 CI 是绿的。
用离线用例把它们钉住，改动时能立刻发现回归。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import probe_core as pc  # noqa: E402


class TestStatus(unittest.TestCase):
    def test_ok_status(self):
        self.assertTrue(pc._ok_status(200))
        self.assertTrue(pc._ok_status(302))
        self.assertFalse(pc._ok_status(0))
        self.assertFalse(pc._ok_status(403))
        self.assertFalse(pc._ok_status(500))


class TestAbsUrl(unittest.TestCase):
    def test_absolute_kept(self):
        self.assertEqual(pc._abs_url("https://a.com/x/index.m3u8", "https://b.com/y.m3u8"),
                         "https://b.com/y.m3u8")

    def test_root_relative(self):
        # 这是 360zy 一类站点的写法：必须以主机根为基准，而不是拼在目录后面
        self.assertEqual(
            pc._abs_url("https://vod.example.com/20240516/abc/index.m3u8",
                        "/20240516/abc/955kb/hls/index.m3u8"),
            "https://vod.example.com/20240516/abc/955kb/hls/index.m3u8")

    def test_sibling_relative(self):
        self.assertEqual(
            pc._abs_url("https://vod.example.com/20240516/abc/955kb/hls/index.m3u8",
                        "seg001.ts"),
            "https://vod.example.com/20240516/abc/955kb/hls/seg001.ts")


class TestEncryption(unittest.TestCase):
    def test_plain(self):
        txt = "#EXTM3U\n#EXTINF:10,\nseg1.ts"
        self.assertFalse(pc._first_segment_encrypted(txt))

    def test_aes(self):
        txt = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n#EXTINF:10,\nseg1.ts'
        self.assertTrue(pc._first_segment_encrypted(txt))

    def test_key_after_first_segment_does_not_matter(self):
        # 关键回归点：METHOD=NONE 出现在后面时，不能把第一个分片判成明文
        txt = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:10,\nseg1.ts\n'
               '#EXT-X-KEY:METHOD=NONE\n#EXTINF:10,\nseg2.ts')
        self.assertTrue(pc._first_segment_encrypted(txt))

    def test_method_none_first(self):
        txt = "#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:10,\nseg1.ts"
        self.assertFalse(pc._first_segment_encrypted(txt))


class TestVariants(unittest.TestCase):
    def test_pick_by_bandwidth_desc(self):
        txt = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\na.m3u8\n"
               "#EXT-X-STREAM-INF:BANDWIDTH=3000000\nb.m3u8\n"
               "#EXT-X-STREAM-INF:BANDWIDTH=1500000\nc.m3u8")
        self.assertEqual(pc._pick_variants(txt, 2), ["b.m3u8", "c.m3u8"])

    def test_empty(self):
        self.assertEqual(pc._pick_variants("#EXTM3U"), [])


class TestPlayUrls(unittest.TestCase):
    def test_hls_first_and_query_kept(self):
        raw = "第1集$https://a.com/x.mp4#第2集$https://b.com/y.m3u8?sign=1"
        self.assertEqual(pc.extract_play_urls(raw),
                         ["https://b.com/y.m3u8?sign=1", "https://a.com/x.mp4"])

    def test_empty(self):
        self.assertEqual(pc.extract_play_urls(""), [])


class TestKeyFromApi(unittest.TestCase):
    def test_domain_second_level(self):
        self.assertEqual(pc.key_from_api("https://cj.ffzyapi.com/api.php/provide/vod/"), "ffzyapi")

    def test_short_host(self):
        self.assertTrue(pc.key_from_api("http://localhost/api.php/provide/vod/"))


if __name__ == "__main__":
    unittest.main()
