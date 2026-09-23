# Prompt for the dashboard agent

I'm building a small always-on bridge that reads session state from your dashboard and drives a Synthstrom Deluge over USB MIDI (repo: abusch419/deluge-claude-control). It will replace the Claude Code hooks I use today, because your dashboard tracks sessions more accurately. I need you to document exactly how an outside process can read that state. Don't build anything new yet. Just answer the questions below in one markdown file, `BRIDGE_INTERFACE.md`, and include real examples from the code.

1. **Where state lives.** How does the dashboard learn about sessions (files it reads, process scanning, an API, a websocket, hooks)? Give file paths, ports, and endpoints.
2. **Data shape.** Paste the exact schema of one session record with a real example. I need: a stable session ID, project/cwd, title, status, parent/child links for subagents, timestamps, and whether the session is closed.
3. **Statuses.** List every status value and what triggers it. I especially need: working, idle (turn finished, chat still open), needs input (permission prompt), and closed/done. Say how fast each one is detected (in seconds).
4. **Subscribing.** Can an outside process get pushed changes (websocket, SSE, file watch, a local socket), or only poll? If only poll, what's the cheapest call, and how often is it safe to call?
5. **Runtime.** What language and runtime does the dashboard use, and how is it started? Does it run at login? Could my bridge live inside it as a small plugin or module instead of running as a separate process?
6. **Session order.** Is there a stable order I can use to assign each chat its own grid row, so rows don't shuffle?
7. **Limits.** Anything that's unreliable or missing (e.g. VS Code sessions, cloud sessions, sessions that crash).

Hard rule for anything you suggest: the bridge must stay near 0% CPU, have no busy loops, and never slow down my machine. Prefer event-driven over polling.

When you're done, give me the path to `BRIDGE_INTERFACE.md` so I can pass it to the Deluge agent.
