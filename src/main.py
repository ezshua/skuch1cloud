import asyncio

from bot import run_polling


def main() -> None:
    """Главная точка входа."""
    try:
        asyncio.run(run_polling())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
