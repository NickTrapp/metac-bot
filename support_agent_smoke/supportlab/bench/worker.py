"""Trusted bootstrap copied with llm.py into a candidate-only temporary directory.

Offline mode installs a fail-closed syscall allowlist BEFORE executing candidate
source. No filesystem opens, socket creation, process spawning, or tracing are
permitted thereafter. SDK live mode explicitly has a weaker threat model: fixed,
reviewed trusted Python transport; only the remote model is untrusted.
"""
from __future__ import annotations

# Preload the exact standalone adapter's standard library dependencies, before
# seccomp. Reading candidate source is not execution; top-level candidate code
# first runs under the filter. Do not import candidate in the bootstrap.
import argparse
from collections import deque
import ctypes
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import sys
import time
import types
from typing import Any, Literal, Protocol
from urllib.parse import urlparse


def install_offline_filter() -> None:
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    # EPERM default. Anything not explicitly allowed is denied, including every
    # pathname open, network syscall, exec/clone/fork, ptrace, process_vm_readv,
    # pidfd_getfd and io_uring. fstat can inspect only inherited pipe descriptors.
    context = library.seccomp_init(0x00050000 | 1)
    if not context:
        raise RuntimeError("seccomp_init failed")
    allowed = ["read", "write", "close", "fstat", "lseek", "brk", "mmap", "mprotect", "munmap", "mremap", "madvise",
               "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack", "futex", "clock_gettime", "gettimeofday",
               "time", "getpid", "gettid", "getuid", "geteuid", "getgid", "getegid", "getrandom", "sched_yield",
               "restart_syscall", "nanosleep", "clock_nanosleep", "exit", "exit_group"]
    try:
        for name in allowed:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add(context, 0x7FFF0000, number, 0) != 0:
                raise RuntimeError(f"seccomp_rule_add failed for {name}")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("seccomp_load failed; offline evaluation requires Linux seccomp")
    finally:
        library.seccomp_release(context)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", required=True, choices=["direct", "procedure"])
    parser.add_argument("--backend", required=True, choices=["fake", "openai"])
    parser.add_argument("--transport", default="sdk", choices=["sdk", "stdlib"])
    args = parser.parse_args()
    config = json.loads(args.config)
    candidate_source = Path(__file__).with_name("llm.py").read_text()
    date.today()
    time.localtime()
    # CPU budget is cumulative per episode; parent independently enforces wall
    # deadlines and JSON response byte limits. The worker gets no evaluator FDs.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
    if args.backend == "fake":
        install_offline_filter()
    module = types.ModuleType("candidate_llm")
    module.__file__ = "<isolated_candidate_llm>"
    sys.modules[module.__name__] = module
    exec(compile(candidate_source, module.__file__, "exec"), module.__dict__)
    if config.get("pricing"):
        config["pricing"] = module.Pricing(**config["pricing"])
    agent_config = module.AgentConfig(**config)
    if args.backend == "fake":
        backend = module.FakeToolBackend()
    elif args.transport == "stdlib":
        transport_module = types.ModuleType("candidate_transport")
        transport_module.__file__ = "<reviewed_stdlib_transport>"
        sys.modules[transport_module.__name__] = transport_module
        transport_source = Path(__file__).with_name("stdlib_transport.py").read_text()
        exec(compile(transport_source, transport_module.__file__, "exec"), transport_module.__dict__)
        backend = transport_module.StdlibResponsesBackend(timeout_seconds=agent_config.timeout_seconds)
    else:
        backend = module.OpenAIResponsesBackend(timeout_seconds=agent_config.timeout_seconds)
    agent = module.LLMAgent(backend=backend, config=agent_config, mode=args.mode)
    print(json.dumps({"ready": True, "isolation": "seccomp-no-files-no-network" if args.backend == "fake" else "trusted-python-remote-model-only"}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            action = agent.decide(request["payload"])
            response = {"action": action, "telemetry": agent.telemetry}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}", "telemetry": agent.telemetry}
        print(json.dumps(response, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
