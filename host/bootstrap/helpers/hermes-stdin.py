"""Run one Hermes prompt without placing prompt content in process arguments."""

from __future__ import annotations

import argparse
import importlib
import os
import sys


MAX_PROMPT_BYTES = 1_000_000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--resume")
    args = parser.parse_args()

    raw_prompt = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if len(raw_prompt) > MAX_PROMPT_BYTES:
        raise SystemExit("Hermes prompt exceeds the launcher byte limit")
    try:
        prompt = raw_prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Hermes prompt is not valid UTF-8") from exc
    if not prompt:
        raise SystemExit("Hermes prompt is empty")

    # cmd_chat normally translates --yolo into this variable before calling
    # cli.main. The wrapper calls cli.main directly so the prompt can arrive
    # over stdin instead of argparse/argv.
    os.environ["HERMES_YOLO_MODE"] = "1"

    # Connect the bundled-tools MCP shim (mcp_servers.trustyclaw in the
    # managed ~/.hermes/config.yaml) before the agent snapshots its tool
    # list. Hermes only starts MCP discovery from its TUI, gateway, and ACP
    # entrypoints, never from the single-query path this wrapper uses, so
    # discovery must run here, synchronously: the server is a local stdio
    # spawn, and a background thread could miss the first (only) turn. A
    # shim that fails to serve tools just leaves them unregistered, matching
    # the shim's own omit-unavailable contract for the other harnesses.
    importlib.import_module("tools.mcp_tool").discover_mcp_tools()

    hermes_main = importlib.import_module("cli").main

    hermes_main(
        query=prompt,
        model=args.model,
        toolsets="terminal,file,trustyclaw",
        quiet=True,
        resume=args.resume,
        pass_session_id=True,
    )


if __name__ == "__main__":
    main()
