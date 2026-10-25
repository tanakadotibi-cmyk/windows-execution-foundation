"""Back up Default\\Preferences to Preferences.before-norestore, then disable session restore.

Chrome for this profile must already be closed. This process does not start a
browser and does not request any page. The next launch opens about:blank.
"""
from __future__ import annotations

import sys

from src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2 import (
    DEFAULT_PROFILE,
    CollectorError,
    prepare_profile_norestore,
    windows_list_processes,
)


def main(argv=None, *, list_processes=None, profile=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    chosen = DEFAULT_PROFILE if profile is None else profile
    if args:
        if len(args) == 2 and args[0] == "--profile" and "://" not in args[1]:
            chosen = args[1]
        else:
            print("usage: prepare-reservation-chrome-norestore.py [--profile PATH]", file=sys.stderr)
            return 2
    if "://" in str(chosen):
        print("CHROME_PREFERENCES_INVALID", file=sys.stderr)
        return 1
    lister = windows_list_processes if list_processes is None else list_processes
    try:
        prepare_profile_norestore(chosen, lister())
    except CollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
