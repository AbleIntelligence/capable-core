FROM python:3.11-slim

WORKDIR /app

# 1. Install dependencies from pyproject.toml (layer-cached)
COPY pyproject.toml .
RUN mkdir -p capable_core && \
    echo '__version__ = "0.2.0"' > capable_core/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf /app/capable_core

# 2. Copy the application package
COPY capable_core/ capable_core/

# 3. PYTHONPATH=/app so "import capable_core" resolves
ENV PYTHONPATH=/app

RUN chmod +x capable_core/start.sh

EXPOSE 8080

# Generic entry point — launches ADK server only.
# Override with capable_core/internal_start.sh for GCP + Chat bridge.
CMD ["capable_core/start.sh"]
