"""Emergency: set a new password when you've forgotten yours.

    docker compose exec -it dayscore python -m scripts.reset_password

Every signed-in device is signed out. Two-factor sign-in stays as it was; turn it off with
scripts.reset_2fa if you've also lost your phone and recovery codes.
"""

import getpass
import sys

from app import auth, db


def main() -> None:
    db.init()
    first = getpass.getpass("New password (at least 8 characters): ")
    if len(first) < 8:
        sys.exit("Too short. Nothing was changed.")
    if getpass.getpass("Repeat it: ") != first:
        sys.exit("The passwords don't match. Nothing was changed.")
    auth.set_password(first)
    auth.end_other_sessions()
    print("Password changed. Sign in with the new one.")


if __name__ == "__main__":
    main()
