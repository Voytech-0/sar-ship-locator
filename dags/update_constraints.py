"""Pin dags/ dependencies to Airflow's official constraints file.

uv has no `--constraint` for `uv lock`, so the official file is copied into pyproject.toml's
`[tool.uv] constraint-dependencies`. Usage: `python update_constraints.py 3.3.2` (needs network), then
`uv lock`.
"""

import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BEGIN, END = "# BEGIN airflow-constraints", "# END airflow-constraints"


def main(version: str) -> None:
    url = f"https://raw.githubusercontent.com/apache/airflow/constraints-{version}/constraints-3.12.txt"
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode()
    pins = sorted(
        line.strip()
        for line in text.splitlines()
        if re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==\S+", line.strip())
        and not line.startswith("apache-airflow==")
        and not line.startswith("apache_airflow==")
    )
    block = "\n".join(
        [BEGIN, "[tool.uv]", "constraint-dependencies = [", *(f'    "{p}",' for p in pins), "]", END]
    )
    path = HERE / "pyproject.toml"
    src = path.read_text()
    if BEGIN in src:
        src = re.sub(f"{re.escape(BEGIN)}.*?{re.escape(END)}", lambda _: block, src, flags=re.S)
    else:
        src = src.rstrip("\n") + "\n\n" + block + "\n"
    path.write_text(src)
    print(f"{len(pins)} pins written for Airflow {version}")


if __name__ == "__main__":
    main(sys.argv[1])
