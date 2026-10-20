"""Generate the VAPID key pair push notifications need. Run once on your own computer:
python generate_vapid_keys.py
Then paste both lines into Render > Environment as environment variables.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


key = ec.generate_private_key(ec.SECP256R1())
private_raw = key.private_numbers().private_value.to_bytes(32, "big")
public_raw = key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
print(f"VAPID_PUBLIC_KEY={b64url(public_raw)}")
print(f"VAPID_PRIVATE_KEY={b64url(private_raw)}")
