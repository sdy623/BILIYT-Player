# Edge 登录桥接扩展

Edge 127+ 默认启用 App-Bound Encryption，普通桌面程序不应绕过。这个 Manifest V3
扩展通过 Edge 官方 `cookies` API **只读取 `bilibili.com` 的 `SESSDATA`**，然后用
BiliYTPlayer 每次启动生成的一次性配对码，通过 `127.0.0.1` 交给当前进程。

Cookie 不写入项目、临时文件、浏览器剪贴板或扩展存储。

## 安装

1. 打开 `edge://extensions`，启用“开发人员模式”。
2. 点击“加载解压缩的扩展”，选择本目录 `extras/edge-session-bridge`。
3. 启动 BiliYTPlayer；没有检测到登录时，软件会把一次性配对码复制到剪贴板。
4. 点击扩展图标，粘贴配对码，选择“导入到本机播放器”。
5. 回到 BiliYTPlayer 点击“重试”。

监听器只绑定 `127.0.0.1:48129`，只接受 `chrome-extension://` 来源及正确的一次性
配对码。关闭播放器后配对码和内存中的 Cookie 一并失效。
