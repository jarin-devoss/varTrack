FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033

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
