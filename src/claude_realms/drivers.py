"""The computer-use driver, passed through: its own tools, its own schemas.

Nothing about the driver's tool surface is written here. The catalog is what
the installed driver reports over MCP (``tools/list``), cached per driver
version; a call is forwarded to a driver session bound to this session's realm
and its result is returned untouched. A new driver release that adds, renames
or reshapes tools shows up in Claude Code with no change to this plugin.

A driver session is the driver's own daemon (``serve``) plus an MCP client
(``mcp``) on a private socket inside the realm runtime, both started through
the realm-bound launcher (bubblewrap for a regular realm, an SSH proxy for an
Omarchy VM). The launch shape is read from the driver's ``manifest``.
"""

from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

# Properties that make the driver write files inside its sandbox, which the
# model could never read back. Results stay inline instead.
FILE_OUTPUT_PROPERTIES = ("screenshot_out_file", "screenshot_file_path")


class DriverUnavailable(RuntimeError):
    pass


def _neutral_env():
    scratch = tempfile.mkdtemp(prefix="realms-driver-catalog-")
    return {"PATH": "/usr/bin:/bin", "HOME": scratch, "XDG_CONFIG_HOME": scratch,
            "XDG_CACHE_HOME": scratch, "XDG_STATE_HOME": scratch, "XDG_DATA_HOME": scratch,
            "LANG": "C.UTF-8", "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}, scratch


def _strip_file_outputs(schema):
    if not isinstance(schema, dict):
        return schema
    schema = json.loads(json.dumps(schema))
    for name in FILE_OUTPUT_PROPERTIES:
        schema.get("properties", {}).pop(name, None)
        if name in schema.get("required", []):
            schema["required"].remove(name)
    return schema


async def load_catalog(home, *, mcp_args=None):
    """The installed driver's tools (cached per version), or ``[]`` when none."""
    from realms_core.install_driver import current_driver, manifest

    driver = current_driver(home)
    if driver is None:
        return []
    cache = Path(driver["path"]).parent / "mcp-tools.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("binary_sha256") == driver["binary_sha256"]:
            return [types.Tool.model_validate(tool) for tool in cached["tools"]]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    if mcp_args is None:
        described = await anyio.to_thread.run_sync(manifest, driver["path"])
        mcp_args = list((described.get("mcp_invocation") or {}).get("args") or ["mcp"])
    env, scratch = _neutral_env()
    try:
        params = StdioServerParameters(command=driver["path"], args=mcp_args, env=env)
        with anyio.fail_after(60):
            async with stdio_client(params, errlog=open(os.devnull, "w")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    from realms_core.lifecycle import atomic_json

    atomic_json(cache, {"binary_sha256": driver["binary_sha256"],
                        "tools": [tool.model_dump(mode="json", exclude_none=True) for tool in tools]})
    return tools


def expose(tools, settings):
    """The catalog as offered to the model: excluded tools dropped, file outputs removed."""
    offered = []
    for tool in tools:
        if tool.name in settings.exclude_tools:
            continue
        data = tool.model_dump(exclude_none=True)
        data["name"] = settings.tool_prefix + tool.name
        data["inputSchema"] = _strip_file_outputs(tool.inputSchema)
        description = data.get("description") or ""
        data["description"] = ("Acts inside this session's private realm desktop, never on the "
                               "person's own screen. " + description)
        offered.append(types.Tool.model_validate(data))
    return offered


def _flags(described, verb):
    for command in described.get("subcommands", []):
        if command.get("name") == verb:
            return {arg.get("name") for arg in command.get("args", [])}
    return set()


class DriverSession:
    """The driver's daemon and an MCP client for one realm incarnation."""

    def __init__(self, record, launcher, env, *, described, overlay, cursor_theme, permission_mode):
        self.record = record
        self.key = (record["id"], record.get("generation"), record.get("invocation_id")
                    or record.get("compute_generation"))
        self.launcher = launcher
        self.env = env
        self.described = described
        self.overlay = overlay
        self.cursor_theme = cursor_theme
        self.permission_mode = permission_mode
        self.socket = Path(record["runtime_dir"]) / ("hc-" + secrets.token_hex(6) + ".sock")
        self.session = None
        self._stack = None
        self._serve = None
        self._closed = anyio.Event()
        self._ready = anyio.Event()
        self._error = None

    def _serve_args(self):
        flags = _flags(self.described, "serve")
        if not {"--socket", "--embedded"} <= flags:
            return None
        args = ["serve", "--embedded", "--socket", str(self.socket)]
        if not self.overlay and "--no-overlay" in flags:
            args.append("--no-overlay")
        if self.overlay and self.cursor_theme and "--cursor-theme" in flags:
            args += ["--cursor-theme", self.cursor_theme]
        if self.permission_mode and "--permission-mode" in flags:
            args += ["--permission-mode", self.permission_mode]
        return args

    def _mcp_args(self):
        flags = _flags(self.described, "mcp")
        if self._serve is not None:
            return ["mcp", "--embedded", "--socket", str(self.socket)]
        invocation = (self.described.get("mcp_invocation") or {}).get("args") or ["mcp"]
        return list(invocation) + (["--direct"] if "--direct" in flags and "--direct" not in invocation else [])

    async def run(self, task_status=anyio.TASK_STATUS_IGNORED):
        """Own the daemon and client until ``close``; report readiness once."""
        try:
            async with AsyncExitStack() as stack:
                serve = self._serve_args()
                if serve is not None:
                    self._serve = await anyio.open_process(
                        [self.launcher, *serve], env=self.env,
                        stdin=None, stdout=None, stderr=None)
                    with anyio.fail_after(30):
                        while not self.socket.exists():
                            if self._serve.returncode is not None:
                                raise DriverUnavailable("driver daemon exited during startup")
                            await anyio.sleep(0.05)
                params = StdioServerParameters(command=self.launcher, args=self._mcp_args(), env=self.env)
                read, write = await stack.enter_async_context(
                    stdio_client(params, errlog=open(os.devnull, "w")))
                self.session = await stack.enter_async_context(ClientSession(read, write))
                with anyio.fail_after(60):
                    await self.session.initialize()
                self._ready.set()
                task_status.started()
                await self._closed.wait()
        except Exception as exc:  # noqa: BLE001 - surfaced to the waiting caller
            self._error = exc
            self._ready.set()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            raise
        finally:
            self.session = None
            await self._stop_serve()

    async def _stop_serve(self):
        process, self._serve = self._serve, None
        if process is None:
            return
        if process.returncode is None:
            try:
                with anyio.move_on_after(5):
                    await anyio.run_process([self.launcher, "stop", "--socket", str(self.socket)],
                                            env=self.env, check=False)
            except OSError:
                pass
            with anyio.move_on_after(5):
                await process.wait()
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(3):
                    await process.wait()
            if process.returncode is None:
                process.kill()
        await process.aclose()

    async def wait_ready(self):
        await self._ready.wait()
        if self._error is not None or self.session is None:
            raise DriverUnavailable("computer-use driver did not start: " + str(self._error))

    async def call(self, name, arguments):
        if self.session is None:
            raise DriverUnavailable("driver session is closed")
        return await self.session.call_tool(name, arguments)

    def close(self):
        self._closed.set()


class DriverHub:
    """Driver sessions per realm incarnation, started on first use."""

    def __init__(self, service, task_group):
        self.service = service
        self.task_group = task_group
        self.sessions = {}
        self._lock = anyio.Lock()

    async def session_for(self, record, service=None):
        from realms_core.install_driver import driver_executable, manifest

        service = service or self.service

        key = (record["id"], record.get("generation"), record.get("invocation_id")
               or record.get("compute_generation"))
        async with self._lock:
            current = self.sessions.get(record["id"])
            if current is not None and current.key == key and current.session is not None:
                return current
            if current is not None:
                current.close()
                self.sessions.pop(record["id"], None)
            launcher = await anyio.to_thread.run_sync(service.driver_launcher, record)
            described = await anyio.to_thread.run_sync(manifest, driver_executable(service.home))
            if record.get("kind") == "omarchy-vm" or str(record["id"]).startswith("v-"):
                env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
                       "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}
            else:
                env = dict(await anyio.to_thread.run_sync(service.manager.env, record["id"]))
                env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
            config = service.config
            session = DriverSession(
                record, launcher, env, described=described,
                overlay=bool(record.get("overlay", config.overlay)),
                cursor_theme=record.get("cursor_theme", config.cursor_theme),
                permission_mode=service.settings.driver_permission_mode)
            self.sessions[record["id"]] = session
            self.task_group.start_soon(session.run)
        await session.wait_ready()
        return session

    def close(self, realm_id=None):
        for key in list(self.sessions):
            if realm_id is None or key == realm_id:
                self.sessions.pop(key).close()
