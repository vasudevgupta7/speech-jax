__version__ = "0.0.1"

import setuptools

with open("readme.md", "r", encoding="utf-8") as file:
    long_description = file.read()

install_requires = [
    "pydantic",
    "flax",
    "optax",
    "wandb",
    "tqdm",
    "pyyaml",
    "huggingface_hub",
    # "jax",
    # "tensorflow",
]
common_requires = [
    "transformers",
    "datasets",
    "soundfile",
    "librosa",
]
dev_requires = ["black", "isort", "flake8"]

setuptools.setup(
    name="speech_jax",
    version=__version__,
    author="Vasudev Gupta",
    author_email="7vasudevgupta@gmail.com",
    description="Speech library in JAX/FLAX",
    long_description=long_description,
    long_description_content_type="text/markdown",
    license="Apache",
    url="https://github.com/VasudevGupta7/speech-jax",
    package_dir={"": "src"},
    packages=setuptools.find_packages("src"),
    install_requires=install_requires,
    extra_require={
        "common": common_requires,
        "dev": dev_requires,
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    python_requires=">=3.7",  # python-37 is necessary for running in colab
)
