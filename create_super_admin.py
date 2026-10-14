"""
Creates or promotes a platform super-admin.

Super-admins are Qifaw Tech staff, not customers. Public signup can only ever
produce a `tenant_admin` (see `app/routes/auth.py`), so this script is the only
way a super-admin comes into existence.

The access control is the database connection itself: you must already be able
to reach Postgres with the credentials in `.env`. There is deliberately no HTTP
endpoint for this — an endpoint is something an attacker can reach, a psql
connection generally is not.

Run:
    python create_super_admin.py --list
    python create_super_admin.py --email you@qifaw.com --name "Your Name"
    python create_super_admin.py --email you@qifaw.com --name "Your Name" --password "..."
    python create_super_admin.py --email someone@qifaw.com --promote
"""

import argparse
import asyncio
import os
import sys
from getpass import getpass

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select  # noqa: E402

from app.database import async_session_factory  # noqa: E402
from app.models.admin_user import AdminUser  # noqa: E402
from app.services.security import hash_password  # noqa: E402

MIN_PASSWORD_LENGTH = 8  # Matches UserRegister in app/schemas/auth.py


async def list_super_admins(db) -> None:
    """Print every super-admin on the platform."""
    result = await db.execute(
        select(AdminUser)
        .where(AdminUser.role == "super_admin")
        .order_by(AdminUser.created_at.asc())
    )
    admins = list(result.scalars().all())

    if not admins:
        print("No super-admins exist yet.")
        return

    print(f"{len(admins)} super-admin(s):")
    for a in admins:
        status = "active" if a.is_active else "INACTIVE"
        # tenant_id should always be NULL for a super-admin — flag it loudly if
        # not, because a bound super-admin is a sign something demoted them.
        bound = "" if a.tenant_id is None else f"  [WARNING: bound to tenant {a.tenant_id}]"
        print(f"  - {a.email}  ({a.full_name})  {status}{bound}")


def resolve_password(supplied: str | None) -> str:
    """Take the password from the flag, or prompt for it when we have a terminal."""
    password = supplied
    if password is None:
        if not sys.stdin.isatty():
            sys.exit(
                "No terminal to prompt on. Pass --password explicitly "
                "(and clear it from your shell history afterwards)."
            )
        password = getpass("Password: ")
        if password != getpass("Confirm password: "):
            sys.exit("Passwords did not match.")

    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or promote a platform super-admin.",
    )
    parser.add_argument("--email", help="Email address of the super-admin.")
    parser.add_argument("--name", help="Full name (required when creating).")
    parser.add_argument(
        "--password",
        help="Password. Omit to be prompted, which keeps it out of shell history.",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Promote an existing account to super_admin instead of creating one.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing super-admins and exit.",
    )
    args = parser.parse_args()

    async with async_session_factory() as db:
        if args.list:
            await list_super_admins(db)
            return 0

        if not args.email:
            parser.error("--email is required (or use --list)")

        email = args.email.strip().lower()
        result = await db.execute(select(AdminUser).where(AdminUser.email == email))
        existing = result.scalar_one_or_none()

        # ---- Promote an existing account --------------------------------
        if args.promote:
            if existing is None:
                sys.exit(f"No account with email {email}. Drop --promote to create one.")
            if existing.role == "super_admin":
                print(f"{email} is already a super_admin. Nothing to do.")
                return 0

            previous_tenant = existing.tenant_id
            existing.role = "super_admin"
            # A super-admin must have tenant_id = NULL: that is what grants
            # access to all tenants. Leaving it set would scope them to one
            # customer and quietly contradict their role.
            existing.tenant_id = None
            await db.commit()

            print(f"Promoted {email} to super_admin.")
            if previous_tenant is not None:
                print(f"  Unbound from tenant {previous_tenant} (required for super_admin).")
            return 0

        # ---- Create a new account ---------------------------------------
        if existing is not None:
            sys.exit(
                f"An account with email {email} already exists "
                f"(role={existing.role}). Use --promote to upgrade it."
            )
        if not args.name:
            parser.error("--name is required when creating a new super-admin")

        password = resolve_password(args.password)

        user = AdminUser(
            email=email,
            hashed_password=hash_password(password),
            full_name=args.name,
            role="super_admin",
            tenant_id=None,  # NULL = access to all tenants
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        print(f"Created super_admin {user.email} (id={user.id}).")
        print("Log in through the normal login page — the JWT carries the role.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
