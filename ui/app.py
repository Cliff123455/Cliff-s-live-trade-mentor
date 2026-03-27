# ui/app.py
# ScalpBot dashboard — Flask + Flask-SocketIO backend.
# Serves the HTML dashboard and pushes live status updates via WebSocket.

import json
import os
import subprocess
import sys
import time
from threading import Thread

import redis
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request
from flask_socketio import SocketIO

from alpaca.trading.client import TradingClient
from src.shared.constants import SK, AGENTS, DEFAULT_MODELS
from src.shared.sim_clock import get_sim_time_ms_sync
from ui.mentor import mentor_bp

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "scalpbot-secret-key")
app.register_blueprint(mentor_bp)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Sync Redis client for Flask (not async)
_redis: redis.Redis | None = None

# Alpaca client for live position data
_alpaca: TradingClient | None = None

# Agent subprocess handle
_agent_proc: subprocess.Popen | None = None


def _get_alpaca() -> TradingClient | None:
    global _alpaca
    if _alpaca is None:
        key = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if key and secret:
            paper = "paper" in os.getenv("ALPACA_BASE_URL", "").lower()
            _alpaca = TradingClient(api_key=key, secret_key=secret, paper=paper)
    return _alpaca

# Channel → which agent produced it
_CHANNEL_AGENT = {
    "scalpbot:market-data":       "scanner",
    "scalpbot:catalysts":         "wire",
    "scalpbot:setups":            "chartist",
    "scalpbot:risk-decisions":    "actuary",
    "scalpbot:directives":        "general",
    "scalpbot:fill-reports":      "sniper",
    "scalpbot:exit-orders":       "watcher",
    "scalpbot:performance-updates": "historian",
    "scalpbot:risk:daily-halt":   "actuary",
}


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis


def _collect_status() -> dict:
    r = _get_redis()

    # Mode detection (needed early for position source decision)
    sim_clock = r.get("state:sim-clock")
    is_backtest = bool(sim_clock)
    current_mode = r.get("state:mode") or ("backtest" if is_backtest else "live")

    # Daily P&L
    pnl_raw = r.get(SK.DAILY_PNL)
    daily_pnl = float(pnl_raw) if pnl_raw else 0.0

    # Halt status
    risk_halted = r.hget(SK.RISK_PARAMS, "halted")
    halted = bool(risk_halted and risk_halted not in ("", "false", "False", "0", "null"))

    # Agent statuses
    agents = {}
    for agent_name in AGENTS.ALL:
        raw = r.hgetall(SK.AGENT_STATUS.format(agent_name))
        agents[agent_name] = {
            "status": raw.get("status", "stopped"),
            "ts": int(raw.get("ts", 0)),
            "name": raw.get("name", agent_name),
        }

    # Open positions — in backtest use Redis only, in live prefer Alpaca
    positions = {}
    use_redis_positions = is_backtest  # backtest positions only exist in Redis

    if not use_redis_positions:
        try:
            alpaca = _get_alpaca()
            if alpaca:
                alpaca_positions = alpaca.get_all_positions()
                for p in alpaca_positions:
                    ticker = p.symbol
                    qty = int(float(p.qty))
                    direction = "short" if qty < 0 else "long"
                    positions[ticker] = {
                        "ticker": ticker,
                        "direction": direction,
                        "shares": abs(qty),
                        "entry_price": float(p.avg_entry_price),
                        "current_price": float(p.current_price),
                        "market_value": float(p.market_value),
                        "unrealized_pnl": float(p.unrealized_pl),
                        "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
                        "cost_basis": float(p.cost_basis),
                    }
                    # Merge stop/target from Redis if available
                    redis_pos_raw = r.hget(SK.POSITIONS, ticker)
                    if redis_pos_raw:
                        try:
                            redis_pos = json.loads(redis_pos_raw)
                            positions[ticker]["stop_price"] = redis_pos.get("stop_price", 0)
                            positions[ticker]["target_price"] = redis_pos.get("target_price", 0)
                            positions[ticker]["trailing_stop"] = redis_pos.get("trailing_stop")
                            positions[ticker]["directive_id"] = redis_pos.get("directive_id")
                            positions[ticker]["entry_time_ms"] = redis_pos.get("entry_time_ms")
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception:
            use_redis_positions = True  # Alpaca unavailable — fall back

    if use_redis_positions:
        raw_positions = r.hgetall(SK.POSITIONS)
        for ticker, val in raw_positions.items():
            try:
                positions[ticker] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                positions[ticker] = {"ticker": ticker, "raw": val}

    return {
        "halted": halted,
        "daily_pnl": round(daily_pnl, 2),
        "agents": agents,
        "positions": positions,
        "timestamp_ms": get_sim_time_ms_sync(r) if is_backtest else int(time.time() * 1000),
        "mode": current_mode,
        "sim_clock_ms": int(float(sim_clock)) if sim_clock else None,
    }


