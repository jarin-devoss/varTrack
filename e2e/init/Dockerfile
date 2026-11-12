FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl git jq dos2unix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY demo.sh /demo.sh
COPY test_runner.py /test_runner.py
COPY grant.py /grant.py
COPY ui_sign_in.py /ui_sign_in.py
RUN dos2unix /demo.sh && chmod +x /demo.sh

WORKDIR /demo
ENTRYPOINT ["/bin/bash", "/demo.sh"]
