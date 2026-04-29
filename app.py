import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "8000"))
BASE_URL = os.getenv("BASE_URL", f"http://{HOST}:{PORT}")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_DIR = Path(os.getenv("COOKIE_DIR", "runtime_cookies")).resolve()
COOKIE_DIR.mkdir(parents=True, exist_ok=True)

STATE_DIR = Path(os.getenv("STATE_DIR", "runtime_state")).resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
LATEST_DOWNLOAD_FILE = STATE_DIR / "latest_download.json"
LAST_COOKIE_STATE_FILE = STATE_DIR / "last_cookie.json"

TOOLS_DIR = Path(os.getenv("TOOLS_DIR", "tools")).resolve()
TOOLS_DIR.mkdir(parents=True, exist_ok=True)

BILIBILI_HOSTS = {
    "www.bilibili.com",
    "bilibili.com",
    "m.bilibili.com",
    "b23.tv",
    "www.b23.tv",
}

COMMON_EXTRA_PATHS = [
    str(TOOLS_DIR),
    r"C:\Windows\System32",
    r"C:\Windows\System32\OpenSSH",
    r"C:\Program Files\nodejs",
    r"D:\ffmpeg\ffmpeg-8.0-essentials_build\bin",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

class DownloadRequest(BaseModel):
    text: str = Field(..., description="Text containing a Bilibili URL or BV number")
    cookie_file: Optional[str] = Field(None, description="Absolute path to cookies.txt")

def build_process_path() -> str:
    path_parts: list[str] = []
    for key in ("PATH", "Path"):
        value = os.environ.get(key)
        if value:
            path_parts.extend(value.split(os.pathsep))
    path_parts.extend(COMMON_EXTRA_PATHS)

    deduped: list[str] = []
    seen: set[str] = set()
    for part in path_parts:
        normalized = part.strip()
        lowered = normalized.lower()
        if not normalized or lowered in seen:
            continue
        deduped.append(normalized)
        seen.add(lowered)
    return os.pathsep.join(deduped)

def build_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("Path", None)
    env["PATH"] = build_process_path()
    return env

def find_executable(name: str, extra_candidates: list[str]) -> Optional[str]:
    found = shutil.which(name, path=build_process_path())
    if found:
        return found
    for candidate in extra_candidates:
        if Path(candidate).exists():
            return candidate
    return None

FFMPEG_PATH = find_executable(
    "ffmpeg",
    [
        r"D:\ffmpeg\ffmpeg-8.0-essentials_build\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ],
)

def remove_cjk_and_whitespace(text: str) -> str:
    return re.sub(r"[\u4e00-\u9fff\s]+", "", text)

def sanitize_candidate(text: str) -> str:
    cleaned = text.strip().replace("\\", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"[\u4e00-\u9fff]+", "", cleaned)
    return cleaned

def extract_bilibili_url(raw_text: str) -> str:
    # Check for bare BV number first (e.g. BV1VEdjB6EEx)
    stripped = raw_text.strip()
    bv_match = re.match(r"^(BV[A-Za-z0-9]+)$", stripped, re.IGNORECASE)
    if bv_match:
        return f"https://www.bilibili.com/video/{bv_match.group(1)}"

    # Also try to find a BV number embedded in text without a URL
    bv_in_text = re.search(r"\b(BV[A-Za-z0-9]{10,})\b", stripped, re.IGNORECASE)

    direct_match = re.search(r"https?://[^\s]+", raw_text)
    candidate = direct_match.group(0) if direct_match else remove_cjk_and_whitespace(raw_text)
    candidate = sanitize_candidate(candidate)
    fallback_match = re.search(r"https?://[^\s]+", candidate)
    url = fallback_match.group(0) if fallback_match else candidate

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in BILIBILI_HOSTS:
        # Fall back to BV number found in text
        if bv_in_text:
            return f"https://www.bilibili.com/video/{bv_in_text.group(1)}"
        raise ValueError("没有从输入文本中解析到有效的 B 站链接或 BV 号。")
    return url

def normalize_filename_part(text: str) -> str:
    cleaned = re.sub(r"[\u4e00-\u9fff]+", "", text)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "video"

def normalize_cookie_file(cookie_file: Optional[str]) -> Optional[str]:
    if not cookie_file:
        return None
    resolved = Path(cookie_file).expanduser()
    if not resolved.is_file():
        raise ValueError(f"cookies.txt 不存在: {resolved}")
    return str(resolved)

def save_uploaded_cookie_file(upload: UploadFile) -> str:
    filename = (upload.filename or "cookies.txt").lower()
    if not filename.endswith(".txt"):
        raise ValueError("上传的 cookies 文件必须是 .txt")
    target = COOKIE_DIR / "uploaded_cookies.txt"
    with target.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)
    return str(target)

def save_last_cookie_state(cookie_path: str = "", cookie_file_path: str = "") -> None:
    state = {}
    if cookie_path:
        state["last_cookie_path"] = cookie_path
    if cookie_file_path:
        state["last_cookie_file"] = cookie_file_path
    if not state:
        return
    # Merge with existing state
    existing = load_last_cookie_state()
    existing.update(state)
    with LAST_COOKIE_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def load_last_cookie_state() -> dict:
    if not LAST_COOKIE_STATE_FILE.is_file():
        return {}
    try:
        with LAST_COOKIE_STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def build_ydl_options(
    *,
    quiet: bool,
    cookie_file: Optional[str],
) -> dict:
    options: dict = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noplaylist": True,
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "windowsfilenames": True,
        "http_headers": {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        },
    }
    if cookie_file:
        options["cookiefile"] = cookie_file
    if FFMPEG_PATH:
        options["ffmpeg_location"] = FFMPEG_PATH
    return options

