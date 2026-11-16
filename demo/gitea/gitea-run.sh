#!/bin/bash
# Custom gitea s6 run script.
# Starts gitea web, waits for HTTP, creates admin user, then stays as the
# foreground process so Docker (and s6) track the right PID.

[[ -f ./setup ]] && source ./setup

pushd /app/gitea >/dev/null

# Start gitea web as background child
su-exec $USER /usr/local/bin/gitea web &
GITEA_PID=$!

# Wait for HTTP (up to 120 s)
for i in $(seq 1 60); do
    if curl -sf http://localhost:3000 >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# Create admin user (idempotent — ignore "already exists" line)
su-exec $USER /usr/local/bin/gitea admin user create \
    --admin \
    --username "${GITEA_ADMIN_USER:-demo-admin}" \
    --password "${GITEA_ADMIN_PASSWORD:-VarTrack123}" \
    --email   "${GITEA_ADMIN_EMAIL:-demo-admin@demo.local}" \
    --must-change-password=false 2>&1 | grep -v "already exists" || true

# Ensure must-change-password is cleared (idempotent safety net)
su-exec $USER /usr/local/bin/gitea admin user must-change-password --all --unset 2>&1 || true

popd >/dev/null

# Hand off to the gitea web process (PID becomes the foreground signal target)
wait $GITEA_PID
