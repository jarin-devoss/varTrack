FROM gitea/gitea:1.22@sha256:538658de667c5d098a274f2f63aa6ec891d88f670cdd5282cf27221ba747dda4

# Override the gitea s6 run script so that gitea web is started in the
# background first, admin user is created once the DB is ready, and then
# the process is adopted (wait) so Docker sees a long-lived foreground PID.
COPY gitea-run.sh /etc/s6/gitea/run
RUN sed -i 's/\r$//' /etc/s6/gitea/run && chmod +x /etc/s6/gitea/run
