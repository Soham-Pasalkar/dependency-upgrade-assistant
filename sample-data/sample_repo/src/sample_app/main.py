"""Sample application entry point for end-to-end verification."""
import requests


def fetch_data(url: str) -> dict:
    """Fetch JSON payload from a remote endpoint.
    
    Contains an intentional bare except clause (code smell) for testing.
    """
    try:
        response = requests.get(url, timeout=10)
        return response.json()
    except:
        return {}


def compute_square(x: int) -> int:
    """Return the square of an integer."""
    return x * x

