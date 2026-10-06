"""
check_setup.py
--------------
A tiny health-check you run at the end of Stage 1.

It confirms that:
  1. Your virtual environment and installed packages work.
  2. Your .env file is being read.
  3. Which pieces are filled in and which are still missing.

It NEVER prints the actual value of any key or token — only whether it is set.
Run it with:  python check_setup.py
"""

def check_packages():
    print("Checking installed packages...")
    ok = True
    packages = [
        ("telegram", "python-telegram-bot"),
        ("googleapiclient", "google-api-python-client"),
        ("google_auth_oauthlib", "google-auth-oauthlib"),
        ("dateparser", "dateparser"),
        ("dotenv", "python-dotenv"),
    ]
    for import_name, pip_name in packages:
        try:
            __import__(import_name)
            print(f"   OK   {pip_name}")
        except ImportError:
            print(f"   MISSING  {pip_name}  ->  run: pip install -r requirements.txt")
            ok = False
    return ok


def mask(value):
    """Show that a secret exists without revealing it."""
    if not value:
        return "NOT SET"
    return f"set ({len(value)} characters)"


def check_settings():
    print("\nChecking your settings...")
    import config

    owners = config.OWNER_CHAT_IDS
    print(f"   TELEGRAM_BOT_TOKEN : {mask(config.TELEGRAM_BOT_TOKEN)}")
    print(f"   OWNER_CHAT_ID      : {f'set ({len(owners)} owner(s))' if owners else 'NOT SET'}")
    print(f"   credentials.json   : {'found' if config.GOOGLE_CREDENTIALS_FILE.exists() else 'NOT FOUND'}")
    print(f"   timezone           : {config.TIMEZONE}")

    missing = config.missing_settings()
    if missing:
        print("\n   Still to do:")
        for item in missing:
            print(f"      - {item}")
    else:
        print("\n   Everything is filled in. You're ready for Stage 2!")


if __name__ == "__main__":
    print("=" * 55)
    print(" Meeting Assistant - Stage 1 setup check")
    print("=" * 55)
    packages_ok = check_packages()
    if packages_ok:
        check_settings()
    print("\nDone.")
