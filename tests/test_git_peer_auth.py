"""Verification tests for Git peer credential authentication and process-ancestry validation."""

import os
import sys
import pytest


def parse_proc_stat(stat_content: str) -> dict:
    """Robust parser for Linux /proc/<pid>/stat handling spaces and parentheses in comm."""
    rparen_idx = stat_content.rfind(")")
    if rparen_idx == -1:
        raise ValueError("Malformed stat file: missing closing parenthesis in comm")
    
    prefix = stat_content[:rparen_idx]
    lparen_idx = prefix.find("(")
    if lparen_idx == -1:
        raise ValueError("Malformed stat file: missing opening parenthesis in comm")
        
    pid_str = prefix[:lparen_idx].strip()
    comm = prefix[lparen_idx + 1:]
    rest = stat_content[rparen_idx + 1:].strip().split()
    
    # In Linux /proc/<pid>/stat:
    # field 1: pid
    # field 2: comm
    # field 3: state (rest[0])
    # field 4: ppid (rest[1])
    # field 22: starttime (rest[19])
    if len(rest) < 20:
        raise ValueError("Malformed stat file: insufficient fields")
        
    return {
        "pid": int(pid_str),
        "comm": comm,
        "state": rest[0],
        "ppid": int(rest[1]),
        "starttime": int(rest[19]),
    }


def verify_linux_ancestry(
    peer_pid: int,
    expected_git_pid: int,
    expected_python_pid: int,
    stat_provider: dict,  # maps pid -> stat_content string
    max_depth: int = 5
) -> bool:
    """Simulates Linux process-ancestry verification via /proc/<pid>/stat.
    
    Returns True if peer_pid descends from expected_git_pid whose parent is expected_python_pid.
    Fails closed (returns False or raises) on any anomaly.
    """
    curr_pid = peer_pid
    child_starttime = None
    
    for _ in range(max_depth):
        if curr_pid not in stat_provider:
            # Process terminated or inaccessible /proc -> fail closed
            return False
            
        try:
            stat_info = parse_proc_stat(stat_provider[curr_pid])
        except Exception:
            # Malformed /proc entry -> fail closed
            return False
            
        # Monotonic starttime check against PID reuse:
        # Child process must have started at or after parent process starttime
        if child_starttime is not None and stat_info["starttime"] > child_starttime:
            # Parent started AFTER child -> PID reuse detected! Fail closed.
            return False
            
        child_starttime = stat_info["starttime"]
        
        if curr_pid == expected_git_pid:
            # Verify git_pid's parent is python_pid
            return stat_info["ppid"] == expected_python_pid
            
        curr_pid = stat_info["ppid"]
        if curr_pid <= 1:
            # Reached init or invalid PID without matching git_pid
            return False
            
    return False


def test_parse_proc_stat_with_spaces_and_parentheses():
    """Verify robust parsing when comm contains tricky characters."""
    raw_stat = "1234 (python (test) 3.11) S 5678 1234 1234 0 -1 4194304 100 0 0 0 10 20 0 0 20 0 1 0 54321 12345 6789"
    parsed = parse_proc_stat(raw_stat)
    assert parsed["pid"] == 1234
    assert parsed["comm"] == "python (test) 3.11"
    assert parsed["state"] == "S"
    assert parsed["ppid"] == 5678
    assert parsed["starttime"] == 54321


def test_linux_ancestry_verification_success():
    """Verify valid process hierarchy passes:
    Python (100) -> Git (200) -> git-remote-https (300) -> askpass_helper (400)
    """
    stat_db = {
        400: "400 (askpass_helper) S 300 400 400 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50300 0 0",
        300: "300 (git-remote-https) S 200 300 300 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50200 0 0",
        200: "200 (git) S 100 200 200 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50100 0 0",
        100: "100 (python) S 1 100 100 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50000 0 0",
    }
    
    assert verify_linux_ancestry(
        peer_pid=400,
        expected_git_pid=200,
        expected_python_pid=100,
        stat_provider=stat_db
    ) is True


def test_linux_ancestry_fails_closed_on_unrelated_process():
    """Verify that an unrelated process trying to connect fails verification."""
    stat_db = {
        999: "999 (malicious_probe) S 888 999 999 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50400 0 0",
        888: "888 (bash) S 1 888 888 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 40000 0 0",
    }
    
    assert verify_linux_ancestry(
        peer_pid=999,
        expected_git_pid=200,
        expected_python_pid=100,
        stat_provider=stat_db
    ) is False


def test_linux_ancestry_fails_closed_on_pid_reuse():
    """Verify that PID reuse (parent starttime newer than child starttime) is rejected."""
    stat_db = {
        400: "400 (askpass_helper) S 300 400 400 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50000 0 0", # started at tick 50000
        300: "300 (git-remote-https) S 200 300 300 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 60000 0 0", # started at tick 60000 (after child!)
        200: "200 (git) S 100 200 200 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 45000 0 0",
    }
    
    assert verify_linux_ancestry(
        peer_pid=400,
        expected_git_pid=200,
        expected_python_pid=100,
        stat_provider=stat_db
    ) is False


def test_linux_ancestry_fails_closed_on_missing_proc():
    """Verify that missing /proc entry (process died during walk) fails closed."""
    stat_db = {
        400: "400 (askpass_helper) S 300 400 400 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 50300 0 0",
        # 300 is missing (terminated abruptly)
    }
    
    assert verify_linux_ancestry(
        peer_pid=400,
        expected_git_pid=200,
        expected_python_pid=100,
        stat_provider=stat_db
    ) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific LOCAL_PEERPID verification")
def test_macos_local_peerpid_and_libproc():
    """Verify that on macOS, getsockopt LOCAL_PEERPID and libproc.proc_pidinfo work as specified."""
    import socket, struct, threading, ctypes
    
    # 1. Test getsockopt(0, 2, 4) for LOCAL_PEERPID
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock_path = "/tmp/test_peerpid_val.sock"
    if os.path.exists(sock_path): os.unlink(sock_path)
    server.bind(sock_path)
    server.listen(1)
    
    stop_event = threading.Event()
    def client():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        stop_event.wait()
        s.close()
        
    t = threading.Thread(target=client)
    t.start()
    conn, _ = server.accept()
    
    raw_pid = conn.getsockopt(0, 2, 4)
    peer_pid = struct.unpack("i", raw_pid)[0]
    assert peer_pid == os.getpid() # Same process running client thread
    
    stop_event.set()
    t.join()
    conn.close()
    server.close()
    if os.path.exists(sock_path): os.unlink(sock_path)
    
    # 2. Test libproc.proc_pidinfo for PPID
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    buf = ctypes.create_string_buffer(1024)
    PROC_PIDTBSDINFO = 3
    bytes_written = libproc.proc_pidinfo(os.getpid(), PROC_PIDTBSDINFO, 0, buf, 1024)
    assert bytes_written > 0
    pbi_pid, pbi_ppid = struct.unpack_from("II", buf.raw, 12)
    assert pbi_pid == os.getpid()
    assert pbi_ppid == os.getppid()

