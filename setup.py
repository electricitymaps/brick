import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="brick",  # Replace with your own username
    version="0.0.1",
    author="Olivier Corradi",
    author_email="olivier.corradi@tmrow.com",
    description="A simple build tool for monorepos based on Docker",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/tmrowco/brick",
    packages=setuptools.find_packages(),
    classifiers=["Programming Language :: Python :: 3",],
    python_requires=">=3.6",
    entry_points={"console_scripts": ["brick=brick.__main__:entrypoint"]},
    install_requires=[
        "arrow==0.12.1",
        "braceexpand==0.1.2",
        "Click==7.0",
        "pyaml==19.4.1",
        "docker==3.7.0",
        "typing-extensions==3.7.4.3",
        "yamllint==1.17.0",
        "wcmatch==6.0.1",
    ],
    extras_require={
        "dev": ["black==19.10b0", "pylint==2.6.0", "pytest==6.0.1", "pytest-cov==2.10.1"]
    },
)
