"""Streaming output capture with strict byte budget enforcement and ANSI sanitization."""
from __future__ import annotations

import logging
import re
import threading
from typing import IO, List, Tuple

logger = logging.getLogger(__name__)

# Strict 512 KB combined ceiling
DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024

# ANSI terminal control code regex
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_sequences(text: str) -> str:
    """Remove ANSI escape sequences and dangerous terminal control codes."""
    if not text:
        return ""
    return ANSI_ESCAPE_PATTERN.sub("", text)


class BoundedStreamCapture:
    """Thread-safe streaming buffer reader enforcing a hard memory ceiling."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
        self.max_bytes = max_bytes
        self.stdout_chunks: List[str] = []
        self.stderr_chunks: List[str] = []
        self.total_bytes = 0
        self.truncated = False
        self._lock = threading.Lock()

    def _append_chunk(self, data: str, is_stdout: bool) -> None:
        with self._lock:
            if not data:
                return

            sanitized = strip_ansi_sequences(data)
            encoded_len = len(sanitized.encode("utf-8", errors="replace"))

            if self.total_bytes >= self.max_bytes:
                if not self.truncated:
                    notice = f"\n... [Output truncated after reaching {self.max_bytes} bytes limit] ...\n"
                    if is_stdout:
                        self.stdout_chunks.append(notice)
                    else:
                        self.stderr_chunks.append(notice)
                    self.truncated = True
                return

            remaining = self.max_bytes - self.total_bytes
            if encoded_len > remaining:
                # Truncate chunk to fit remaining quota
                encoded = sanitized.encode("utf-8", errors="replace")[:remaining]
                safe_str = encoded.decode("utf-8", errors="replace")
                if is_stdout:
                    self.stdout_chunks.append(safe_str)
                else:
                    self.stderr_chunks.append(safe_str)
                self.total_bytes += len(encoded)

                notice = f"\n... [Output truncated after reaching {self.max_bytes} bytes limit] ...\n"
                if is_stdout:
                    self.stdout_chunks.append(notice)
                else:
                    self.stderr_chunks.append(notice)
                self.truncated = True
            else:
                if is_stdout:
                    self.stdout_chunks.append(sanitized)
                else:
                    self.stderr_chunks.append(sanitized)
                self.total_bytes += encoded_len

    def capture_streams(
        self,
        stdout_pipe: Optional[IO[str]],
        stderr_pipe: Optional[IO[str]],
    ) -> Tuple[threading.Thread, threading.Thread]:
        """Start daemon reader threads to stream stdout and stderr without blocking."""

        def reader(pipe: Optional[IO[str]], is_stdout: bool) -> None:
            if not pipe:
                return
            try:
                for chunk in iter(lambda: pipe.read(4096), ""):
                    if not chunk:
                        break
                    self._append_chunk(chunk, is_stdout)
            except Exception as e:
                logger.debug(f"Stream reader finished or pipe closed: {e}")
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=reader, args=(stdout_pipe, True), daemon=True)
        t_err = threading.Thread(target=reader, args=(stderr_pipe, False), daemon=True)

        t_out.start()
        t_err.start()
        return t_out, t_err

    def get_output(self) -> Tuple[str, str, bool]:
        """Return (stdout, stderr, is_truncated)."""
        with self._lock:
            out_str = "".join(self.stdout_chunks)
            err_str = "".join(self.stderr_chunks)
            return out_str, err_str, self.truncated

