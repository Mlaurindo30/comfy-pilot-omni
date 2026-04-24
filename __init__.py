# ComfyUI Comfy Pilot Plugin
# A floating multi-terminal extension with CLI-agnostic adapters

import asyncio
import json
import os
import struct
import subprocess
import sys

from aiohttp import web

try:
    from .cli_adapters import (
        DEFAULT_ADAPTER_ID,
        ensure_adapter_mcp_config,
        get_adapter,
        get_terminal_backend_status,
        install_claude_code,
        list_adapters,
        pick_active_adapter_id,
    )
    from .settings_store import SettingsStore
except ImportError:  # pragma: no cover - fallback for direct execution
    from cli_adapters import (
        DEFAULT_ADAPTER_ID,
        ensure_adapter_mcp_config,
        get_adapter,
        get_terminal_backend_status,
        install_claude_code,
        list_adapters,
        pick_active_adapter_id,
    )
    from settings_store import SettingsStore

# Platform detection
IS_WINDOWS = sys.platform == "win32"

# Unix-only imports (for terminal functionality)
if not IS_WINDOWS:
    import fcntl
    import pty
    import resource
    import signal
    import termios
else:
    fcntl = None
    pty = None
    resource = None
    signal = None
    termios = None
    try:
        from winpty import PTY
    except ImportError:
        PTY = None

WEB_DIRECTORY = "./js"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

PLUGIN_LOG_PREFIX = "[Comfy Pilot]"
ROUTE_BASE = "/comfy-pilot"
LEGACY_ROUTE_BASE = "/claude-code"
WS_ROUTE = "/ws/comfy-pilot-terminal"
LEGACY_WS_ROUTE = "/ws/claude-terminal"
WORKFLOW_CLIENT_PARAM = "client_id"
WORKFLOW_CLIENT_ENV_VAR = "COMFY_PILOT_CLIENT_ID"
WORKFLOW_ADAPTER_ENV_VAR = "COMFY_PILOT_ADAPTER_ID"
DEFAULT_WORKFLOW_CLIENT_ID = "default"
WORKFLOW_CLIENT_CONNECTED_MS = 5000


def plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


settings_store = SettingsStore(os.path.join(plugin_dir(), ".comfy_pilot_settings.json"))


