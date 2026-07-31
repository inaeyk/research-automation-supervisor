from pathlib import Path

expected = b"ready\n"
actual = Path("src/ready.txt").read_bytes() if Path("src/ready.txt").is_file() else None
raise SystemExit(0 if actual == expected else 1)
