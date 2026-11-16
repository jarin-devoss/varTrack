FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

# Built with the repository root as its context (e2e/docker-compose.yml), so it can take the demo's pinned requirements and
# its grant minting instead of keeping copies; Dockerfile.dockerignore lets in only the three files it copies.
RUN apt-get update && apt-get install -y --no-install-recommends         bash curl git jq     && rm -rf /var/lib/apt/lists/*

COPY demo/seed/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY e2e/init/test_runner.py /test_runner.py
COPY demo/seed/grant.py /grant.py

WORKDIR /demo
# The runner exits with the number of failed suites' status: 0 only when every suite passed.
ENTRYPOINT ["python3", "/test_runner.py"]
