"""
secrets_loader.py — Centrale secrets loader.

Prioriteit:
  1. OCI Vault        — als OCI_VAULT_OCID gezet is (cloud instance)
  2. .env bestand     — lokale ontwikkeling via python-dotenv

Op de OCI instance wordt Instance Principal auth gebruikt (geen config-bestand
nodig). Lokaal valt de OCI-auth terug op ~/.oci/config als OCI_VAULT_OCID wel
gezet is maar je lokaal wil testen.

Bestaande env vars worden nooit overschreven (hogere prioriteit dan vault/dotenv).
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Namen van de secrets — identiek aan de env var namen in .env
_SECRET_NAMES = [
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def load_secrets(env_path: Path | None = None) -> None:
    """
    Laad secrets in os.environ.

    Parameters
    ----------
    env_path : Path, optional
        Pad naar .env bestand. Standaard: <projectroot>/.env
    """
    vault_ocid = os.environ.get("OCI_VAULT_OCID")
    if vault_ocid:
        _load_from_oci_vault(vault_ocid)
    else:
        _load_from_dotenv(env_path)


# ---------------------------------------------------------------------------
# Implementaties
# ---------------------------------------------------------------------------

def _load_from_dotenv(env_path: Path | None) -> None:
    from dotenv import load_dotenv
    if env_path is None:
        env_path = _PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    logger.debug("Secrets geladen uit %s", env_path)


def _load_from_oci_vault(vault_ocid: str) -> None:
    try:
        import oci
    except ImportError:
        raise ImportError(
            "OCI SDK niet geïnstalleerd. Voer uit: pip install oci"
        )

    secrets_client = _make_secrets_client(oci)

    for name in _SECRET_NAMES:
        if os.environ.get(name):
            logger.debug("Secret '%s' al aanwezig in env, overgeslagen", name)
            continue
        try:
            bundle = secrets_client.get_secret_bundle_by_name(
                secret_name=name,
                vault_id=vault_ocid,
            )
            encoded = bundle.data.secret_bundle_content.content
            os.environ[name] = base64.b64decode(encoded).decode("utf-8")
            logger.debug("Secret '%s' geladen uit OCI Vault", name)
        except Exception as exc:
            logger.warning("Secret '%s' niet gevonden in OCI Vault: %s", name, exc)


def _make_secrets_client(oci):
    """
    Maak een OCI SecretsClient aan.

    Op een OCI instance: Instance Principal (geen config-bestand nodig).
    Lokaal (of als Instance Principal faalt): file-based ~/.oci/config.
    """
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        logger.debug("OCI auth: Instance Principal")
        return oci.secrets.SecretsClient(config={}, signer=signer)
    except Exception:
        logger.debug("OCI auth: file-based config (~/.oci/config)")
        config = oci.config.from_file()
        return oci.secrets.SecretsClient(config)
