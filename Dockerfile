# Dokploy expects a root Dockerfile when using Docker build mode.
# Use the official Neko Firefox image, which already includes
# the server, web client assets, and runtime dependencies.
FROM ghcr.io/m1k1o/neko/firefox:latest
