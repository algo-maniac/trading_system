# Shared image for all service agents (reader, backtester, …).
# Each compose service runs the same image with a different `command:`.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (better layer caching), then the package itself.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# No default service — compose sets the command per agent. `trading` CLI is also on PATH.
CMD ["python", "-c", "import sys; sys.exit('Specify a service command, e.g. python -m trading_system.agents.reader.app')"]
