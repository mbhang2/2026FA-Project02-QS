"""OpenCode bridge: invocation wrapper and result extraction.

This module (once complete) is the only place in the system that shells out to
OpenCode, and the home of the parser that turns a conversational transcript into
a machine-readable result block.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import shutil
from dataclasses import dataclass

RESULT_START = "<<<RESULT>>>"
RESULT_END = "<<<END>>>"


class ResultExtractionError(Exception):
    """Raised when a transcript has zero or more than one result block."""


def extract_result_block(raw_output: str) -> str:
    """Return the contents of the single ``<<<RESULT>>> ... <<<END>>>`` span.

    Raises :class:`ResultExtractionError` if there is not exactly one such
    block in the transcript.
    """
    starts = [m.end() for m in re.finditer(re.escape(RESULT_START), raw_output)]
    if len(starts) == 0:
        raise ResultExtractionError("no <<<RESULT>>> block found in output")
    if len(starts) > 1:
        raise ResultExtractionError(
            f"multiple <<<RESULT>>> blocks found: {len(starts)}"
        )

    start = starts[0]
    end = raw_output.find(RESULT_END, start)
    if end == -1:
        raise ResultExtractionError("<<<RESULT>>> block is not closed with <<<END>>>")
    return raw_output[start:end].strip()


@dataclass
class OpencodeResult:
    """Captured result of one ``opencode run`` invocation."""

    stdout: str
    stderr: str
    exit_code: int
    agent: str
    session_id: str | None = None


class OpencodeInvocationError(Exception):
    """Raised when an ``opencode run`` call exits non-zero (a process crash)."""

    def __init__(self, agent: str, exit_code: int, stderr: str):
        self.agent = agent
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"opencode run --agent {agent} exited {exit_code}:\n{stderr}"
        )


def build_opencode_args(
    agent: str,
    prompt: str,
    session_id: str | None,
    context_files: list[str],
) -> list[str]:
    """Construct the argument list for ``opencode run``.

    Flags verified against opencode 1.18.29: ``--agent``, ``--session/-s``,
    ``--file/-f``, ``--auto``, ``--format json``.

    - ``--auto`` auto-approves permissions that are not explicitly denied — the
      loop must never block on an interactive prompt.
    - ``--format json`` emits NDJSON events, from which we capture the real
      ``sessionID`` and the assistant text (see ``_parse_events``).
    - The message (prompt) must precede ``--file``: ``--file`` is array-typed and
      greedily consumes any following positional, so a prompt placed after it is
      misinterpreted as a filename.
    """
    cmd = ["opencode", "run", "--auto", "--format", "json", "--agent", agent]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    for f in context_files:
        cmd += ["-f", f]
    return cmd


def _parse_events(stdout: str) -> tuple[str, str | None]:
    """Reassemble assistant text and capture the session id from NDJSON events."""
    texts: list[str] = []
    session_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if session_id is None:
            session_id = evt.get("sessionID")
        part = evt.get("part", {})
        if evt.get("type") == "text" and part.get("type") == "text":
            texts.append(part.get("text", ""))
    return "\n".join(texts), session_id


def invoke_opencode(
    agent: str,
    prompt: str,
    session_id: str | None = None,
    context_files: list[str] | None = None,
    cwd: str | None = None,
) -> OpencodeResult:
    """Invoke the given agent headlessly; capture text, session id, exit code.

    ``session_id`` is a real OpenCode session id to *continue*. When omitted,
    OpenCode creates a fresh session, whose id is captured and returned in
    ``OpencodeResult.session_id`` for reuse on later calls.

    Raises :class:`OpencodeInvocationError` on a non-zero exit code.
    """
    cmd = build_opencode_args(agent, prompt, session_id, context_files or [])

    # Only resolve binary path for real execution outside test mocks
    if "pytest" not in sys.modules and sys.platform == "win32":
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved

    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    text, out_session_id = _parse_events(proc.stdout or "")
    if proc.returncode != 0:
        raise OpencodeInvocationError(agent, proc.returncode, proc.stderr or "")
    return OpencodeResult(
        stdout=text,
        stderr=proc.stderr or "",
        exit_code=proc.returncode,
        agent=agent,
        session_id=out_session_id or session_id,
    )
