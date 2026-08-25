#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV M3U 播放列表检测工具 (M3U Playlist Checker)
功能：
1. 支持从网络 URL 或本地文件读取 M3U / M3U8 播放列表。
2. 多线程高并发检测每个频道的有效性、HTTP 状态及响应延迟。
3. 校验 HLS 头部或分片，有效识别死链及运营商拦截页。
4. 自动保留原始频道属性（分组、Logo、EPG等），生成纯净的可用 M3U 文件及 Markdown 检测报告。
5. 包含安全阈值保护 (--min-valid)，防止因网络波动导致播放列表被意外覆盖清空。
6. 纯 Python 标准库实现，零第三方依赖。
"""

import os
import sys
import re
import time
import argparse
import urllib.request
import urllib.error
import concurrent.futures
from typing import List, Dict, Any

# 确保在 Windows 控制台下 UTF-8 正常输出
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass


class M3UChannel:
    def __init__(self, raw_extinf: str, name: str, url: str, group: str = ""):
        self.raw_extinf = raw_extinf
        self.name = name
        self.url = url
        self.group = group
        self.status = "PENDING"
        self.latency_ms = -1
        self.error_msg = ""

    def __repr__(self):
        return f"<Channel: {self.name} [{self.status}] {self.latency_ms}ms>"


class M3UChecker:
    def __init__(
        self,
        source: str,
        timeout: float = 4.0,
        threads: int = 25,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        sort_by_latency: bool = False,
        min_valid: int = 0
    ):
        self.source = source
        self.timeout = timeout
        self.threads = threads
        self.user_agent = user_agent
        self.sort_by_latency = sort_by_latency
        self.min_valid = min_valid
        self.header_lines: List[str] = []
        self.channels: List[M3UChannel] = []

    def fetch_content(self) -> str:
        """获取并解析 M3U 内容（支持网络 URL 或本地路径）"""
        if self.source.startswith("http://") or self.source.startswith("https://"):
            print(f"[*] 正在从网络下载播放列表: {self.source}")
            req = urllib.request.Request(
                self.source,
                headers={"User-Agent": self.user_agent}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw_data = resp.read()
                try:
                    return raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    return raw_data.decode("gbk", errors="ignore")
        else:
            if not os.path.exists(self.source):
                raise FileNotFoundError(f"未找到本地文件: {self.source}")
            print(f"[*] 正在读取本地播放列表: {self.source}")
            with open(self.source, "rb") as f:
                raw_data = f.read()
                try:
                    return raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    return raw_data.decode("gbk", errors="ignore")

    def parse_m3u(self, content: str):
        """解析 M3U 内容中的频道与元数据"""
        lines = content.splitlines()
        current_extinf = ""
        current_name = "未知频道"
        current_group = ""
        header_collected = False

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            if line_str.startswith("#EXTM3U"):
                self.header_lines.append(line_str)
                header_collected = True
                continue

            if not header_collected and line_str.startswith("#"):
                self.header_lines.append(line_str)
                continue

            if line_str.startswith("#EXTINF:"):
                current_extinf = line_str
                # 提取频道名称
                name_match = re.search(r',([^,]+)$', line_str)
                if name_match:
                    current_name = name_match.group(1).strip()
                else:
                    current_name = "未知频道"

                # 提取分组信息 group-title="..."
                group_match = re.search(r'group-title="([^"]+)"', line_str)
                if group_match:
                    current_group = group_match.group(1).strip()
                else:
                    current_group = "未分组"

            elif line_str.startswith("http://") or line_str.startswith("https://") or line_str.startswith("rtmp://") or line_str.startswith("rtp://"):
                ch = M3UChannel(
                    raw_extinf=current_extinf if current_extinf else f'#EXTINF:-1 group-title="{current_group}",{current_name}',
                    name=current_name,
                    url=line_str,
                    group=current_group
                )
                self.channels.append(ch)
                current_extinf = ""

        if not self.header_lines:
            self.header_lines = ['#EXTM3U']

    def test_single_channel(self, channel: M3UChannel) -> M3UChannel:
        """测试单个频道流的连通性与内容有效性"""
        start_time = time.time()
        try:
            req = urllib.request.Request(
                channel.url,
                headers={"User-Agent": self.user_agent}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                elapsed_ms = int((time.time() - start_time) * 1000)
                channel.latency_ms = elapsed_ms
                
                if resp.status == 200:
                    content_head = resp.read(256).decode("utf-8", errors="ignore")
                    is_hls = (
                        "#EXTM3U" in content_head or 
                        "#EXT-X-" in content_head or 
                        ".ts" in content_head or 
                        ".m3u8" in content_head or
                        ".m4s" in content_head
                    )
                    if is_hls or resp.headers.get("Content-Type", "").startswith("video/"):
                        channel.status = "OK"
                    else:
                        channel.status = "INVALID_CONTENT"
                        channel.error_msg = f"非有效流媒体 (返回: {content_head[:20].strip()})"
                else:
                    channel.status = "FAILED"
                    channel.error_msg = f"HTTP {resp.status}"

        except urllib.error.HTTPError as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            channel.latency_ms = elapsed_ms
            channel.status = "FAILED"
            channel.error_msg = f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            channel.latency_ms = elapsed_ms
            channel.status = "FAILED"
            channel.error_msg = f"连接失败: {e.reason}"
        except TimeoutError:
            elapsed_ms = int((time.time() - start_time) * 1000)
            channel.latency_ms = elapsed_ms
            channel.status = "TIMEOUT"
            channel.error_msg = f"连接超时 (>{int(self.timeout*1000)}ms)"
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            channel.latency_ms = elapsed_ms
            channel.status = "FAILED"
            channel.error_msg = str(e)

        return channel

    def run(self):
        """执行检测"""
        raw_content = self.fetch_content()
        self.parse_m3u(raw_content)
        total = len(self.channels)
        print(f"[*] 成功解析播放列表，共发现 {total} 个频道")
        print(f"[*] 启动并发检测 (线程数: {self.threads}, 超时限制: {self.timeout}s)...")
        print("=" * 65)

        completed = 0
        valid_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_to_channel = {executor.submit(self.test_single_channel, ch): ch for ch in self.channels}
            for future in concurrent.futures.as_completed(future_to_channel):
                ch = future.result()
                completed += 1
                if ch.status == "OK":
                    valid_count += 1
                    status_tag = "[可用]"
                    detail = f"延迟: {ch.latency_ms}ms"
                else:
                    status_tag = "[失效]"
                    detail = ch.error_msg or ch.status

                progress = f"[{completed}/{total}]"
                print(f"{progress:<10} {status_tag} {ch.name:<18} {detail}")

        print("=" * 65)
        print(f"[*] 检测完成！有效: {valid_count} / {total} (可用率: {valid_count/total*100:.1f}%)")

        if self.min_valid > 0 and valid_count < self.min_valid:
            print(f"[!] 警告: 有效频道数 ({valid_count}) 低于设定的安全下限 ({self.min_valid})，判定为网络异常，放弃保存以保护旧文件！")
            return False
        return True

    def export_valid_m3u(self, output_path: str):
        """导出过滤后的有效 M3U 播放列表"""
        valid_channels = [ch for ch in self.channels if ch.status == "OK"]
        if self.sort_by_latency:
            valid_channels.sort(key=lambda x: x.latency_ms)

        with open(output_path, "w", encoding="utf-8") as f:
            for h in self.header_lines:
                f.write(h + "\n")
            f.write(f"# Generated by M3U Checker at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Total valid channels: {len(valid_channels)}\n\n")

            for ch in valid_channels:
                f.write(ch.raw_extinf + "\n")
                f.write(ch.url + "\n\n")

        print(f"[+] 已保存可用播放列表至: {os.path.abspath(output_path)}")

    def export_report_markdown(self, output_path: str):
        """导出 Markdown 格式的详细检测报告"""
        valid_channels = [ch for ch in self.channels if ch.status == "OK"]
        failed_channels = [ch for ch in self.channels if ch.status != "OK"]
        total = len(self.channels)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("# IPTV 播放列表可用性检测报告\n\n")
            f.write(f"- **检测来源**: `{self.source}`\n")
            f.write(f"- **检测时间**: `{time.strftime('%Y-%m-%d %H:%M:%S')}`\n")
            f.write(f"- **总频道数**: {total} 个\n")
            f.write(f"- **有效频道 (OK)**: **{len(valid_channels)}** 个 ({len(valid_channels)/total*100:.1f}%)\n")
            f.write(f"- **失效频道 (FAIL)**: **{len(failed_channels)}** 个 ({len(failed_channels)/total*100:.1f}%)\n\n")

            f.write("## 1. 可用频道列表 (Top 30 按延迟排序)\n\n")
            f.write("| 序号 | 频道名称 | 分组 | 延迟 (ms) | 播放链接 |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- |\n")
            sorted_valid = sorted(valid_channels, key=lambda x: x.latency_ms)
            for i, ch in enumerate(sorted_valid[:30], 1):
                f.write(f"| {i} | `{ch.name}` | {ch.group} | {ch.latency_ms}ms | `{ch.url}` |\n")

            if len(sorted_valid) > 30:
                f.write(f"\n*(仅展示前 30 个，完整有效列表共 {len(sorted_valid)} 个，请直接使用生成的 m3u 文件)*\n")

            f.write(f"\n## 2. 失效频道列表 (共 {len(failed_channels)} 个)\n\n")
            f.write("| 序号 | 频道名称 | 分组 | 错误原因 | 原始链接 |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- |\n")
            for i, ch in enumerate(failed_channels, 1):
                err = ch.error_msg if ch.error_msg else ch.status
                f.write(f"| {i} | `{ch.name}` | {ch.group} | {err} | `{ch.url}` |\n")

        print(f"[+] 已生成 Markdown 检测报告至: {os.path.abspath(output_path)}")


def main():
    parser = argparse.ArgumentParser(description="IPTV M3U/M3U8 播放列表可用性检测工具")
    parser.add_argument("source", nargs="?", default="https://raw.githubusercontent.com/imDazui/Tvlist-awesome-m3u-m3u8/master/m3u/%E5%B9%BF%E8%A5%BF%E7%A7%BB%E5%8A%A8.m3u", help="M3U 文件的网络 URL 或本地文件路径")
    parser.add_argument("-o", "--output", default="广西移动_valid.m3u", help="导出的有效 M3U 播放列表保存路径 (默认: 广西移动_valid.m3u)")
    parser.add_argument("-r", "--report", default="广西移动_report.md", help="导出的 Markdown 检测报告保存路径 (默认: 广西移动_report.md)")
    parser.add_argument("-t", "--timeout", type=float, default=4.0, help="单频道请求超时时间(秒) (默认: 4.0)")
    parser.add_argument("-n", "--threads", type=int, default=25, help="并发检测线程数 (默认: 25)")
    parser.add_argument("-s", "--sort", action="store_true", help="按网络延迟从低到高排序输出")
    parser.add_argument("--min-valid", type=int, default=0, help="最小有效频道数安全阈值，低于该数量则判定异常且不覆盖 (默认: 0)")

    args = parser.parse_args()

    checker = M3UChecker(
        source=args.source,
        timeout=args.timeout,
        threads=args.threads,
        sort_by_latency=args.sort,
        min_valid=args.min_valid
    )
    is_success = checker.run()
    if is_success:
        checker.export_valid_m3u(args.output)
        checker.export_report_markdown(args.report)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
