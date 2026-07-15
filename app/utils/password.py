import secrets
import string

import bcrypt

PWD_CHARS = string.ascii_uppercase + string.digits


def generate_temp_password(length: int = 5) -> str:
    return "".join(secrets.choice(PWD_CHARS) for _ in range(length))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
