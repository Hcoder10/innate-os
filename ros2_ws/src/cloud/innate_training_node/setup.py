# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from setuptools import setup

package_name = "innate_training_node"

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
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="ROS 2 node for Innate training job management.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "training_node = innate_training_node.node:main",
        ],
    },
)
