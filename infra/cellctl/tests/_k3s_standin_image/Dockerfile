# Minimal stand-in cell image for tests/test_k3s_integration.py (task 3.10).
# Not a real Exomem Cloud runtime -- see exomem.py's module docstring.
# restic 0.17+ per D8 (the backup/restore Jobs use this same image); lane
# A's real image pins 0.19.1, alpine's community package is close enough for
# this stand-in's purposes.
FROM python:3.12-alpine
RUN apk add --no-cache restic
COPY exomem.py /usr/local/bin/exomem
RUN chmod +x /usr/local/bin/exomem
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
