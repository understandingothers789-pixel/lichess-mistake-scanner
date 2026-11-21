"""Copy license files provided by the installed dependency distributions."""

import re
import shutil
import sys
from importlib.metadata import distributions
from pathlib import Path


def main():
    output = Path(sys.argv[1]) / "licenses"
    count = 0
    for distribution in distributions():
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", distribution.metadata["Name"])
        for item in distribution.files or ():
            if ".." in item.parts or not item.parts:
                continue
            filename = item.name.casefold()
            if "licenses" not in item.parts and not filename.startswith(("license", "notice", "copying", "authors")):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file() or source.suffix in (".py", ".pyc"):
                continue
            destination = output / name / Path(*item.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            count += 1
    print(f"Copied {count} distribution notice files.")


if __name__ == "__main__":
    main()
