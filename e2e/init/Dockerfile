FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

# One image, two jobs: e2e/docker-compose.yml starts it as the seed (/seed.sh) and as the regression's runner (/test_runner.py),
# each with its own entrypoint. Built with this folder as its context; .dockerignore lets in only the files copied below.
RUN apt-get update && apt-get install -y --no-install-recommends         bash curl git jq dos2unix     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY seed.sh /seed.sh
COPY grant.py /grant.py
COPY ui_sign_in.py /ui_sign_in.py
COPY test_runner.py /test_runner.py
RUN dos2unix /seed.sh && chmod +x /seed.sh

WORKDIR /e2e