def _collect_models() -> dict:
    r = _get_redis()
    models = {}
    for agent_name in AGENTS.ALL:
        val = r.hget(SK.MODELS, agent_name)
        models[agent_name] = val or DEFAULT_MODELS.get(agent_name, "google/gemini-2.0-flash-001")
    return models


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/agents/start", methods=["POST"])
def api_agents_start():
    global _agent_proc
    if _agent_proc and _agent_proc.poll() is None:
        return jsonify({"ok": False, "message": "Agents already running"})
    try:
        python_exe = sys.executable
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_path = os.path.join(root, "logs", "agents.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_fh = open(log_path, "w", buffering=1)  # line-buffered

        # Build command based on current mode
        r = _get_redis()
        mode = r.get("state:mode") or "live"
        cmd = [python_exe, "agents_runner.py", "--mode", mode]

        if mode == "backtest":
            bt_date = r.get("state:backtest-date") or ""
            bt_speed = r.get("state:backtest-speed") or "10"
            if not bt_date:
                return jsonify({"ok": False, "message": "No backtest date set. Use /api/mode first."}), 400
            cmd.extend(["--date", bt_date, "--speed", bt_speed])

        _agent_proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_fh,
            stderr=log_fh,
        )
        return jsonify({"ok": True, "pid": _agent_proc.pid, "mode": mode,
                        "message": f"Agents starting in {mode.upper()} mode...", "log": log_path})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/agents/log")
def api_agents_log():
    """Return last N lines of the agent runner log."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_path = os.path.join(root, "logs", "agents.log")
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
        tail = "".join(lines[-100:])  # last 100 lines
        return jsonify({"ok": True, "log": tail})
    except FileNotFoundError:
        return jsonify({"ok": True, "log": "(no log yet — start agents first)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})




@app.route("/api/agents/stop", methods=["POST"])
def api_agents_stop():
    global _agent_proc
    if _agent_proc and _agent_proc.poll() is None:
        pid = _agent_proc.pid
        # On Windows, terminate() only kills the parent — use taskkill to nuke the whole tree
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:
            _agent_proc.kill()
        _agent_proc = None
        return jsonify({"ok": True, "message": "Agents stopped"})
    # Fallback: kill any stray agents_runner.py processes
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "python.exe", "/FI", f"WINDOWTITLE eq agents_runner*"],
            capture_output=True, timeout=3
        )
    except Exception:
        pass
    return jsonify({"ok": False, "message": "No agents running"})



@app.route("/api/agents/status")
def api_agents_process_status():
    global _agent_proc
    running = bool(_agent_proc and _agent_proc.poll() is None)
    return jsonify({"running": running, "pid": _agent_proc.pid if running else None})


def _kill_agents():
    """Kill the agent subprocess tree. Used by emergency stop."""
    global _agent_proc
    if _agent_proc and _agent_proc.poll() is None:
        pid = _agent_proc.pid
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:
            try:
                _agent_proc.kill()
            except Exception:
                pass
    _agent_proc = None
    # Fallback: kill any stray agents_runner.py processes
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "python.exe", "/FI", "WINDOWTITLE eq agents_runner*"],
            capture_output=True, timeout=3
        )
    except Exception:
        pass


@app.route("/api/emergency-stop", methods=["POST"])
def api_emergency_stop():
    """Nuclear option — kills every Python process, including Flask itself."""
    global _agent_proc
    # Kill agents
    _kill_agents()
    # Schedule Flask self-destruct after response is sent
    import threading
    def _suicide():
        import time, os, signal
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_suicide, daemon=True).start()
    return jsonify({"ok": True, "message": "Emergency stop triggered — server shutting down"})


@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/account")
def api_account():
    """Return Alpaca account summary — portfolio value, cash, buying power, P&L."""
    try:
        alpaca = _get_alpaca()
        if not alpaca:
            return jsonify({"error": "Alpaca not configured"}), 500
        acct = alpaca.get_account()
        return jsonify({
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "long_market_value": float(acct.long_market_value),
            "short_market_value": float(acct.short_market_value),
            "initial_margin": float(acct.initial_margin),
            "last_equity": float(acct.last_equity),
            "day_pnl": round(float(acct.equity) - float(acct.last_equity), 2),
            "day_pnl_pct": round((float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity) * 100, 3) if float(acct.last_equity) > 0 else 0,
            "status": acct.status.value if hasattr(acct.status, 'value') else str(acct.status),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/liquidate-all", methods=["POST"])
def api_liquidate_all():
    """Close every open position via Alpaca.  Returns a list of close orders."""
    try:
        alpaca = _get_alpaca()
        if not alpaca:
            return jsonify({"error": "Alpaca client not configured"}), 500

        # cancel_orders=True cancels any pending orders first, then market-sells everything
        closed = alpaca.close_all_positions(cancel_orders=True)

        results = []
        for item in closed:
            # Each item is a ClosePositionResponse with .status and .body
            body = item.body if hasattr(item, "body") else {}
            results.append({
                "status": item.status if hasattr(item, "status") else 200,
                "symbol": getattr(body, "symbol", "?"),
                "qty":    str(getattr(body, "qty", "")),
                "side":   str(getattr(body, "side", "")),
            })

        # Also flush Redis positions so UI updates immediately
        r = _get_redis()
        if r:
            r.delete("state:positions")

        return jsonify({"ok": True, "closed": results, "count": len(results)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status")
def api_status():
    try:
        return jsonify(_collect_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-rules", methods=["GET"])
def api_trade_rules_get():
    """Return current trade rules from Redis (or defaults from config)."""
    try:
        r = _get_redis()
        saved = r.hgetall("state:trade-rules")
        # Defaults
        rules = {
            "max_position_dollars": 20000,
            "max_loss_per_trade_pct": 2.0,
            "max_daily_loss_pct": 3.0,
            "trailing_stop_activation_pct": 0.5,
            "trailing_stop_trail_pct": 0.25,
            "conviction_threshold": 7.0,
            "time_exit_minutes": 30,
            "use_target_exit": False,
        }
        for k, v in saved.items():
            try:
                if v.lower() in ("true", "false"):
                    rules[k] = v.lower() == "true"
                else:
                    rules[k] = float(v)
            except (ValueError, AttributeError):
                rules[k] = v
        return jsonify(rules)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-rules", methods=["POST"])
def api_trade_rules_post():
    """Save trade rules to Redis. Agents read these on every evaluation."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        r = _get_redis()
        for k, v in data.items():
            r.hset("state:trade-rules", k, str(v))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["GET"])
