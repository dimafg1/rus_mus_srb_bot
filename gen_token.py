# gen_token.py
import os
import sys
import argparse
from pathlib import Path
from app.web.security import sign_token

def load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # не перезаписываем уже заданные переменные окружения
        os.environ.setdefault(k.strip(), v.strip())

def main():
    # Корень проекта = папка, где лежит этот скрипт
    root = Path(__file__).resolve().parent
    # 1) Подхватим .env (если есть), 2) затем .env.web (где лежит ключ подписи)
    load_env_file(root / ".env")
    load_env_file(root / ".env.web")

    parser = argparse.ArgumentParser(
        description="Generate signed token for editor/media links."
    )
    parser.add_argument("listing_id", type=int, help="Listing ID")
    parser.add_argument("owner_id", type=int, help="Owner (Telegram user) ID")
    parser.add_argument("--ttl", type=int, default=3600, help="TTL seconds (default: 3600)")
    parser.add_argument("--purpose", choices=["editor", "media"], default="editor",
                        help='Token purpose (default: "editor")')
    args = parser.parse_args()

    token = sign_token(
        listing_id=args.listing_id,
        owner_id=args.owner_id,
        ttl_seconds=args.ttl,
        purpose=args.purpose,
    )
    print(token)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
