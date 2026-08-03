#!/usr/bin/env python3
"""Create the Caddie release-signing key pair.

The private key is intentionally written to a gitignored directory. Back it up
outside the repository; losing it means existing clients cannot trust new
updates without a manual reinstall.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATH = ROOT / ".release-private" / "update-signing-key.pem"
PUBLIC_PATH = ROOT / "assets" / "update_public_key.pem"


def main():
    if PRIVATE_PATH.exists() or PUBLIC_PATH.exists():
        if PRIVATE_PATH.exists() and PUBLIC_PATH.exists():
            print(f"Keys already exist:\nprivate: {PRIVATE_PATH}\npublic:  {PUBLIC_PATH}")
            return
        raise SystemExit("Only one update key exists. Refusing to replace the trust chain.")

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    PRIVATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_PATH.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PRIVATE_PATH.chmod(0o600)
    PUBLIC_PATH.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"Created:\nprivate: {PRIVATE_PATH}\npublic:  {PUBLIC_PATH}")
    print("Back up the private key securely. Never commit or distribute it.")


if __name__ == "__main__":
    main()
