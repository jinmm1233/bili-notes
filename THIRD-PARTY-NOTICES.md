# 第三方组件声明

BiliNotes 本体以 **Apache License 2.0** 授权（见 [LICENSE](LICENSE)）。
本文件列出随项目分发的第三方组件及其授权，供再分发时一并保留。

## 运行期 Python 依赖

| 组件 | 版本 | 许可证 |
|---|---|---|
| fastapi | 0.162.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| pydantic-settings | 2.16.2 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| yt-dlp | 2026.10.26 | Unlicense |
| faster-whisper | 1.2.1 | MIT |
| ctranslate2 | 4.8.4 | MIT |
| onnxruntime | 1.31.1 | MIT |
| av (PyAV) | 19.0.2 | BSD-3-Clause |
| rapidocr-onnxruntime | 1.2.3 | Apache-2.0 |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 |
| Pillow | 13.0.0 | MIT-CMU |
| numpy | 2.5.4 | BSD-3-Clause（含 0BSD / MIT / Zlib / CC0-1.0 片段） |
| imagehash | 4.3.2 | BSD-2-Clause |
| qrcode | 8.2 | BSD-3-Clause |
| pypinyin | 0.55.0 | MIT |
| pywebview | 6.2.1 | BSD-3-Clause |
| python-docx | 1.2.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| latex2mathml | 3.81.1 | MIT |
| mathml2omml | 0.0.2 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

`vendor/` 目录随包分发上述导出相关库（python-docx / lxml / latex2mathml /
mathml2omml / typing_extensions），其许可证原文保存在各自的
`*.dist-info/licenses/` 下，未做改动。

## 随安装包分发的二进制

### FFmpeg / FFprobe

安装包内含 `ffmpeg.exe` 与 `ffprobe.exe`，来自 gyan.dev 的
**essentials 构建（FFmpeg 7.1）**，其编译参数包含 `--enable-gpl`
与 `--enable-version3`，因此这两个二进制以
**GNU General Public License v3.0** 分发。

- GPLv3 全文：https://www.gnu.org/licenses/gpl-3.0.txt
- 该构建的来源与构建脚本：https://www.gyan.dev/ffmpeg/builds/
- FFmpeg 上游源码：https://ffmpeg.org/download.html

BiliNotes 通过**独立进程**调用 ffmpeg / ffprobe（`subprocess`），不链接其代码、
不修改其源码，属于聚合分发。因此本项目自身仍按 Apache-2.0 授权，
ffmpeg / ffprobe 二进制保持其原有 GPLv3 授权。

> 若要替换为 LGPL 构建（不启用 libx264 / libx265），需自行编译并沿用其
> LGPLv2.1+ 条款；当前使用的是 GPL 构建，请勿删除本节说明。

### vendor/bin

`l2m.exe`、`latex2mathml.exe` 是用 PyInstaller 打包的 latex2mathml（MIT），
仅用于公式转换，无额外授权要求。

## 运行期下载（不随本项目分发）

- **语音识别模型**：faster-whisper 系列（Systran/faster-whisper-*，MIT），
  首次运行时由用户机器从 hf-mirror.com 下载。
- **B 站字幕与视频**：内容版权归原作者及平台所有，本工具仅做本地转写与摘录。

## 维护说明

依赖升级后请同步更新上表。可用以下命令核对实际安装版本与许可证：

```bash
python -c "import importlib.metadata as m;print(m.version('fastapi'))"
```
