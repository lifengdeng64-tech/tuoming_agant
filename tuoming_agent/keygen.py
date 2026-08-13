from __future__ import annotations

import base64
import secrets


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def main() -> None:
    print(generate_master_key())


if __name__ == "__main__":
    main()

