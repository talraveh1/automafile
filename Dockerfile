# Lightweight Linux image for the Drag'n'Doc pipeline.
# Works under Docker Desktop, Podman Desktop, or any Compose-compatible runtime.

FROM python:3.12-slim

# system dependencies:
#   tesseract-ocr + heb/eng — OCR engine and language packs
#   poppler-utils           — pdf2image (PDF rasterization for OCR)
#   libmagic1               — python-magic (MIME sniffing for unknown extensions)
#   git                     — for working with the bind-mounted workspace repo
#   procps                  — ps/top/kill/pgrep for inspecting running processes
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
        procps \
        tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Node.js + Claude Code CLI — used by the /triage skill from inside the container.
# Sandboxed here on purpose: Claude only sees the bind-mounted /workspace and /docs.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# non-root user for safer file ops on bind-mounted volumes
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} dragndoc \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash dragndoc \
    && mkdir -p /home/dragndoc/.vscode-server \
    && chown -R dragndoc:dragndoc /home/dragndoc/.vscode-server

WORKDIR /workspace

# install Python deps into a workspace-local venv that will seed the named
# volume mounted at /workspace/.venv on first container create.
COPY pyproject.toml ./
COPY dragndoc/__init__.py dragndoc/__init__.py
RUN python -m venv /workspace/.venv \
    && mkdir -p build \
    && /workspace/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /workspace/.venv/bin/pip install --no-cache-dir -e .[dev] \
    && rm -rf dragndoc \
    && chown -R dragndoc:dragndoc /workspace/.venv

USER dragndoc

# container-local venv that lives on a named volume at /workspace/.venv.
# the parent /workspace remains a host bind mount, so both sides keep the
# same path while using different env contents.
ENV VIRTUAL_ENV=/workspace/.venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

# the docs root and Ollama URL are container-local; the host's config.jsonc
# and host paths are unaffected
ENV DOCS=/docs \
    OLLAMA_URL=http://host.docker.internal:11434

CMD ["python", "-m", "dragndoc", "watch"]
