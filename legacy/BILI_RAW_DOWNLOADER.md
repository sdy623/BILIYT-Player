# Bili RAW 原流下载器

双击 **BiliRawDownloader.exe**，粘贴哔哩哔哩视频或合集链接，选择保存目录，然后点击“开始下载”。界面使用 Microsoft Edge WebView2，并按系统 DPI 清晰缩放。

## 成品结构

1. 视频：接口返回的最高质量 DASH 原流（包含可用的 Dolby Vision 候选）
2. 音频按接口实际返回内容选择：
   - Hi-Res + Dolby 同时存在：保留两条，Dolby 默认播放；
   - 只有 Hi-Res：保留 FLAC 单音轨；
   - 只有 Dolby：保留 E-AC-3 JOC 单音轨；
   - 两者都没有：保留最高码率普通 DASH 音频。

视频与音频能力独立判断：普通视频、仅 Hi-Res、仅 Dolby Vision（配普通音频）、Hi-Res + Dolby Vision 四种情况都能下载；Atmos 音轨有则另外保留。

封装过程不重新编码。普通视频由 FFmpeg 使用 \`-c copy\` 写入 Matroska；\`dvh1\`/\`dvhe\` Dolby Vision 会自动改用 MKVToolNix，避免 MP4 codec tag 与 Matroska 冲突，并保留 Dolby Vision 配置与 RPU。完成后程序会用 FFprobe 按本次选中的轨道组合复核编码与数量。

## 登录与权限

- 直接只读使用本机 Edge 或 Firefox 已有的 bilibili 登录状态，不扫码。
- Cookie 只存在当前进程内存，不写入下载器配置或日志。
- 不要求 Dolby 或 Hi-Res。普通视频、仅 Hi-Res、仅 Dolby Vision、Hi-Res + Dolby Vision 都可以下载；Atmos 有则保留。不会用普通 AAC 冒充 Hi-Res 或 Atmos。
- 支持 BV、分 P、EP、SS、MD 和 b23.tv 短链接。

## 保存与断点

默认保存到项目的 \`downloads\` 目录。临时原流放在 \`downloads\\.bili-raw-work\`；下载中断后再次运行会尝试续传，成品验证通过后自动清理临时文件。下载引擎依次尝试 aria2、GNU Wget2（若已安装）和内置 Python/OpenSSL 多连接实现，并在 CDN 拒绝高并发时自动降连接或换节点。

## 运行条件

本机需要可用的 \`ffmpeg.exe\` 与 \`ffprobe.exe\`。下载 Dolby Vision 时还需要 \`mkvmerge.exe\`（MKVToolNix）。aria2c 和 wget2 都是可选高速引擎；没有时仍可使用内置多连接下载。程序会依次查找下载器同目录、WinGet 链接、MKVToolNix 默认安装目录和系统 PATH。
