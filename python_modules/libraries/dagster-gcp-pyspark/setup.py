from pathlib import Path
from typing import Dict

from setuptools import find_packages, setup


def get_version() -> str:
    version: Dict[str, str] = {}
    with open(Path(__file__).parent / "dagster_gcp_pyspark/version.py", encoding="utf8") as fp:
        exec(fp.read(), version)  # pylint: disable=W0122

    return version["__version__"]


ver = get_version()
# dont pin dev installs to avoid pip dep resolver issues
pin = "" if ver == "1!0+dev" else f"=={ver}"
setup(
    name="dagster_gcp_pyspark",
    version=get_version(),
    author="Elementl",
    author_email="hello@elementl.com",
    license="Apache-2.0",
    description="Package for storing PySpark DataFrames in GCP",
    url=(
        "https://github.com/dagster-io/dagster/tree/master/python_modules/libraries/"
        "dagster-gcp-pyspark"
    ),
    classifiers=[
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    packages=find_packages(exclude=["dagster_gcp_pyspark_tests*"]),
    install_requires=[
        f"dagster{pin}",
        f"dagster-gcp{pin}",
        "pyspark",
    ],
    extras_require={"test": ["pandas-gbq"]},
    zip_safe=False,
)
