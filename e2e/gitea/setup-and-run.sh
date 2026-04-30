#!/bin/bash
# Start gitea in background, wait for it, create admin user, then bring to foreground.
set -e

# Run original gitea entrypoint setup (copies /etc/s6/gitea/setup logic)
# We source the setup script to get env vars configured
cd /app/gitea

# Start gitea web in the background temporarily
su-exec git gitea web &
GITEA_PID=$!

# Wait for gitea HTTP to be ready
echo "[gitea-setup] Waiting for Gitea HTTP..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:3000 >/dev/null 2>&1; then
        echo "[gitea-setup] Gitea HTTP ready"
        break
    fi
    sleep 2
done

# Create admin user (idempotent - ignore "already exists" error)
echo "[gitea-setup] Creating admin user demo-admin..."
su-exec git gitea admin user create \
    --admin \
    --username demo-admin \
    --password VarTrack123 \
    --email demo-admin@demo.local \
    --must-change-password false 2>&1 | grep -v "already exists" || true
echo "[gitea-setup] Admin user setup complete"

# Wait for gitea process (bring to foreground)
wait $GITEA_PID
