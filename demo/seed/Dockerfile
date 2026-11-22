FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

RUN apt-get update && apt-get install -y --no-install-recommends         bash curl git jq dos2unix     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY seed.sh /seed.sh
COPY grant.py /grant.py
COPY ui_sign_in.py /ui_sign_in.py
RUN dos2unix /seed.sh && chmod +x /seed.sh

WORKDIR /demo
ENTRYPOINT ["/bin/bash", "/seed.sh"]
