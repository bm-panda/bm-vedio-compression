"""
视频压缩 - 一键压缩视频减小体积
统一输出: MP4 (H.264)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List

# ── 路径与模板 ──
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_TEMPLATE = BASE_DIR / "config.html"
APP_DATA_MARKER = "/*__APP_DATA__*/"

# ==================== 压缩参数定义 ====================
# 压缩等级 → CRF(libx264)，数值越大体积越小、画质越低
COMPRESSION_LEVELS = {
    "light": 20,  # 轻度压缩: 画质优先
    "standard": 23,  # 标准压缩
    "compress": 28,  # 高效压缩(默认)
    "heavy": 33,  # 强力压缩
    "extreme": 38,  # 极致压缩
}

# 分辨率选项（"original" = 跟随输入）
RESOLUTIONS = {"1080p": 1080, "720p": 720, "480p": 480}

# 音频码率选项
ALLOWED_AUDIO_BITRATES = {"192k", "128k", "96k", "64k"}

# 统一输出格式与编码器
OUTPUT_FORMAT = "mp4"
VIDEO_CODEC = "libx264"
PRESET = "medium"

# 下拉选项的显示文案（dict 顺序即下拉顺序）
COMPRESSION_LEVEL_OPTIONS = {
    "light": "轻度压缩 (CRF 20 · 画质优先)",
    "standard": "标准压缩 (CRF 23)",
    "compress": "高效压缩 (CRF 28 · 推荐)",
    "heavy": "强力压缩 (CRF 33)",
    "extreme": "极致压缩 (CRF 38 · 体积最小)",
}

RESOLUTION_OPTIONS = {
    "original": "原始 (跟随输入)",
    "1080p": "1080p (全高清)",
    "720p": "720p (高清)",
    "480p": "480p (标清)",
}

AUDIO_BITRATE_OPTIONS = {
    "192k": "192kbps (较高)",
    "128k": "128kbps (推荐)",
    "96k": "96kbps (较低)",
    "64k": "64kbps (最小)",
}

DEFAULT_CONFIG = {
    "compression_level": "compress",
    "output_dir": "",
    "resolution": "original",
    "audio_bitrate": "128k",
    "overwrite": False,
}

# 右键可选的视频扩展名（与 bm-scripts-box-rc.toml 的 filters 一致）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rmvb", ".rm", ".asf",
}


class VideoCompressor:
    """视频压缩器（基于 FFmpeg）：统一输出 MP4(H.264)，承载通用 subprocess 执行，只产数据。"""

    @staticmethod
    def _run(cmd, **kw):
        """执行命令，默认隐藏控制台窗口、按 UTF-8 容错解码。"""
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kw.setdefault("encoding", "utf-8")
        return subprocess.run(cmd, text=True, errors="replace", **kw)

    @staticmethod
    def _require_binaries():
        """同时校验 ffmpeg 与 ffprobe，缺则直接报错（供 Cli 开局预检）。"""
        if not shutil.which("ffmpeg"):
            raise FileNotFoundError("未找到 FFmpeg，请安装并加入环境变量 PATH（https://ffmpeg.org/download.html）")
        if not shutil.which("ffprobe"):
            raise FileNotFoundError("未找到 ffprobe，请确认已完整安装 FFmpeg（含 ffprobe）并加入 PATH")

    def __init__(self, videos, config):
        """videos: 视频文件路径列表；config: 配置 dict（见 DEFAULT_CONFIG）。"""
        self.videos = [v for v in videos if Path(v).exists()]
        c = config

        self.output_dir = str(c.get("output_dir") or "").strip()
        # 非法参数值回退默认
        self.compression_level = c.get("compression_level") if c.get("compression_level") in COMPRESSION_LEVELS else "compress"
        self.resolution = c.get("resolution") if c.get("resolution") in {"original", *RESOLUTIONS} else "original"
        self.audio_bitrate = c.get("audio_bitrate") if c.get("audio_bitrate") in ALLOWED_AUDIO_BITRATES else "128k"
        self.overwrite = bool(c.get("overwrite"))

        self._require_binaries()
        self._ffmpeg = shutil.which("ffmpeg")
        self._ffprobe = shutil.which("ffprobe")

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

    def _get_duration(self, input_path: str):
        """探测输入视频总时长（秒），失败返回 None。"""
        if not self._ffprobe:
            return None
        try:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    return float(proc.stdout.strip())
                except ValueError:
                    return None
        except Exception:
            pass
        return None

    def _get_video_height(self, input_path: str):
        """探测输入视频第一路视频流的高度（像素），失败返回 None。"""
        if not self._ffprobe:
            return None
        try:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", input_path],
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    return int(proc.stdout.strip())
                except ValueError:
                    return None
        except Exception:
            pass
        return None

    def _get_output_path(self, input_path: str) -> str:
        """生成输出文件路径（统一 .mp4；mp4 源在源目录时加 _compressed 后缀避免覆盖原文件）。"""
        stem = Path(input_path).stem
        name = f"{stem}.{OUTPUT_FORMAT}"

        if self.output_dir:
            return os.path.join(self.output_dir, name)

        parent = Path(input_path).parent
        out = os.path.join(parent, name)
        if os.path.abspath(out) == os.path.abspath(input_path):
            out = os.path.join(parent, f"{stem}_compressed.{OUTPUT_FORMAT}")
        return out

    def _build_command(self, input_path: str, output_path: str) -> List[str]:
        """构建 ffmpeg 命令"""
        cmd = [self._ffmpeg, "-y", "-i", input_path]

        # 显式只取第一条视频流 + 可选第一条音轨
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

        # 视频：H.264 + CRF + 兼容像素格式
        cmd += ["-c:v", VIDEO_CODEC, "-crf", str(COMPRESSION_LEVELS[self.compression_level])]
        cmd += ["-preset", PRESET, "-pix_fmt", "yuv420p"]

        # 分辨率（滤镜缩放，偶数宽度；仅当源高于目标时才降档，绝不放大）
        if self.resolution != "original":
            target_h = RESOLUTIONS[self.resolution]
            src_h = self._get_video_height(input_path)
            if src_h is None or src_h > target_h:
                cmd += ["-vf", f"scale=-2:{target_h}"]

        # 音频：重新编码为 AAC 降码率
        cmd += ["-c:a", "aac", "-b:a", self.audio_bitrate]

        # 保留元数据，失败时输出错误信息
        cmd += ["-map_metadata", "0", "-loglevel", "error"]
        cmd.append(output_path)

        return cmd

    def _convert_single(self, video_path: str, on_start=None, on_progress=None) -> tuple:
        """压缩单个视频，返回 (路径, 状态, 信息)，状态: success/skipped/failed。"""
        try:
            output_path = self._get_output_path(video_path)

            # 压缩始终重编码，无"同格式跳过"；仅按覆盖模式处理已存在输出
            if os.path.exists(output_path) and not self.overwrite:
                return video_path, "skipped", "文件已存在"

            cmd = self._build_command(video_path, output_path)

            if on_start:
                on_start(video_path)

            # 探测总时长，用于实时百分比
            total_us = None
            if on_progress:
                duration = self._get_duration(video_path)
                if duration:
                    total_us = duration * 1_000_000

            # -progress 将编码进度输出到 stdout，错误信息仍走 stderr
            cmd += ["-nostats", "-progress", "pipe:1"]

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            # 后台线程排空 stderr，避免管道满阻塞
            stderr_lines = []

            def _read_stderr():
                try:
                    for line in proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # 实时解析 stdout 进度（out_time_us 为微秒）
            last_pct = -1.0
            for line in proc.stdout:
                if not line.startswith("out_time_us="):
                    continue
                try:
                    cur_us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                pct = min(100.0, cur_us / total_us * 100.0) if total_us else None
                # 按 0.5% 粒度回调，避免刷屏
                if pct is None or pct - last_pct >= 0.5 or pct >= 100.0:
                    last_pct = pct
                    secs = cur_us / 1_000_000
                    time_str = f"{int(secs // 3600):02d}:{int(secs % 3600 // 60):02d}:{secs % 60:04.1f}"
                    if on_progress:
                        on_progress(pct, time_str)

            proc.wait()
            stderr_thread.join()

            if proc.returncode != 0:
                return video_path, "failed", ("".join(stderr_lines).strip() or "压缩失败")
            return video_path, "success", output_path

        except Exception as e:
            return video_path, "failed", str(e)

    def convert(self, on_start=None, on_progress=None, on_done=None) -> dict:
        """顺序压缩全部视频，回调供 Cli 展示；返回分组结果 dict。"""
        results = {"success": [], "skipped": [], "failed": [], "total": len(self.videos)}

        for path in self.videos:
            path, status, info = self._convert_single(path, on_start=on_start, on_progress=on_progress)
            if status == "success":
                results["success"].append((path, info))
            elif status == "skipped":
                results["skipped"].append((path, info))
            else:
                results["failed"].append((path, info))
            if on_done:
                on_done(path, status, info)

        return results

    @staticmethod
    def get_file_size(path: str) -> str:
        """获取文件大小（人性化显示）"""
        size = os.path.getsize(path)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


class Gui:
    """webview-cli 配置窗口（含配置的读写与校验）。"""

    @staticmethod
    def _render(data):
        """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")

    @staticmethod
    def _webview_bin():
        webview = shutil.which("webview-cli") or shutil.which("webview")
        if not webview:
            raise FileNotFoundError(
                "未找到 webview-cli，请确认已安装并加入 PATH\n"
                "https://github.com/just-be-dev/webview-cli"
            )
        return webview

    @staticmethod
    def _validate(data):
        """校验配置窗口返回的数据（镜像 HTML 里的 JS 规则），返回规范化后的 dict。"""
        if not isinstance(data, dict):
            raise ValueError("返回的数据格式无效")

        if data.get("compression_level") not in COMPRESSION_LEVELS:
            raise ValueError(f"无效的压缩强度：{data.get('compression_level')}")
        if data.get("resolution") not in {"original", *RESOLUTIONS}:
            raise ValueError(f"无效的分辨率：{data.get('resolution')}")
        if data.get("audio_bitrate") not in ALLOWED_AUDIO_BITRATES:
            raise ValueError(f"无效的音频码率：{data.get('audio_bitrate')}")

        data.update(
            output_dir=str(data.get("output_dir") or "").strip(),
            overwrite=bool(data.get("overwrite")),
        )
        # 补齐默认字段，保证 config.json 全字段、下游解析安全
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
        return data

    @staticmethod
    def load_config():
        """读取配置；缺失/损坏/非法返回 None（触发首次引导）。"""
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data if data.get("compression_level") in COMPRESSION_LEVELS else None

    @staticmethod
    def save_config(data):
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def ask(self):
        """打开配置窗口，返回校验后的配置 dict；取消/出错返回 None。"""
        webview = self._webview_bin()
        data = {"saved": self.load_config() or {},
                "DEFAULTS": DEFAULT_CONFIG,
                "COMPRESSION_LEVEL_OPTIONS": COMPRESSION_LEVEL_OPTIONS,
                "RESOLUTION_OPTIONS": RESOLUTION_OPTIONS,
                "AUDIO_BITRATE_OPTIONS": AUDIO_BITRATE_OPTIONS}
        html = self._render(data)
        cmd = [webview, "--title", "视频压缩 - 配置窗口", "--width", "500", "--height", "720"]
        try:
            proc = VideoCompressor._run(cmd, input=html, capture_output=True)
        except (OSError, ValueError):
            # stdin 管道不可用时回退到临时 HTML 文件
            fd, path = tempfile.mkstemp(suffix=".html", prefix="video-zip-webview-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(html)
                proc = VideoCompressor._run(cmd + [path], input="", capture_output=True)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if proc.returncode:
            if proc.returncode != 2 and (proc.stderr or "").strip():
                print((proc.stderr or "").strip())
            return None  # 取消(2) / 出错
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            print("配置窗口返回的数据无法解析")
            return None
        try:
            return self._validate(payload)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return None


class Cli:
    """批处理命令行流程（含盒子参数解析与输出编码修复）。"""

    @staticmethod
    def _fix_encoding():
        # 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    @staticmethod
    def _dw(text):
        """近似显示宽度：CJK/全角/emoji 计 2，其余计 1（横幅自适应宽度用）。"""
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)

    @staticmethod
    def _version():
        try:
            for line in (BASE_DIR / "bm-scripts-box-rc.toml").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    @staticmethod
    def _title():
        v = Cli._version()
        return f"🗜️ 视频压缩{(' v' + v) if v else ''} · 一键减小视频体积"

    @staticmethod
    def _banner(text):
        w = Cli._dw(text) + 4
        bar = "─" * w
        print("┌" + bar + "┐")
        print("│  " + text + "  │")
        print("└" + bar + "┘")

    @staticmethod
    def _section(title):
        print(f"── {title} " + "─" * 22)

    @staticmethod
    def get_path(param_path):
        """解析盒子传入的 JSON 参数文件，返回存在的视频路径列表。"""
        if not (param_path and Path(param_path).exists()):
            return []
        try:
            with open(param_path, "r", encoding="utf-8") as f:
                params = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        raw = params.get("data", {}).get("target_paths", [])
        return [p for p in raw if Path(p).exists()]

    def _config_summary(self, config):
        """压缩参数摘要（配置分节说明用，两行）。"""
        level = {"light": "轻度(CRF 20)", "standard": "标准(CRF 23)", "compress": "高效(CRF 28)",
                 "heavy": "强力(CRF 33)", "extreme": "极致(CRF 38)"}
        res = {"original": "原始", "1080p": "1080p", "720p": "720p", "480p": "480p"}
        line1 = (f"🗜️ 压缩强度 {level.get(config.get('compression_level'), config.get('compression_level'))}"
                 f" · 分辨率 {res.get(config.get('resolution'), config.get('resolution'))}"
                 f" · 音频 {config.get('audio_bitrate')}")
        out = config.get("output_dir") or "源文件所在目录"
        mode = "允许覆盖" if config.get("overwrite") else "跳过已存在文件"
        return f"{line1}\n  📁 输出 {OUTPUT_FORMAT.upper()}({VIDEO_CODEC}) · 输出目录 {out} · {mode}"

    def run(self, paths):
        """批处理主流程：扫描 → 配置 → 处理 → 结果 → 倒计时退出。"""
        Cli._banner(Cli._title())

        videos, skipped = [], []
        for p in paths:
            if Path(p).suffix.lower() in VIDEO_EXTS:
                videos.append(p)
            else:
                skipped.append(p)
        if skipped:
            self._section("扫描")
            for p in skipped:
                print(f"  ⏭️ 忽略非视频: {Path(p).name}")

        if not videos:
            print("  ❌ 未选择有效的视频文件")
            self._exit()
            return

        self._section("配置")
        config = Gui.load_config()
        if config is None:
            print("  📋 首次使用，请配置压缩参数...")
            config = Gui().ask()
            if config is None:
                print("  ❌ 未获取到配置，已取消压缩")
                self._exit()
                return
            Gui.save_config(config)
            print("  ✅ 配置已保存")
        else:
            print("  💾 使用已保存的配置")
        print(f"  {self._config_summary(config)}")

        self._section("处理")
        total = len(videos)
        started = [0]

        def on_start(path):
            started[0] += 1
            print(f"  ▶ ({started[0]}/{total}) 正在压缩: {Path(path).name}")

        def on_progress(pct, time_str):
            if pct is not None:
                print(f"\r    进度: {pct:5.1f}%  已编码 {time_str}", end="", flush=True)
            else:
                print(f"\r    进度: ...  已编码 {time_str}", end="", flush=True)

        def on_done(path, status, info):
            print("\r" + " " * 60, end="\r")
            name = Path(path).name
            if status == "success":
                size = VideoCompressor.get_file_size(info)
                print(f"  ✅ {name} → {Path(info).name}（{size}）")
            elif status == "skipped":
                print(f"  ⏭️ {name}  {info}")
            else:
                print(f"  ❌ {name}  {(info or '未知错误').strip().splitlines()[0]}")

        compressor = VideoCompressor(videos, config)
        result = compressor.convert(on_start=on_start, on_progress=on_progress, on_done=on_done)

        self._section("结果")
        parts = [f"✅ 成功 {len(result['success'])} 个"]
        if result["skipped"]:
            parts.append(f"⏭️ 跳过 {len(result['skipped'])} 个")
        if result["failed"]:
            parts.append(f"❌ 失败 {len(result['failed'])} 个")
        print("  " + " · ".join(parts))
        self._exit()

    @staticmethod
    def _exit():
        width, total = 10, 5
        for i in range(total, 0, -1):
            filled = round(width * (total - i + 1) / total)
            bar = "█" * filled + "░" * (width - filled)
            print(f"\r  ⏳ {i}s {bar}  按任意键立即退出", end="")
            time.sleep(1)
        print("\r" + " " * 60, end="\r")
        print("  👋 已退出")
        sys.exit(0)


def main():
    Cli._fix_encoding()                      # 先修编码，再打印任何东西
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        if param_path:                        # 盒子传入 JSON 参数 → 批处理
            paths = Cli.get_path(param_path)
            if not paths:
                print("未获取到有效的文件路径")
                time.sleep(2)
            else:
                Cli().run(paths)
        else:                                 # 无参 → 打开配置窗口
            Cli._banner(Cli._title())
            config = Gui().ask()
            if config is not None:
                Gui.save_config(config)
            print(("  ✅ 配置已保存" if config else "  未保存配置") + "\n")
            time.sleep(2)
    except FileNotFoundError as e:            # 缺二进制/webview → 中文报错，停留 3 秒
        print(f"❌ {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
