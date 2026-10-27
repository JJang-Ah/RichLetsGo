"""Interactive registration, run by the eventual Windows service account."""

import argparse
import getpass
import json
import warnings

from . import CredentialError, KeyStore, Secret, windows_backend


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["set"])
    parser.add_argument("item")
    parser.add_argument(
        "--api", action="store_true", help="prompt separately for API key and secret"
    )
    args = parser.parse_args()
    try:
        warnings.simplefilter("error", getpass.GetPassWarning)
        store = KeyStore(windows_backend())
        value = (
            json.dumps(
                {"key": getpass.getpass("API key: "), "secret": getpass.getpass("API secret: ")}
            )
            if args.api
            else getpass.getpass("Credential value: ")
        )
        store.set(args.item, Secret(value))
    except (CredentialError, getpass.GetPassWarning) as error:
        parser.exit(1, str(error) + "\n")
    print("Credential stored; no value displayed.")


if __name__ == "__main__":
    main()
