FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

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
