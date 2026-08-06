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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

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

# 下拉选项的显示文案（key 与 converter.py 的合法值一致；dict 顺序即下拉顺序）
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

TEMPLATE_PATH = Path(__file__).parent / "config.html"
CONFIG_PATH = Path(__file__).parent / "config.json"

# 模板里这个占位符会被替换为 `const APP_DATA = {...};`
APP_DATA_MARKER = "/*__APP_DATA__*/"

# 统一输出编码，避免 GBK 控制台下 emoji/中文报错（盒子环境已设 PYTHONUTF8=1）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class VideoCompressor:
    """视频压缩器(基于 FFmpeg)：统一输出 MP4(H.264)"""

    def __init__(self, videos: List[str], output_dir: str = "",
                 compression_level: str = "compress", resolution: str = "original",
                 audio_bitrate: str = "128k", overwrite: bool = False):
        """
        初始化压缩器

        Args:
            videos: 视频文件路径列表
            output_dir: 输出目录（为空则保存到源文件目录）
            compression_level: 压缩强度 (light/standard/compress/heavy/extreme)
            resolution: 分辨率 (original/1080p/720p/480p)
            audio_bitrate: 音频码率 (192k/128k/96k/64k)
            overwrite: 是否覆盖已存在的文件
        """
        self.videos = videos
        self.output_dir = output_dir or ""

        # 非法参数值回退默认
        self.compression_level = compression_level if compression_level in COMPRESSION_LEVELS else "compress"
        self.resolution = resolution if resolution in {"original", *RESOLUTIONS} else "original"
        self.audio_bitrate = audio_bitrate if audio_bitrate in ALLOWED_AUDIO_BITRATES else "128k"
        self.overwrite = overwrite

        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise FileNotFoundError("未找到 FFmpeg（ffmpeg 命令），请确认已安装并在环境变量中")
        self._ffprobe = shutil.which("ffprobe")

        # 创建输出目录
        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

    def _get_duration(self, input_path: str):
        """探测输入视频总时长（秒），失败返回 None"""
        if not self._ffprobe:
            return None
        try:
            proc = subprocess.run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        """探测输入视频第一路视频流的高度（像素），失败返回 None"""
        if not self._ffprobe:
            return None
        try:
            proc = subprocess.run(
                [self._ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", input_path],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        """生成输出文件路径（统一 .mp4；mp4 源在源目录时加 _compressed 后缀避免覆盖原文件）"""
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
        """压缩单个视频文件，返回 (路径, 状态, 信息)，状态: success/skipped/failed

        Args:
            on_start: 开始编码前的回调，接收 (文件路径)
            on_progress: 编码中的实时进度回调，接收 (百分比, 已编码时间字符串)；百分比未知时为 None
        """
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

    def convert(self, max_workers: int = 1, progress_callback=None, on_start=None, on_progress=None) -> dict:
        """
        批量压缩视频

        Args:
            max_workers: 并发线程数（视频编码为 CPU 密集型，建议 1 保证进度清晰）
            progress_callback: 每个文件完成后的回调，接收 (已完成数, 总数)
            on_start: 每个文件开始编码前的回调，接收 (文件路径)
            on_progress: 每个文件编码中的实时进度回调，接收 (百分比, 已编码时间字符串)

        Returns:
            dict: 压缩结果统计
        """
        total = len(self.videos)
        results = {"success": [], "skipped": [], "failed": [], "total": total}

        if total == 0:
            return results

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._convert_single, path, on_start, on_progress): path
                       for path in self.videos}

            for idx, future in enumerate(as_completed(futures), 1):
                path, status, info = future.result()
                if status == "success":
                    results["success"].append((path, info))
                elif status == "skipped":
                    results["skipped"].append((path, info))
                else:
                    results["failed"].append((path, info))

                if progress_callback:
                    progress_callback(idx, total)

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


def _render_html(config) -> str:
    """读取 HTML 模板并注入 APP_DATA（常量单一来源在 Python）。"""
    data = {
        "saved": config,
        "DEFAULTS": DEFAULT_CONFIG,
        "COMPRESSION_LEVEL_OPTIONS": COMPRESSION_LEVEL_OPTIONS,
        "RESOLUTION_OPTIONS": RESOLUTION_OPTIONS,
        "AUDIO_BITRATE_OPTIONS": AUDIO_BITRATE_OPTIONS,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace(APP_DATA_MARKER, f"const APP_DATA = {payload};")


def _validate_saved(data):
    """对页面返回的配置做二次校验（镜像 HTML 里的 JS 规则）。"""
    if not isinstance(data, dict):
        raise ValueError("返回的数据格式无效")
    if data.get("compression_level") not in COMPRESSION_LEVELS:
        raise ValueError(f"无效的压缩强度：{data.get('compression_level')}")
    if data.get("resolution") not in {"original", *RESOLUTIONS}:
        raise ValueError(f"无效的分辨率：{data.get('resolution')}")
    if data.get("audio_bitrate") not in ALLOWED_AUDIO_BITRATES:
        raise ValueError(f"无效的音频码率：{data.get('audio_bitrate')}")


def _write_config(data) -> Path:
    data["overwrite"] = bool(data.get("overwrite"))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def _spawn_webview(base_cmd, html):
    """先尝试 stdin 管道传入 HTML；失败则回退到临时 HTML 文件。

    注意 webview 的输入优先级：非空 stdin 优先于位置参数。
    """
    common = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return subprocess.run([*base_cmd], input=html, **common)
    except (OSError, ValueError):
        fd, path = tempfile.mkstemp(suffix=".html", prefix="video-zip-config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html)
            return subprocess.run([*base_cmd, path], input="", **common)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def run_config_window(config) -> bool:
    """打开 HTML 配置窗口。返回 True 表示已保存配置，False 表示取消/出错。"""
    webview = shutil.which("webview-cli") or shutil.which("webview")
    if not webview:
        raise FileNotFoundError(
            "未找到 webview-cli，请确认已安装并加入 PATH\n"
            "https://github.com/just-be-dev/webview-cli"
        )

    html = _render_html(config)
    base_cmd = [webview, "--title", "视频压缩 - 配置窗口", "--width", "500", "--height", "720"]
    proc = _spawn_webview(base_cmd, html)

    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout)
        except ValueError as e:
            print(f"配置窗口返回的数据无法解析：{e}")
            return False
        try:
            _validate_saved(data)
        except (ValueError, TypeError) as e:
            print(f"配置校验失败：{e}")
            return False
        _write_config(data)
        return True
    elif proc.returncode == 2:
        return False  # 用户直接关窗 = 取消
    else:
        # reject(1) / 超时(3) / 用法错误(64)
        msg = (proc.stderr or "").strip()
        if msg:
            print(msg)
        return False


def load_config():
    """加载配置文件，不存在则返回默认配置。"""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception:
            pass
    return config


def get_path(param_path):
    initial_files = []

    if param_path and Path(param_path).exists():
        with open(param_path, "r", encoding="utf-8") as f:
            params = json.load(f)
        raw = params.get("data", {}).get("target_paths", [])
        initial_files = [p for p in raw if Path(p).exists()]
    return initial_files


def get_config():
    """读取配置文件，不存在则返回默认配置"""
    config_path = Path(__file__).parent / "config.json"

    # 默认配置（单一来源在 scr/gui.py）
    default_config = dict(DEFAULT_CONFIG)

    if not os.path.exists(config_path):
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return {**default_config, **config}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"配置文件格式错误：{e}，使用默认配置")
        return default_config


def _format_param(value, mapping=None):
    """格式化参数显示值"""
    if mapping and value in mapping:
        return mapping[value]
    return value


def cli(video_path: list, config: dict):
    level_map = {"light": "轻度(CRF 20)", "standard": "标准(CRF 23)",
                 "compress": "高效(CRF 28)", "heavy": "强力(CRF 33)",
                 "extreme": "极致(CRF 38)"}
    res_map = {"original": "跟随输入", "1080p": "1080p", "720p": "720p", "480p": "480p"}

    print("-" * 50)
    print('视频压缩')
    print("-" * 50)
    print(f"压缩强度: {_format_param(config['compression_level'], level_map)}"
          f"   分辨率: {_format_param(config['resolution'], res_map)}"
          f"   音频码率: {config['audio_bitrate']}")
    print(f"输出: MP4(H.264)   输出目录: {config['output_dir'] if config['output_dir'] else '源文件所在目录'}"
          f"   覆盖模式: {'允许覆盖' if config['overwrite'] else '跳过已存在文件'}")
    print("-" * 50)

    compressor = VideoCompressor(
        videos=video_path,
        output_dir=config["output_dir"],
        compression_level=config["compression_level"],
        resolution=config["resolution"],
        audio_bitrate=config["audio_bitrate"],
        overwrite=bool(config["overwrite"]),
    )

    total_files = len(video_path)
    started = [0]

    def on_start(path):
        started[0] += 1
        print(f"\n▶ 正在压缩 ({started[0]}/{total_files}): {os.path.basename(path)}")

    def on_progress(pct, time_str):
        if pct is not None:
            print(f"\r   进度: {pct:5.1f}%   已编码 {time_str}", end="", flush=True)
        else:
            print(f"\r   进度: ...   已编码 {time_str}", end="", flush=True)

    # 视频编码为 CPU 密集型任务，顺序转换可吃满单核且进度清晰
    result = compressor.convert(max_workers=1, on_start=on_start, on_progress=on_progress)

    print("\n")
    print(f"✅ 成功：{len(result['success'])} 个")
    for path, output in result["success"]:
        print(f"   {os.path.basename(path)} → {os.path.basename(output)}")

    if result["skipped"]:
        print(f"\n⏭️ 跳过：{len(result['skipped'])} 个")
        for path, reason in result["skipped"]:
            print(f"   {os.path.basename(path)}：{reason}")

    if result["failed"]:
        print(f"\n❌ 失败：{len(result['failed'])} 个")
        for path, error in result["failed"]:
            print(f"   {os.path.basename(path)}：{error}")

    # ── 倒计时 + 按键退出 ──
    print("\n" + "-" * 50)
    print("按任意键立即退出，或等待倒计时自动退出")

    # 倒计时
    for i in range(5, 0, -1):
        print(f"\r⏳ {i} 秒后自动退出... (按任意键退出)", end="")
        time.sleep(1)
    print("\r👋 已退出")
    sys.exit(0)


def main():
    param_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = get_config()
    if param_path:
        paths = get_path(param_path)
        cli(paths, config)
    else:
        run_config_window(config)


if __name__ == "__main__":
    main()
