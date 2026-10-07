"""Configuration for the Neko automation service (env-driven)."""
import os


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- auth -------------------------------------------------------------
    # Bearer token required on every /api/* route. If empty the API refuses
    # to serve anything except /health and only listens on loopback.
    TOKEN: str = os.environ.get("NEKO_AUTOMATION_TOKEN", "").strip()
    # Master switch for the raw cookie-export endpoint (off by default).
    ALLOW_COOKIE_EXPORT: bool = _bool("NEKO_AUTOMATION_ALLOW_COOKIE_EXPORT")

    # --- network ----------------------------------------------------------
    HOST: str = os.environ.get("NEKO_AUTOMATION_BIND", "0.0.0.0")
    PORT: int = int(os.environ.get("NEKO_AUTOMATION_PORT", "9100"))
    # Optional path prefix when routed behind a reverse proxy that does NOT
    # strip the prefix (e.g. Traefik PathPrefix rule). The router is mounted
    # at both / and <prefix>/ so both routing styles work.
    PREFIX: str = os.environ.get("NEKO_AUTOMATION_PREFIX", "").rstrip("/")

    # --- browser ----------------------------------------------------------
    PROFILE_DIR: str = os.environ.get(
        "NEKO_AUTOMATION_PROFILE_DIR", "/home/neko/.mozilla/firefox")
    BIDI_URL: str = os.environ.get(
        "NEKO_AUTOMATION_BIDI_URL", "ws://127.0.0.1:9222/session")
    # Seconds to wait after session.new before first command (Firefox race).
    BIDI_SETTLE: float = float(os.environ.get("NEKO_AUTOMATION_BIDI_SETTLE", "5"))
    # Last-resort recovery for leaked BiDi sessions (they block new sessions
    # until Firefox restarts). Requires supervisorctl access.
    ALLOW_BROWSER_RESTART: bool = _bool("NEKO_AUTOMATION_ALLOW_BROWSER_RESTART", "true")
    SUPERVISOR_CONF: str = os.environ.get(
        "NEKO_AUTOMATION_SUPERVISOR_CONF", "/etc/neko/supervisord.conf")

    # --- playwright -------------------------------------------------------
    PLAYWRIGHT_BROWSER: str = os.environ.get("NEKO_AUTOMATION_PLAYWRIGHT_BROWSER", "chromium")
    CONTEXT_IDLE_TTL: int = int(os.environ.get("NEKO_AUTOMATION_CONTEXT_IDLE_TTL", "900"))

    # --- state / persistence ----------------------------------------------
    STATE_DIR: str = os.environ.get("NEKO_AUTOMATION_STATE_DIR", "/var/lib/neko-automation")
    AUDIT_LOG: str = os.environ.get("NEKO_AUTOMATION_AUDIT_LOG", "")

    # --- jobs ---------------------------------------------------------------
    JOB_CONCURRENCY: int = int(os.environ.get("NEKO_AUTOMATION_JOB_CONCURRENCY", "1"))
    # Network capture ring buffer per page
    NETWORK_BUFFER: int = int(os.environ.get("NEKO_AUTOMATION_NETWORK_BUFFER", "500"))
    NETWORK_BODY_LIMIT: int = int(os.environ.get("NEKO_AUTOMATION_NETWORK_BODY_LIMIT", str(2 * 1024 * 1024)))

    def __post_init__(self) -> None:  # pragma: no cover
        pass


CFG = Config()
