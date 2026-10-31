import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Argon2id com o mínimo recomendado pela OWASP (19 MiB, 2 iterações, paralelismo 1):
# leve para a CPU compartilhada do plano gratuito do Render.
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

# Hash fictício com os mesmos parâmetros, calculado uma vez no início. O login com e-mail
# inexistente verifica a senha contra ele, gastando o mesmo tempo de um e-mail cadastrado.
DUMMY_PASSWORD_HASH = hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session_token() -> tuple[str, str]:
    """Devolve (token, token_hash). O banco guarda só o hash."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)
