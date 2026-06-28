"""Entry point: python -m tui [--api-url URL]"""

import argparse

from tui.app import TUIApp


def main() -> None:
    parser = argparse.ArgumentParser(description="MLaaS Consensus TUI")
    parser.add_argument(
        "--api-url",
        default="http://localhost:8800",
        help="Base URL of the consensus API (default: http://localhost:8800)",
    )
    args = parser.parse_args()
    app = TUIApp(api_url=args.api_url)
    app.run()


if __name__ == "__main__":
    main()
