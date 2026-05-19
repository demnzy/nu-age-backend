from passlib.context import CryptContext

# Schemes define the algorithms to use; deprecated="auto" safely upgrades old hashes
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")

# 1. Hash a new password
plain_password = "Nuage2026"
hashed = pwd_context.hash(plain_password)
print(hashed)