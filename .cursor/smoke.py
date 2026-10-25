#!/usr/bin/env python3
"""Run the comment-collector extension end to end in headless Chrome.

The live Bilibili page is not required. content.js walks open shadow roots on
bili-comments web components, so this builds that tree, then drives popup.js
through the JSON and Markdown download paths. It also loads the unpacked
extension to confirm Chrome accepts the manifest.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = ROOT / "bilibili"
ARTIFACTS = Path("/opt/cursor/artifacts")

CONTENT_FIXTURE = r"""
window.__messages = [];
window.__onMessage = null;
window.chrome = {
  runtime: {
    sendMessage(message) { window.__messages.push(message); },
    onMessage: { addListener(fn) { window.__onMessage = fn; } }
  }
};

function richHost(tag, text) {
  const el = document.createElement(tag);
  const shadow = el.attachShadow({ mode: 'open' });
  const rich = document.createElement('bili-rich-text');
  const richShadow = rich.attachShadow({ mode: 'open' });
  richShadow.innerHTML = '<style>.hidden{}</style><span></span>';
  richShadow.querySelector('span').textContent = text;
  shadow.appendChild(rich);
  return el;
}

const root = document.createElement('bili-comments');
const rootShadow = root.attachShadow({ mode: 'open' });
document.body.appendChild(root);

const thread = document.createElement('bili-comment-thread-renderer');
const threadShadow = thread.attachShadow({ mode: 'open' });
const main = richHost('div', '主评论甲');
main.id = 'comment';
threadShadow.appendChild(main);

const replies = document.createElement('bili-comment-replies-renderer');
const repliesShadow = replies.attachShadow({ mode: 'open' });
const expand = document.createElement('bili-text-button');
expand.textContent = '点击查看更多';
const expandShadow = expand.attachShadow({ mode: 'open' });
const expandButton = document.createElement('button');
expandShadow.appendChild(expandButton);
expandButton.addEventListener('click', () => {
  expand.remove();
  repliesShadow.appendChild(richHost('bili-comment-reply-renderer', '这是一条回复'));
});
repliesShadow.appendChild(expand);

const pagination = document.createElement('bili-pagination');
const paginationShadow = pagination.attachShadow({ mode: 'open' });
const next = document.createElement('button');
next.setAttribute('aria-label', '下一页');
next.addEventListener('click', () => {
  next.disabled = true;
  repliesShadow.appendChild(richHost('bili-comment-reply-renderer', '第二页回复'));
});
paginationShadow.appendChild(next);
repliesShadow.appendChild(pagination);
threadShadow.appendChild(replies);
rootShadow.appendChild(thread);

