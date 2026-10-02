"""The plugin's MCP server: realm control tools plus the driver's own tools.

Claude Code starts one stdio server per session, so this process *is* the
session as far as realms are concerned: it owns the session's realm and stops
its compute when the session (and therefore stdin) ends.
"""

import base64
import json
import logging
import os
import signal
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from realms_core.lifecycle import RealmError

from .drivers import DriverHub, DriverUnavailable, FILE_OUTPUT_PROPERTIES, expose, load_catalog
from .host import ClaudeSettings, data_home, session_owner
from .service import RealmOff, RealmService, SetupRequired, describe
from .setup import Jobs

log = logging.getLogger("claude_realms")

ACTIONS = ("status", "on", "off", "stop", "size", "shot", "watch", "push", "pull", "launch",
           "repair", "list", "delete", "clean", "doctor", "setup", "driver", "control", "events")

REALM_TOOL = types.Tool(
    name="realm",
    description=(
        "This session's private Linux desktop (a 'realm'): start, stop, inspect or capture it. "
        "Use a realm to run and test GUI apps without touching the person's own screen. "
        "Desktop tools (screenshots, clicks, typing) and realm_exec/realm_launch start the realm "
        "on first use. Actions: status; on [kind: realm|omarchy]; off (disable for this session); "
        "stop (power down, keep its home); size WIDTHxHEIGHT; shot (screenshot); watch (browser "
        "link for the person); push/pull (Omarchy VM file copy); launch (a GUI program); repair (reconnect "
        "the desktop driver only, keeping the desktop and its apps); list (realms and VM disks kept "
        "on this machine); doctor; setup; driver "
        "(status|check|update|rollback)."),
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS), "default": "status"},
            "kind": {"type": "string", "enum": ["realm", "omarchy"],
                     "description": "Realm kind for 'on' and 'setup'"},
            "size": {"type": "string", "pattern": "^[0-9]+x[0-9]+$", "description": "For 'size'"},
            "source": {"type": "string", "description": "For push/pull"},
            "destination": {"type": "string", "description": "For push/pull"},
            "control": {"type": "boolean",
                        "description": "watch: allow the person to take control; "
                                       "control: whether the person holds control now"},
            "operation": {"type": "string", "enum": ["status", "check", "update", "rollback"],
                          "description": "For 'driver'"},
            "command": {"type": "string", "description": "For 'launch': a program and arguments"},
            "id": {"type": "string", "description": "For 'delete' (the person's command only)"},
            "separate": {"type": "boolean",
                         "description": "on: a subagent's own clean realm instead of sharing the session's"},
        },
        "additionalProperties": False,
    },
)

EXEC_TOOL = types.Tool(
    name="realm_exec",
    description=(
        "Run a shell command inside this session's private realm desktop and wait for it "
        "(bash -lc). The command sees the realm's own display, D-Bus and HOME, never the "
        "person's desktop. Use it to build, install or inspect things for the app under test. "
        "In an Omarchy VM it runs in the guest."),
    inputSchema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 600, "default": 60},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)

LAUNCH_TOOL = types.Tool(
    name="realm_launch",
    description=(
        "Start a GUI program inside this session's private realm desktop without waiting for "
        "it to exit (e.g. 'gtk3-demo', 'firefox --new-instance', './build/myapp'). Then use the "
        "desktop tools to see and drive it."),
    inputSchema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Program and arguments, shell-quoted"},
            "cwd": {"type": "string"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)


def text(value):
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, default=str)
    return types.TextContent(type="text", text=value)


def error(message):
    return types.CallToolResult(content=[text(message)], isError=True)


