# B站视频下载器

一个基于 FastAPI + yt-dlp 的 B 站视频下载工具，提供 Web 界面，支持链接解析、BV 号直接下载。

## 功能

1. 支持从整段分享文本中自动提取 B 站视频链接。
2. **支持直接输入 BV 号**（如 `BV1VEdjB6EEx`），无需完整链接。
3. 使用 `yt-dlp + ffmpeg` 下载视频，自动合并为 `mp4` 格式。
4. 支持 `cookies.txt` 路径输入或页面直接上传 `cookies.txt`。
5. **自动记忆上次使用的 cookie 路径**，下次打开自动填充。
6. 页面显示最近一次下载结果，支持一键复制本地链接和文件路径。
7. 支持打包为可执行文件，通过 `一键启动.bat` 运行。

## 环境要求

- Python 3.10+
- ffmpeg（需配置到 PATH 或放在默认路径下）

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 启动

### 开发模式

```powershell
python app.py
```

浏览器打开 http://127.0.0.1:8000

### 整合包

进入 `dist/B站下载器/` 目录，双击 `一键启动.bat` 即可运行。

## 使用方式

1. 在输入框中粘贴 B 站分享文本、完整链接，或直接输入 BV 号。
2. 如遇到 412 错误，填写 `cookies.txt` 路径或上传 `cookies.txt` 文件。
3. 点击「开始下载」。
4. 下载完成后，可在「最近一次下载」区域复制本地链接或文件路径。

> 输入框旁有清空按钮（✕），可快速清除已输入内容。

## 主要接口

### 下载视频

```http
POST /api/download
Content-Type: application/json

{
  "text": "BV1VEdjB6EEx",
  "cookie_file": "D:\\path\\to\\cookies.txt"
}
```

`text` 字段支持以下格式：
- 纯 BV 号：`BV1VEdjB6EEx`
- 完整链接：`https://www.bilibili.com/video/BV1VEdjB6EEx`
- 分享文本：`【标题】 https://www.bilibili.com/video/BV1VEdjB6EEx`

### 上传 cookies.txt 后下载

```http
POST /api/download/upload
Content-Type: multipart/form-data
```

表单字段：

- `text` — 视频链接或 BV 号（必填）
- `cookie_file` — cookies.txt 路径（可选）
- `cookie_upload` — 上传的 cookies.txt 文件（必填）

### 最近一次下载

```http
GET /api/share/latest
```

返回最近一次下载文件的 `file_path`、`local_url`、`title` 等信息。

### Cookie 状态

```http
GET /api/cookie-state
```

返回上次使用的 cookie 路径，用于前端自动填充。

## 项目结构

```
├── app.py                # 主程序
├── requirements.txt      # Python 依赖
├── downloads/            # 视频下载目录
├── runtime_cookies/      # 上传的 cookies 存放目录
├── runtime_state/        # 状态持久化（最近下载记录、cookie 路径）
├── tools/                # 外部工具目录
└── dist/                 # 打包输出目录
    └── B站下载器/
        ├── B站下载器.exe
        └── 一键启动.bat
```

## 如何获取 Cookie

下载部分视频（尤其是高画质）需要登录态，需要提供 `cookies.txt` 文件。

### 方法一：浏览器插件导出（推荐）

1. 安装浏览器插件 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)（Chrome / Edge 均可用）。
2. 登录 [bilibili.com](https://www.bilibili.com)。
3. 在 B 站页面上点击插件图标，选择「Export」导出为 `cookies.txt`。
4. 将导出的文件路径填入下载器的 cookie 路径输入框，或直接上传该文件。

### 方法二：手动从浏览器 DevTools 导出

1. 登录 B 站后，按 `F12` 打开开发者工具。
2. 切换到「Application」（应用）→「Cookies」→ `https://www.bilibili.com`。
3. 将所有 cookie 条目手动整理为 Netscape 格式，保存为 `.txt` 文件。

> Netscape 格式示例（每行一条，Tab 分隔）：
> ```
> .bilibili.com	TRUE	/	FALSE	0	SESSDATA	你的值
> .bilibili.com	TRUE	/	FALSE	0	bili_jct	你的值
> ```

### 注意事项

- Cookie 有有效期，过期后需要重新导出。
- 不要把 cookie 文件分享给他人，它等同于你的登录凭证。
- 下载器会记住上次使用的 cookie 路径，下次无需重复填写。

## 说明

- `ffmpeg` 通过本机路径调用，默认搜索 `D:\ffmpeg\ffmpeg-8.0-essentials_build\bin`。
- `cookies.txt` 需要是 Netscape 格式（可用浏览器插件导出）。
- 如果 B 站返回 `412`，通常需要登录态；最稳的方式是导出 `cookies.txt`。
