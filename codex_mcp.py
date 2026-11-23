#!/usr/bin/env python3
"""stdio MCP：让 Codex / Claude Code / Cursor 调用网关的生图 / 生视频，不替换客户端自带模型。

环境变量优先读 MCP 配置里的 env（客户端启动时放进进程环境）。
某一项没写时，才用程序旁边的 .env 补上。
  MUSE2API_BASE       网关地址，默认 https://muse.aiia.cool。写成带 /v1 的地址也会自动去掉。
  MUSE2API_KEY        必填，网关 API 密钥
  MUSE2API_ENV_FILE   可选。指定用来补空项的 .env，不会覆盖 MCP 配置里已经写的值。
  MUSE2API_MEDIA_DIR  成品目录。本机能看到文件时才填本地路径，默认放在程序旁边的 data/media。
  MUSE2API_IMAGE_WAIT 生图最长等待秒数，默认 420
  MUSE2API_VIDEO_WAIT 生视频最长等待秒数，默认 600
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urljoin


def normalize_base(value: str) -> str:
    base = value.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def parse_env_file(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return found
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            found[name] = value
    return found


def load_env_file() -> None:
    """只补 MCP 配置 env 里没写的项。进程里已有的值来自 MCP 配置，不覆盖。"""
    explicit = os.environ.get("MUSE2API_ENV_FILE", "").strip()
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(install_dir() / ".env")
    paths.append(Path.cwd() / ".env")
    merged: dict[str, str] = {}
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        for name, value in parse_env_file(path).items():
            merged.setdefault(name, value)
    names = (
        "MUSE2API_BASE",
        "MUSE2API_KEY",
        "MUSE2API_MEDIA_DIR",
        "MUSE2API_IMAGE_WAIT",
        "MUSE2API_VIDEO_WAIT",
    )
    for name in names:
        if not os.environ.get(name, "").strip() and merged.get(name, "").strip():
            os.environ[name] = merged[name].strip()
    if not os.environ.get("MUSE2API_BASE", "").strip() and merged.get("GATEWAY_PUBLIC_BASE", "").strip():
        os.environ["MUSE2API_BASE"] = merged["GATEWAY_PUBLIC_BASE"].strip()


load_env_file()
BASE = normalize_base(os.environ.get("MUSE2API_BASE", "https://muse.aiia.cool"))
KEY = os.environ.get("MUSE2API_KEY", "").strip()
MEDIA_DIR = Path(os.environ.get("MUSE2API_MEDIA_DIR") or install_dir() / "data" / "media")
IMAGE_WAIT = int(os.environ.get("MUSE2API_IMAGE_WAIT", "420"))
VIDEO_WAIT = int(os.environ.get("MUSE2API_VIDEO_WAIT", "600"))
DONE = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "error", "cancelled", "canceled"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def read_message():
    buf = b""
    while True:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        if chunk in b" \t\r\n" and not buf:
            continue
        buf = chunk
        break
    if buf == b"{":
        while b"\n" not in buf:
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                return None
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode())
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        buf += chunk
    sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
    header, rest = buf.split(sep, 1)
    length = 0
    for line in header.decode("ascii", "replace").splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    while len(rest) < length:
        chunk = sys.stdin.buffer.read(length - len(rest))
        if not chunk:
            return None
        rest += chunk
    return json.loads(rest[:length].decode())


def write_message(payload: dict) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
    sys.stdout.buffer.flush()


def reply(msg_id, result) -> None:
    write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


def reply_error(msg_id, code: int, message: str) -> None:
    write_message({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def tool_text(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def error_text(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        detail = parsed.get("detail", raw)
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)
        return str(detail)
    return raw


def api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    if not KEY:
        raise RuntimeError("未设置 MUSE2API_KEY")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = error_text(exc.read().decode("utf-8", "replace"))
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上网关（{BASE}）：{exc.reason}") from exc
    if not raw:
        return {}
    return json.loads(raw)


def as_reference(item: str) -> str:
    text = str(item).strip()
    if not text:
        raise RuntimeError("参考素材是空的")
    if text.startswith(("http://", "https://", "data:")):
        return text
    path = Path(text).expanduser()
    if not path.is_file():
        raise RuntimeError(f"找不到参考文件: {text}")
    size = path.stat().st_size
    if size > 40 * 1024 * 1024:
        raise RuntimeError(f"参考文件超过 40MiB: {text}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def references(args: dict) -> list[str]:
    raw = []
    if isinstance(args.get("images"), list):
        raw.extend(args["images"])
    elif isinstance(args.get("images"), str):
        raw.append(args["images"])
    if args.get("image"):
        raw.append(args["image"])
    return [as_reference(item) for item in raw]


def absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(BASE + "/", url.lstrip("/"))


def media_path(url: str | None) -> str | None:
    if not url or "/v1/media/" not in url:
        return None
    name = unquote(url.split("/v1/media/", 1)[1].split("?", 1)[0].strip("/"))
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = MEDIA_DIR / name
    return str(path) if path.is_file() else None


def result_url(task: dict) -> str | None:
    if isinstance(task.get("data"), list) and task["data"]:
        url = task["data"][0].get("url")
        if url:
            return url
    if task.get("url"):
        return task["url"]
    result = task.get("result")
    if isinstance(result, dict):
        return result.get("url")
    video = task.get("video")
    if isinstance(video, dict):
        return video.get("url")
    return None


def describe(kind: str, task: dict, prompt: str, pending_tool: str) -> dict:
    status = str(task.get("status") or "").lower()
    task_id = task.get("task_id") or task.get("id")
    url = absolute_url(result_url(task))
    path = media_path(url)
    if status in DONE and not path:
        time.sleep(0.4)
        path = media_path(url)
    if status in FAILED:
        return tool_text({
            "ok": False,
            "kind": kind,
            "task_id": task_id,
            "status": status,
            "error": task.get("error") or "生成失败",
            "prompt": prompt,
        }, is_error=True)
    if status not in DONE:
        return tool_text({
            "ok": False,
            "pending": True,
            "kind": kind,
            "task_id": task_id,
            "status": status or "unknown",
            "progress": task.get("progress"),
            "prompt": prompt,
            "message": f"还在生成，不要重新提交。把这个 task_id 交给 {pending_tool} 继续查。",
        })
    return tool_text({
        "ok": True,
        "kind": kind,
        "task_id": task_id,
        "status": "completed",
        "url": url,
        "path": path,
        "bytes": (task.get("data") or [{}])[0].get("bytes") if isinstance(task.get("data"), list) else None,
        "prompt": prompt,
    })


def poll(kind: str, task_id: str, wait_s: int) -> dict:
    path = f"/v1/images/tasks/{task_id}" if kind == "image" else f"/v1/videos/{task_id}"
    deadline = time.monotonic() + max(0, wait_s)
    last = {}
    while True:
        last = api("GET", path)
        status = str(last.get("status") or "").lower()
        if status in DONE or status in FAILED or time.monotonic() >= deadline:
            return last
        time.sleep(3)


def require_prompt(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt 不能为空")
    return prompt


def wait_arg(args: dict, default: int) -> int:
    if "wait_seconds" in args and args["wait_seconds"] is not None:
        return max(0, int(args["wait_seconds"]))
    return default


def muse_image(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if task_id:
        task = poll("image", task_id, wait_arg(args, IMAGE_WAIT))
        return describe("image", task, prompt or str(task.get("prompt") or ""), "muse_image")
    prompt = require_prompt(args)
    body = {
        "model": "muse-image",
        "prompt": prompt,
        "response_format": "url",
        "async": True,
    }
    if args.get("size"):
        body["size"] = str(args["size"])
    refs = references(args)
    if refs:
        body["images"] = refs
    created = api("POST", "/v1/images/tasks", body)
    new_id = created.get("task_id") or created.get("id")
    if not new_id:
        raise RuntimeError(f"没有返回任务号: {created}")
    task = poll("image", new_id, wait_arg(args, IMAGE_WAIT))
    return describe("image", task, prompt, "muse_image")


def muse_image_edit(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if task_id:
        task = poll("image", task_id, wait_arg(args, IMAGE_WAIT))
        return describe("image", task, prompt or str(task.get("prompt") or ""), "muse_image_edit")
    prompt = require_prompt(args)
    refs = references(args)
    if not refs:
        raise RuntimeError("图生图至少要一张参考图：image 或 images")
    body = {
        "model": "muse-image",
        "prompt": prompt,
        "response_format": "url",
        "async": True,
        "images": refs,
    }
    if args.get("size"):
        body["size"] = str(args["size"])
    created = api("POST", "/v1/images/edits", body)
    new_id = created.get("task_id") or created.get("id")
    if not new_id:
        raise RuntimeError(f"没有返回任务号: {created}")
    task = poll("image", new_id, wait_arg(args, IMAGE_WAIT))
    return describe("image", task, prompt, "muse_image_edit")


def muse_video(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if task_id:
        task = poll("video", task_id, wait_arg(args, VIDEO_WAIT))
        return describe("video", task, prompt or str(task.get("prompt") or ""), "muse_video_status")
    prompt = require_prompt(args)
    body = {"model": "muse-video", "prompt": prompt}
    if args.get("size"):
        body["size"] = str(args["size"])
    if args.get("duration") is not None:
        body["duration"] = int(args["duration"])
    refs = references(args)
    if refs:
        body["images"] = refs
    created = api("POST", "/v1/videos", body)
    new_id = created.get("task_id") or created.get("id")
    if not new_id:
        raise RuntimeError(f"没有返回任务号: {created}")
    task = poll("video", new_id, wait_arg(args, VIDEO_WAIT))
    return describe("video", task, prompt, "muse_video_status")


def muse_video_status(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("task_id 不能为空")
    task = poll("video", task_id, wait_arg(args, 0))
    return describe("video", task, str(task.get("prompt") or ""), "muse_video_status")


TOOLS = {
    "muse_image": muse_image,
    "muse_image_edit": muse_image_edit,
    "muse_video": muse_video,
    "muse_video_status": muse_video_status,
}

SCHEMAS = [
    {
        "name": "muse_image",
        "description": "用网关文生图，或带参考图生图。完成后返回图片链接。若返回 pending，用同一个 task_id 再调本工具，不要重新提交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "画面描述。续查已有任务时可省略。"},
                "size": {"type": "string", "description": "画幅，如 1:1、16:9、9:16、4:3、3:4。"},
                "image": {"type": "string", "description": "可选。一张参考图的本地路径、http(s) URL 或 data URL。"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "可选。多张参考图，最多 10 张。"},
                "task_id": {"type": "string", "description": "上次返回的未完成任务号。传入后只继续查询，不新建任务。"},
                "wait_seconds": {"type": "integer", "description": "最长等待秒数，默认 420。"},
            },
        },
    },
    {
        "name": "muse_image_edit",
        "description": "用参考图做图生图 / 编辑。至少一张参考图。完成后返回图片链接。pending 时用 task_id 再调本工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要怎么改这张图。"},
                "image": {"type": "string", "description": "参考图本地路径、http(s) URL 或 data URL。"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "多张参考图。"},
                "size": {"type": "string"},
                "task_id": {"type": "string"},
                "wait_seconds": {"type": "integer"},
            },
        },
    },
    {
        "name": "muse_video",
        "description": "用网关提交文生视频或图生视频，并轮询到结束或超时。参考图的第一张作为起始画面。不要换成别的视频模型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "size": {"type": "string", "description": "16:9 或 9:16。"},
                "duration": {"type": "integer", "description": "请求时长，常用 5、6、8、10 秒。实际成片以上游为准。"},
                "image": {"type": "string", "description": "可选首帧或参考图。"},
                "images": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string", "description": "续查时传入，不新建任务。"},
                "wait_seconds": {"type": "integer", "description": "最长等待秒数，默认 600。"},
            },
        },
    },
    {
        "name": "muse_video_status",
        "description": "查询一个已提交的 Muse 视频任务。默认只查一次。不要用它重新生成。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "wait_seconds": {"type": "integer", "description": "额外等待秒数，默认 0。"},
            },
            "required": ["task_id"],
        },
    },
]


def handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method and method.startswith("notifications/"):
        return
    if msg_id is None:
        return
    if method == "initialize":
        reply(msg_id, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "muse", "version": "1.0.0"},
        })
        return
    if method == "ping":
        reply(msg_id, {})
        return
    if method == "tools/list":
        reply(msg_id, {"tools": SCHEMAS})
        return
    if method == "tools/call":
        name = params.get("name")
        fn = TOOLS.get(name)
        if fn is None:
            reply(msg_id, tool_text({"ok": False, "error": f"未知工具 {name}"}, is_error=True))
            return
        try:
            reply(msg_id, fn(params.get("arguments") or {}))
        except Exception as exc:  # noqa: BLE001
            log(f"{name} failed: {exc}")
            reply(msg_id, tool_text({"ok": False, "error": str(exc)}, is_error=True))
        return
    if method in ("resources/list", "prompts/list"):
        reply(msg_id, {method.split("/")[0]: []})
        return
    reply_error(msg_id, -32601, f"method 不支持: {method}")


def main() -> None:
    log(f"muse mcp ready base={BASE} media={MEDIA_DIR}")
    while True:
        msg = read_message()
        if msg is None:
            return
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001
            log(f"mcp error: {exc}")
            if msg.get("id") is not None:
                reply_error(msg["id"], -32603, str(exc))


if __name__ == "__main__":
    main()
