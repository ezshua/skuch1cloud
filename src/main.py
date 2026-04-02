import asyncio

from bot import run_polling


def main() -> None:
    """Главная точка входа."""
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()