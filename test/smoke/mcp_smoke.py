"""Drive the plugin's MCP server like Claude Code would, over stdio."""
import anyio, base64, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ART = os.environ.get("ARTIFACTS", "/tmp")

def show(label, result):
    parts = []
    for c in result.content:
        if c.type == "text":
            parts.append(c.text[:400])
        elif c.type == "image":
            parts.append(f"<image {c.mimeType} {len(c.data)}b64>")
    print(f"--- {label} error={result.isError}:", " | ".join(parts).replace("\n", " ")[:900], flush=True)

def save_image(result, name):
    for c in result.content:
        if c.type == "image":
            open(os.path.join(ART, name), "wb").write(base64.b64decode(c.data))
            return True
    return False

async def main():
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID="smoke-session")
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_realms.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("server", init.serverInfo.name, init.serverInfo.version, "instructions:", (init.instructions or "")[:60])
            tools = (await s.list_tools()).tools
            names = [t.name for t in tools]
            print(len(names), "tools:", names[:6], "...", flush=True)
            show("status", await s.call_tool("realm", {"action": "status"}))
            show("setup", await s.call_tool("realm", {"action": "setup"}))
            for _ in range(120):
                await anyio.sleep(1)
                r = await s.call_tool("realm", {"action": "status"})
                data = json.loads(r.content[1].text)
                if data["driver_tools"]:
                    break
            tools = (await s.list_tools()).tools
            print("after setup:", len(tools), "tools:", [t.name for t in tools][:10], "...", flush=True)
            show("launch", await s.call_tool("realm_launch", {"command": "gtk3-demo"}))
            await anyio.sleep(3)
            show("list_windows", await s.call_tool("list_windows", {}))
            r = await s.call_tool("get_desktop_state", {"max_image_dimension": 1280})
            show("get_desktop_state", r); print("saved:", save_image(r, "desktop.png"))
            r = await s.call_tool("realm", {"action": "shot"})
            show("shot", r); save_image(r, "shot.png")
            show("exec", await s.call_tool("realm_exec", {"command": "ls /opt; echo $HOME"}))
            show("control on", await s.call_tool("realm", {"action": "control", "control": True}))
            show("blocked click", await s.call_tool("list_windows", {}))
            show("control off", await s.call_tool("realm", {"action": "control", "control": False}))
            show("driver", await s.call_tool("realm", {"action": "driver", "operation": "check"}))
    print("server exited", flush=True)

anyio.run(main)
