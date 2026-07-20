#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-vout-watchdog",
    version=get_version(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board Vout undervoltage watchdog",
    license="MIT",
    url="https://github.com/wirenboard/wb-vout-watchdog",
    packages=["wb_vout_watchdog"],
)
