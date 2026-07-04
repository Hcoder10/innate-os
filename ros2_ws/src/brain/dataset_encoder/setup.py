# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from setuptools import setup

package_name = "dataset_encoder"

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
        ("share/" + package_name + "/launch", ["launch/dataset_encoder.launch.py"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="Background episode → H.264 MP4 encoder, gated on robot activity.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "dataset_encoder_node = dataset_encoder.dataset_encoder_node:main",
            "profile_recorder_node = dataset_encoder.profile_recorder_node:main",
        ],
    },
)