class RealmServer:
    def __init__(self):
        self.home = data_home()
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.settings = ClaudeSettings.load(self.home)
        self.service = RealmService(self.home, session_owner(), self.settings)
        self.jobs = Jobs()
        self.catalog = []
        self.offered = {}
        self.controlled = False
        self.hub = None
        self.session = None
        # Subagents that asked for a clean realm of their own: agent id -> service.
        self.separate = {}
        self.server = Server("claude-realms", version=_version(),
                             instructions=(
                                 "Realms are private Linux desktops for this session. Use the realm "
                                 "tools to test GUI apps; they never act on the person's own screen."))
        self.server.list_tools()(self.list_tools)
        self.server.call_tool(validate_input=False)(self.call_tool)

    # ------------------------------------------------------------- catalog

    async def refresh_catalog(self, *, notify=True):
        try:
            self.catalog = await load_catalog(self.home)
        except Exception as exc:  # noqa: BLE001 - a broken driver must not take down realm control
            log.warning("driver catalog unavailable: %s", exc)
            self.catalog = []
        self.offered = {tool.name: tool for tool in expose(self.catalog, self.settings)}
        if notify and self.session is not None:
            try:
                await self.session.send_tool_list_changed()
            except Exception:  # noqa: BLE001
                pass

    async def list_tools(self):
        self._remember_session()
        return [REALM_TOOL, EXEC_TOOL, LAUNCH_TOOL, *self.offered.values()]

    def _remember_session(self):
        try:
            self.session = self.server.request_context.session
        except LookupError:
            pass

    # ---------------------------------------------------------------- calls

    def service_for(self, arguments):
        """The realm a call acts on: the session's, or a subagent's own one.

        The mod tags a subagent's calls with ``_agent`` (only Claude Code knows
        which loop made a call). A subagent shares the session's realm unless
        it asked for a separate one.
        """
        agent = arguments.pop("_agent", None)
        return self.separate.get(agent, self.service) if agent else self.service

    async def call_tool(self, name, arguments, *, person=False):
        self._remember_session()
        arguments = dict(arguments or {})
        try:
            if name == "realm":
                return await self.realm(arguments, person=person)
            service = self.service_for(arguments)
            await self._auto_setup(service)
            if name == "realm_exec":
                result = await anyio.to_thread.run_sync(
                    lambda: service.exec(arguments["command"], cwd=arguments.get("cwd"),
                                         timeout=arguments.get("timeout", 60)))
                failed = result.get("returncode") not in (0, None)
                return types.CallToolResult(content=[text(result)], isError=failed)
            if name == "realm_launch":
                result = await anyio.to_thread.run_sync(
                    lambda: service.launch(arguments["command"], cwd=arguments.get("cwd")))
                return types.CallToolResult(content=[text(result)])
            if name in self.offered:
                return await self.forward(name, arguments, service)
            return error("Unknown tool " + name)
        except (SetupRequired, RealmOff) as exc:
            return error(str(exc))
        except (RealmError, DriverUnavailable, ValueError, OSError) as exc:
            return error(type(exc).__name__ + ": " + str(exc))

    async def forward(self, name, arguments, service=None):
        service = service or self.service
        if self.controlled:
            return error("The person has taken control of the realm desktop. Wait for them to "
                         "hand it back (they will tell you), or ask what they need.")
        for key in FILE_OUTPUT_PROPERTIES:
            if key in arguments:
                return error(key + " is not available here; results are returned inline")
        record = await anyio.to_thread.run_sync(lambda: service.ensure(agent=True))
        upstream = name[len(self.settings.tool_prefix):] if self.settings.tool_prefix else name
        session = await self.hub.session_for(record, service)
        try:
            return await session.call(upstream, arguments)
        except Exception:
            # One reconnect: the realm may have been replaced underneath us.
            self.hub.close(record["id"])
            record = await anyio.to_thread.run_sync(lambda: service.ensure(agent=True))
            session = await self.hub.session_for(record, service)
            return await session.call(upstream, arguments)

    async def _auto_setup(self, service):
        """First use: install the verified driver (no root) instead of refusing."""
        from realms_core.install_driver import current_driver

        if not self.settings.auto_setup or current_driver(self.home) is not None:
            return
        status = await anyio.to_thread.run_sync(service.setup_status)
        if status["missing"] or status["blockers"] or any("driver" not in step for step in status["steps"]):
            return  # something only the person can provide; the call reports it
        from realms_core import install_driver

        job = self.jobs.get("driver")
        if job is None or job["state"] != "running":
            job = self.jobs.start("driver", lambda progress: install_driver.install(
                self.home, service.config, progress=progress))
        while job["state"] == "running":
            await anyio.sleep(0.2)
        if job["state"] == "done":
            service._note("installed the computer-use driver " + job["result"]["version"])
            await self.refresh_catalog()

    async def realm(self, arguments, *, person=False):
        from .service import clean, delete, human_bytes, inventory

        action = arguments.get("action") or "status"
        agent = arguments.get("_agent")
        if action == "on" and arguments.get("separate"):
            if not agent:
                return error("A separate realm is for a subagent; this session's own realm is its main realm.")
            if agent not in self.separate:
                import hashlib

                owner = self.service.owner + "-" + hashlib.sha256(str(agent).encode()).hexdigest()[:12]
                self.separate[agent] = RealmService(self.home, owner, self.settings)
        service = self.service_for(arguments)
        run = anyio.to_thread.run_sync
        if action == "status":
            result = await run(service.status)
            result["controlled"] = self.controlled
            result["jobs"] = self.jobs.all()
            result["driver_tools"] = len(self.offered)
            return [text(describe(result)), text(result)]
        if action == "list":
            rows = await run(inventory, service)
            lines = [f"{row['id']}  {row['kind']:<10} {row['state']:<8} {human_bytes(row['bytes']):>9}"
                     + ("  (this session)" if row["this_session"] else "") for row in rows]
            total = human_bytes(sum(row["bytes"] for row in rows))
            return [text("\n".join(lines + [f"{len(rows)} kept, {total} on disk."]) if rows
                         else "No realms or VM disks are kept on this machine."), text({"realms": rows})]
        if action in ("delete", "clean"):
            if not person:
                return error("Deleting realm data is the person's decision. Ask them to run "
                             "/realm delete ID or /realm clean.")
            if action == "delete":
                removed = await run(delete, service, arguments.get("id") or "")
                return [text("Deleted " + removed + ".")]
            result = await run(clean, service)
            return [text(f"Deleted {len(result['removed'])} stopped realm(s), freed "
                         f"{human_bytes(result['freed_bytes'])}."
                         + (f" Kept {len(result['kept'])} that could not be deleted." if result["kept"] else "")),
                    text(result)]
        if action in ("on", "shot", "launch", "watch", "size"):
            await self._auto_setup(service)
        if action == "on":
            if arguments.get("kind"):
                await run(service.select, arguments["kind"])
            service.enabled = True
            record = await run(service.ensure)
            return [text(f"{service.kind} {record['id']} is live."),
                    text({"id": record["id"], "kind": service.kind, "vnc_socket": record.get("vnc_socket")})]
        if action == "off":
            self.hub.close()
            self.controlled = False
            await run(service.off)
            return [text("Realm use is off for this session; its desktop is stopped and its home kept.")]
        if action == "stop":
            self.hub.close()
            self.controlled = False
            await run(service.stop)
            return [text("Realm stopped; its home is kept for the next start.")]
        if action == "size":
            record = await run(service.resize, arguments.get("size", ""))
            return [text("Realm resized to " + record["size"])]
        if action == "shot":
            shot = await run(service.shot)
            return [types.ImageContent(type="image", mimeType="image/png",
                                       data=base64.b64encode(shot["png"]).decode()),
                    text({k: shot[k] for k in ("realm_id", "width", "height", "sha256")})]
        if action == "watch":
            ttl = 3600 if person else 300
            result = await run(lambda: service.watch(control=bool(arguments.get("control")), ttl=ttl))
            return [text(f"Open this link in a browser on this machine (valid {result['expires_in'] // 60} minutes): " + result["url"]),
                    text(result)]
        if action in ("push", "pull"):
            if action == "push":
                result = await run(lambda: service.push(arguments["source"], arguments.get("destination")))
            else:
                result = await run(lambda: service.pull(arguments["source"], arguments["destination"]))
            return [text(result)]
        if action == "launch":
            result = await run(lambda: service.launch(arguments.get("command") or ""))
            return [text("Started " + " ".join(result["started"]) + " in the realm."), text(result)]
        if action == "repair":
            record = await run(service.running)
            if record is None:
                return error("No realm is running; repair never creates a replacement.")
            self.hub.close(record["id"])
            return [text("Desktop driver connection reset; the next desktop action reconnects. "
                         "Take a fresh capture before acting again.")]
        if action == "doctor":
            return [text(await run(service.doctor))]
        if action == "setup":
            return await self.setup(arguments)
        if action == "driver":
            return await self.driver(arguments.get("operation") or "status")
        if action == "control":
            self.controlled = bool(arguments.get("control"))
            return [text("The person holds control." if self.controlled else "Control returned to the agent.")]
        if action == "events":
            status = await run(service.status)
            return [text({"events": status["events"], "jobs": self.jobs.all()})]
        return error("Unknown realm action " + action)

    async def setup(self, arguments):
        from realms_core import install_driver

        kind = {"omarchy": "omarchy-vm"}.get(arguments.get("kind"), arguments.get("kind") or self.service.kind)
        status = await anyio.to_thread.run_sync(self.service.setup_status, kind)
        started = []
        if status["driver"] is None:
            def install(progress):
                return install_driver.install(self.home, self.service.config, progress=progress)

            self.jobs.start("driver", install)
            started.append("driver")
        from .service import hermes_bases, import_base

        bases = hermes_bases() if kind == "omarchy-vm" else []
        if kind == "omarchy-vm" and bases and any("base image" in step for step in status["steps"]):
            source = bases[0]["path"]
            self.jobs.start("omarchy-base", lambda progress: import_base(self.service, source, progress))
            started.append("omarchy-base (reusing the base hermes-realms built: " + source + ")")
        elif kind == "omarchy-vm" and any("base image" in step for step in status["steps"]) \
                and not status["blockers"] and not status["missing"]:
            log_path = self.home / "realms" / "logs" / "omarchy-base-install.log"

            def base(progress):
                log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                progress("installing the Omarchy base image; log: " + str(log_path))
                with log_path.open("w") as log:
                    os.chmod(log_path, 0o600)
                    return self.service.vm.install_base(stdout=log)

            self.jobs.start("omarchy-base", base)
            started.append("omarchy-base")
        if started:
            self._after_jobs([name.split(" ")[0] for name in started])
        return [text(status["message"] if not started else
                     "Started: " + ", ".join(started) + ". Check progress with /realm status."),
                text({"setup": status, "started": started})]

    def _after_jobs(self, names):
        async def watch():
            while any((self.jobs.get(n) or {}).get("state") == "running" for n in names):
                await anyio.sleep(0.5)
            if "driver" in names:
                await self.refresh_catalog()

        self.task_group.start_soon(watch)

    async def driver(self, operation):
        from realms_core import install_driver

        home, config = self.home, self.service.config
        if operation == "status":
            return [text(install_driver.current_driver(home) or "No driver installed; run /realm setup.")]
        if operation == "check":
            return [text(await anyio.to_thread.run_sync(
                lambda: install_driver.check_update(home, config, use_cache=False)))]
        if operation == "update":
            self.hub.close()
            self.jobs.start("driver", lambda progress: install_driver.update(home, config, progress=progress))
            self._after_jobs(["driver"])
            return [text("Driver update started; /realm status shows progress.")]
        if operation == "rollback":
            self.hub.close()
            result = await anyio.to_thread.run_sync(install_driver.rollback, home)
            await self.refresh_catalog()
            return [text("Driver rolled back to " + result["version"])]
        return error("Unknown driver operation " + operation)

    # ----------------------------------------------------------------- run

    async def run(self):
        await self.refresh_catalog(notify=False)
        options = self.server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True))
        try:
            async with anyio.create_task_group() as task_group:
                self.task_group = task_group
                self.hub = DriverHub(self.service, task_group)
                task_group.start_soon(self._update_check)
                task_group.start_soon(self._serve_control)
                task_group.start_soon(self._retention)
                async with stdio_server() as (read, write):
                    await self.server.run(read, write, options)
                task_group.cancel_scope.cancel()
        finally:
            if self.hub is not None:
                self.hub.close()
            if self.settings.stop_on_exit:
                with anyio.CancelScope(shield=True):
                    for separate in list(self.separate.values()):
                        await anyio.to_thread.run_sync(separate.shutdown)
                    await anyio.to_thread.run_sync(self.service.shutdown)

    async def _serve_control(self):
        """The person's channel (pane, /realm): same actions, no model tools."""
        from . import control

        async def handle(arguments):
            result = await self.call_tool("realm", dict(arguments or {}), person=True)
            if isinstance(result, types.CallToolResult):
                blocks, failed = result.content, bool(result.isError)
            else:
                blocks, failed = result, False
            return [block.text for block in blocks if block.type == "text"], failed

        try:
            await control.serve(handle, session_id=self.service.owner)
        except OSError as exc:
            log.warning("control socket unavailable: %s", exc)

    async def _retention(self):
        """Delete stopped workspaces nobody used for ``retention_days``."""
        from .service import clean, human_bytes

        days = self.settings.retention_days
        if not days or days <= 0:
            return
        try:
            result = await anyio.to_thread.run_sync(lambda: clean(self.service, older_than_days=days))
        except Exception as exc:  # noqa: BLE001 - housekeeping never blocks a session
            log.warning("realm retention skipped: %s", exc)
            return
        if result["removed"]:
            self.service._note(f"removed {len(result['removed'])} realm(s) unused for {days:g} days, "
                               f"freed {human_bytes(result['freed_bytes'])}")

    async def _update_check(self):
        """Quietly note a newer driver on the configured channel; never auto-install."""
        from realms_core import install_driver

        if not self.service.config.driver.check_updates:
            return
        try:
            result = await anyio.to_thread.run_sync(
                lambda: install_driver.check_update(self.home, self.service.config))
        except Exception:  # noqa: BLE001 - offline is fine
            return
        if result["update_available"] and result["current"]:
            self.service._note(f"driver {result['candidate']} is available on the "
                               f"{result['channel']} channel (/realm driver update)")


def _version():
    try:
        from importlib.metadata import version

        return version("claude-realms")
    except Exception:  # noqa: BLE001
        return "0"


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="claude-realms: %(levelname)s %(message)s")
    # Newer clients probe methods this SDK does not know yet (server/discover);
    # the SDK logs each probe through the root logger. That is not an error.
    logging.getLogger("mcp").setLevel(logging.ERROR)

    class _QuietProbes(logging.Filter):
        def filter(self, record):
            return not record.getMessage().startswith("Failed to validate request")

    for handler in logging.getLogger().handlers:
        handler.addFilter(_QuietProbes())
    server = RealmServer()

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        anyio.run(server.run)
    except KeyboardInterrupt:
        for separate in list(server.separate.values()):
            separate.shutdown()
        server.service.shutdown()
    finally:
        # The MCP library reads stdin in a worker thread that may still be
        # blocked in read(); a normal interpreter teardown then aborts while
        # closing stdin (a harmless but noisy SIGABRT core dump). Everything is
        # cleaned up by now, so leave directly.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)


if __name__ == "__main__":
    main()
