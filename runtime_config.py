"""Fail-closed runtime configuration checks for the PAPER-only web app."""
import os
from pathlib import Path


def validate_runtime_environment(environ=None):
    environ = os.environ if environ is None else environ
    token = environ.get("PAPER_API_TOKEN", "").strip()
    db_path = environ.get("PAPER_DB_PATH", "").strip()
    missing = [name for name, value in (("PAPER_API_TOKEN", token), ("PAPER_DB_PATH", db_path)) if not value]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    if len(token) < 32:
        raise RuntimeError("PAPER_API_TOKEN must contain at least 32 characters")
    # Render paths use POSIX syntax; accept that syntax in Windows-based CI too.
    if not (Path(db_path).is_absolute() or db_path.startswith("/")):
        raise RuntimeError("PAPER_DB_PATH must be an absolute path")

    on_render = any(environ.get(key) for key in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL"))
    require_disk = on_render or environ.get("PAPER_REQUIRE_PERSISTENT_DB", "").lower() == "true"
    if require_disk:
        mount = Path(environ.get("PAPER_PERSISTENT_DISK_PATH", "/var/data")).resolve()
        target = Path(db_path).resolve()
        if target == mount or mount not in target.parents:
            raise RuntimeError(f"PAPER_DB_PATH must be inside the mounted persistent disk: {mount}")

    # The returned config intentionally contains no trading-mode switch and no secret.
    return {"database_path": db_path, "paper_only": True}
