FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

RUN apt-get update && apt-get install -y --no-install-recommends         bash curl git jq dos2unix     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY seed.sh /seed.sh
COPY grant.py /grant.py
COPY ui_sign_in.py /ui_sign_in.py
RUN dos2unix /seed.sh && chmod +x /seed.sh

WORKDIR /demo
ENTRYPOINT ["/bin/bash", "/seed.sh"]
