# BiliYTPlayer — 剪贴板直连播放器

> 复制 B 站 / YouTube 链接到剪贴板，自动用本机 MPV Anime 播放。
> 支持 B 站 DASH 直链、杜比视界、HDR 动态映射、YouTube 最高 4K 流。

## 架构

```
bili_yt_player.pyw      GUI 入口（tkinter），剪贴板监听 + 工作线程（生产者-消费者）
bili_clipboard_dolby.py 后端核心：B 站 API 鉴权 / WBI 签名 / DASH 流提取 / mpv 启动参数
bili-ipc.lua            mpv 插件：Ctrl+←/→ 切集按键 → 信号文件 → Python 执行切流
mpv-portable/mpv.exe    播放器（所有解码、渲染、色调映射在此完成）
mpv-portable/yt-dlp.exe YouTube URL 解析（由 mpv 内置 ytdl_hook 调用）
config/mpv.conf         mpv 渲染 / 同步 / 缓冲 / HDR 映射配置（参考副本）
config/input.conf       mpv 快捷键配置（参考副本）
```

播放流程：剪贴板监听线程只做读取 + 正则匹配，检测到新链接入队；工作线程消费队列，调用 B 站 API 或 yt-dlp 解析出真实流地址，启动 mpv 播放。监听线程全程非阻塞。

## 使用

1. 在 Edge 或 Firefox 中登录 B 站，同时准备好本机 MPV Anime
2. 解压后双击 `BiliYTPlayer.exe`
3. 复制 B 站 / YouTube 视频链接到剪贴板，自动播放
4. 按 `q` 退出，`f` 全屏，`` ` `` 查看渲染统计（mpv 默认快捷键见 `config/input.conf`）

可选（一键唤起）：安装 [`extras/biliyt-link/`](extras/biliyt-link/) 配套组件——网页端装油猴脚本、本机装 `biliyt://` 转发器，在 B 站 / YouTube 视频页点一下 ▶ 按钮就直接唤起播放，不用手动复制网址。三步安装见该目录 README。脚本沿用旧名称「⚡ BiliYTPlayer 一键 MPV 播放」，装过旧版的直接覆盖安装即可升级。

### 下载 RAW 原始流

需要保存原始流时，使用独立的 WebView2 `BiliRawDownloader.exe`：粘贴链接后，它会下载接口返回的最高质量视频，并按实际可用内容保留普通音频、Hi-Res/FLAC、Dolby Atmos，支持普通、仅 Hi-Res、仅 Dolby Vision（配普通音频）、Hi-Res + Dolby Vision 四种组合。下载依次尝试 aria2、GNU Wget2（若已安装）和内置 Python/OpenSSL 多连接实现；封装全程不转码。普通视频使用 FFmpeg `-c copy`，检测到 `dvh1`/`dvhe` Dolby Vision 时自动改用 MKVToolNix，保留 DV 配置与 RPU。空间合集链接会按顺序批量下载。详见 [`legacy/BILI_RAW_DOWNLOADER.md`](legacy/BILI_RAW_DOWNLOADER.md)。

### 从源码运行

```bash
# 1. 准备本机 MPV Anime；程序会优先自动探测其 mpv.exe 与 portable_config

# 2. 安装依赖
pip install -r requirements.txt

# 3. B 站登录：直接复用 Edge / Firefox 已登录的会话，不显示二维码，也不另存 Cookie

# 4. 运行
pythonw legacy/bili_yt_player.pyw
```

## 打包

```bash
cd legacy
.venv-tk/Scripts/python.exe -m PyInstaller --noconfirm bili_yt_player.spec
# 输出 dist/BiliYTPlayer.exe，与 mpv-portable/ 同级放置
```

> `.venv-tk` 必须基于**系统版 Python** 创建（自带 tkinter）。用托管版 Python
> 建的虚拟环境没有 tkinter，打包不报错但 exe 双击即崩。

## 关键设计

- **B 站直连**：裸 socket HTTPS 直连 B 站 API，避免 `requests` 库在 Windows 下的代理探测延迟
- **YouTube 代理**：mpv 不会自动读取环境变量代理，`launch_player` 会显式传 `--http-proxy`（mpv 拉 CDN 流）和 `--ytdl-raw-options=...,proxy=`（yt-dlp 解析 URL）。代理检测支持环境变量 / Windows 系统代理 / socks5→http 端口探测 / PAC 识别
- **HDR 动态映射**：`mpv.conf` 通过 `profile-cond` 依据视频源色彩空间自动切换 HDR10 / HLG / SDR 映射参数
- **DASH 同步**：`video-sync=audio` 让视频服从音频时钟，避免 B 站 / YouTube 双 CDN 分离流时钟打架导致卡顿

## 维护

非程序员维护指南（存档/验收/发布流程）：见 [`docs/维护手册.md`](docs/维护手册.md)。开发行为准则见 [`.cursorrules`](.cursorrules)。

## 历史与归档

本节说明仓库里那些看起来像「另一个项目」的东西，避免误认为仍在维护或误删。

**现行项目**：`main` 分支。项目曾用名 `CB-to-MPV` → `BiliYTPlayer`，仓库现名 `BILIYT-Player`。旧的仓库名 `Bili--smplayer-` 由 GitHub 自动重定向到本仓库，**不是另一个仓库**。可下载版本见 Releases 页。

**为什么要专门说明**：2026-10 前后仓库历史做过一次重置。因此下面的标签和分支与当前 `main` **没有共同祖先**，是重命名前项目的独立代码线，不是现版本的「旧版本」。删掉它们会永久失去那条线上的独有文件。

### 归档分支（只读保留，不再维护）

| 分支 | 时期 | 内容 |
|---|---|---|
| `archive/cb-player-pre-reset-2026-10` | CB-to-MPV 时期 | 自动打包 CI 配置、Cookie 泄漏 / IPC 配对 / 单实例三个测试、早期实现 |
| `archive/worktree-exe-pre-reset-2026-07` | 2026-07 | PyInstaller 打包修复实验，旧目录布局 |

### 历史标签（无 Release 资产，仅用于回溯）

`v1.2.0`、`v2.0`、`v2.1.0`、`v2.2.0`、`v2.3.0`，均为 2026-07 的早期版本里程碑。其中 `v1.2.0` 与 `v2.0` 指向同一提交（项目重命名）。**保留用于历史回溯，不代表可用版本**，请勿据此下载使用。

### 其它仓库

`archive-AutoEq-fork-2023` 是 2023 年 fork 的耳机均衡项目，与本播放器无关，已标注为遗留归档。

## 敏感信息声明

仓库不含任何账号凭证。B 站登录态仅从本机 Edge / Firefox 的 Cookie 数据库中读取 `bilibili.com` 的 `SESSDATA`，只在当前进程内存中使用；不会复制完整 Cookie 库，不会写入项目目录、临时 Cookie 文件或新的凭证文件。播放时凭证通过 HTTP 请求头直接传给 mpv。

Edge 使用当前 Windows 用户的 DPAPI/AES-GCM 保护。Edge 127+ 启用 App-Bound（`v20`）时，本程序不会绕过该保护；请加载 [`extras/edge-session-bridge/`](extras/edge-session-bridge/) 扩展，通过 Edge 官方 Cookie API 和一次性配对码导入。Firefox 可直接只读导入。登录来源可在主窗口选择“自动 / Edge / Firefox”。

## 环境要求

- Windows 10/11（x64）
- 运行时：mpv ≥ 0.36（推荐 0.41，需支持 `profile-cond`）、yt-dlp
- 开发：Python 3.10+、PyInstaller
