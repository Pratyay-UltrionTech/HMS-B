import secrets
import string

from sqlalchemy.orm import Session

from app.models import Hospital

HOSPITAL_ID_CHARS = string.ascii_uppercase + string.digits


def generate_hospital_id(db: Session) -> str:
    for _ in range(20):
        code = "HMS-" + "".join(secrets.choice(HOSPITAL_ID_CHARS) for _ in range(6))
        exists = db.query(Hospital.id).filter(Hospital.hospital_id == code).first()
        if not exists:
            return code
    raise RuntimeError("Unable to generate unique hospital ID")
