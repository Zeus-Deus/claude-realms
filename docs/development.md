# Development

Everything is tested in a disposable, unprivileged Docker container (uid 1000,
all capabilities dropped; seccomp relaxed only so bubblewrap can create
namespaces). See `test/docker/`.

```sh
docker build -t claude-realms-rig test/docker
test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q && /home/tester/venv/bin/python -m pytest -q'
test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q && /home/tester/venv/bin/python -m pytest -q -m integration tests/integration'
# mod tests and validation need the claude binary mounted:
test/docker/run.sh --mount "$(command -v claude):/usr/local/bin/claude:ro" -- bash -lc 'cp -r /plugin /tmp/p && claude plugin test /tmp/p'
```

`test/e2e/tui.sh` drives a real interactive Claude Code session in tmux inside
the container (the `/realm` UI and the pane need no model; agent turns need
`CLAUDE_CODE_OAUTH_TOKEN`).