def api_models_get():
    try:
        return jsonify(_collect_models())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["POST"])
def api_models_post():
    data = request.get_json(force=True, silent=True) or {}
    agent_name = data.get("agent", "").strip()
    model_id = data.get("model", "").strip()

    if not agent_name or agent_name not in AGENTS.ALL:
        return jsonify({"error": f"Unknown agent: {agent_name}"}), 400
    if not model_id:
        return jsonify({"error": "model cannot be empty"}), 400

    try:
        r = _get_redis()
        r.hset(SK.MODELS, agent_name, model_id)
        return jsonify({"ok": True, "agent": agent_name, "model": model_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    """Return the current watchlist: fallback + user-added tickers."""
    try:
        r = _get_redis()
        # Load fallback from config
        import yaml
        with open("config/indicators.yaml", "r") as fh:
            cfg = yaml.safe_load(fh)
        fallback = [str(t).upper() for t in cfg.get("fallback_watchlist", [])]

        # Load user-added tickers from Redis
        user_raw = r.get(SK.WATCHLIST)
        user_tickers = json.loads(user_raw) if user_raw else []

        return jsonify({
            "fallback": fallback,
            "user_added": user_tickers,
            "combined": list(dict.fromkeys(fallback + user_tickers)),  # deduped, order preserved
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_post():
    """Add ticker(s) to the user watchlist in Redis. Scanner picks them up on next refresh."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        tickers = data.get("tickers", [])
        if isinstance(tickers, str):
            # Support comma-separated string: "AAPL, TSLA, NVDA"
            tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        else:
            tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]

        if not tickers:
            return jsonify({"error": "No tickers provided"}), 400

        r = _get_redis()
        existing_raw = r.get(SK.WATCHLIST)
        existing = json.loads(existing_raw) if existing_raw else []

        added = []
        for t in tickers:
            if t not in existing:
                existing.append(t)
                added.append(t)

        r.set(SK.WATCHLIST, json.dumps(existing))
        return jsonify({"ok": True, "added": added, "watchlist": existing})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["DELETE"])
def api_watchlist_delete():
    """Remove a ticker from the user watchlist."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        ticker = str(data.get("ticker", "")).strip().upper()
        if not ticker:
            return jsonify({"error": "No ticker provided"}), 400

        r = _get_redis()
        existing_raw = r.get(SK.WATCHLIST)
        existing = json.loads(existing_raw) if existing_raw else []

        if ticker in existing:
            existing.remove(ticker)
            r.set(SK.WATCHLIST, json.dumps(existing))
            return jsonify({"ok": True, "removed": ticker, "watchlist": existing})
        else:
            return jsonify({"ok": False, "message": f"{ticker} not in user watchlist"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mode", methods=["GET"])
def api_mode_get():
    """Return current operating mode and backtest settings if active."""
    try:
        r = _get_redis()
        mode = r.get("state:mode") or "live"
        result = {"mode": mode, "is_running": bool(_agent_proc and _agent_proc.poll() is None)}
        if mode == "backtest":
            result["date"] = r.get("state:backtest-date") or ""
            result["speed"] = float(r.get("state:backtest-speed") or 10)
        sim_clock = r.get("state:sim-clock")
        if sim_clock:
            result["sim_clock_ms"] = int(float(sim_clock))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mode", methods=["POST"])
def api_mode_post():
    """Set operating mode. Must be called BEFORE /api/agents/start."""
    try:
        if _agent_proc and _agent_proc.poll() is None:
            return jsonify({"error": "Agents are running. Stop them first."}), 400

        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "live").lower()
        if mode not in ("live", "backtest"):
            return jsonify({"error": "mode must be 'live' or 'backtest'"}), 400

        r = _get_redis()
        r.set("state:mode", mode)

        if mode == "backtest":
            date = data.get("date", "").strip()
            speed = data.get("speed", 10)
            if not date:
                return jsonify({"error": "backtest mode requires 'date' (YYYY-MM-DD)"}), 400
            r.set("state:backtest-date", date)
            r.set("state:backtest-speed", str(speed))
        else:
            # Clean up sim clock when switching to live
            r.delete("state:sim-clock")

        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SocketIO background emitter ───────────────────────────────────────────────

def _status_emitter():
    """Background thread: push status_update every 2 seconds."""
    while True:
        try:
            status = _collect_status()
            socketio.emit("status_update", status)
        except Exception:
            pass
        socketio.sleep(2)


def _activity_listener():
    """Background thread: subscribe to all scalpbot channels and re-emit to browser."""
    while True:
        try:
            r = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379"),
                decode_responses=True,
            )
            ps = r.pubsub(ignore_subscribe_messages=True)
            ps.subscribe(*_CHANNEL_AGENT.keys())
            for message in ps.listen():
                if message["type"] != "message":
                    continue
                channel = message["channel"]
                agent = _CHANNEL_AGENT.get(channel, "unknown")
                try:
                    data = json.loads(message["data"])
                except Exception:
                    data = {"raw": message["data"]}
                socketio.emit("agent_log", {
                    "agent": agent,
                    "channel": channel,
                    "data": data,
                    "ts": int(time.time() * 1000),
                })
        except Exception:
            time.sleep(3)  # Redis not ready, retry


@socketio.on("connect")
def on_connect():
    # Send current state immediately on connect
    try:
        socketio.emit("status_update", _collect_status())
        socketio.emit("models_update", _collect_models())
    except Exception:
        pass


# ── Start function called from main.py ───────────────────────────────────────

def start_ui():
    host = os.getenv("UI_HOST", "127.0.0.1")
    port = int(os.getenv("UI_PORT", 8501))
    # Start background threads
    Thread(target=_status_emitter, daemon=True).start()
    Thread(target=_activity_listener, daemon=True).start()

    # Try the requested port first, then fall back to alternatives
    import socket
    ports_to_try = [port, 5000, 5050, 8080, 8888, 9000, 3000, 4000]
    hosts_to_try = [host, "0.0.0.0", "localhost"]

    for try_host in hosts_to_try:
        for try_port in ports_to_try:
            try:
                # Quick test if we can bind
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_sock.bind((try_host, try_port))
                test_sock.close()
                print(f" * CliffClaw Mentor starting on http://{try_host}:{try_port}")
                print(f" * Open http://127.0.0.1:{try_port} in your browser")
                socketio.run(app, host=try_host, port=try_port,
                             use_reloader=False, log_output=False)
                return
            except OSError as e:
                print(f" * Port {try_host}:{try_port} blocked ({e}) — trying next...")
                try:
                    test_sock.close()
                except Exception:
                    pass
                continue

    print(" * ERROR: Could not bind to ANY port. Check firewall/antivirus settings.")
    print(" *        Try: Windows Security > Firewall > Allow an app > Add python.exe")
