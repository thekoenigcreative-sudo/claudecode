"""Store Rick's IBKR login for IB Gateway in Windows Credential Manager. Rick runs it himself.

Double-click ibkr_login_setup.cmd in the repo folder (it runs this in a console):

    ibkr_login_setup.cmd            ask for the username and password, and store them
    ibkr_login_setup.cmd --check    say whether a login is stored (shows nothing of it)
    ibkr_login_setup.cmd --forget   delete the stored login

The password is typed with hidden input (getpass), asked twice, and handed straight to
Windows (asxbot.ibkr.credentials.store): a generic credential, DPAPI-encrypted under Rick's
Windows account, on this PC only. It is never printed, written to a file or logged. The
Gateway launcher reads it at each start; the next restart of Gateway logs in by itself and
leaves only the IBKR Mobile approval on the phone, which IBKR requires and nothing can skip.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asxbot.ibkr import credentials  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Store the IBKR login for IB Gateway (IBC).")
    ap.add_argument("--check", action="store_true", help="say whether a login is stored")
    ap.add_argument("--forget", action="store_true", help="delete the stored login")
    args = ap.parse_args(argv)
    if args.check:
        print("A login is stored." if credentials.stored() else "No login is stored.")
        return 0
    if args.forget:
        print("Deleted." if credentials.forget() else "There was no stored login.")
        return 0

    print("IB Gateway: store your IBKR login (this PC, your Windows account only).")
    print("It goes into Windows Credential Manager; nothing is written to a file.\n")
    user = input("IBKR username: ").strip()
    if not user:
        print("No username given; nothing stored.")
        return 1
    pw = getpass.getpass("IBKR password (not shown): ")
    again = getpass.getpass("Same password again: ")
    if not pw or pw != again:
        print("The two passwords were empty or did not match; nothing stored. Run it again.")
        return 1
    try:
        credentials.store(user, pw)
    except credentials.CredentialError as e:
        print(f"Could not store it: {e}")
        return 1
    back = credentials.load()
    ok = back is not None and back.user == user and back.password == pw
    del pw, again, back
    if not ok:
        print("Windows did not give the same login back; run this again.")
        return 1
    print("\nStored. From the next restart, IB Gateway logs in by itself; you only approve")
    print("the IBKR login on your phone (IBKR's rule). Nothing else to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
