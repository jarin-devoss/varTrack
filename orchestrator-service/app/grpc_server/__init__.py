"""
app/grpc_server/__init__.py
────────────────────────────
Public surface of the grpc_server package.
"""
from app.grpc_server.server import OrchestratorServicer, create_server, run_server  # noqa: F401
