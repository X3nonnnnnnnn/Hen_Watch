import argparse
from .core import run_once

def main():
    ap = argparse.ArgumentParser(description="Hen_Watch: multi-author watcher for E-Hentai.")
    ap.add_argument("--config", default="config.toml", help="Path to config.toml")
    args = ap.parse_args()
    raise SystemExit(run_once(args.config))

if __name__ == "__main__":
    main()
