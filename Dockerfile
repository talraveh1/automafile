# Lightweight Linux image for the Automafile pipeline.
# Works under Docker Desktop, Podman Desktop, or any Compose-compatible runtime.

FROM python:3.12-slim

# system dependencies:
#   tesseract-ocr + heb/eng — OCR engine and language packs
#   poppler-utils           — pdf2image (PDF rasterization for OCR)
#   libmagic1               — python-magic (MIME sniffing for unknown extensions)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-heb \
        poppler-utils \
        libmagic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# non-root user for safer file ops on bind-mounted volumes
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} automafile \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash automafile

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