const second = document.createElement('bili-comment-thread-renderer');
const secondShadow = second.attachShadow({ mode: 'open' });
const secondMain = richHost('div', '主评论乙');
secondMain.id = 'comment';
secondShadow.appendChild(secondMain);
rootShadow.appendChild(second);
"""

POPUP_STUB = r"""
window.__downloads = [];
window.__tabUrl = 'https://www.bilibili.com/video/BV1smoke';
window.__popupListener = null;
window.__lastText = '';
const OriginalBlob = window.Blob;
window.Blob = class extends OriginalBlob {
  constructor(parts, options) {
    super(parts, options);
    window.__lastText = (parts || []).map((part) => String(part)).join('');
  }
};
const originalCreateObjectURL = URL.createObjectURL.bind(URL);
URL.createObjectURL = (blob) => {
  try { return originalCreateObjectURL(blob); }
  catch (err) { return 'blob:smoke'; }
};
window.chrome = {
  tabs: {
    query: async () => [{ id: 7, url: window.__tabUrl }],
    sendMessage: async () => {}
  },
  scripting: {
    executeScript: async () => {}
  },
  downloads: {
    download(opts, cb) {
      window.__downloads.push({ filename: opts.filename, text: window.__lastText });
      if (typeof cb === 'function') cb();
    }
  },
  runtime: {
    onMessage: {
      addListener(fn) { window.__popupListener = fn; }
    }
  }
};
"""

POPUP_TRAILER = r"""
document.addEventListener('DOMContentLoaded', () => {
  const status = document.getElementById('status');
  const report = { steps: [] };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (predicate) => {
    for (let i = 0; i < 40; i++) {
      if (predicate()) return;
      await wait(25);
    }
    throw new Error('timed out waiting for popup. status=' + status.textContent);
  };

  (async () => {
    try {
      window.__tabUrl = 'https://example.com/';
      document.getElementById('startJsonButton').click();
      await wait(30);
      report.steps.push({ name: 'reject-non-video', status: status.textContent });

      window.__tabUrl = 'https://www.bilibili.com/video/BV1smoke';
      document.getElementById('startJsonButton').click();
      await waitFor(() => status.textContent.includes('脚本注入成功'));
      report.steps.push({ name: 'inject-json', status: status.textContent });

      window.__popupListener({
        action: 'extractionComplete',
        data: [{ m: '主评', r: ['回复A'] }]
      });
      await waitFor(() => window.__downloads.length >= 1 && status.textContent.includes('下载完成'));
      report.steps.push({
        name: 'download-json',
        status: status.textContent,
        download: window.__downloads[0]
      });

      document.getElementById('startMdButton').click();
      await waitFor(() => status.textContent.includes('脚本注入成功'));
      window.__popupListener({
        action: 'extractionComplete',
        data: [{ m: '主评', r: ['回复A\n第二行'] }]
      });
      await waitFor(() => window.__downloads.length >= 2 && status.textContent.includes('下载完成'));
      report.steps.push({
        name: 'download-md',
        status: status.textContent,
        download: window.__downloads[1]
      });
      report.ok = true;
    } catch (err) {
      report.ok = false;
      report.error = String(err);
      report.downloads = window.__downloads;
    }
    document.getElementById('result').textContent = JSON.stringify(report);
  })();
});
"""


def find_chrome() -> str:
    for candidate in (
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/google-chrome",
        shutil.which("google-chrome-stable"),
        shutil.which("google-chrome"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("google-chrome is not installed")


def escape_script(source: str) -> str:
    return source.replace("</", "<\\/")


def run_chrome(chrome: str, html: Path, screenshot: Path | None, dump_path: Path) -> str:
    cmd = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--virtual-time-budget=20000",
        "--timeout=20000",
        f"--screenshot={screenshot}" if screenshot else "--screenshot=/dev/null",
        "--window-size=480,720",
        "--dump-dom",
        html.as_uri(),
    ]
    # --screenshot is always passed; /dev/null keeps the run focused on DOM when unused.
    if screenshot is None:
        cmd = [arg for arg in cmd if not arg.startswith("--screenshot=")]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=60)
    dump_path.write_text(completed.stdout)
    if completed.returncode != 0:
        raise SystemExit(
            f"chrome exited {completed.returncode}\n{completed.stderr[-4000:]}\n{completed.stdout[-2000:]}"
        )
    return completed.stdout


def parse_result(dom: str) -> dict:
    marker = '<pre id="result">'
    start = dom.find(marker)
    if start < 0:
        raise SystemExit(f"result element missing from chrome DOM:\n{dom[-3000:]}")
    start += len(marker)
    end = dom.find("</pre>", start)
    if end < 0:
        raise SystemExit("result element was not closed")
    raw = dom[start:end]
    raw = raw.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return json.loads(raw)


def content_page(work: Path) -> Path:
    content_js = escape_script((EXT_DIR / "content.js").read_text())
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Bilibili comment extraction</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 16px; }}
  h1 {{ color: #00a1d6; font-size: 18px; }}
  pre {{ white-space: pre-wrap; background: #f4f4f4; padding: 12px; }}
</style></head>
<body>
  <h1>B站评论采集 smoke</h1>
  <p id="summary">running content.js against a shadow-DOM fixture</p>
  <pre id="result">PENDING</pre>
  <script>
{CONTENT_FIXTURE}
  </script>
  <script>
{content_js}
  </script>
  <script>
    if (!window.__onMessage) throw new Error('content.js did not register a listener');
    window.__onMessage({{ action: 'startExtraction' }});
    const timer = setInterval(() => {{
      const done = window.__messages.find((message) =>
        message.action === 'extractionComplete' || message.action === 'extractionFailed');
      if (!done) return;
      clearInterval(timer);
      document.getElementById('summary').textContent =
        done.action === 'extractionComplete'
          ? ('collected ' + done.data.length + ' threads')
          : ('failed: ' + done.message);
      document.getElementById('result').textContent = JSON.stringify(done);
    }}, 20);
  </script>
</body></html>
"""
    path = work / "content.html"
    path.write_text(html)
    return path


