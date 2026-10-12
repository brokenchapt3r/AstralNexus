FROM node:22-bookworm-slim AS tui
WORKDIR /client
COPY tui/ ./
RUN npm ci && npm run build:ink && npm run build

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY setup.py pyproject.toml MANIFEST.in README.md LICENSE ./
COPY src/ ./src/
COPY --from=tui /client/dist/entry.js ./tui/dist/entry.js
RUN pip install --no-cache-dir .
ENV ASTRALNEXUS_HOME=/data
CMD ["astralnexus", "--help"]
