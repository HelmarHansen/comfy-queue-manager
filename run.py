"""Startskript: liest die Listen-Adresse aus der Config und startet uvicorn."""
import uvicorn

from app.config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run("app.main:app", host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