def popup_page(work: Path) -> Path:
    html = (EXT_DIR / "popup.html").read_text()
    popup_js = escape_script((EXT_DIR / "popup.js").read_text())
    injection = (
        "<script>\n" + POPUP_STUB + "\n</script>\n"
        "<script>\n" + popup_js + "\n</script>\n"
        "<script>\n" + POPUP_TRAILER + "\n</script>\n"
        '<pre id="result">PENDING</pre>\n'
    )
    if '<script src="popup.js"></script>' not in html:
        raise SystemExit("popup.html no longer loads popup.js from a script tag")
    html = html.replace('<script src="popup.js"></script>', injection)
    path = work / "popup.html"
    path.write_text(html)
    return path


def assert_content(payload: dict) -> None:
    if payload.get("action") != "extractionComplete":
        raise SystemExit(f"content extraction failed: {payload}")
    expected = [
        {"m": "主评论甲", "r": ["这是一条回复", "这是一条回复", "第二页回复"]},
        {"m": "主评论乙", "r": []},
    ]
    if payload.get("data") != expected:
        raise SystemExit(f"unexpected comments:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
    print("content.js collected 2 threads, including expanded and paged replies")


def assert_popup(payload: dict) -> None:
    if not payload.get("ok"):
        raise SystemExit(f"popup flow failed: {json.dumps(payload, ensure_ascii=False)}")
    steps = {step["name"]: step for step in payload["steps"]}
    rejected = steps["reject-non-video"]["status"]
    if rejected != "请在B站视频页面使用此插件。":
        raise SystemExit(f"non-video tab was not rejected: {rejected}")
    if "脚本注入成功" not in steps["inject-json"]["status"]:
        raise SystemExit(f"script injection status missing: {steps['inject-json']}")

    json_download = steps["download-json"]["download"]
    if not json_download["filename"].startswith("bilibili_comments_") or not json_download["filename"].endswith(".json"):
        raise SystemExit(f"unexpected json filename: {json_download['filename']}")
    if json.loads(json_download["text"]) != [{"m": "主评", "r": ["回复A"]}]:
        raise SystemExit(f"unexpected json body: {json_download['text']}")

    md_download = steps["download-md"]["download"]
    if not md_download["filename"].endswith(".md"):
        raise SystemExit(f"unexpected markdown filename: {md_download['filename']}")
    markdown = md_download["text"]
    for snippet in ("# B站评论采集", "[M]: 主评", "> [R]: 回复A", "> 第二行"):
        if snippet not in markdown:
            raise SystemExit(f"markdown missing {snippet!r}:\n{markdown}")
    print("popup.js rejected a non-video tab, then downloaded JSON and Markdown")


class Cdp:
    """Minimal Chrome DevTools Protocol client for the browser websocket."""

    def __init__(self, debugger_url: str):
        rest = debugger_url[len("ws://"):]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += self.sock.recv(4096)
        if b" 101 " not in buffer.split(b"\r\n", 1)[0]:
            raise SystemExit(f"websocket handshake failed: {buffer[:200]!r}")
        self.sock.settimeout(15)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        self._send(json.dumps(message))
        while True:
            frame = self._recv()
            if frame.get("id") == self.next_id:
                if "error" in frame:
                    raise SystemExit(f"CDP {method} failed: {frame['error']}")
                return frame["result"]

    def _send(self, text: str) -> None:
        data = text.encode()
        header = bytearray([0x81])
        length = len(data)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv(self) -> dict:
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 1:
                return json.loads(payload.decode())
            if opcode == 8:
                raise SystemExit("Chrome closed the DevTools websocket")

    def _read(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self.sock.recv(size - len(buffer))
            if not chunk:
                raise SystemExit("Chrome DevTools connection closed")
            buffer += chunk
        return buffer


def assert_extension_loads(chrome: str, work: Path) -> None:
    # Branded Chrome ignores --load-extension. Load the unpacked folder through
    # the DevTools Extensions domain, then click the real popup.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    profile = work / "chrome-profile"
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--enable-unsafe-extension-debugging",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        version_url = f"http://127.0.0.1:{port}/json/version"
        debugger_url = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(version_url, timeout=1) as response:
                    debugger_url = json.load(response)["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.1)
        if not debugger_url:
            raise SystemExit("Chrome remote debugging did not start")
        cdp = Cdp(debugger_url)
        loaded = cdp.call("Extensions.loadUnpacked", {"path": str(EXT_DIR)})
        extension_id = loaded["id"]
        cdp.call("Target.createTarget", {"url": "https://example.com/"})
        popup = cdp.call("Target.createTarget", {"url": f"chrome-extension://{extension_id}/popup.html"})
        attached = cdp.call("Target.attachToTarget", {"targetId": popup["targetId"], "flatten": True})
        session = attached["sessionId"]
        cdp.call("Runtime.enable", session=session)
        cdp.call("Page.enable", session=session)
        time.sleep(0.5)
        status = cdp.call(
            "Runtime.evaluate",
            {
                "expression": """
                  new Promise((resolve) => {
                    const deadline = Date.now() + 3000;
                    const attempt = () => {
                      const button = document.getElementById('startJsonButton');
                      const status = document.getElementById('status');
                      if (button) button.click();
                      const text = status ? status.textContent : '';
                      if (text && text !== '等待开始...') {
                        resolve({
                          title: document.querySelector('h3').textContent,
                          status: text,
                          jsonDisabled: button.disabled
                        });
                        return;
                      }
                      if (Date.now() > deadline) {
                        resolve({
                          title: document.querySelector('h3') ? document.querySelector('h3').textContent : '',
                          status: text,
                          jsonDisabled: !!(button && button.disabled),
                          timedOut: true
                        });
                        return;
                      }
                      setTimeout(attempt, 50);
                    };
                    attempt();
                  })
                """,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session=session,
        )
        value = status["result"]["value"]
        if value.get("title") != "B站评论展开与采集器 V5":
            raise SystemExit(f"unexpected popup title: {value}")
        if value.get("status") != "请在B站视频页面使用此插件。":
            raise SystemExit(f"popup did not reject the non-video tab: {value}")
        if value.get("jsonDisabled"):
            raise SystemExit(f"popup left the button disabled: {value}")
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png"}, session=session)
        (ARTIFACTS / "extension-popup.png").write_bytes(base64.b64decode(screenshot["data"]))
        print(f"unpacked extension loaded in Chrome ({extension_id}) and popup rejected a non-video tab")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    chrome = find_chrome()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bili-smoke-") as temporary:
        work = Path(temporary)
        content_dom = run_chrome(
            chrome,
            content_page(work),
            ARTIFACTS / "content-extraction.png",
            work / "content.dom",
        )
        assert_content(parse_result(content_dom))

        popup_dom = run_chrome(
            chrome,
            popup_page(work),
            ARTIFACTS / "popup-flow.png",
            work / "popup.dom",
        )
        assert_popup(parse_result(popup_dom))
        assert_extension_loads(chrome, work)
    print("smoke ok")


if __name__ == "__main__":
    main()
