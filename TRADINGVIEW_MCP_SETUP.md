# Connecting Claude to TradingView (TradingView MCP)

This wires up the [tradesdontlie/tradingview-mcp](https://github.com/tradesdontlie/tradingview-mcp)
server so **Claude Desktop** can read your charts, write/analyze Pine Script,
control layouts, and drive replay mode — all by talking to the **TradingView
Desktop app** running on your own machine.

> **Important:** This MCP is *local-only by design*. It connects to the
> TradingView **Desktop** app over Chrome DevTools Protocol on
> `localhost:9222`, and Claude Desktop launches it as a local `node` process.
> It cannot run in the cloud — it has to run on the same machine as TradingView.
> That's why setup happens on your PC, not from a remote Claude session.

---

## What you need first

| Requirement | Notes |
|-------------|-------|
| **TradingView *Desktop* app** | The downloadable app, **not** the website. Requires a **paid** TradingView subscription. Get it: https://www.tradingview.com/desktop/ |
| **Node.js 18+** | https://nodejs.org — pick the **LTS** installer. |
| **git** | https://git-scm.com |
| **Claude Desktop** | You already have this. |

No API keys or credentials are required — everything runs locally.

---

## The easy path (one script)

From this repo folder, double-click:

```
setup_tradingview_mcp.bat
```

It will:
1. Check that Node and git are installed.
2. Clone the MCP into `%USERPROFILE%\tradingview-mcp` (or update it if already there).
3. Run `npm install`.
4. Print the exact config block to paste into Claude Desktop.

Then do the three "Next steps" it prints (config + launch TradingView in debug
mode + restart Claude). Details below if you'd rather do it by hand.

---

## Manual steps (if you skip the script)

### 1. Clone and install
```bat
git clone https://github.com/tradesdontlie/tradingview-mcp.git "%USERPROFILE%\tradingview-mcp"
cd /d "%USERPROFILE%\tradingview-mcp"
npm install
```

### 2. Add the server to Claude Desktop's config

Open (or create via **Claude Desktop → Settings → Developer → Edit Config**):

```
%APPDATA%\Claude\claude_desktop_config.json
```

Add the `tradingview` entry inside `mcpServers`. Replace `<YOU>` with your
Windows username, and note the **double backslashes** — JSON requires them:

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["C:\\Users\\<YOU>\\tradingview-mcp\\src\\server.js"]
    }
  }
}
```

If you already have other MCP servers in there, just add `"tradingview": { ... }`
alongside them — don't create a second `mcpServers` block.

### 3. Launch TradingView in debug mode (every time, before Claude)

The MCP can only see TradingView if the Desktop app is started with the remote
debugging port open. Use the bundled launcher:

```bat
"%USERPROFILE%\tradingview-mcp\scripts\launch_tv_debug.bat"
```

(That's the equivalent of running TradingView with
`--remote-debugging-port=9222`.)

### 4. Restart Claude Desktop

Fully **quit** Claude Desktop (check the system tray — don't just close the
window) and reopen it. The TradingView tools should now show up in the tools
menu.

---

## Verifying it works

1. TradingView Desktop is open (launched via the debug script) with a chart up.
2. Claude Desktop is restarted.
3. Ask Claude: *"Use the tradingview health check tool"* or *"what symbol and
   timeframe is my current TradingView chart on?"*

If Claude reports your live chart, you're connected.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tools don't appear in Claude | Make sure you **fully quit** Claude (tray icon), and that the JSON is valid (no trailing commas, double backslashes in the path). |
| "Cannot connect" / port errors | TradingView Desktop must be started via `launch_tv_debug.bat` **before** Claude tries to use the tools. The web version won't work. |
| `node` not recognized | Reinstall Node LTS and reopen your terminal so PATH refreshes. |
| Path wrong in config | The entry file is `src\server.js` inside the cloned folder. Confirm the folder exists at `%USERPROFILE%\tradingview-mcp`. |

The MCP project's own `SETUP_GUIDE.md` and `README.md` (in the cloned folder)
have the full list of all ~78 tools and CLI usage.

---

## How this fits with ScalpBot

This is a **separate, complementary** tool from ScalpBot:

- **ScalpBot** (this repo) = autonomous agents that *trade* via Alpaca.
- **TradingView MCP** = lets *you + Claude* interactively *analyze and annotate*
  charts in TradingView Desktop.

They don't talk to each other — but using Claude + TradingView to research
setups, then encoding what works into ScalpBot's `config/` files, is a natural
workflow.
