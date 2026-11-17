# Always-on host for familiekilder-mcp: MCP over HTTP behind the built-in OAuth.
#
# From a source checkout, in the familiekilder directory:
#   docker build -f src/familiekilder/data/container/Dockerfile -t familiekilder-mcp .
# A released version from PyPI instead of the checkout:
#   --build-arg FAMILIEKILDER_PACKAGE=familiekilder-mcp==VERSION
# Word documents need LibreOffice, which makes the image much larger:
#   --build-arg WITH_LIBREOFFICE=1
#
# Data lives in /data/familiekilder (mode 0700). The server runs as the
# unprivileged user "familiekilder"; see docs/alltid-pa.md.
FROM python:3.12-slim-bookworm

ARG FAMILIEKILDER_PACKAGE=/build
ARG WITH_LIBREOFFICE=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FAMILIEKILDER_ROOT=/data/familiekilder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends poppler-utils \
    && if [ "$WITH_LIBREOFFICE" = "1" ]; then \
        apt-get install --yes --no-install-recommends libreoffice-writer-nogui; \
    fi \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --user-group --create-home \
        --home-dir /home/familiekilder --shell /usr/sbin/nologin familiekilder \
    && mkdir -p /data/familiekilder \
    && chown -R familiekilder:familiekilder /data \
    && chmod 0700 /data/familiekilder

COPY . /build
RUN pip install "$FAMILIEKILDER_PACKAGE" && rm -rf /build

EXPOSE 8080
# Starts as root only to prepare the data disk, then drops to "familiekilder".
ENTRYPOINT ["python", "-m", "familiekilder.container"]
