"""
app/utils/vault_client.py
──────────────────────────
HashiCorp Vault client for resolving secrets.

Supports multiple auth methods:
- Token auth
- AppRole auth  
- Kubernetes auth
- UserPass auth

Based on the VaultConfig proto definition.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import hvac  # pip install hvac
    HVAC_AVAILABLE = True
except ImportError:
    HVAC_AVAILABLE = False
    logger.warning("hvac library not installed - Vault secret resolution unavailable")


class VaultClient:
    """
    HashiCorp Vault client for secret resolution.
    
    Usage:
        client = VaultClient(
            endpoint="https://vault.example.com",
            mount_point="secret",
            kv_version=2,
            auth={"token": "s.abc123"}
        )
        
        value = client.get_secret("database/prod", "password")
    """
    
    def __init__(
        self,
        endpoint: str,
        mount_point: str = "secret",
        kv_version: int = 2,
        namespace: Optional[str] = None,
        auth: Optional[dict[str, Any]] = None,
        verify_ssl: bool = True,
        ssl_ca: Optional[str] = None,
        timeout: int = 10,
        max_retries: int = 3,
    ):
        """
        Parameters
        ----------
        endpoint:
            Vault server URL (e.g., "https://vault.example.com")
        mount_point:
            KV engine mount point (e.g., "secret", "kv")
        kv_version:
            KV engine version: 1 or 2
        namespace:
            Vault Enterprise namespace
        auth:
            Authentication config. Examples:
            - {"token": "s.abc123"}
            - {"app_role": {"role_id": "...", "secret_id": "..."}}
            - {"kubernetes": {"role": "...", "jwt_path": "..."}}
            - {"userpass": {"username": "...", "password": "..."}}
        verify_ssl:
            Whether to verify SSL certificates
        ssl_ca:
            CA certificate content for server verification
        timeout:
            Request timeout in seconds
        max_retries:
            Number of retry attempts for failed requests
        """
        if not HVAC_AVAILABLE:
            raise RuntimeError("hvac library not installed. Run: pip install hvac")
            
        self.endpoint = endpoint
        self.mount_point = mount_point
        self.kv_version = kv_version
        self.namespace = namespace
        self.timeout = timeout
        self.max_retries = max_retries
        self._auth_config = auth  # stored for token renewal
        
        # Initialize hvac client
        self._client = hvac.Client(
            url=endpoint,
            namespace=namespace,
            verify=ssl_ca if ssl_ca else verify_ssl,
            timeout=(timeout, timeout),
        )
        
        # Authenticate
        if auth:
            self._authenticate(auth)
    
    def get_secret(self, path: str, key: str) -> str:
        """
        Fetch a secret value from Vault.
        
        Automatically re-authenticates once if the token has expired,
        retrying transient auth failures without propagating them to the caller.
        
        Parameters
        ----------
        path:
            Secret path within the mount point (e.g., "database/prod")
        key:
            Key within the secret (e.g., "password")
            
        Returns
        -------
        str
            The secret value
            
        Raises
        ------
        ValueError:
            If the secret or key doesn't exist
        RuntimeError:
            If Vault request fails after re-authentication attempt
        """
        try:
            return self._fetch_secret(path, key)
        except hvac.exceptions.Forbidden:
            # Token expired or revoked — re-authenticate once and retry.
            logger.warning("Vault token expired/forbidden — re-authenticating")
            self._reauthenticate()
            return self._fetch_secret(path, key)

    def _fetch_secret(self, path: str, key: str) -> str:
        """Internal: fetch without retry logic."""
        try:
            if self.kv_version == 2:
                # KV v2: secrets are nested under 'data'
                response = self._client.secrets.kv.v2.read_secret_version(
                    path=path,
                    mount_point=self.mount_point,
                )
                secret_data = response["data"]["data"]
            else:
                # KV v1: direct access
                response = self._client.secrets.kv.v1.read_secret(
                    path=path,
                    mount_point=self.mount_point,
                )
                secret_data = response["data"]
            
            if key not in secret_data:
                raise ValueError(f"Key '{key}' not found in secret at {path}")
                
            value = secret_data[key]
            
            # Ensure we return a string
            if not isinstance(value, str):
                value = str(value)
                
            return value
            
        except hvac.exceptions.InvalidPath:
            raise ValueError(f"Secret not found at path: {path}")
        except hvac.exceptions.Forbidden:
            raise   # re-raised to trigger retry in get_secret()
        except hvac.exceptions.VaultError as exc:
            raise RuntimeError(f"Vault error fetching {path}/{key}: {exc}")
    
    def _authenticate(self, auth: dict[str, Any]) -> None:
        """Authenticate with Vault using the provided auth config."""
        
        if "token" in auth:
            # Token auth
            self._client.token = auth["token"]
            
        elif "app_role" in auth:
            # AppRole auth
            app_role = auth["app_role"]
            role_id = app_role["role_id"]
            secret_id = app_role["secret_id"]
            mount_path = app_role.get("mount_path", "approle")
            
            response = self._client.auth.approle.login(
                role_id=role_id,
                secret_id=secret_id,
                mount_point=mount_path,
            )
            self._client.token = response["auth"]["client_token"]
            
        elif "kubernetes" in auth:
            # Kubernetes auth
            k8s = auth["kubernetes"]
            role = k8s["role"]
            jwt_path = k8s.get("jwt_path", "/var/run/secrets/kubernetes.io/serviceaccount/token")
            mount_path = k8s.get("mount_path", "kubernetes")
            
            with open(jwt_path, "r") as f:
                jwt = f.read().strip()
            
            response = self._client.auth.kubernetes.login(
                role=role,
                jwt=jwt,
                mount_point=mount_path,
            )
            self._client.token = response["auth"]["client_token"]
            
        elif "userpass" in auth:
            # UserPass auth
            up = auth["userpass"]
            username = up["username"]
            password = up["password"]
            mount_path = up.get("mount_path", "userpass")
            
            response = self._client.auth.userpass.login(
                username=username,
                password=password,
                mount_point=mount_path,
            )
            self._client.token = response["auth"]["client_token"]
            
        else:
            raise ValueError(f"Unsupported auth method: {list(auth.keys())}")
        
        # Verify authentication
        if not self._client.is_authenticated():
            raise RuntimeError("Vault authentication failed")
            
        logger.info("Successfully authenticated with Vault")

    def _reauthenticate(self) -> None:
        """Re-run the original auth flow to refresh an expired token.

        Called automatically by get_secret() when a Forbidden error is returned.
        If no auth config was stored (bare token auth with no rotation possible),
        this raises RuntimeError so the caller surfaces a clear error.
        """
        if not self._auth_config:
            raise RuntimeError(
                "Vault token expired and no auth config available for renewal. "
                "Provide app_role, kubernetes, or userpass auth for automatic renewal."
            )
        time.sleep(random.uniform(0.0, 2.0))
        try:
            self._authenticate(self._auth_config)
            logger.info("Vault token renewed successfully")
        except Exception as exc:
            raise RuntimeError(f"Vault re-authentication failed: {exc}") from exc


def create_vault_client_from_proto_config(config: dict[str, Any]) -> VaultClient:
    """
    Create a VaultClient from a VaultConfig proto dict.
    
    Parameters
    ----------
    config:
        Dict representation of VaultConfig proto message
        
    Returns
    -------
    VaultClient
        Configured Vault client ready to fetch secrets
    """
    # Extract auth config
    auth_config: dict[str, Any] = {}
    
    if "token_auth" in config:
        auth_config["token"] = config["token_auth"]["token"]
        
    elif "app_role_auth" in config:
        app_role = config["app_role_auth"]
        auth_config["app_role"] = {
            "role_id": app_role["role_id"],
            "secret_id": app_role["secret_id"],
            "mount_path": app_role.get("mount_path", "approle"),
        }
        
    elif "kubernetes_auth" in config:
        k8s = config["kubernetes_auth"]
        auth_config["kubernetes"] = {
            "role": k8s["role"],
            "jwt_path": k8s.get("jwt_path"),
            "mount_path": k8s.get("mount_path", "kubernetes"),
        }
        
    elif "userpass_auth" in config:
        up = config["userpass_auth"]
        auth_config["userpass"] = {
            "username": up["username"],
            "password": up["password"],
            "mount_path": up.get("mount_path", "userpass"),
        }
    
    # Build client
    return VaultClient(
        endpoint=config["endpoint"],
        mount_point=config.get("mount_point", "secret"),
        kv_version=config.get("kv_version", 2),
        namespace=config.get("namespace"),
        auth=auth_config,
        verify_ssl=config.get("verify_ssl", True),
        ssl_ca=config.get("ssl_ca"),
        timeout=int(
            config["timeout"].get("seconds", 10)
            if isinstance(config.get("timeout"), dict)
            else config.get("timeout", 10)
        ),
        max_retries=config.get("max_retries", 3),
    )
