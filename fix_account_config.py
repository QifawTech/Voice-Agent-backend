"""
Fixes two account config issues:
  1. Updates api_base_url from :3001 to :9000 (mock restaurant API port)
  2. Shows current state of all accounts

Run:
    python fix_tenant_config.py
"""

import asyncio
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import text
from app.database import async_session_factory


async def main():
    async with async_session_factory() as db:

        # -- Show current state ------------------------------------------
        rows = await db.execute(text("""
            SELECT id, name, api_base_url, widget_api_key
            FROM accounts
            ORDER BY created_at ASC
        """))
        accounts = rows.fetchall()

        print("\n[BEFORE]")
        for t in accounts:
            print(f"  {t.name}  api_base_url={t.api_base_url}")

        # -- Fix 1: Update api_base_url from :3001 to :9000 ---------------
        result = await db.execute(text("""
            UPDATE accounts
            SET api_base_url = 'http://localhost:9000',
                updated_at   = NOW()
            WHERE api_base_url = 'http://localhost:3001'
            RETURNING id, name
        """))
        updated = result.fetchall()

        if updated:
            for u in updated:
                print(f"\n  [FIXED] {u.name} -> api_base_url set to http://localhost:9000")
        else:
            print("\n  [OK] No accounts had :3001 URL (already correct or different URL)")

        await db.commit()

        # -- Show after state --------------------------------------------
        rows2 = await db.execute(text("""
            SELECT id, name, api_base_url
            FROM accounts
            ORDER BY created_at ASC
        """))
        tenants2 = rows2.fetchall()

        print("\n[AFTER]")
        for t in tenants2:
            print(f"  {t.name}  api_base_url={t.api_base_url}")

        print("\n[DONE] Restart your backend server for changes to take effect.")
        print("       Make sure mock API is running: uvicorn mock_restaurant_api:app --port 9000\n")


if __name__ == "__main__":
    asyncio.run(main())
