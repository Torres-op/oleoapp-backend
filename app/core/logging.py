import logging
import sys

# Bibliotecas que registram a URL completa de cada requisição em INFO/DEBUG. As URLs do
# Google levam a chave de API e o texto buscado na query string: nunca podem ir para o log.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging() -> None:
    """Logs só na saída padrão (o disco do Render é efêmero). Nada de arquivos."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
