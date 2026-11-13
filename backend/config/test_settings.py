"""The suite runs against a real PostgreSQL: these defaults reach the one the development
compose file publishes on localhost (see README); CI and a developer can override them."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "dev-only-postgres-password")
os.environ.setdefault("SOLVER_TOKEN", "test-token")

from config.settings import *  # noqa: E402, F403

# The password hash is the slow part of a login, on purpose. Not here.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
