FROM python:3.14-slim@sha256:fb83750094b46fd6b8adaa80f66e2302ecbe45d513f6cece637a841e1025b4ca

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl git jq dos2unix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY demo.sh /demo.sh
COPY test_runner.py /test_runner.py
RUN dos2unix /demo.sh && chmod +x /demo.sh

WORKDIR /demo
ENTRYPOINT ["/bin/bash", "/demo.sh"]
