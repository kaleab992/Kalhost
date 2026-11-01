FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Interpreters so hosted bots can be written in many languages + build tools for pip packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm php-cli ruby perl lua5.4 golang-go default-jre-headless \
        git curl ca-certificates build-essential libffi-dev libssl-dev \
    && npm install -g tsx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY FINALHOST.py .

# Run as root on purpose: the host then runs every user's scripts under a separate unprivileged uid
# (see ISOLATE_USERS) so a hosted script can never read the host's token, database or other users' files.
CMD ["python", "FINALHOST.py"]
