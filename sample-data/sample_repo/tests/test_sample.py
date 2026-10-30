"""Predictable unit tests for sample_app."""
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sample_app.main import compute_square
from src.sample_app.utils import add_numbers, format_greeting


def test_format_greeting():
    assert format_greeting("Alice") == "Hello, Alice!"
    assert format_greeting("  Bob  ") == "Hello, Bob!"


def test_add_numbers():
    assert add_numbers(2, 3) == 5.0
    assert add_numbers(-1, 1) == 0.0


def test_compute_square():
    assert compute_square(4) == 16
    assert compute_square(-5) == 25

