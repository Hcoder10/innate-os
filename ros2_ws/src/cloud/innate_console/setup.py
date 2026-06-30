# SPDX-License-Identifier: Apache-2.0
from setuptools import setup

package_name = "innate_console"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/console.launch.py"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="Lightweight bridge that streams tmux pane stdout and /rosout to the webapp over rosbridge, with on-request backfill.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "console_node = innate_console.console_node:main",
        ],
    },
)
