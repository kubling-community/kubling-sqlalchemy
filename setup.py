from setuptools import setup, find_packages

setup(
    name="kubling_sqlalchemy",
    version="24.5.4",
    description="SQLAlchemy dialect for Kubling",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/kubling-community/kubling-sqlalchemy",
    packages=find_packages(),
    install_requires=[
        "sqlalchemy>=1.4",
        "psycopg2-binary",
    ],
    entry_points={
        "sqlalchemy.dialects": [
            "kubling = kubling.dialect:KublingDialect",
        ]
    },
)