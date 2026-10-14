"""Minimal synchronous MCP client for GitHub's MCP server.

Gives the pipeline `call_tool(name, arguments)` over MCP's streamable HTTP transport. Tokens are
read from environment variables and are never logged or written anywhere.

One MCP session per profile is opened on the first call and reused, so the connection cost is paid
once per run. Sessions are closed at exit (or explicitly with close_session()).

Two profiles keep reading history apart from everything that touches pull requests. Each allows
only the tools listed, whatever its token would allow:
  read  GITHUB_TOKEN     read-only token; mining the app repo's history (TDD step 1)
  pr    GITHUB_PR_TOKEN  opening a PR and reading PRs and reviews (TDD steps 5 and 9)

Requires the `mcp` package (py -3 -m pip install mcp). Written against mcp 2.3.0.

Usage:  py -3 mcp_client.py --list-tools     show the tools the server offers (a connector check)
"""

import asyncio
import atexit
import concurrent.futures
import json
import os
import sys
import threading
from dataclasses import dataclass

DEFAULT_URL = "https://api.githubcopilot.com/mcp/"
CALL_TIMEOUT_S = 120


@dataclass(frozen=True)
class Profile:
    name: str
    token_env: str
    allowed_tools: frozenset


READ = Profile("read", "GITHUB_TOKEN", frozenset({"list_commits", "get_commit", "get_file_contents"}))
PR = Profile("pr", "GITHUB_PR_TOKEN", frozenset({"create_pull_request", "list_pull_requests", "pull_request_read"}))


def _require_allowed(profile: Profile, name: str) -> None:
    if name not in profile.allowed_tools:
        raise PermissionError(f"MCP tool {name!r} is not on the {profile.name} profile's allowlist.")


def _settings(profile: Profile = READ) -> tuple[str, dict]:
    token = os.environ.get(profile.token_env)
    if not token:
        raise SystemExit(f"{profile.token_env} is not set.")
    return os.environ.get("MCP_URL", DEFAULT_URL), {"Authorization": f"Bearer {token}"}


class _Connection:
    """One MCP session kept open for the life of the process.

    The SDK's transports must be entered and exited by the same task, so one long-lived task on a
    background event loop owns the session, and callers hand it work from the calling thread.
    """

    def __init__(self, profile: Profile = READ):
        self._profile = profile
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-client", daemon=True)
        self._thread.start()
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._session = None
        self._closing: asyncio.Event | None = None
        self._owner = asyncio.run_coroutine_threadsafe(self._own_session(), self._loop)
        self._ready.result(timeout=CALL_TIMEOUT_S)  # re-raises if the connection failed

    async def _own_session(self):
        # Imported here so the rest of the pipeline works without the mcp package installed.
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        try:
            url, headers = _settings(self._profile)
            self._closing = asyncio.Event()
            async with create_mcp_http_client(headers=headers) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set_result(None)
                        await self._closing.wait()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)  # a startup failure goes to the caller
            else:
                raise

    def run(self, action):
        """Run `action(session)` on the session's loop and return its result."""
        future = asyncio.run_coroutine_threadsafe(action(self._session), self._loop)
        return future.result(timeout=CALL_TIMEOUT_S)

    def close(self):
        if self._closing is not None:
            self._loop.call_soon_threadsafe(self._closing.set)
        try:
            self._owner.result(timeout=30)
        except BaseException:
            pass  # closing is best effort
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


_connections: dict[str, _Connection] = {}
_atexit_registered = False


def _get_connection(profile: Profile = READ) -> _Connection:
    global _atexit_registered
    if profile.name not in _connections:
        _connections[profile.name] = _Connection(profile)
        if not _atexit_registered:
            atexit.register(close_session)
            _atexit_registered = True
    return _connections[profile.name]


def close_session() -> None:
    """Close every open MCP session. Safe to call more than once."""
    while _connections:
        _, conn = _connections.popitem()
        conn.close()


def list_tools() -> list[tuple[str, str]]:
    async def action(session):
        result = await session.list_tools()
        return [(t.name, (t.description or "").splitlines()[0] if t.description else "")
                for t in result.tools]
    return _get_connection().run(action)


def call_tool(name: str, arguments: dict, profile: Profile = READ):
    """Call one MCP tool and return its JSON payload."""
    _require_allowed(profile, name)

    async def action(session):
        result = await session.call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") or "" for c in result.content)
        if result.is_error:
            raise RuntimeError(f"MCP tool {name} failed: {text[:300]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if profile is READ:
                raise  # history mining expects JSON; anything else is a real error
            return {"text": text}  # some write tools answer in plain text
    return _get_connection(profile).run(action)


def call_tool_text(name: str, arguments: dict, profile: Profile = READ) -> tuple[bool, str]:
    """Call one MCP tool and return (is_error, raw text), for tools that don't return JSON."""
    _require_allowed(profile, name)

    async def action(session):
        result = await session.call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") or "" for c in result.content)
        return bool(result.is_error), text
    return _get_connection(profile).run(action)


def read_file(owner: str, repo: str, path: str, ref: str) -> str | None:
    """Text of `path` at `ref` via get_file_contents, or None if it doesn't exist or isn't text.

    The server returns a status line as text content and the file body as a separate embedded
    resource, so the body is read from the resource.
    """
    _require_allowed(READ, "get_file_contents")

    async def action(session):
        result = await session.call_tool(
            "get_file_contents", {"owner": owner, "repo": repo, "path": path, "ref": ref})
        if result.is_error:
            return None
        for item in result.content:
            text = getattr(getattr(item, "resource", None), "text", None)
            if text is not None:
                return text
        return None
    return _get_connection(READ).run(action)


if __name__ == "__main__":
    if sys.argv[1:] == ["--list-tools"]:
        try:
            for tool_name, description in list_tools():
                print(f"{tool_name}: {description}")
        finally:
            close_session()
    else:
        print(__doc__)