def normalize_workflow_client_id(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def build_terminal_env(window_session_id=None, adapter_id=None):
    env = {}
    normalized_client_id = normalize_workflow_client_id(window_session_id)
    if normalized_client_id:
        env[WORKFLOW_CLIENT_ENV_VAR] = normalized_client_id
    if adapter_id:
        env[WORKFLOW_ADAPTER_ENV_VAR] = str(adapter_id)
    return env


def _build_windows_spawn_target(command, extra_env=None):
    if not extra_env:
        return command or [os.environ.get("COMSPEC", "cmd.exe")]

    spawn_target = command or [os.environ.get("COMSPEC", "cmd.exe")]
    if isinstance(spawn_target, str):
        command_line = spawn_target
    else:
        command_line = subprocess.list2cmdline(spawn_target)

    env_prefix = " && ".join(
        f'set "{key}={str(value).replace(chr(34), chr(34) * 2)}"'
        for key, value in extra_env.items()
    )
    wrapped_command = f"{env_prefix} && {command_line}" if env_prefix else command_line
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", wrapped_command]


class WebSocketTerminal:
    """Manages a PTY session connected via WebSocket."""

    def __init__(self):
        self.fd = None
        self.pid = None
        self.process = None
        self.running = False
        self.last_error = ""
        self._decoder = None

    def spawn(self, command=None, rows=24, cols=80, extra_env=None):
        """Spawn a new PTY with an optional command."""
        if IS_WINDOWS:
            if PTY is None:
                self.last_error = get_terminal_backend_status().get("reason") or (
                    "Windows terminal backend unavailable"
                )
                print(f"{PLUGIN_LOG_PREFIX} {self.last_error}")
                return False

            spawn_target = _build_windows_spawn_target(command, extra_env=extra_env)
            if isinstance(spawn_target, str):
                appname = os.environ.get("COMSPEC", "cmd.exe")
                cmdline = f'/d /s /c "{spawn_target}"'
            else:
                appname = spawn_target[0]
                cmdline = subprocess.list2cmdline(spawn_target[1:]) if len(spawn_target) > 1 else None

            try:
                self.process = PTY(cols, rows)
                self.process.spawn(appname, cmdline=cmdline, cwd=os.getcwd())
                self.pid = self.process.pid
            except Exception as exc:
                self.process = None
                self.pid = None
                self.last_error = f"Failed to start Windows terminal: {exc}"
                print(f"{PLUGIN_LOG_PREFIX} {self.last_error}")
                return False

            self.running = True
            return True

        shell = os.environ.get("SHELL", "/bin/bash")
        self.pid, self.fd = pty.fork()

        if self.pid == 0:
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env.update(extra_env or {})

            if command:
                os.execlpe(shell, shell, "-l", "-i", "-c", command, env)
            else:
                shell_name = os.path.basename(shell)
                os.execlpe(shell, f"-{shell_name}", env)
        else:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self.running = True
            return True

    def resize(self, rows, cols):
        """Resize the PTY and notify the child process."""
        if IS_WINDOWS:
            if not self.process:
                return
            try:
                self.process.set_size(cols, rows)
            except Exception:
                self.running = False
            return

        if not self.fd:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGWINCH)
            except OSError:
                pass

    def write(self, data):
        """Write data to the PTY."""
        if IS_WINDOWS:
            if not self.process:
                return
            try:
                self.process.write(data)
            except Exception:
                self.running = False
            return

        if not self.fd:
            return
        os.write(self.fd, data.encode("utf-8"))

    def read_nonblock(self):
        """Non-blocking read from PTY."""
        if IS_WINDOWS:
            if not self.process:
                return None
            try:
                data = self.process.read(blocking=False)
                if data:
                    return data
                if not self.process.isalive() or self.process.iseof():
                    self.running = False
            except Exception:
                if not self.process.isalive() or self.process.iseof():
                    self.running = False
            return None

        if not self.fd:
            return None
        try:
            data = os.read(self.fd, 4096)
            if data:
                if self._decoder is None:
                    import codecs

                    self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                return self._decoder.decode(data)
        except BlockingIOError:
            return None
        except (OSError, IOError):
            self.running = False
        return None

    def close(self):
        """Close the PTY."""
        self.running = False
        if IS_WINDOWS:
            if self.process:
                try:
                    self.process.cancel_io()
                except Exception:
                    pass
                try:
                    if self.process.isalive():
                        os.kill(self.process.pid, 9)
                except Exception:
                    pass
                del self.process
                self.process = None
            self.pid = None
            return
        if self.fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.pid:
            try:
                os.kill(self.pid, 9)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
            self.pid = None


class TerminalSessionManager:
    """Tracks live terminal sessions independently of a specific CLI provider."""

    def __init__(self):
        self._sessions = {}

    def add(self, session_id, adapter_id, terminal, websocket=None, window_session_id=None):
        self._sessions[session_id] = {
            "adapter_id": adapter_id,
            "window_session_id": window_session_id,
            "terminal": terminal,
            "websocket": websocket,
        }

    def remove(self, session_id):
        self._sessions.pop(session_id, None)

    def count(self):
        return len(self._sessions)

    def get_adapter_ids_for_window_session(self, window_session_id):
        adapter_ids = {
            session.get("adapter_id")
            for session in self._sessions.values()
            if session.get("window_session_id") == window_session_id
        }
        return sorted(adapter_id for adapter_id in adapter_ids if adapter_id)

    async def close_window_session(self, window_session_id):
        if not window_session_id:
            return

        matching_session_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.get("window_session_id") == window_session_id
        ]

        for session_id in matching_session_ids:
            session = self._sessions.get(session_id)
            if not session:
                continue
            terminal = session.get("terminal")
            websocket = session.get("websocket")
            if terminal:
                terminal.running = False
                terminal.close()
            if websocket and not websocket.closed:
                try:
                    await websocket.close()
                except Exception:
                    pass
            self.remove(session_id)


