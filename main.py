#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - ECU書き込みサイクル判定システム エントリポイント
"""

import sys
import argparse
from modules.app import MainApplication


def main():
    parser = argparse.ArgumentParser(description="ECU書き込みサイクル判定システム")
    parser.add_argument("--test", action="store_true", help="GUIを表示せずテストモードで起動")
    args = parser.parse_args()

    app = MainApplication(is_test_mode=args.test)
    if not args.test and hasattr(app, "root") and hasattr(app.root, "mainloop"):
        try:
            app.root.mainloop()
        except KeyboardInterrupt:
            app.on_closing()


if __name__ == "__main__":
    main()
