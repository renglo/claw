"""
Claw extension package
OpenClaw-style agent runtime handlers for the NOMA platform
"""

from setuptools import setup, find_packages

setup(
    name="claw-mod",
    version="1.0.0",
    description="Claw extension — OpenClaw-style agent runtime handlers",
    author="NOMA Team",
    packages=find_packages(),
    python_requires=">=3.12",
    install_requires=[
        "openai>=1.0.0",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.12",
    ],
)
