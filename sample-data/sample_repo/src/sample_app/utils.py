"""Clean helper utilities with zero code smells or issues."""


def format_greeting(name: str) -> str:
    """Return a formatted greeting message."""
    cleaned = name.strip() if isinstance(name, str) else "Guest"
    return f"Hello, {cleaned}!"


def add_numbers(a: float, b: float) -> float:
    """Return the arithmetic sum of two numbers."""
    return a + b

