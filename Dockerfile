# Lightweight Linux image for the Automafile pipeline.
# Works under Docker Desktop, Podman Desktop, or any Compose-compatible runtime.

FROM python:3.12-slim

# system dependencies:
#   tesseract-ocr + heb/eng — OCR engine and language packs
#   poppler-utils           — pdf2image (PDF rasterization for OCR)
#   libmagic1               — python-magic (MIME sniffing for unknown extensions)
#   git                     — for working with the bind-mounted workspace repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-heb \
        poppler-utils \
        libmagic1 \
        git \
        curl \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Node.js + Claude Code CLI — used by the /triage skill from inside the container.
# Sandboxed here on purpose: Claude only sees the bind-mounted /workspace and /docs.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# serena-agent — provides the `serena-hooks` binary called by Claude Code's
# PreToolUse / SessionStart / Stop hooks in ~/.claude/settings.json.
# Installed system-wide so it's on PATH for the non-root user below.
RUN pip install --no-cache-dir serena-agent

# non-root user for safer file ops on bind-mounted volumes
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} automafile \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash automafile \
    && mkdir -p /home/automafile/.vscode-server \
    && chown -R automafile:automafile /home/automafile/.vscode-server

WORKDIR /workspace

# install Python deps via the project's pyproject.toml
# (the workspace itself is bind-mounted at runtime, so we install into the image
# from a copy here purely so cold container starts don't have to repeat pip)
COPY pyproject.toml ./
COPY automafile/__init__.py automafile/__init__.py
RUN mkdir -p build \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .[dev] \
    && rm -rf automafile

USER automafile

# the docs root and Ollama URL are container-local; the host's config.jsonc
# and host paths are unaffected
ENV DOCUMENTS_ROOT=/docs \
    OLLAMA_URL=http://host.docker.internal:11434

CMD ["python", "-m", "automafile", "watch"]