terminal_session_manager = TerminalSessionManager()

class WorkflowClientManager:
    """Tracks live workflow state and pending graph commands per browser page."""

    def __init__(self, terminal_manager):
        self._terminal_manager = terminal_manager
        self._clients = {}
        self._pending_commands = {}
        self._command_results = {}
        self._latest_client_id = None

    def _now_ms(self):
        import time

        return int(time.time() * 1000)

    def _ensure_client(self, client_id=None):
        normalized = (
            normalize_workflow_client_id(client_id) or self._latest_client_id or DEFAULT_WORKFLOW_CLIENT_ID
        )
        state = self._clients.setdefault(
            normalized,
            {
                "client_id": normalized,
                "workflow": None,
                "workflow_api": None,
                "timestamp": None,
                "updated_at": None,
                "last_seen": None,
            },
        )
        return normalized, state

    def touch(self, client_id=None):
        normalized, state = self._ensure_client(client_id)
        state["last_seen"] = self._now_ms()
        return normalized

    def update_workflow(self, client_id=None, workflow=None, workflow_api=None, timestamp=None):
        normalized = self.touch(client_id)
        self._latest_client_id = normalized
        state = self._clients[normalized]
        if workflow is not None:
            state["workflow"] = workflow
        state["workflow_api"] = workflow_api
        if timestamp is not None:
            state["timestamp"] = timestamp
        state["updated_at"] = self._now_ms()
        return dict(state)

    def resolve_client_id(self, client_id=None):
        normalized = normalize_workflow_client_id(client_id)
        if normalized:
            return normalized
        return self._latest_client_id

    def get_workflow(self, client_id=None):
        resolved = self.resolve_client_id(client_id)
        if not resolved:
            return {
                "client_id": None,
                "workflow": None,
                "workflow_api": None,
                "timestamp": None,
                "updated_at": None,
                "last_seen": None,
            }
        state = self._clients.get(resolved)
        if state is None:
            return None
        return dict(state)

    def list_clients(self):
        now_ms = self._now_ms()
        clients = []
        for client_id, state in self._clients.items():
            workflow = state.get("workflow")
            node_count = None
            if isinstance(workflow, dict) and isinstance(workflow.get("nodes"), list):
                node_count = len(workflow["nodes"])

            terminal_adapters = self._terminal_manager.get_adapter_ids_for_window_session(client_id)
            last_seen = state.get("last_seen")
            clients.append(
                {
                    "client_id": client_id,
                    "timestamp": state.get("timestamp"),
                    "updated_at": state.get("updated_at"),
                    "last_seen": last_seen,
                    "is_connected": bool(last_seen and (now_ms - last_seen) <= WORKFLOW_CLIENT_CONNECTED_MS),
                    "has_workflow": state.get("workflow") is not None,
                    "workflow_node_count": node_count,
                    "terminal_adapters": terminal_adapters,
                    "selected": client_id == self._latest_client_id,
                }
            )

        clients.sort(key=lambda item: item.get("last_seen") or 0, reverse=True)
        return {"default_client_id": self._latest_client_id, "clients": clients}

    def pop_pending_command(self, client_id=None):
        normalized = self.touch(client_id)
        queue = self._pending_commands.get(normalized, [])
        if queue:
            return queue.pop(0)
        return None

    def queue_command(self, client_id, action, params):
        normalized = self.touch(client_id)
        import uuid

        cmd_id = str(uuid.uuid4())
        cmd = {"id": cmd_id, "action": action, "params": params or {}}
        self._pending_commands.setdefault(normalized, []).append(cmd)
        return normalized, cmd_id

    def store_result(self, client_id, command_id, result):
        normalized = self.touch(client_id)
        self._command_results.setdefault(normalized, {})[command_id] = result
        return normalized

    def pop_result(self, client_id, command_id):
        results = self._command_results.get(client_id, {})
        if command_id in results:
            return results.pop(command_id)
        return None

    def has_result(self, client_id, command_id):
        return command_id in self._command_results.get(client_id, {})

    def get_size_breakdown(self):
        workflow_size = len(json.dumps(self._clients)) if self._clients else 0
        commands_size = len(json.dumps(self._pending_commands)) if self._pending_commands else 0
        results_size = len(json.dumps(self._command_results)) if self._command_results else 0
        return {
            "workflow_clients_bytes": workflow_size,
            "pending_commands_bytes": commands_size,
            "command_results_bytes": results_size,
            "workflow_clients": len(self._clients),
            "terminal_sessions": self._terminal_manager.count(),
            "total_plugin_kb": round((workflow_size + commands_size + results_size) / 1024, 2),
        }


