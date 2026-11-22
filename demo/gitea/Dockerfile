FROM gitea/gitea:28.4@sha256:aa90e8f00b755b0d665214ab9c14447ea44d3190dbe6b6d0cb46cd9c4f55d309

# Override the gitea s6 run script so that gitea web is started in the
# background first, admin user is created once the DB is ready, and then
# the process is adopted (wait) so Docker sees a long-lived foreground PID.
COPY gitea-run.sh /etc/s6/gitea/run
RUN sed -i 's/\r$//' /etc/s6/gitea/run && chmod +x /etc/s6/gitea/run
