"""Command-line entry point for cynx."""

from importlib.metadata import version


def main() -> None:
    """Print the installed package name and version."""
    print(f"cynx {version('cynx')}")


if __name__ == "__main__":
    main()