workflow_client_manager = WorkflowClientManager(terminal_session_manager)

# Memory logging
_last_memory_log = 0
MEMORY_LOG_INTERVAL = 60


def load_settings(force=False):
    return settings_store.load(force=force)


def save_settings(updates):
    return settings_store.update(updates)


def get_requested_adapter_id(request):
    adapter_id = request.query.get("adapter")
    if adapter_id:
        return adapter_id
    if request.path == LEGACY_WS_ROUTE or request.path.startswith(LEGACY_ROUTE_BASE):
        return DEFAULT_ADAPTER_ID
    return load_settings().get("default_cli", DEFAULT_ADAPTER_ID)


def get_requested_adapter(request):
    return get_adapter(get_requested_adapter_id(request))


def get_requested_workflow_client_id(request, data=None, allow_fallback=False):
    client_id = request.query.get(WORKFLOW_CLIENT_PARAM)
    if client_id is None and isinstance(data, dict):
        client_id = data.get(WORKFLOW_CLIENT_PARAM)
    client_id = normalize_workflow_client_id(client_id)
    if client_id:
        return client_id
    if allow_fallback:
        return workflow_client_manager.resolve_client_id() or DEFAULT_WORKFLOW_CLIENT_ID
    return None


def get_memory_mb():
    """Get current memory usage in MB."""
    if IS_WINDOWS:
        try:
            import psutil

            process = psutil.Process(os.getpid())
            return process.memory_info().rss / (1024 * 1024)
        except ImportError:
            return 0

    usage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return usage.ru_maxrss / (1024 * 1024)
    return usage.ru_maxrss / 1024


def get_plugin_memory_breakdown():
    """Get memory breakdown of plugin data structures."""
    return workflow_client_manager.get_size_breakdown()


def log_memory(context=""):
    """Log memory usage if enough time has passed since last log."""
    global _last_memory_log
    import time

    now = time.time()
    if now - _last_memory_log >= MEMORY_LOG_INTERVAL:
        _last_memory_log = now
        breakdown = get_plugin_memory_breakdown()
        suffix = f" | {context}" if context else ""
        print(
            f"{PLUGIN_LOG_PREFIX} Plugin data: {breakdown['total_plugin_kb']:.1f}KB | "
            f"Sessions: {breakdown['terminal_sessions']}{suffix}"
        )


async def memory_stats_handler(request):
    """Return current memory stats as JSON."""
    mem_mb = get_memory_mb()
    breakdown = get_plugin_memory_breakdown()

    return web.json_response(
        {
            "process_memory_mb": round(mem_mb, 2),
            "note": "process_memory_mb is the entire ComfyUI process, not just this plugin",
            "plugin_data": breakdown,
        }
    )


async def workflow_handler(request):
    """Handle workflow GET/POST requests."""
    if request.method == "POST":
        try:
            data = await request.json()
            client_id = get_requested_workflow_client_id(request, data=data) or DEFAULT_WORKFLOW_CLIENT_ID
            current_workflow = workflow_client_manager.update_workflow(
                client_id=client_id,
                workflow=data.get("workflow"),
                workflow_api=data.get("workflow_api"),
                timestamp=data.get("timestamp"),
            )
            log_memory("workflow update")
            return web.json_response({"status": "ok", "client_id": current_workflow.get("client_id")})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    client_id = get_requested_workflow_client_id(request, allow_fallback=True)
    workflow = workflow_client_manager.get_workflow(client_id)
    if workflow is None:
        return web.json_response({"error": f"Unknown client_id: {client_id}"}, status=404)
    return web.json_response(workflow)


