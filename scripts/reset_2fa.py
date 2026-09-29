"""Emergency: turn off two-factor sign-in (authenticator app and recovery codes).

Use it if you're locked out, then sign in with just your password and set 2FA up again:

    docker compose exec dayscore python -m scripts.reset_2fa
"""

from app import db, security


def main() -> None:
    db.init()
    security.reset_all()
    print("Two-factor sign-in is turned off. Sign in with your password and set it up again in Settings.")


if __name__ == "__main__":
    main()
