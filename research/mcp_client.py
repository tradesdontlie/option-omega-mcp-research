"""Use Codex's authenticated MCP transport without reading credentials."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone


class ToolError(RuntimeError):
    """An explicit rejection returned by the MCP server."""


def unwrap(result):
    if result.get("isError"):
        raise ToolError(json.dumps(result))
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return {"text": texts[0]}
    return {"content": result.get("content", [])}


def reserve_call(tool):
    """Coordinate account-level pacing across independent local runners."""
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "omega-research"
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "rate-lock"
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError("MCP rate lock held; inspect owner before retrying")
            time.sleep(0.1)
    try:
        path = root / "rate-state.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        scheduled = max(time.time(), state.get("next_call", 0))
        if tool in {"run_backtest", "run_portfolio"}:
            scheduled = max(scheduled, state.get("next_start", 0))
            state["next_start"] = scheduled + 11
        state["next_call"] = scheduled + 1.2
        path.write_text(json.dumps(state))
    finally:
        lock.rmdir()
    time.sleep(max(0, scheduled - time.time()))


class OmegaClient:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        launcher = shutil.which("codex.cmd") or shutil.which("codex")
        if not launcher:
            raise RuntimeError("Codex CLI not found")
        base = Path(launcher).parent
        node = base / "node.exe"
        cli = base / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if not node.exists() or not cli.exists():
            raise RuntimeError("Set up the installed npm Codex CLI before running research")
        self.process = subprocess.Popen(
            [str(node), str(cli), "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        )
        self.responses = queue.Queue()
        self.sequence = 0
        self.last_call = 0.0
        self.last_start = 0.0
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc("initialize", {
            "clientInfo": {"name": "omega-research", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        })
        # This allocates a transient tool context, not an agent turn.
        context = self.rpc("thread/start", {
            "ephemeral": True, "cwd": os.getcwd(), "approvalPolicy": "never",
        })
        self.thread_id = context["thread"]["id"]

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except ValueError:
                pass
        self.responses.put({"transportClosed": True})

    def rpc(self, method, params):
        self.sequence += 1
        sequence = self.sequence
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"id": sequence, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 180
        while True:
            response = self.responses.get(timeout=max(0.01, deadline - time.monotonic()))
            if response.get("transportClosed"):
                raise RuntimeError("Codex MCP transport closed")
            if response.get("id") == sequence:
                if "error" in response:
                    raise RuntimeError(json.dumps(response["error"]))
                return response["result"]
            if time.monotonic() > deadline:
                raise TimeoutError(method)

    def call(self, tool, arguments=None, server="option-omega"):
        arguments = arguments or {}
        if server == "option-omega":
            reserve_call(tool)
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "server": server,
                  "tool": tool, "arguments": arguments}
        try:
            result = self.rpc("mcpServer/tool/call", {
                "threadId": self.thread_id, "server": server, "tool": tool, "arguments": arguments,
            })
            record["response"] = result
        except Exception as error:
            record["error"] = str(error)
            raise
        finally:
            with (self.output / "mcp-journal.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
        return unwrap(result)

    def inventory(self):
        result = self.rpc("mcpServerStatus/list", {
            "serverName": "option-omega", "threadId": self.thread_id, "detail": "toolsAndAuthOnly",
        })
        (self.output / "tool-schemas.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