async def graph_command_handler(request):
    """Handle graph manipulation commands from the MCP server."""
    if request.method == "GET":
        client_id = get_requested_workflow_client_id(request, allow_fallback=True)
        cmd = workflow_client_manager.pop_pending_command(client_id)
        if cmd:
            return web.json_response({"client_id": client_id, "command": cmd})
        return web.json_response({"command": None})

    try:
        data = await request.json()
        client_id = get_requested_workflow_client_id(request, data=data, allow_fallback=True)

        if "result" in data:
            cmd_id = data.get("command_id")
            workflow_client_manager.store_result(client_id, cmd_id, data.get("result"))
            return web.json_response({"status": "ok", "client_id": client_id})

        client_id, cmd_id = workflow_client_manager.queue_command(
            client_id,
            data.get("action"),
            data.get("params", {}),
        )

        import time

        start = time.time()
        while not workflow_client_manager.has_result(client_id, cmd_id) and time.time() - start < 5:
            await asyncio.sleep(0.1)

        result = workflow_client_manager.pop_result(client_id, cmd_id)
        if result is not None:
            return web.json_response(result)

        return web.json_response({"error": "Timeout waiting for frontend to execute command"}, status=504)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return web.json_response({"error": str(exc)}, status=500)


async def run_node_handler(request):
    """Run the workflow up to a specific node."""
    try:
        data = await request.json()
        node_id = data.get("node_id")
        client_id = get_requested_workflow_client_id(request, data=data, allow_fallback=True)

        if not node_id:
            return web.json_response({"error": "node_id is required"}, status=400)

        current_workflow = workflow_client_manager.get_workflow(client_id)
        if not current_workflow or not current_workflow.get("workflow_api"):
            return web.json_response(
                {"error": "No workflow available. Make sure ComfyUI is open in browser."},
                status=400,
            )

        workflow_api = current_workflow["workflow_api"]
        prompt = workflow_api.get("output", workflow_api)
        node_id_str = str(node_id)

        if node_id_str not in prompt:
            return web.json_response({"error": f"Node {node_id} not found in workflow"}, status=400)

        from server import PromptServer
        import uuid

        prompt_id = str(uuid.uuid4())
        PromptServer.instance.prompt_queue.put(
            (0, prompt_id, prompt, {"client_id": client_id or "comfy-pilot"}, [node_id_str])
        )

        return web.json_response(
            {"status": "queued", "prompt_id": prompt_id, "node_id": node_id_str, "client_id": client_id}
        )
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return web.json_response({"error": str(exc)}, status=500)


async def workflow_clients_handler(request):
    """List live workflow clients known to the plugin backend."""
    return web.json_response(workflow_client_manager.list_clients())


def build_cli_inventory():
    settings = load_settings()
    adapters = []
    for adapter in list_adapters():
        adapter_info = adapter.to_public_dict()
        adapter_info["enabled"] = adapter.id in settings.get("enabled_clis", [])
        adapter_info["selected"] = adapter.id == settings.get("default_cli")
        adapters.append(adapter_info)

    return {
        "default_cli": settings.get("default_cli", DEFAULT_ADAPTER_ID),
        "active_default_cli": pick_active_adapter_id(
            settings.get("default_cli"), require_terminal_usable=True
        ),
        "enabled_clis": settings.get("enabled_clis", []),
        "show_unavailable": settings.get("show_unavailable", False),
        "window_closed": settings.get("window_closed", False),
        "adapters": adapters,
    }


async def clis_handler(request):
    """Return CLI adapter inventory and settings."""
    return web.json_response(build_cli_inventory())


