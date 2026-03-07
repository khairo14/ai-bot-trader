"""
pytest configuration for the backend test suite.

Sets environment variables required by pydantic Settings before any module
import touches config.py, so tests never need a real .env file.
"""
import os

# Force-override the insecure-default guard (F-001) for the test environment.
# Must use direct assignment (not setdefault) because docker-compose injects
# SECRET_KEY=change_this from .env into the container env before pytest starts.
os.environ["SECRET_KEY"] = (
    "test_only_not_for_prod_aabbccddeeff00112233445566778899aabbccddeeff0011"
)
