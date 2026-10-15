"""External candidate process protocol, with fail-closed offline isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


class CandidateProcess:
    def __init__(self, mode: str, backend: str = "fake", config: dict | None = None,
                 timeout_seconds: float = 40.0, candidate_path: Path | None = None,
                 transport: str = "sdk"):
        if transport not in {"sdk", "stdlib"} or (transport == "stdlib" and backend != "openai"):
            raise ValueError("Explicit stdlib transport is only for reviewed live OpenAI execution")
        self._directory = tempfile.TemporaryDirectory(prefix="supportlab-candidate-")
        directory = Path(self._directory.name)
        candidate_path = candidate_path or Path(__file__).parent.parent / "llm.py"
        shutil.copyfile(candidate_path, directory / "llm.py")
        shutil.copyfile(Path(__file__).with_name("worker.py"), directory / "worker.py")
        if transport == "stdlib":
            shutil.copyfile(Path(__file__).parent.parent / "stdlib_transport.py", directory / "stdlib_transport.py")
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC"}
        if backend == "openai":
            if not config or not config.get("allow_paid"):
                raise ValueError("Live evaluation requires explicit approved budget and allow_paid")
            # Credentials are passed only to reviewed SDK transport. There is no
            # generic environment inheritance, no benchmark paths, no proxies.
            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("OPENAI_API_KEY is missing")
            environment["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        self.timeout_seconds = timeout_seconds
        self._process = subprocess.Popen([sys.executable, "-I", str(directory/"worker.py"), "--mode", mode, "--backend", backend,
                                          "--config", json.dumps(config or {"model": "fake-tool-v1"}), "--transport", transport],
                                         cwd=directory, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        self._buffer = b""
        self.telemetry: dict = {}
        try:
            ready = self._read()
            if not ready.get("ready"):
                raise RuntimeError("Candidate did not start")
            self.isolation = ready["isolation"]
        except BaseException:
            self.close()
            raise

    def _read(self, deadline: float | None = None) -> dict:
        deadline = deadline or time.monotonic() + self.timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Candidate response exceeded wall deadline")
                if not selector.select(remaining):
                    raise TimeoutError("Candidate response exceeded wall deadline")
                data = os.read(self._process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError(f"Candidate exited before response (exit={self._process.poll()})")
                self._buffer += data
                if len(self._buffer) > 1_000_000:
                    raise ValueError("Candidate response byte limit exceeded")
        line, self._buffer = self._buffer.split(b"\n", 1)
        result = json.loads(line)
        if not isinstance(result, dict):
            raise ValueError("Candidate response must be an object")
        return result

    def decide(self, payload: dict) -> dict:
        encoded = json.dumps({"payload": payload}, allow_nan=False).encode()+b"\n"
        if len(encoded) > 200000:
            raise ValueError("Candidate payload byte limit exceeded")
        deadline = time.monotonic() + self.timeout_seconds
        fd = self._process.stdin.fileno()
        os.set_blocking(fd, False)
        offset = 0
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            while offset < len(encoded):
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Candidate stopped reading its input before wall deadline")
                try:
                    offset += os.write(fd, encoded[offset:])
                except BlockingIOError:
                    continue
        reply = self._read(deadline)
        self.telemetry = reply.get("telemetry", {})
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["action"]

    def close(self):
        if getattr(self, "_process", None) is not None:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=5)
            for stream in [self._process.stdin, self._process.stdout]:
                if stream:
                    stream.close()
        self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