async def settings_handler(request):
    """Get or update persisted Comfy Pilot settings."""
    if request.method == "GET":
        return web.json_response(load_settings())

    try:
        data = await request.json()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)

    saved = save_settings(data)
    if not IS_WINDOWS:
        maybe_setup_default_adapter_mcp(saved)
    return web.json_response(saved)


async def platform_info_handler(request):
    """Return platform information."""
    terminal_backend = get_terminal_backend_status()
    return web.json_response(
        {
            "platform": sys.platform,
            "is_windows": IS_WINDOWS,
            "terminal_supported": terminal_backend["supported"],
            "terminal_backend": terminal_backend.get("backend"),
            "python_version": sys.version,
            "comfyui_url": get_comfyui_url_cached(),
            "default_cli": load_settings().get("default_cli", DEFAULT_ADAPTER_ID),
        }
    )


async def websocket_handler(request):
    """Handle WebSocket connections for terminal sessions."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    adapter = get_requested_adapter(request)
    window_session_id = request.query.get("session")

    terminal_backend = get_terminal_backend_status()
    if not terminal_backend["supported"]:
        await ws.send_str(
            json.dumps(
                {
                    "type": "error",
                    "message": terminal_backend["reason"],
                }
            )
        )
        await ws.close()
        return ws

    await terminal_session_manager.close_window_session(window_session_id)

    session_id = id(ws)
    terminal = WebSocketTerminal()
    terminal_session_manager.add(
        session_id,
        adapter.id,
        terminal,
        websocket=ws,
        window_session_id=window_session_id,
    )
    terminal_started = False
    terminal_env = build_terminal_env(window_session_id=window_session_id, adapter_id=adapter.id)

    print(f"{PLUGIN_LOG_PREFIX} WebSocket connected: session={session_id} adapter={adapter.id}")
    log_memory(f"ws connect {adapter.id}")

    settings = load_settings()
    command_override = settings.get("command_overrides", {}).get(adapter.id)
    explicit_command = request.query.get("cmd")
    if explicit_command and IS_WINDOWS:
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", explicit_command]
    else:
        command = explicit_command or adapter.build_spawn_command(
            os.getcwd(), command_override=command_override
        )

    if not explicit_command and adapter.id == "claude" and not adapter.is_available():
        print(f"{PLUGIN_LOG_PREFIX} Claude CLI not found, attempting auto-install...")
        success, message = install_claude_code()
        if success:
            command = adapter.build_spawn_command(os.getcwd(), command_override=command_override)
            print(f"{PLUGIN_LOG_PREFIX} Claude CLI installed, using command: {command}")
        else:
            print(f"{PLUGIN_LOG_PREFIX} Claude auto-install failed: {message}")

    if not IS_WINDOWS and adapter.supports_mcp_autoconfig:
        result = ensure_adapter_mcp_config(adapter, plugin_dir(), sys.executable)
        if result.get("configured"):
            print(f"{PLUGIN_LOG_PREFIX} {adapter.label} MCP ready")
        elif result.get("error"):
            print(f"{PLUGIN_LOG_PREFIX} {adapter.label} MCP setup skipped: {result['error']}")

    async def read_pty():
        """Read from PTY and send to WebSocket."""
        if IS_WINDOWS:
            try:
                while terminal.running and not ws.closed:
                    data = terminal.read_nonblock()
                    if data:
                        await ws.send_str("o" + data)
                        continue
                    await asyncio.sleep(0.02)
            except Exception as exc:
                print(f"{PLUGIN_LOG_PREFIX} Read error ({adapter.id}): {exc}")
            return

        loop = asyncio.get_event_loop()
        fd = terminal.fd
        read_event = asyncio.Event()
        pending_data = []

        def on_readable():
            try:
                data = terminal.read_nonblock()
                if data:
                    pending_data.append(data)
                    read_event.set()
            except Exception as exc:
                print(f"{PLUGIN_LOG_PREFIX} Read callback error ({adapter.id}): {exc}")

        loop.add_reader(fd, on_readable)

        try:
            while terminal.running and not ws.closed:
                await read_event.wait()
                read_event.clear()
                while pending_data:
                    await ws.send_str("o" + pending_data.pop(0))
        except Exception as exc:
            print(f"{PLUGIN_LOG_PREFIX} Read error ({adapter.id}): {exc}")
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    read_task = None

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "i":
                        terminal.write(data.get("d", ""))
                    elif msg_type == "input":
                        terminal.write(data.get("data", ""))
                    elif msg_type == "resize":
                        rows = data.get("rows", 24)
                        cols = data.get("cols", 80)

                        if not terminal_started:
                            terminal_started = terminal.spawn(
                                command,
                                rows=rows,
                                cols=cols,
                                extra_env=terminal_env,
                            )
                            if not terminal_started:
                                await ws.send_str(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "message": terminal.last_error
                                            or f"Failed to start {adapter.label} terminal.",
                                        }
                                    )
                                )
                                await ws.close()
                                break
                            terminal.resize(rows, cols)
                            read_task = asyncio.create_task(read_pty())
                            print(
                                f"{PLUGIN_LOG_PREFIX} Terminal started: "
                                f"adapter={adapter.id} size={cols}x{rows}"
                            )
                        else:
                            terminal.resize(rows, cols)
                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                print(f"{PLUGIN_LOG_PREFIX} WebSocket error ({adapter.id}): {ws.exception()}")
                break
    finally:
        terminal.running = False
        if read_task:
            read_task.cancel()
        terminal.close()
        terminal_session_manager.remove(session_id)
        print(f"{PLUGIN_LOG_PREFIX} WebSocket disconnected: session={session_id} adapter={adapter.id}")
        log_memory(f"ws disconnect {adapter.id}")

    return ws


_comfyui_url_cache = None


def get_comfyui_url_cached():
    """Get the cached ComfyUI URL."""
    global _comfyui_url_cache
    if _comfyui_url_cache:
        return _comfyui_url_cache
    try:
        from server import PromptServer

        address = PromptServer.instance.address
        port = PromptServer.instance.port
        _comfyui_url_cache = f"http://{address}:{port}"
        return _comfyui_url_cache
    except Exception:
        return "http://127.0.0.1:8188"


def write_comfyui_url():
    """Write the ComfyUI server URL to a file for the MCP server to read."""
    url_file = os.path.join(plugin_dir(), ".comfyui_url")

    try:
        from server import PromptServer

        address = PromptServer.instance.address
        port = PromptServer.instance.port
        url = f"http://{address}:{port}"
        with open(url_file, "w", encoding="utf-8") as file:
            file.write(url)
        print(f"{PLUGIN_LOG_PREFIX} ComfyUI URL written to {url_file}: {url}")
    except Exception:
        with open(url_file, "w", encoding="utf-8") as file:
            file.write("http://127.0.0.1:8188")
        print(f"{PLUGIN_LOG_PREFIX} Using default ComfyUI URL")


def maybe_setup_default_adapter_mcp(settings=None):
    """Attempt MCP configuration for the selected default adapter when supported."""
    if IS_WINDOWS:
        print(f"{PLUGIN_LOG_PREFIX} Skipping MCP auto-config on Windows")
        return

    settings = settings or load_settings()
    adapter = get_adapter(settings.get("default_cli"))
    if not adapter.supports_mcp_autoconfig:
        print(f"{PLUGIN_LOG_PREFIX} {adapter.label} requires manual MCP setup")
        return

    result = ensure_adapter_mcp_config(adapter, plugin_dir(), sys.executable)
    if result.get("configured"):
        print(f"{PLUGIN_LOG_PREFIX} {adapter.label} MCP configured")
    elif result.get("error"):
        print(f"{PLUGIN_LOG_PREFIX} {adapter.label} MCP not configured: {result['error']}")


def add_route_once(app, method, path, handler):
    """Register a route unless it already exists."""
    method = method.upper()
    for route in app.router.routes():
        resource = getattr(route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if canonical == path and getattr(route, "method", None) == method:
            return False

    add_method = getattr(app.router, f"add_{method.lower()}")
    add_method(path, handler)
    return True


def setup_routes(app):
    """Set up provider-neutral and compatibility API routes."""
    routes = [
        ("GET", WS_ROUTE, websocket_handler),
        ("GET", LEGACY_WS_ROUTE, websocket_handler),
        ("GET", f"{ROUTE_BASE}/workflow", workflow_handler),
        ("POST", f"{ROUTE_BASE}/workflow", workflow_handler),
        ("GET", f"{LEGACY_ROUTE_BASE}/workflow", workflow_handler),
        ("POST", f"{LEGACY_ROUTE_BASE}/workflow", workflow_handler),
        ("GET", f"{ROUTE_BASE}/workflow-clients", workflow_clients_handler),
        ("GET", f"{LEGACY_ROUTE_BASE}/workflow-clients", workflow_clients_handler),
        ("POST", f"{ROUTE_BASE}/run-node", run_node_handler),
        ("POST", f"{LEGACY_ROUTE_BASE}/run-node", run_node_handler),
        ("GET", f"{ROUTE_BASE}/graph-command", graph_command_handler),
        ("POST", f"{ROUTE_BASE}/graph-command", graph_command_handler),
        ("GET", f"{LEGACY_ROUTE_BASE}/graph-command", graph_command_handler),
        ("POST", f"{LEGACY_ROUTE_BASE}/graph-command", graph_command_handler),
        ("GET", f"{ROUTE_BASE}/memory", memory_stats_handler),
        ("GET", f"{LEGACY_ROUTE_BASE}/memory", memory_stats_handler),
        ("GET", f"{ROUTE_BASE}/platform", platform_info_handler),
        ("GET", f"{LEGACY_ROUTE_BASE}/platform", platform_info_handler),
        ("GET", f"{ROUTE_BASE}/clis", clis_handler),
        ("GET", f"{ROUTE_BASE}/settings", settings_handler),
        ("POST", f"{ROUTE_BASE}/settings", settings_handler),
    ]

    for method, path, handler in routes:
        add_route_once(app, method, path, handler)

    print(f"{PLUGIN_LOG_PREFIX} Terminal WebSocket endpoint registered at {WS_ROUTE}")
    print(f"{PLUGIN_LOG_PREFIX} Workflow API endpoint registered at {ROUTE_BASE}/workflow")
    print(f"{PLUGIN_LOG_PREFIX} Workflow clients endpoint registered at {ROUTE_BASE}/workflow-clients")
    print(f"{PLUGIN_LOG_PREFIX} Graph command endpoint registered at {ROUTE_BASE}/graph-command")
    print(f"{PLUGIN_LOG_PREFIX} CLI inventory endpoint registered at {ROUTE_BASE}/clis")
    print(f"{PLUGIN_LOG_PREFIX} Settings endpoint registered at {ROUTE_BASE}/settings")
    terminal_backend = get_terminal_backend_status()
    if IS_WINDOWS and not terminal_backend["supported"]:
        print(f"{PLUGIN_LOG_PREFIX} Note: {terminal_backend['reason']}")


# Hook into ComfyUI's server setup
try:
    from server import PromptServer

    setup_routes(PromptServer.instance.app)
    write_comfyui_url()
    maybe_setup_default_adapter_mcp()

    mem_mb = get_memory_mb()
    terminal_backend = get_terminal_backend_status()
    if IS_WINDOWS and not terminal_backend["supported"]:
        platform_note = " (Windows terminal backend unavailable)"
    elif IS_WINDOWS:
        platform_note = " (Windows terminal enabled)"
    else:
        platform_note = ""
    print(f"{PLUGIN_LOG_PREFIX} Plugin loaded successfully{platform_note} (Memory: {mem_mb:.1f}MB)")
except Exception as exc:
    print(f"{PLUGIN_LOG_PREFIX} Failed to register routes: {exc}")
    import traceback

    traceback.print_exc()
