"""Deck as Controller: Decky backend.

Decky's bundled Python can't load the system D-Bus/GLib bindings, so the real
work happens in a daemon (py_modules/sdcd) run by the system Python. This
module supervises it, relays its state to the frontend and guarantees cleanup.
"""
import asyncio
import json
import os

import decky

SYSTEM_PYTHON = "/usr/bin/python3"
DEFAULT_OPTIONS = {"screen_off": True, "deadzone": 0.08}


def _daemon_env() -> dict:
    # Drop the loader's PyInstaller environment so the system Python starts cleanly.
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE")}
    env["PYTHONPATH"] = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Plugin:
    proc: asyncio.subprocess.Process | None = None
    state: dict = {}
    options: dict = {}

    # ---- lifecycle --------------------------------------------------------

    async def _main(self):
        self.options = self._load_options()
        self.state = self._idle_state()
        await self._restore()  # clean up after a crash, if any

    async def _unload(self):
        await self.set_enabled(False)

    async def _uninstall(self):
        await self._restore()

    # ---- frontend API -----------------------------------------------------

    async def get_state(self) -> dict:
        return self.state

    async def set_enabled(self, enabled: bool) -> dict:
        if enabled and not self._running():
            await self._start()
        elif not enabled and self._running():
            await self._send({"cmd": "stop"})
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                decky.logger.warning("daemon did not stop in time; killing it")
                self.proc.kill()
                await self.proc.wait()
        return self.state

    async def pair(self):
        await self._send({"cmd": "pair"})

    async def toggle_screen(self):
        await self._send({"cmd": "screen"})

    async def set_option(self, key: str, value) -> dict:
        if key in DEFAULT_OPTIONS:
            self.options[key] = value
            self._save_options()
            await self._send({"cmd": "options", key: value})
            self.state = {**self.state, "options": self.options}
        return self.state

    # ---- daemon -----------------------------------------------------------

    def _running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _start(self):
        self.proc = await asyncio.create_subprocess_exec(
            SYSTEM_PYTHON, "-m", "sdcd",
            "--settings-dir", decky.DECKY_PLUGIN_SETTINGS_DIR,
            "--options", json.dumps(self.options),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_daemon_env())
        await self._publish({**self.state, "running": True, "state": "starting", "error": None})
        asyncio.get_event_loop().create_task(self._pump_stdout(self.proc))
        asyncio.get_event_loop().create_task(self._pump_stderr(self.proc))

    async def _send(self, msg: dict):
        if not self._running():
            return
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _pump_stdout(self, proc):
        error = None
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.pop("type", None)
            if kind == "state":
                await self._publish({**self.state, **event, "running": True})
            elif kind == "error":
                await decky.emit("error", event.get("message", ""))
            elif kind == "stopped":
                reason = event.get("reason", "")
                if reason.startswith("error"):
                    error = reason
                elif reason == "stopped from the Deck":
                    await decky.emit("stopped_from_deck")
        await proc.wait()
        await self._restore()
        await self._publish({**self._idle_state(), "hosts": self.state.get("hosts", []),
                             "error": error or (f"daemon exited with code {proc.returncode}"
                                                if proc.returncode else None)})

    async def _pump_stderr(self, proc):
        async for line in proc.stderr:
            decky.logger.info("sdcd: %s", line.decode(errors="replace").rstrip())

    async def _restore(self):
        """Put Bluetooth, the controller and the screen back to normal. Idempotent."""
        proc = await asyncio.create_subprocess_exec(
            SYSTEM_PYTHON, "-m", "sdcd", "--restore",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_daemon_env())
        out, _ = await proc.communicate()
        if proc.returncode:
            decky.logger.error("restore failed: %s", out.decode(errors="replace"))

    async def _publish(self, state: dict):
        self.state = state
        await decky.emit("state", state)

    # ---- settings ---------------------------------------------------------

    def _idle_state(self) -> dict:
        return {"running": False, "state": "off", "host": "", "hosts": self._load_hosts(),
                "screen_off": False, "options": self.options, "error": None}

    def _load_hosts(self) -> list:
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "hosts.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _load_options(self) -> dict:
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "options.json")) as f:
                return {**DEFAULT_OPTIONS, **json.load(f)}
        except (OSError, ValueError):
            return dict(DEFAULT_OPTIONS)

    def _save_options(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        with open(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "options.json"), "w") as f:
            json.dump(self.options, f)
