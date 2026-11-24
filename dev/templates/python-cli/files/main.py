"""{{TITLE}} -- command-line program."""
import argparse
import sys


def run(args: argparse.Namespace) -> str:
    """The program's work; returns what it prints."""
    return f"{{TITLE}}: ready (input: {args.text or 'none'})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="{{TITLE}}")
    parser.add_argument("text", nargs="?", default="", help="what to work on")
    args = parser.parse_args(argv)
    print(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