def format_download_error(exc: Exception) -> str:
    message = str(exc)
    if "HTTP Error 412" in message:
        return (
            "B 站返回了 412。通常需要登录态。"
            " 请提供 cookies.txt，或填写 edge/chrome 并先完全关闭对应浏览器后再试。"
        )
    if "Could not copy Chrome cookie database" in message or "failed to load cookies" in message:
        return "浏览器 cookies 读取失败。请先完全关闭浏览器，或改用导出的 cookies.txt。"
    return f"下载失败: {message}"

def save_latest_download(record: dict) -> None:
    payload = dict(record)
    payload["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with LATEST_DOWNLOAD_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

def load_latest_download() -> Optional[dict]:
    if not LATEST_DOWNLOAD_FILE.is_file():
        return None
    try:
        with LATEST_DOWNLOAD_FILE.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None

def infer_latest_download_from_files() -> Optional[dict]:
    candidates = sorted(
        (path for path in DOWNLOAD_DIR.glob("*.mp4") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    latest_file = candidates[0]
    return {
        "input_text": "",
        "video_url": "",
        "title": latest_file.stem,
        "file_path": str(latest_file),
        "filename": latest_file.name,
        "local_url": "",
    }

def download_bilibili_video(
    video_url: str,
    *,
    cookie_file: Optional[str],
) -> dict:
    normalized_cookie_file = normalize_cookie_file(cookie_file)

    info_opts = build_ydl_options(
        quiet=True,
        cookie_file=normalized_cookie_file,
    )
    try:
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except DownloadError as exc:
        raise RuntimeError(format_download_error(exc)) from exc

    video_id = str(info.get("id") or "video")
    title = str(info.get("title") or video_id)
    safe_stem = f"{video_id}_{normalize_filename_part(title)}"
    target_path = DOWNLOAD_DIR / f"{safe_stem}.mp4"

    download_opts = build_ydl_options(
        quiet=False,
        cookie_file=normalized_cookie_file,
    )
    download_opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

    try:
        with YoutubeDL(download_opts) as ydl:
            ydl.download([video_url])
    except DownloadError as exc:
        raise RuntimeError(format_download_error(exc)) from exc

    downloaded_candidates = list(DOWNLOAD_DIR.glob(f"{video_id}.*"))
    merged_file: Optional[Path] = None
    for candidate in downloaded_candidates:
        if candidate.is_file() and candidate.suffix.lower() == ".mp4":
            merged_file = candidate
            break
    if merged_file is None:
        for candidate in downloaded_candidates:
            if candidate.is_file():
                merged_file = candidate
                break

    if merged_file is None or not merged_file.exists():
        raise RuntimeError("下载完成后没有找到合并后的 mp4 文件。")

    if target_path.exists() and target_path != merged_file:
        target_path.unlink()
    if merged_file != target_path:
        merged_file.rename(target_path)

    return {
        "video_id": video_id,
        "title": title,
        "path": str(target_path),
        "filename": target_path.name,
    }


def build_download_response(payload: DownloadRequest) -> dict:
    video_url = extract_bilibili_url(payload.text)

    # Persist cookie path if provided
    if payload.cookie_file:
        save_last_cookie_state(cookie_path=payload.cookie_file)

    result = download_bilibili_video(
        video_url,
        cookie_file=payload.cookie_file,
    )

    local_url = f"{BASE_URL}/files/{result['filename']}"

    response = {
        "input_text": payload.text,
        "video_url": video_url,
        "title": result["title"],
        "file_path": result["path"],
        "filename": result["filename"],
        "local_url": local_url,
    }
    
    save_latest_download(response)
    return response


app = FastAPI(title="Bilibili Downloader")
app.mount("/files", StaticFiles(directory=str(DOWNLOAD_DIR)), name="files")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bilibili Downloader</title>
  <style>
    :root { color-scheme: light; }
    body {
      font-family: "Segoe UI", sans-serif;
      max-width: 920px;
      margin: 32px auto;
      padding: 0 16px 32px;
      line-height: 1.5;
    }
    h1, h2 { margin-bottom: 12px; }
    section { margin-top: 28px; }
    textarea, input {
      width: 100%;
      box-sizing: border-box;
      padding: 12px;
      margin-top: 10px;
      font: inherit;
    }
    button {
      margin-top: 14px;
      padding: 10px 16px;
      cursor: pointer;
      font: inherit;
    }
    pre {
      background: #111827;
      color: #f9fafb;
      padding: 12px;
      overflow: auto;
      border-radius: 8px;
      white-space: pre-wrap;
    }
    .row { margin-top: 10px; }
    .textarea-row {
      position: relative;
      margin-top: 10px;
    }
    .textarea-row textarea {
      margin-top: 0;
      padding-right: 40px;
    }
    .clear-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      margin-top: 0;
      padding: 2px 8px;
      background: #e5e7eb;
      border: none;
      border-radius: 4px;
      font-size: 16px;
      line-height: 1;
      cursor: pointer;
      color: #374151;
    }
    .clear-btn:hover {
      background: #d1d5db;
    }
    .share-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      align-items: center;
      margin-top: 10px;
    }
    .share-row input { margin-top: 0; }
    .meta { color: #374151; margin-top: 6px; }
    @media (max-width: 720px) {
      .share-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <h1>B站视频下载器</h1>
  <p>贴入 B 站链接或直接输入 BV 号即可下载。若 B 站返回 412，可填写 cookies.txt 路径或直接上传 cookies.txt。</p>

  <section>
    <div class="textarea-row">
      <textarea id="text" placeholder="例如：BV1VEdjB6EEx 或 https://www.bilibili.com/video/BV..."></textarea>
      <button class="clear-btn" onclick="clearInput()" title="清空输入">✕</button>
    </div>
    <input id="cookieFile" placeholder="可选：cookies.txt 绝对路径">
    <input id="cookieUpload" type="file" accept=".txt">
    <button onclick="submitDownload()">开始下载</button>
  </section>

  <section>
    <h2>最近一次下载</h2>
    <div class="meta" id="shareMeta">尚未生成本地地址。</div>
    <input id="shareTitle" readonly placeholder="最近一次下载的视频标题">
    <div class="share-row">
      <input id="localShareUrl" readonly placeholder="本地下载链接">
      <button onclick="copyField('localShareUrl')">复制本地链接</button>
      <button onclick="openField('localShareUrl')">打开</button>
    </div>
    <div class="share-row">
      <input id="filePath" readonly placeholder="本地文件路径">
      <button onclick="copyField('filePath')">复制文件路径</button>
      <button onclick="refreshShareInfo()">刷新</button>
    </div>
  </section>

  <section>
    <h2>接口结果</h2>
    <pre id="result"></pre>
  </section>

  <script>
    async function copyText(text) {
      if (!text) return;
      await navigator.clipboard.writeText(text);
    }

    async function copyField(id) {
      const element = document.getElementById(id);
      if (!element || !element.value) return;
      await copyText(element.value);
    }

    function openField(id) {
      const element = document.getElementById(id);
      if (!element || !element.value) return;
      window.open(element.value, "_blank");
    }

    function renderShareInfo(data) {
      const title = data && data.title ? data.title : "";
      const localUrl = data && data.local_url ? data.local_url : "";
      const filePath = data && data.file_path ? data.file_path : "";

      document.getElementById("shareTitle").value = title;
      document.getElementById("localShareUrl").value = localUrl;
      document.getElementById("filePath").value = filePath;
      document.getElementById("shareMeta").textContent =
        title ? `已就绪` : "尚未生成本地地址。";
    }

    async function refreshShareInfo() {
      const response = await fetch(`/api/share/latest`);
      const result = document.getElementById("result");
      const data = await response.json().catch(() => ({ detail: "请求失败" }));

      if (response.status === 404) {
        renderShareInfo(null);
        result.textContent = JSON.stringify(data, null, 2);
        return;
      }

      result.textContent = JSON.stringify(data, null, 2);
      if (response.ok) {
        renderShareInfo(data);
      }
    }

    function clearInput() {
      document.getElementById("text").value = "";
      document.getElementById("text").focus();
    }

    async function loadCookieState() {
      try {
        const resp = await fetch("/api/cookie-state");
        if (resp.ok) {
          const data = await resp.json();
          if (data.last_cookie_path) {
            document.getElementById("cookieFile").value = data.last_cookie_path;
          }
        }
      } catch(e) {}
    }

    async function submitDownload() {
      const result = document.getElementById("result");
      result.textContent = "处理中...";
      const uploadInput = document.getElementById("cookieUpload");
      const hasUpload = uploadInput.files && uploadInput.files.length > 0;
      let response;

      if (hasUpload) {
        const formData = new FormData();
        formData.append("text", document.getElementById("text").value);
        formData.append("cookie_file", document.getElementById("cookieFile").value || "");
        formData.append("cookie_upload", uploadInput.files[0]);
        response = await fetch("/api/download/upload", {
          method: "POST",
          body: formData
        });
      } else {
        const payload = {
          text: document.getElementById("text").value,
          cookie_file: document.getElementById("cookieFile").value || null
        };
        response = await fetch("/api/download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
      }

      const data = await response.json().catch(() => ({ detail: "请求失败" }));
      result.textContent = JSON.stringify(data, null, 2);
      if (response.ok) {
        renderShareInfo(data);
      }
    }

    loadCookieState();
    refreshShareInfo();
  </script>
</body>
</html>
"""

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "base_url": BASE_URL,
        "downloads_dir": str(DOWNLOAD_DIR),
        "cookie_dir": str(COOKIE_DIR),
        "state_dir": str(STATE_DIR),
        "tools_dir": str(TOOLS_DIR),
        "ffmpeg_path": FFMPEG_PATH,
    }


@app.get("/api/share/latest")
def latest_share() -> dict:
    latest = load_latest_download()
    if not latest:
        latest = infer_latest_download_from_files()
        if not latest:
            raise HTTPException(status_code=404, detail="暂无最近一次下载记录。")

    file_path = Path(latest["file_path"])
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="最近一次下载的文件已经不存在。")

    latest["local_url"] = f"{BASE_URL}/files/{latest['filename']}"
    save_latest_download(latest)
    return latest


@app.post("/api/download")
def download_video(payload: DownloadRequest) -> dict:
    try:
        return build_download_response(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/cookie-state")
def get_cookie_state() -> dict:
    return load_last_cookie_state()


@app.post("/api/download/upload")
def download_video_with_upload(
    text: str = Form(...),
    cookie_file: str = Form(""),
    cookie_upload: UploadFile = File(...),
) -> dict:
    try:
        uploaded_cookie_path = save_uploaded_cookie_file(cookie_upload)
        # Persist the uploaded cookie file path
        if uploaded_cookie_path:
            save_last_cookie_state(cookie_file_path=uploaded_cookie_path)
        payload = DownloadRequest(
            text=text,
            cookie_file=uploaded_cookie_path if uploaded_cookie_path else (cookie_file or None),
        )
        return build_download_response(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)