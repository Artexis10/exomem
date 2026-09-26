# A never-ready variant of the 3.10 stand-in cell image (see exomem.py's
# module docstring): used only to force a canary failure in
# tests/test_k3s_integration.py's forced-upgrade-failure scenario.
FROM python:3.12-alpine
RUN apk add --no-cache restic
COPY exomem.py /usr/local/bin/exomem
RUN chmod +x /usr/local/bin/exomem
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
