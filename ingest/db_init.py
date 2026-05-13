from db.engine import engine, Base
from db import models  # noqa: F401  register models on Base


def main() -> None:
    Base.metadata.create_all(engine)
    print(f"Schema applied to {engine.url}")


if __name__ == "__main__":
    main()
