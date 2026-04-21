"""Async stdio JSON-RPC reader/writer for MCP protocol.

All MCP traffic flows through stdin (incoming) and stdout (outgoing).
Logging and user-facing messages go to stderr so they never corrupt the
JSON-RPC stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


class StdioHandler:
    """Reads JSON-RPC messages from stdin and writes responses to stdout."""

    def __init__(self) -> None:
        self._reader: Optional[asyncio.StreamReader] = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._reader),
            sys.stdin.buffer,
        )

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed JSON-RPC messages from stdin, one per line."""
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
                if not line:
                    logger.info("stdin closed, shutting down")
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                    logger.debug("stdin  <<  %s", _truncate(text))
                    yield msg
                except json.JSONDecodeError:
                    logger.warning("Ignoring invalid JSON from stdin: %s", _truncate(text))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Error reading from stdin", exc_info=True)
                break

    async def write_message(self, msg: dict[str, Any]) -> None:
        """Write a single JSON-RPC message to stdout."""
        async with self._write_lock:
            data = json.dumps(msg, separators=(",", ":"))
            logger.debug("stdout >>  %s", _truncate(data))
            sys.stdout.buffer.write(data.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()

    async def write_error(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:
        """Write a JSON-RPC error response."""
        error_msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        await self.write_message(error_msg)


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
