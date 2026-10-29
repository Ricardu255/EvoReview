"""Command line entry point for the EvoReview service."""
import argparse

from . import __version__
from .api import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evoagent",
        description="EvoReview: agentic code review and self-evolution service",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__,
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate configuration and exit without starting the server",
    )
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    if args.check:
        from .config import Settings

        settings = Settings.from_env()
        settings.validate_evolution()
        print("configuration ok")
        return
    run()


if __name__ == "__main__":
    main()
