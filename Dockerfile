# Neko Firefox + Automation Service
#
# Extends the official Neko Firefox image with:
#   * a persistent Python automation service (FastAPI, port 9100)
#   * Playwright + headless Chromium for session-reusing scraping
#   * Firefox remote debugging (WebDriver BiDi on 127.0.0.1:9222) for
#     live tab discovery/control
#   * fixed policies.json: cookies/sessions are NO LONGER wiped on
#     shutdown, and tabs are restored on startup
#
# Build context: repository root (this file + ./automation).
FROM ghcr.io/m1k1o/neko/firefox:latest

USER root

# python + pip
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 python3-pip; \
    apt-get clean -y; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# automation service dependencies + headless chromium
# (--ignore-installed: Debian's pip cannot uninstall apt-managed packages)
# PLAYWRIGHT_BROWSERS_PATH: install browsers to a shared location so the
# service (running as user neko) can use them, not just root's cache)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
COPY automation/requirements.txt /tmp/automation-requirements.txt
RUN set -eux; \
    pip3 install --break-system-packages --no-cache-dir --ignore-installed \
        -r /tmp/automation-requirements.txt; \
    python3 -m playwright install --with-deps chromium; \
    chmod -R a+rX /opt/ms-playwright; \
    rm -f /tmp/automation-requirements.txt

# automation service code
COPY automation /opt/neko-automation
RUN chmod +x /opt/neko-automation/supervisord.automation.conf || true

# firefox: enable WebDriver BiDi (loopback only inside the container)
COPY automation/firefox.conf /etc/neko/supervisord/firefox.conf
# policies: keep cookies/sessions across restarts, restore previous session
COPY automation/policies.json /usr/lib/firefox/distribution/policies.json
# automation service under supervisord (auto-start, auto-restart)
COPY automation/supervisord.automation.conf /etc/neko/supervisord/automation.conf

# persistent state (jobs, site registry, audit log) - mount a volume here
RUN mkdir -p /var/lib/neko-automation && chown neko:neko /var/lib/neko-automation

ENV NEKO_AUTOMATION_PROFILE_DIR=/home/neko/.mozilla/firefox \
    NEKO_AUTOMATION_STATE_DIR=/var/lib/neko-automation \
    PYTHONUNBUFFERED=1

# 8080 neko web UI / WebRTC signalling; 9100 automation API
EXPOSE 8080 9100
