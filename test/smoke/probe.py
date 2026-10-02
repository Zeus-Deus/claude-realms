"""Ad-hoc: call arbitrary driver tools against a fresh realm. argv: JSON list of [tool, args]."""
import anyio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    calls = json.loads(sys.argv[1])
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID="probe")
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_realms.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if not any(t.name == "click" for t in (await s.list_tools()).tools):
                await s.call_tool("realm", {"action": "setup"})
                for _ in range(120):
                    await anyio.sleep(1)
                    if any(t.name == "click" for t in (await s.list_tools()).tools):
                        break
            for tool, args in calls:
                if tool == "sleep":
                    await anyio.sleep(args); continue
                res = await s.call_tool(tool, args)
                out = []
                for c in res.content:
                    if c.type == "image" and os.environ.get("ARTIFACTS"):
                        import base64
                        global shots
                        shots = globals().get("shots", 0) + 1
                        path = os.path.join(os.environ["ARTIFACTS"], f"probe-{shots}.png")
                        open(path, "wb").write(base64.b64decode(c.data))
                        out.append(f"<image saved {path}>")
                        continue
                    out.append(c.text if c.type == "text" else f"<{c.type}>")
                print(f"=== {tool} {json.dumps(args)[:80]} error={res.isError}\n" + "\n".join(out)[:2500], flush=True)
anyio.run(main)
